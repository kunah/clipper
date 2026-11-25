"""Resolve + repair (DESIGN §19): a `JudgeAnswer` becomes a valid `EditPlan`, a reject, a context
request, or `Unrepairable` (one retry with the error, then `failed`); an operator's `PlanPatch`
becomes a new `EditPlan` or `PatchRefused`. Pure; every change is recorded as a `Repair` code in
`plan.repairs` (each code once, in the order first applied).

Cut (`_cut`):
- relative seconds -> UTC; in > out swapped; clamped to the context (a cut with nothing in it:
  the core moment), then into the video (an edge in a hole moves to the video, a cut wholly in a
  hole or outside the video to the nearest video range).
- **Snap**, each edge inside its own video range: in -> a sentence start within 1.5 s, else a word
  start, minus 0.15 s but not before the previous word's end; out -> a sentence end within 1.5 s,
  else a word end, plus 0.3 s but not after the next word's start. No edge nearby: the time stays
  (`unsnapped_*`).
- **Never inside speech**: words merged where they overlap or touch are speech blocks
  (`Transcript.blocks`); an edge inside one moves to the nearer of the block's edges that fits
  (inside its video range, the segment, the length limit). A final pass (`_settle`) does this for
  every segment edge, drops segments that can't be cut cleanly or are shorter than
  `min_segment_s` when there are several, and snaps a new outer edge inwards.
- **Holes** in the video: jitter holes (<= CHUNK_JITTER) are bridged (`merge_segments`); a cut
  across shorter holes than `split_gap_max_s` is split into segments (`split_gap`), across longer
  ones shrunk to the side holding T (`shrink_gap`).
- **Length** into `[min_s, max_s]`: extended to the neighbouring sentence edges (the side with
  less room around T first; seconds where no sentence fits), or the side farther from T trimmed
  (until both sides are even, then both), the new edges snapped inwards. Less video than `min_s`
  around the cut: kept with `short_media` down to CLIP_MIN.
- **No cut** of CLIP_MIN (a cut in a small video island, speech everywhere): the core moment is
  cut instead (`core_fallback`); if that fails too, `rejected`/`no_video` when the video near the
  moment (`[moment_from - 30 s, moment_to + 30 s]`) is shorter than CLIP_MIN, else `Unrepairable`.

The rest of the plan: hashtags stripped of `#`, ones with whitespace or repeated dropped, at most
5; hook text cleaned and cut to 40 characters at a word boundary (empty -> mode none); a teaser
clamped into its segment, out of speech, at most 3 s (shorter than `teaser_min_s` -> none);
emphasis resolved to real words of the named sentence, inside the segments, at most one per 3
words; word fixes only for existing words inside the segments, one per word; zooms scaled into
`[1, zoom_max_scale]`, split at cuts (pieces < 0.5 s dropped), overlaps merged; SFX only with a
manifest tag, inside the segments, >= 2 s apart, at most 6; a thumbnail outside the segments
dropped (the renderer's default); title cleaned (`clean_line`: one line, no invisible or bidi
characters; "I <3 this" stays), cut to 100 characters at a word boundary, blank -> the first
sentence of the core (`[moment_from - 5 s, moment_to + 5 s]`) -> "<channel> clip"; blocklisted
words in the title or hook text -> flag `sensitive`; music >= `music_risk` inside the clip -> flag
`copyright_music_risk`; confidence and `layout.focus_x` clamped to 0..1.

Context requests (`extend_context`): before/after clamped to `[0, max_before_s]`/`[0,
max_after_s]` and the stream; the context only grows (union with the current one); nothing new
or the last round -> `Unrepairable(final=True)`: the retry uses the final round's schema.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Any, ClassVar, Literal

from pydantic import ValidationError

from clipper_core.contracts import (
    CHUNK_JITTER,
    CLIP_MAX,
    CLIP_MIN,
    TEASER_MAX,
    CaptionStyle,
    ClipRejectReason,
    EditPlan,
    EmphasisWord,
    Hook,
    HookMode,
    LayoutHint,
    LayoutKind,
    MomentType,
    MusicPlan,
    PlanPatch,
    PlanSegment,
    SfxCue,
    SoundGroup,
    Verdict,
    WordFix,
    Zoom,
)
from clipper_core.timeline import Interval, ensure_utc, merge
from clipper_judge.answer import ContextAsk, JudgeAnswer, PlanAnswer
from clipper_judge.inputs import JudgeInputs, bridge_jitter
from clipper_judge.params import DEFAULT_PARAMS, JudgeParams
from clipper_judge.transcript import Transcript, clean_line, nearest, norm_word

OPERATOR_MODEL = "operator"  # EditPlan.model of a plan resolved from a PlanPatch
FLAG_SENSITIVE = "sensitive"
FLAG_MUSIC_RISK = "copyright_music_risk"
_DERIVED_FLAGS = frozenset({FLAG_SENSITIVE, FLAG_MUSIC_RISK})  # recomputed on every resolve
_MAX_REL_S = 86_400.0  # relative seconds are clamped to a day (timedelta overflow)
_WORD_TOLERANCE = timedelta(milliseconds=1)  # a stored word time vs the word's start
_EPS = timedelta(microseconds=1)
_REASON_CHARS = 300
_NEAR_MOMENT = timedelta(
    seconds=30
)  # §19 step 1: video over [moment_from - 30 s, moment_to + 30 s]


class Repair(StrEnum):
    DEFAULT_PLAN = "default_plan"  # verdict clip without a plan: the core moment
    SWAP_IN_OUT = "swap_in_out"
    CLAMP_CONTEXT = "clamp_context"
    CLAMP_VIDEO = "clamp_video"  # an edge lay in a hole of the video: moved to the video
    MOVE_TO_MEDIA = "move_to_media"  # the whole cut lay in a hole or outside the video
    CORE_FALLBACK = "core_fallback"  # no clean cut of CLIP_MIN there: the core moment instead
    SNAP_IN = "snap_in"
    SNAP_OUT = "snap_out"
    UNSNAPPED_IN = "unsnapped_in"  # no sentence or word edge nearby: the time stays
    UNSNAPPED_OUT = "unsnapped_out"
    EDGE_SPEECH = "edge_speech"  # the clip's in or out point moved out of speech
    MERGE_SEGMENTS = "merge_segments"  # jitter holes bridged / touching segments merged
    SPLIT_GAP = "split_gap"
    SHRINK_GAP = "shrink_gap"
    GAP_EDGE_WORD = "gap_edge_word"  # a segment edge at a hole moved out of speech
    DROP_SHORT_SEGMENT = "drop_short_segment"  # < min_segment_s, or no clean cut inside it
    EXTEND_SHORT = "extend_short"
    TRIM_LONG = "trim_long"
    SHORT_MEDIA = "short_media"  # less video than min_s around the cut
    HASHTAG_STRIP = "hashtag_strip"
    HASHTAG_DROP = "hashtag_drop"
    HASHTAG_CAP = "hashtag_cap"
    HOOK_TEXT_TRIM = "hook_text_trim"
    HOOK_NONE = "hook_none"
    TEASER_CLAMP = "teaser_clamp"
    EMPHASIS_DROP = "emphasis_drop"
    EMPHASIS_CAP = "emphasis_cap"
    WORD_FIX_DROP = "word_fix_drop"
    ZOOM_SCALE = "zoom_scale"
    ZOOM_SPLIT = "zoom_split"
    ZOOM_DROP = "zoom_drop"
    ZOOM_MERGE = "zoom_merge"
    SFX_UNKNOWN = "sfx_unknown"
    SFX_OUTSIDE = "sfx_outside"
    SFX_CLOSE = "sfx_close"
    SFX_CAP = "sfx_cap"
    THUMBNAIL_DROP = "thumbnail_drop"
    TITLE_TRIM = "title_trim"
    TITLE_FROM_TRANSCRIPT = "title_from_transcript"
    TITLE_FALLBACK = "title_fallback"
    CONFIDENCE_CLAMP = "confidence_clamp"
    FOCUS_CLAMP = "focus_clamp"
    REASONS_TRIM = "reasons_trim"
    CONTEXT_DEFAULT = "context_default"  # need_context without before/after: the caps
    CONTEXT_CLAMP = "context_clamp"


class _Repairs:
    def __init__(self) -> None:
        self._codes: dict[str, None] = {}

    def add(self, code: Repair) -> None:
        self._codes.setdefault(code.value, None)

    def extend(self, other: _Repairs) -> None:
        for code in other._codes:
            self._codes.setdefault(code, None)

    def codes(self) -> tuple[str, ...]:
        return tuple(self._codes)


# --------------------------------------------------------------------------- outcomes


@dataclass(frozen=True, slots=True)
class Planned:
    verdict: ClassVar[Verdict] = Verdict.CLIP
    plan: EditPlan

    @property
    def repairs(self) -> tuple[str, ...]:
        return tuple(self.plan.repairs)


@dataclass(frozen=True, slots=True)
class Rejected:
    verdict: ClassVar[Verdict] = Verdict.REJECT
    reason: ClipRejectReason
    reasons: tuple[str, ...] = ()
    confidence: float | None = None
    repairs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NeedsContext:
    """Round + 1 with this context (`clips.context_from/to`); `before_s`/`after_s` are its edges
    relative to T (`clip_plans.context_request`)."""

    verdict: ClassVar[Verdict] = Verdict.NEED_CONTEXT
    context: Interval
    before_s: float
    after_s: float
    why: str = ""
    reasons: tuple[str, ...] = ()
    confidence: float | None = None
    repairs: tuple[str, ...] = ()

    def context_request(self) -> dict[str, Any]:
        return {"before_s": self.before_s, "after_s": self.after_s, "why": self.why}


@dataclass(frozen=True, slots=True)
class Unrepairable:
    """The answer can't become a plan: retry once with `error` stated, then the clip fails.
    `final`: the retry must not ask for more context again, so it uses the final round's schema
    (`build_request(..., final=True)`)."""

    error: str
    final: bool = False


@dataclass(frozen=True, slots=True)
class PatchRefused:
    """An operator's patch can't be applied (`notify(REVISION_REFUSED)`, §21). Never a reject:
    an operator's edit must not change the clip's verdict."""

    error: str


Resolution = Planned | Rejected | NeedsContext | Unrepairable


# --------------------------------------------------------------------------- entry points


def resolve(
    answer: JudgeAnswer,
    inputs: JudgeInputs,
    params: JudgeParams = DEFAULT_PARAMS,
    *,
    model: str,
    prompt_version: str,
) -> Resolution:
    r = _Repairs()
    reasons = _reasons(answer.reasons, params, r)
    confidence = _clamp01(answer.confidence, r)
    if answer.verdict is Verdict.REJECT:
        return Rejected(ClipRejectReason.MODEL, reasons, confidence, r.codes())
    if answer.verdict is Verdict.NEED_CONTEXT:
        return extend_context(
            answer.need_context, inputs, params, reasons=reasons, confidence=confidence, repairs=r
        )
    ctx = _Ctx(inputs, params)
    draft = _draft_from_answer(answer.plan, ctx, r)
    lengths = (params.min_clip_s, params.max_clip_s)
    segments = _cut(draft.in_utc, draft.out_utc, True, True, ctx, r, lengths)
    if segments is None:
        core = ctx.core.intersection(ctx.context)
        if core is not None and (draft.in_utc, draft.out_utc) != (core.start, core.end):
            segments = _cut(core.start, core.end, True, True, ctx, r, lengths)
            if segments is not None:
                r.add(Repair.CORE_FALLBACK)
    if segments is None:
        if ctx.video_near_moment() < CLIP_MIN.total_seconds():
            why = f"less than {CLIP_MIN.total_seconds():g} s of video near the moment"
            return Rejected(ClipRejectReason.NO_VIDEO, (why, *reasons), confidence, r.codes())
        return Unrepairable(
            f"no cut of at least {CLIP_MIN.total_seconds():g} s fits the video and the speech "
            "there: choose in_s and out_s inside one of the video ranges listed under Media"
        )
    return _finish(
        draft,
        segments,
        ctx,
        r,
        model=model,
        prompt_version=prompt_version,
        reasons=reasons,
        confidence=confidence,
    )


def resolve_patch(
    base: EditPlan,
    patch: PlanPatch,
    inputs: JudgeInputs,
    params: JudgeParams = DEFAULT_PARAMS,
    *,
    model: str = OPERATOR_MODEL,
    prompt_version: str | None = None,
) -> Planned | PatchRefused:
    """An operator's patch on `base` (§21 revisions, author `operator`): the same repairs, with
    the contract's length bounds (3-180 s) instead of the judge's, and only the edges the patch
    moves snapped (a moved edge with no word nearby stays: `unsnapped_*`). No core fallback: a
    patch that can't be cut is refused."""
    r = _Repairs()
    ctx = _Ctx(inputs, params)
    hook = patch.hook or base.hook
    draft = _Draft(
        in_utc=patch.in_utc or base.in_utc,
        out_utc=patch.out_utc or base.out_utc,
        hook_mode=hook.mode,
        hook_text=hook.text,
        hook_start=hook.start_utc,
        hook_end=hook.end_utc,
        title=patch.title or base.title,
        hashtags=list(base.hashtags),
        caption_style=patch.caption_style or base.caption_style,
        emphasis=[(e.start_utc, e.kind) for e in base.emphasis],
        word_fixes=list(base.word_fixes if patch.word_fixes is None else patch.word_fixes),
        zooms=[(z.start_utc, z.end_utc, z.scale, z.target) for z in base.zooms],
        sfx=[(c.at_utc, c.tag, c.gain_db) for c in base.sfx],
        music=None if patch.no_music else (patch.music or base.music),
        layout_kind=(patch.layout or base.layout).kind,
        focus_x=(patch.layout or base.layout).focus_x,
        thumbnail=base.thumbnail_utc,
        moment_type=base.moment_type,
        flags=[f for f in base.flags if f not in _DERIVED_FLAGS],
    )
    lengths = (CLIP_MIN.total_seconds(), CLIP_MAX.total_seconds())
    segments = _cut(
        draft.in_utc,
        draft.out_utc,
        patch.in_utc is not None,
        patch.out_utc is not None,
        ctx,
        r,
        lengths,
    )
    if segments is None:
        return PatchRefused(
            f"the patched cut leaves less than {CLIP_MIN.total_seconds():g} s of video that can "
            "be cut cleanly (outside speech)"
        )
    res = _finish(
        draft,
        segments,
        ctx,
        r,
        model=model,
        prompt_version=prompt_version or base.prompt_version,
        reasons=tuple(base.reasons),
        confidence=base.confidence,
    )
    return PatchRefused(res.error) if isinstance(res, Unrepairable) else res


def resolve_instruction(
    answer: JudgeAnswer,
    base: EditPlan,
    inputs: JudgeInputs,
    params: JudgeParams = DEFAULT_PARAMS,
    *,
    model: str,
    prompt_version: str,
) -> Planned | Rejected | Unrepairable:
    """The model's revision of `base` after an operator's free-text instruction (§19
    "Revisions", author `judge`): the answer resolved like a round's, but with the contract's
    length bounds (3-180 s: the operator may ask for a long or short clip) and no core fallback;
    what only an operator sets is kept from `base`: the caption word fixes, and the music's gain
    and ducking while the mood stays. `Rejected`: the model didn't answer `clip` (the revision is
    refused, the clip keeps its plan)."""
    r = _Repairs()
    reasons = _reasons(answer.reasons, params, r)
    confidence = _clamp01(answer.confidence, r)
    if answer.verdict is not Verdict.CLIP:
        return Rejected(ClipRejectReason.MODEL, reasons, confidence, r.codes())
    if answer.plan is None:
        return Unrepairable("a revision answers verdict clip with the whole revised plan")
    ctx = _Ctx(inputs, params)
    draft = _draft_from_answer(answer.plan, ctx, r)
    draft.word_fixes = list(base.word_fixes)
    if base.music is not None and draft.music is not None and draft.music.mood == base.music.mood:
        draft.music = base.music
    lengths = (CLIP_MIN.total_seconds(), CLIP_MAX.total_seconds())
    segments = _cut(draft.in_utc, draft.out_utc, True, True, ctx, r, lengths)
    if segments is None:
        return Unrepairable(
            f"no cut of at least {CLIP_MIN.total_seconds():g} s fits the video and the speech "
            "there: choose in_s and out_s inside one of the video ranges listed under Media"
        )
    return _finish(
        draft,
        segments,
        ctx,
        r,
        model=model,
        prompt_version=prompt_version,
        reasons=reasons,
        confidence=confidence,
    )


def extend_context(
    ask: ContextAsk | None,
    inputs: JudgeInputs,
    params: JudgeParams = DEFAULT_PARAMS,
    *,
    reasons: tuple[str, ...] = (),
    confidence: float | None = None,
    repairs: _Repairs | None = None,
) -> NeedsContext | Unrepairable:
    """The next round's context: the current one united with `[T - before_s, T + after_s]`,
    the request clamped to the caps (§19: 10 min before, 2 min after) and the stream. At most
    `max_rounds` extensions (rounds are 0-based), so at most `max_rounds + 1` calls per clip."""
    r = repairs or _Repairs()
    p = params
    if inputs.round >= p.max_rounds:
        return Unrepairable(
            f"round {inputs.round} is the last one (at most {p.max_rounds} context extensions): "
            "decide clip or reject with the context you have",
            final=True,
        )
    t, cur = inputs.anchor, inputs.context
    if ask is None:
        before, after, why = p.max_before_s, p.max_after_s, ""
        r.add(Repair.CONTEXT_DEFAULT)
    else:
        before, after, why = ask.before_s, ask.after_s, clean_line(ask.why)[:_REASON_CHARS]
    b = min(max(before, 0.0), p.max_before_s)
    a = min(max(after, 0.0), p.max_after_s)
    if (b, a) != (before, after):
        r.add(Repair.CONTEXT_CLAMP)
    start, end = t - timedelta(seconds=b), t + timedelta(seconds=a)
    started = ensure_utc(inputs.stream_started_at)
    if start < started:
        start = started
        r.add(Repair.CONTEXT_CLAMP)
    if inputs.stream_ended_at is not None and end > ensure_utc(inputs.stream_ended_at):
        end = ensure_utc(inputs.stream_ended_at)
        r.add(Repair.CONTEXT_CLAMP)
    new = Interval(min(cur.start, start), max(cur.end, end))
    if new == cur:
        return Unrepairable(
            "need_context: there is nothing more to see within the limits "
            f"({p.max_before_s:g} s before and {p.max_after_s:g} s after T, inside the stream); "
            "decide clip or reject",
            final=True,
        )
    return NeedsContext(
        context=new,
        before_s=(t - new.start).total_seconds(),
        after_s=(new.end - t).total_seconds(),
        why=why,
        reasons=reasons,
        confidence=confidence,
        repairs=r.codes(),
    )


# --------------------------------------------------------------------------- the draft


@dataclass
class _Draft:
    """A plan in UTC before repairs (from an answer or a patch)."""

    in_utc: datetime
    out_utc: datetime
    title: str
    hook_mode: HookMode = HookMode.NONE
    hook_text: str | None = None
    hook_start: datetime | None = None
    hook_end: datetime | None = None
    hashtags: list[str] = field(default_factory=list)
    caption_style: CaptionStyle = CaptionStyle.BOLD_POP
    emphasis: list[tuple[datetime, Literal["highlight", "big"]]] = field(default_factory=list)
    word_fixes: list[WordFix] = field(default_factory=list)
    zooms: list[tuple[datetime, datetime, float, Any]] = field(default_factory=list)
    sfx: list[tuple[datetime, str, float]] = field(default_factory=list)
    music: MusicPlan | None = None
    layout_kind: LayoutKind = LayoutKind.AUTO
    focus_x: float | None = None
    thumbnail: datetime | None = None
    moment_type: MomentType = MomentType.UNKNOWN
    flags: list[str] = field(default_factory=list)


class _Ctx:
    def __init__(self, inputs: JudgeInputs, params: JudgeParams) -> None:
        self.i = inputs
        self.p = params
        self.t = inputs.anchor
        self.context = inputs.context
        self.tr = Transcript(
            inputs.words,
            inputs.context,
            pause_s=params.sentence_pause_s,
            max_s=params.sentence_max_s,
        )
        parts = (iv.intersection(inputs.context) for iv in inputs.video)
        self.raw_video = merge(p for p in parts if p is not None)
        self.video = bridge_jitter(self.raw_video, CHUNK_JITTER)
        self.sfx_tags = frozenset(t.strip() for t in inputs.sfx_tags if t.strip())
        c = inputs.candidate
        self.moment = Interval(ensure_utc(c.moment_from), ensure_utc(c.moment_to))
        self.core = self.moment.expand(
            timedelta(seconds=params.core_pad_s), timedelta(seconds=params.core_pad_s)
        )

    def at(self, rel_s: float) -> datetime:
        return self.t + timedelta(seconds=min(max(rel_s, -_MAX_REL_S), _MAX_REL_S))

    def moment_type(self) -> MomentType:
        try:
            return MomentType(self.i.candidate.moment_type)
        except ValueError:
            return MomentType.UNKNOWN

    def video_near_moment(self) -> float:
        near = self.moment.expand(_NEAR_MOMENT, _NEAR_MOMENT)
        parts = (v.intersection(near) for v in self.video)
        return sum(part.seconds for part in parts if part is not None)

    def home(self, t: datetime) -> Interval:
        """The (bridged) video range holding t (closed bounds)."""
        for v in self.video:
            if v.start <= t <= v.end:
                return v
        raise ValueError(f"{t} is not inside the video")  # callers keep edges inside

    # ------------------------------------------------------------------ speech

    def clear(
        self,
        t: datetime,
        lo: datetime,
        hi: datetime,
        prefer: Literal["near", "start", "end"] = "near",
    ) -> datetime | None:
        """t if it isn't inside speech; else an edge of its speech block within [lo, hi] (the
        nearer one, or the preferred one when both fit); None when neither fits."""
        block = self.tr.block_at(t)
        if block is None:
            return t
        options = [x for x in block if lo <= x <= hi]
        if not options:
            return None
        if prefer == "start" and block[0] in options:
            return block[0]
        if prefer == "end" and block[1] in options:
            return block[1]
        return min(options, key=lambda x: (abs(x - t), x))

    def cleared(self, t: datetime, lo: datetime, hi: datetime, prefer="near") -> datetime:
        """`clear`, or t itself when no block edge fits (the final pass handles that)."""
        x = self.clear(t, lo, hi, prefer)
        return t if x is None else x

    # ------------------------------------------------------------------ snapping

    def snap_in(self, t: datetime, lo: datetime, hi: datetime, r: _Repairs) -> datetime:
        p, tr = self.p, self.tr
        window = timedelta(seconds=p.snap_window_s)
        edge = nearest(tr.sentence_starts(), t, window, lo, hi)
        if edge is None:
            edge = nearest(tr.word_starts(), t, window, lo, hi)
        if edge is None:
            r.add(Repair.UNSNAPPED_IN)
            return self.cleared(t, lo, hi)
        new = edge - timedelta(seconds=p.in_pad_s)
        prev_end = tr.end_before(edge)
        if prev_end is not None:
            new = max(new, min(prev_end, edge))
        new = self.cleared(min(max(new, lo), edge), lo, hi)
        if new != t:
            r.add(Repair.SNAP_IN)
        return new

    def snap_out(self, t: datetime, lo: datetime, hi: datetime, r: _Repairs) -> datetime:
        p, tr = self.p, self.tr
        window = timedelta(seconds=p.snap_window_s)
        edge = nearest(tr.sentence_ends(), t, window, lo, hi)
        if edge is None:
            edge = nearest(tr.word_ends(), t, window, lo, hi)
        if edge is None:
            r.add(Repair.UNSNAPPED_OUT)
            return self.cleared(t, lo, hi)
        new = edge + timedelta(seconds=p.out_pad_s)
        next_start = tr.start_after(edge - _EPS)
        if next_start is not None:
            new = min(new, max(next_start, edge))
        new = self.cleared(max(min(new, hi), edge), lo, hi)
        if new != t:
            r.add(Repair.SNAP_OUT)
        return new


def _draft_from_answer(plan: PlanAnswer | None, ctx: _Ctx, r: _Repairs) -> _Draft:
    if plan is None:
        r.add(Repair.DEFAULT_PLAN)
        core = ctx.core.intersection(ctx.context) or Interval(ctx.t, ctx.t)
        return _Draft(
            in_utc=core.start,
            out_utc=core.end,
            title="",
            moment_type=ctx.moment_type(),
            flags=list(ctx.i.flags),
        )
    hook = plan.hook
    layout = plan.layout
    return _Draft(
        in_utc=ctx.at(plan.in_s),
        out_utc=ctx.at(plan.out_s),
        title=plan.title,
        hook_mode=hook.mode,
        hook_text=hook.text,
        hook_start=None if hook.from_s is None else ctx.at(hook.from_s),
        hook_end=None if hook.to_s is None else ctx.at(hook.to_s),
        hashtags=list(plan.hashtags),
        caption_style=plan.caption_style,
        emphasis=_emphasis_words(plan, ctx, r),
        zooms=[(ctx.at(z.from_s), ctx.at(z.to_s), z.scale, z.target) for z in plan.zooms],
        sfx=[(ctx.at(c.at_s), c.tag, 0.0) for c in plan.sfx],
        music=None if plan.music_mood is None else MusicPlan(mood=plan.music_mood),
        layout_kind=layout.kind,
        focus_x=layout.focus_x,
        thumbnail=None if plan.thumbnail_s is None else ctx.at(plan.thumbnail_s),
        moment_type=plan.moment_type or ctx.moment_type(),
        flags=list(ctx.i.flags),
    )


def _sentence_id(raw: str) -> str:
    s = raw.strip().upper()
    return f"S{s}" if s.isdigit() else s


def _emphasis_words(
    plan: PlanAnswer, ctx: _Ctx, r: _Repairs
) -> list[tuple[datetime, Literal["highlight", "big"]]]:
    """(sentence id, word) -> the word's start: the first word of that sentence with the same
    normalised text not taken yet (the model's order is its priority)."""
    out: list[tuple[datetime, Literal["highlight", "big"]]] = []
    taken: set[datetime] = set()
    for e in plan.emphasis:
        sentence = ctx.tr.by_id.get(_sentence_id(e.sentence))
        tokens = e.word.split()
        target = norm_word(tokens[0]) if tokens else ""
        hit = None
        if sentence is not None and target:
            hit = next(
                (
                    w
                    for w in sentence.words
                    if norm_word(w.text) == target and w.start_utc not in taken
                ),
                None,
            )
        if hit is None:
            r.add(Repair.EMPHASIS_DROP)
            continue
        taken.add(hit.start_utc)
        out.append((hit.start_utc, e.kind))
    return out


# --------------------------------------------------------------------------- finishing


def _finish(
    d: _Draft,
    segments: list[Interval],
    ctx: _Ctx,
    r: _Repairs,
    *,
    model: str,
    prompt_version: str,
    reasons: tuple[str, ...],
    confidence: float,
) -> Planned | Unrepairable:
    p = ctx.p
    title = _title(d.title, segments, ctx, r)
    hook = _hook(d, segments, ctx, r)
    flags = {clean_line(f) for f in d.flags if clean_line(f)} - _DERIVED_FLAGS
    if _blocked(title, p) or (hook.text is not None and _blocked(hook.text, p)):
        flags.add(FLAG_SENSITIVE)
    if _music_risk(segments, ctx):
        flags.add(FLAG_MUSIC_RISK)
    focus = d.focus_x
    if focus is not None and not 0.0 <= focus <= 1.0:
        focus = min(max(focus, 0.0), 1.0)
        r.add(Repair.FOCUS_CLAMP)
    try:
        plan = EditPlan(
            clip_id=ctx.i.clip_id,
            stream_id=ctx.i.candidate.stream_id,
            anchor_utc=ctx.t,
            segments=[PlanSegment(start_utc=s.start, end_utc=s.end) for s in segments],
            hook=hook,
            title=title,
            hashtags=_hashtags(d.hashtags, p, r),
            caption_style=d.caption_style,
            emphasis=_emphasis(d.emphasis, segments, ctx, r),
            word_fixes=_word_fixes(d.word_fixes, segments, ctx, r),
            zooms=_zooms(d.zooms, segments, ctx, r),
            sfx=_sfx(d.sfx, segments, ctx, r),
            music=d.music,
            layout=LayoutHint(kind=d.layout_kind, focus_x=focus),
            thumbnail_utc=_thumbnail(d.thumbnail, segments, r),
            moment_type=d.moment_type,
            confidence=confidence,
            reasons=list(reasons),
            flags=sorted(flags),
            repairs=list(r.codes()),
            model=model,
            prompt_version=prompt_version,
        )
    except ValidationError as exc:  # a resolver bug: the property test guards against it
        return Unrepairable(f"the repaired plan is still invalid: {exc}")
    return Planned(plan)


def _at(t: datetime, segments: Sequence[Interval]) -> Interval | None:
    return next((s for s in segments if s.start <= t < s.end), None)


# --------------------------------------------------------------------------- the cut


def _cut(
    a: datetime,
    b: datetime,
    snap_a: bool,
    snap_b: bool,
    ctx: _Ctx,
    repairs: _Repairs,
    lengths: tuple[float, float],
) -> list[Interval] | None:
    """The segments of the cut [a, b], or None when no clean cut of CLIP_MIN fits. Repair codes
    are added to `repairs` only when it succeeds."""
    if not ctx.video:
        return None
    r = _Repairs()
    a, b = ensure_utc(a), ensure_utc(b)
    if b < a:
        a, b = b, a
        snap_a, snap_b = snap_b, snap_a
        r.add(Repair.SWAP_IN_OUT)
    lo_ctx, hi_ctx = ctx.context.start, ctx.context.end
    if b <= lo_ctx or a >= hi_ctx:  # nothing of it in the context: the core moment instead
        core = ctx.core.intersection(ctx.context) or Interval(ctx.t, ctx.t)
        ca, cb = core.start, core.end
    else:
        ca, cb = max(a, lo_ctx), min(b, hi_ctx)
    if (ca, cb) != (a, b):
        r.add(Repair.CLAMP_CONTEXT)
    a, b = _into_video(ca, cb, ctx, r)

    # snap the edges, each inside its own video range
    va = ctx.home(a)
    hi_a = min(b, va.end)
    a = ctx.snap_in(a, va.start, hi_a, r) if snap_a else ctx.cleared(a, va.start, hi_a)
    vb = ctx.home(b)
    lo_b = max(a, vb.start)
    b = ctx.snap_out(b, lo_b, vb.end, r) if snap_b else ctx.cleared(b, lo_b, vb.end)

    pieces = [
        Interval(max(a, v.start), min(b, v.end)) for v in ctx.video if v.start <= b and a <= v.end
    ]
    pieces = _holes(pieces, ctx, r)
    pieces = _settle(_fit_length(pieces, ctx, r, lengths), ctx, r, lengths)
    if pieces and sum(pc.seconds for pc in pieces) < lengths[0]:  # the final pass shortened it
        pieces = _settle(_fit_length(pieces, ctx, r, lengths), ctx, r, lengths)
    pieces = [pc for pc in merge(pieces) if pc.seconds > 0]  # touching pieces: one segment
    total = sum(pc.seconds for pc in pieces)
    if total < CLIP_MIN.total_seconds():
        return None
    if any(sum(1 for v in ctx.raw_video if v.overlaps(pc)) > 1 for pc in pieces):
        r.add(Repair.MERGE_SEGMENTS)  # jitter holes inside a segment were bridged
    if total < lengths[0]:
        r.add(Repair.SHORT_MEDIA)
    repairs.extend(r)
    return pieces


def _into_video(a: datetime, b: datetime, ctx: _Ctx, r: _Repairs) -> tuple[datetime, datetime]:
    """a -> the first video time >= a, b -> the last video time <= b; a cut with no video in it
    becomes the nearest video point (a zero-length cut there, extended later)."""
    video = ctx.video
    na = next((max(a, v.start) for v in video if v.end >= a), None)
    nb = next((min(b, v.end) for v in reversed(video) if v.start <= b), None)
    if na is not None and nb is not None and na <= nb:
        if (na, nb) != (a, b):
            r.add(Repair.CLAMP_VIDEO)
        return na, nb

    def distance(v: Interval) -> tuple[float, float, datetime]:
        gap = max((v.start - b).total_seconds(), (a - v.end).total_seconds(), 0.0)
        to_t = max((v.start - ctx.t).total_seconds(), (ctx.t - v.end).total_seconds(), 0.0)
        return gap, to_t, v.start

    v = min(video, key=distance)
    point = v.end if v.end <= a else v.start if v.start >= b else min(max(a, v.start), v.end)
    r.add(Repair.MOVE_TO_MEDIA)
    return point, point


def _holes(pieces: list[Interval], ctx: _Ctx, r: _Repairs) -> list[Interval]:
    """Split at short holes, shrink to T's side of long ones (the new edge snapped inwards),
    pieces shorter than min_segment_s next to holes dropped. Edges at holes are moved out of
    speech by `_settle`."""
    if len(pieces) <= 1:
        return pieces
    p = ctx.p
    clusters: list[list[Interval]] = [[pieces[0]]]
    for prev, pc in pairwise(pieces):
        if (pc.start - prev.end).total_seconds() > p.split_gap_max_s:
            clusters.append([pc])
        else:
            clusters[-1].append(pc)
    if len(clusters) > 1:

        def key(cl: list[Interval]) -> tuple[bool, float, float]:
            # a side whose video ranges can't hold CLIP_MIN only when no side can
            room = sum(ctx.home(pc.start).seconds for pc in cl)
            to_t = max(
                (cl[0].start - ctx.t).total_seconds(), (ctx.t - cl[-1].end).total_seconds(), 0.0
            )
            return room < CLIP_MIN.total_seconds(), to_t, -sum(pc.seconds for pc in cl)

        chosen = list(min(clusters, key=key))
        r.add(Repair.SHRINK_GAP)
        if chosen[0].start != pieces[0].start:  # a new in point at the hole: snap it inwards
            f = chosen[0]
            start = ctx.snap_in(f.start, f.start, f.end, r)
            chosen[0] = Interval(start, max(start, f.end))
        if chosen[-1].end != pieces[-1].end:  # a new out point at the hole
            last_piece = chosen[-1]
            end = ctx.snap_out(last_piece.end, last_piece.start, last_piece.end, r)
            chosen[-1] = Interval(last_piece.start, max(last_piece.start, end))
        pieces = chosen
    if len(pieces) <= 1:
        return pieces
    r.add(Repair.SPLIT_GAP)
    kept = [pc for pc in pieces if pc.seconds >= p.min_segment_s]
    if len(kept) < len(pieces):
        r.add(Repair.DROP_SHORT_SEGMENT)
    if not kept:
        kept = [min(pieces, key=lambda pc: (-pc.seconds, abs((pc.start - ctx.t).total_seconds())))]
    return kept


def _settle(
    pieces: list[Interval], ctx: _Ctx, r: _Repairs, lengths: tuple[float, float]
) -> list[Interval]:
    """The final pass: every segment edge out of speech (inside its own video range, without
    passing the length limit), segments without a clean cut or (with several) shorter than
    min_segment_s dropped, a new outer edge snapped inwards; until nothing changes."""
    max_s, min_seg = lengths[1], ctx.p.min_segment_s
    pieces = [pc for pc in pieces if pc.seconds > 0] or list(pieces)
    for _ in range(2 * len(pieces) + 4):
        changed = False
        out: list[Interval] = []
        total = sum(pc.seconds for pc in pieces)
        dropped_first = dropped_last = False
        n = len(pieces)
        for i, pc in enumerate(pieces):
            home = ctx.home(pc.start)
            room = timedelta(seconds=max(max_s - total, 0.0))
            start = ctx.clear(pc.start, max(home.start, pc.start - room), pc.end)
            end = None
            if start is not None:
                room -= max(pc.start - start, timedelta(0))
                end = ctx.clear(pc.end, start, min(home.end, pc.end + room))
            if (
                start is None
                or end is None
                or end <= start
                or (n > 1 and _secs(start, end) < min_seg)
            ):
                r.add(Repair.DROP_SHORT_SEGMENT)
                dropped_first |= i == 0
                dropped_last |= i == n - 1
                total -= pc.seconds
                changed = True
                continue
            if start != pc.start:
                r.add(Repair.EDGE_SPEECH if i == 0 else Repair.GAP_EDGE_WORD)
            if end != pc.end:
                r.add(Repair.EDGE_SPEECH if i == n - 1 else Repair.GAP_EDGE_WORD)
            changed |= (start, end) != (pc.start, pc.end)
            total += _secs(start, end) - pc.seconds
            out.append(Interval(start, end))
        if out and dropped_first:  # a former inner edge is now the in point
            f = out[0]
            start = ctx.snap_in(f.start, f.start, f.end, r)
            out[0] = Interval(start, max(start, f.end))
        if out and dropped_last:
            last_piece = out[-1]
            end = ctx.snap_out(last_piece.end, last_piece.start, last_piece.end, r)
            out[-1] = Interval(last_piece.start, max(last_piece.start, end))
        pieces = out
        if not changed or not pieces:
            break
    return pieces


def _secs(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds()


def _content_before(pieces: Sequence[Interval], t: datetime) -> float:
    return sum(
        max(0.0, (min(pc.end, t) - pc.start).total_seconds()) for pc in pieces if pc.start < t
    )


def _slice(pieces: Sequence[Interval], u: float, v: float) -> list[Interval]:
    """The content seconds [u, v] of the pieces (content time runs over the pieces only)."""
    out: list[Interval] = []
    pos = 0.0
    for pc in pieces:
        n = pc.seconds
        a, b = max(u, pos), min(v, pos + n)
        if b > a or (n == 0 and u <= pos <= v):
            start = pc.start + timedelta(seconds=a - pos)
            end = pc.start + timedelta(seconds=b - pos) if b > a else start
            out.append(Interval(start, min(max(end, start), pc.end)))
        pos += n
    return out


def _fit_length(
    pieces: list[Interval], ctx: _Ctx, r: _Repairs, lengths: tuple[float, float]
) -> list[Interval]:
    if not pieces:
        return pieces
    min_s, max_s = lengths
    total = sum(pc.seconds for pc in pieces)
    if total > max_s:
        return _trim(pieces, ctx, r, max_s)
    if total < min_s:
        return _extend(pieces, ctx, r, min_s, max_s)
    return pieces


def _trim(pieces: list[Interval], ctx: _Ctx, r: _Repairs, max_s: float) -> list[Interval]:
    """Trim the side farther from T until both are even, then both sides."""
    total = sum(pc.seconds for pc in pieces)
    excess = total - max_s + 1e-3  # 1 ms spare: edges round to whole microseconds
    first, last = pieces[0], pieces[-1]
    if ctx.t <= first.start:
        cut_in, cut_out = 0.0, excess
    elif ctx.t >= last.end:
        cut_in, cut_out = excess, 0.0
    else:
        before = _content_before(pieces, ctx.t)
        after = total - before
        lead = min(excess, abs(before - after))
        rest = (excess - lead) / 2
        cut_in = rest + (lead if before > after else 0.0)
        cut_out = rest + (lead if after >= before else 0.0)
    kept = [pc for pc in _slice(pieces, cut_in, total - cut_out) if pc.seconds > 0]
    if not kept:
        return pieces
    r.add(Repair.TRIM_LONG)
    if cut_in > 0:  # the new edges snap inwards: the clip only gets shorter
        f = kept[0]
        start = ctx.snap_in(f.start, f.start, f.end, r)
        kept[0] = Interval(start, max(start, f.end))
    if cut_out > 0:
        last_piece = kept[-1]
        end = ctx.snap_out(last_piece.end, last_piece.start, last_piece.end, r)
        kept[-1] = Interval(last_piece.start, max(last_piece.start, end))
    return kept


def _extend(
    pieces: list[Interval], ctx: _Ctx, r: _Repairs, min_s: float, max_s: float
) -> list[Interval]:
    """Grow to the neighbouring sentence edges (the side with less room around T first); seconds
    where no sentence edge fits. Each edge stays inside its video range; never above max_s."""
    pieces = list(pieces)
    changed = False
    for _ in range(200):
        total = sum(pc.seconds for pc in pieces)
        if total >= min_s:
            break
        first, last = pieces[0], pieces[-1]
        lo, hi = ctx.home(first.start).start, ctx.home(last.end).end
        if first.start <= lo and last.end >= hi:
            break
        room = max_s - total
        before = (ctx.t - first.start).total_seconds()
        after = (last.end - ctx.t).total_seconds()
        sides = ("in", "out") if before <= after else ("out", "in")
        moved = False
        for seconds in (None, min_s - total):  # a sentence edge first, else seconds
            for side in sides:
                if side == "in":
                    t = _grow_in(ctx, first, lo, room, seconds)
                    if t is not None:
                        pieces[0] = Interval(t, pieces[0].end)
                else:
                    t = _grow_out(ctx, last, hi, room, seconds)
                    if t is not None:
                        pieces[-1] = Interval(pieces[-1].start, t)
                if t is not None:
                    moved = True
                    break
            if moved:
                break
        if not moved:
            break
        changed = True
    if changed:
        r.add(Repair.EXTEND_SHORT)
    return pieces


def _grow_in(
    ctx: _Ctx, first: Interval, lo: datetime, room: float, seconds: float | None
) -> datetime | None:
    """A new in point before `first.start` (>= lo, growing by at most `room` seconds): the
    previous sentence start (padded), or with `seconds` that many seconds earlier, out of speech.
    None if it doesn't grow."""
    tr = ctx.tr
    if seconds is None:
        starts = [s for s in tr.sentence_starts() if lo <= s < first.start]
        if not starts:
            return None
        edge = starts[-1]
        new = edge - timedelta(seconds=ctx.p.in_pad_s)
        prev_end = tr.end_before(edge)
        if prev_end is not None:
            new = max(new, min(prev_end, edge))
        new = ctx.clear(min(max(new, lo), edge), lo, first.end, "start")
    else:
        raw = max(first.start - timedelta(seconds=seconds), lo)
        new = ctx.clear(raw, lo, first.end, "start")
        if new is None or (first.start - new).total_seconds() > room:  # too long: skip it
            new = ctx.clear(raw, lo, first.start, "end")
    if new is None:
        return None
    grow = (first.start - new).total_seconds()
    return new if 0 < grow <= room else None


def _grow_out(
    ctx: _Ctx, last: Interval, hi: datetime, room: float, seconds: float | None
) -> datetime | None:
    """`_grow_in` for the out point: the next sentence end (padded), or `seconds` later."""
    tr = ctx.tr
    if seconds is None:
        ends = [e for e in tr.sentence_ends() if last.end < e <= hi]
        if not ends:
            return None
        edge = ends[0]
        new = edge + timedelta(seconds=ctx.p.out_pad_s)
        next_start = tr.start_after(edge - _EPS)
        if next_start is not None:
            new = min(new, max(next_start, edge))
        new = ctx.clear(max(min(new, hi), edge), last.start, hi, "end")
    else:
        raw = min(last.end + timedelta(seconds=seconds), hi)
        new = ctx.clear(raw, last.start, hi, "end")
        if new is None or (new - last.end).total_seconds() > room:
            new = ctx.clear(raw, last.end, hi, "start")
    if new is None:
        return None
    grow = (new - last.end).total_seconds()
    return new if 0 < grow <= room else None


# --------------------------------------------------------------------------- the rest


def _cut_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if text[limit] != " " and " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip() or text[:limit].strip()


def _title(raw: str, segments: Sequence[Interval], ctx: _Ctx, r: _Repairs) -> str:
    limit = ctx.p.title_max
    title = clean_line(raw)
    if title:
        cut = _cut_words(title, limit)
        if cut != title:
            r.add(Repair.TITLE_TRIM)
        return cut
    core = ctx.core
    in_core = (s for s in ctx.tr.sentences if s.start_utc < core.end and s.end_utc > core.start)
    sentence = next((s for s in in_core if s.text), None) or next(
        (s for s in ctx.tr.sentences if _at(s.start_utc, segments) is not None and s.text), None
    )
    if sentence is not None:
        r.add(Repair.TITLE_FROM_TRANSCRIPT)
        return _cut_words(sentence.text, limit)
    r.add(Repair.TITLE_FALLBACK)
    return _cut_words(f"{clean_line(ctx.i.channel) or 'Twitch'} clip", limit)


def _blocked(text: str, p: JudgeParams) -> bool:
    if not p.title_blocklist:
        return False
    words = {norm_word(t) for t in text.split()}
    return any(norm_word(b) in words for b in p.title_blocklist)


def _hashtags(raw: Iterable[str], p: JudgeParams, r: _Repairs) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for tag in raw:
        t = tag.strip()
        if t.startswith("#"):
            t = t.lstrip("#").strip()
            r.add(Repair.HASHTAG_STRIP)
        if not t or any(ch.isspace() for ch in t) or t.startswith("#") or t.lower() in seen:
            r.add(Repair.HASHTAG_DROP)
            continue
        if len(out) >= p.hashtags_max:
            r.add(Repair.HASHTAG_CAP)
            break
        seen.add(t.lower())
        out.append(t)
    return out


def _hook(d: _Draft, segments: Sequence[Interval], ctx: _Ctx, r: _Repairs) -> Hook:
    p = ctx.p
    if d.hook_mode is HookMode.TEXT:
        text = clean_line(d.hook_text or "")
        cut = _cut_words(text, p.hook_text_max)
        if not cut:
            r.add(Repair.HOOK_NONE)
            return Hook()
        if cut != text:
            r.add(Repair.HOOK_TEXT_TRIM)
        return Hook(mode=HookMode.TEXT, text=cut)
    if d.hook_mode is HookMode.TEASER:
        teaser = _teaser(d.hook_start, d.hook_end, segments, ctx, r)
        if teaser is None:
            r.add(Repair.HOOK_NONE)
            return Hook()
        return Hook(mode=HookMode.TEASER, start_utc=teaser[0], end_utc=teaser[1])
    return Hook()


def _teaser(
    start: datetime | None,
    end: datetime | None,
    segments: Sequence[Interval],
    ctx: _Ctx,
    r: _Repairs,
) -> tuple[datetime, datetime] | None:
    if start is None or end is None:
        return None
    s, e = sorted((ensure_utc(start), ensure_utc(end)))
    want = Interval(s, e)

    def score(seg: Interval) -> tuple[float, float]:
        overlap = seg.intersection(want)
        return -(overlap.seconds if overlap else 0.0), abs((seg.start - s).total_seconds())

    seg = _at(s, segments) or min(segments, key=score)
    a, b = min(max(s, seg.start), seg.end), min(max(e, seg.start), seg.end)
    a = ctx.clear(a, seg.start, seg.end, "start")
    if a is None:
        return None
    hi = min(seg.end, a + TEASER_MAX)
    b = ctx.clear(min(max(b, a), hi), a, hi, "end")  # speech cut at hi: up to its start
    if b is None or (b - a).total_seconds() < ctx.p.teaser_min_s:
        return None
    if (a, b) != (s, e):
        r.add(Repair.TEASER_CLAMP)
    return a, b


def _emphasis(
    items: Sequence[tuple[datetime, Literal["highlight", "big"]]],
    segments: Sequence[Interval],
    ctx: _Ctx,
    r: _Repairs,
) -> list[EmphasisWord]:
    words = [w for seg in segments for w in ctx.tr.words_in(seg.start, seg.end)]
    cap = len(words) // ctx.p.words_per_emphasis
    out: list[EmphasisWord] = []
    seen: set[datetime] = set()
    for start, kind in items:
        word = ctx.tr.word_at(ensure_utc(start), _WORD_TOLERANCE)
        if word is None or word.start_utc in seen or _at(word.start_utc, segments) is None:
            r.add(Repair.EMPHASIS_DROP)
            continue
        text = clean_line(word.text)
        if not text:
            r.add(Repair.EMPHASIS_DROP)
            continue
        if len(out) >= cap:
            r.add(Repair.EMPHASIS_CAP)
            break
        seen.add(word.start_utc)
        out.append(EmphasisWord(start_utc=word.start_utc, text=text, kind=kind))
    return sorted(out, key=lambda e: e.start_utc)


def _word_fixes(
    fixes: Sequence[WordFix], segments: Sequence[Interval], ctx: _Ctx, r: _Repairs
) -> list[WordFix]:
    out: list[WordFix] = []
    seen: set[datetime] = set()
    for fix in fixes:
        word = ctx.tr.word_at(fix.start_utc, _WORD_TOLERANCE)
        if word is None or word.start_utc in seen or _at(word.start_utc, segments) is None:
            r.add(Repair.WORD_FIX_DROP)
            continue
        seen.add(word.start_utc)
        out.append(WordFix(start_utc=word.start_utc, text=fix.text))
    return sorted(out, key=lambda f: f.start_utc)


def _zooms(
    items: Sequence[tuple[datetime, datetime, float, Any]],
    segments: Sequence[Interval],
    ctx: _Ctx,
    r: _Repairs,
) -> list[Zoom]:
    p = ctx.p
    pieces: list[tuple[datetime, datetime, float, Any]] = []
    for start, end, scale, target in items:
        a, b = sorted((ensure_utc(start), ensure_utc(end)))
        s = min(max(scale, 1.0), p.zoom_max_scale)
        if s != scale:
            r.add(Repair.ZOOM_SCALE)
        parts = [
            (max(a, seg.start), min(b, seg.end))
            for seg in segments
            if max(a, seg.start) < min(b, seg.end)
        ]
        if len(parts) > 1:
            r.add(Repair.ZOOM_SPLIT)
        long_enough = [(x, y) for x, y in parts if (y - x).total_seconds() >= p.zoom_min_s]
        if len(long_enough) < len(parts) or not parts:
            r.add(Repair.ZOOM_DROP)
        pieces += [(x, y, s, target) for x, y in long_enough]
    pieces.sort(key=lambda z: (z[0], z[1]))
    merged: list[tuple[datetime, datetime, float, Any]] = []
    for z in pieces:
        if merged and z[0] < merged[-1][1]:
            a, b, s, target = merged[-1]
            merged[-1] = (a, max(b, z[1]), max(s, z[2]), target)
            r.add(Repair.ZOOM_MERGE)
        else:
            merged.append(z)
    return [Zoom(start_utc=a, end_utc=b, scale=s, target=t) for a, b, s, t in merged]


def _sfx(
    items: Sequence[tuple[datetime, str, float]],
    segments: Sequence[Interval],
    ctx: _Ctx,
    r: _Repairs,
) -> list[SfxCue]:
    p = ctx.p
    cues: list[tuple[datetime, str, float]] = []
    for at, tag, gain in items:
        tag = tag.strip()
        if tag not in ctx.sfx_tags:
            r.add(Repair.SFX_UNKNOWN)
            continue
        at = ensure_utc(at)
        if _at(at, segments) is None:
            r.add(Repair.SFX_OUTSIDE)
            continue
        cues.append((at, tag, min(max(gain, -30.0), 6.0)))
    cues.sort(key=lambda c: c[0])
    out: list[SfxCue] = []
    gap = timedelta(seconds=p.sfx_min_gap_s)
    for at, tag, gain in cues:
        if out and at - out[-1].at_utc < gap:
            r.add(Repair.SFX_CLOSE)
            continue
        if len(out) >= p.sfx_max:
            r.add(Repair.SFX_CAP)
            break
        out.append(SfxCue(at_utc=at, tag=tag, gain_db=gain))
    return out


def _thumbnail(t: datetime | None, segments: Sequence[Interval], r: _Repairs) -> datetime | None:
    if t is None:
        return None
    if _at(ensure_utc(t), segments) is None:
        r.add(Repair.THUMBNAIL_DROP)
        return None
    return ensure_utc(t)


def _music_risk(segments: Sequence[Interval], ctx: _Ctx) -> bool:
    for row in ctx.i.audio:
        score = (row.classes or {}).get(SoundGroup.MUSIC)
        if not isinstance(score, int | float) or score < ctx.p.music_risk:
            continue
        second = Interval(ensure_utc(row.ts), ensure_utc(row.ts) + timedelta(seconds=1))
        if any(second.overlaps(seg) for seg in segments):
            return True
    return False


def _reasons(raw: Iterable[str], p: JudgeParams, r: _Repairs) -> tuple[str, ...]:
    out = []
    for reason in raw:
        text = clean_line(reason)
        if not text:
            r.add(Repair.REASONS_TRIM)
            continue
        if len(text) > _REASON_CHARS:
            text = _cut_words(text, _REASON_CHARS)
            r.add(Repair.REASONS_TRIM)
        if len(out) >= p.reasons_max:
            r.add(Repair.REASONS_TRIM)
            break
        out.append(text)
    return tuple(out)


def _clamp01(x: float, r: _Repairs) -> float:
    if 0.0 <= x <= 1.0:
        return x
    r.add(Repair.CONFIDENCE_CLAMP)
    return min(max(x, 0.0), 1.0)
