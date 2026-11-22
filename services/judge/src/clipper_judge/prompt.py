"""The LLM input of one judge round (DESIGN §19): a versioned, byte-stable document + its hash.

`build_request(inputs)` is pure: the same inputs give the same bytes (inputs are sorted, numbers
formatted with fixed precision, no wall-clock values), so requests are cacheable and the format
is golden-file tested. Sections (stable text first, for prompt caching): the system prompt, then
the user content with meta, options, media, transcript (one line per sentence, `S14
[-12.4→-8.1] text`, low-confidence words marked `(?)`, holes as `[-40.0→-25.0 no audio: ad]`), chat
per 5 s on video time, audio events, earlier rounds and the task. Seconds are relative to T; no
speaker labels. Stream content (transcript, chat, title) is untrusted: it sits inside `<data>`
fences, on one line per item, with angle brackets replaced (`transcript.clean_text`).

Prompt versions: `clipper_judge/prompts/v{N}/` holds `system.md` and `user.md` (a
`string.Template`); `prompt_version` = `v{N}-{sha8}` over both texts, the code's line format
(`INPUT_FORMAT`) and both answer schemas, so editing any of them shows in every plan and hash.
`request_hash(model, request, options)` = sha256 over the model name, the provider options
(temperature, reasoning effort, ...: everything that changes the answer) and the canonical
request (prompt version, schema version, system, user, schema, max tokens):
`judge_calls.request_hash`.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import cache
from importlib import resources
from string import Template
from typing import Any

from clipper_core.chat import is_command, is_mention, normalised_text, strip_links, user_key
from clipper_core.contracts import CaptionStyle, LayoutKind, MusicMood, SoundGroup
from clipper_core.timeline import Interval, ensure_utc, merge, uncovered
from clipper_judge.answer import ANSWER_SCHEMA_VERSION, answer_schema
from clipper_judge.inputs import ChatMessageLike, JudgeInputs, bridge_jitter
from clipper_judge.params import DEFAULT_PARAMS, JudgeParams
from clipper_judge.transcript import Sentence, Transcript, clean_text

PROMPT = "v1"  # the prompt directory used by default
INPUT_FORMAT = 1  # bump with every change to the line formats built in this module
_TRAILING = ".,!?;:"  # a low-confidence marker goes before these: "go(?)."
_TIMES = chr(0xD7)  # multiplication sign: '"no way" x3'


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    name: str  # "v1"
    version: str  # "v1-<sha8>"
    system: str
    user: Template


@cache
def load_prompt(name: str = PROMPT) -> PromptTemplate:
    """A prompt directory of the package (line endings normalised to LF)."""
    base = resources.files("clipper_judge").joinpath("prompts", name)
    system = base.joinpath("system.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    user = base.joinpath("user.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    digest = hashlib.sha256()
    for part in (
        system,
        user,
        str(INPUT_FORMAT),
        str(ANSWER_SCHEMA_VERSION),
        _canonical(answer_schema(False)),
        _canonical(answer_schema(True)),
    ):
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return PromptTemplate(name, f"{name}-{digest.hexdigest()[:8]}", system, Template(user))


@dataclass(frozen=True, slots=True)
class JudgeRequest:
    """What goes to the model (`JudgeModel.judge`, build item 6c)."""

    prompt_version: str
    schema_version: int
    final: bool  # the last round: the schema has no need_context
    system: str
    user: str
    schema_name: str
    schema: Mapping[str, Any]
    max_tokens: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "final": self.final,
            "system": self.system,
            "user": self.user,
            "schema_name": self.schema_name,
            "schema": self.schema,
            "max_tokens": self.max_tokens,
        }

    def canonical(self) -> str:
        """Canonical JSON (sorted keys, no spaces, UTF-8 text): what the hash covers."""
        return _canonical(self.as_dict())


def request_hash(
    model: str, request: JudgeRequest, options: Mapping[str, Any] | None = None
) -> str:
    """`judge_calls.request_hash`: sha256 of the model, the provider options and the canonical
    request. `options` must hold every provider setting that changes the answer (temperature,
    top_p, reasoning effort or thinking budget, seed, ...), JSON values only, so two settings
    never share a cached answer; the adapter (6d) passes the ones it sends."""
    payload = _canonical(
        {"model": model, "options": dict(options or {}), "request": request.as_dict()}
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_request(
    inputs: JudgeInputs,
    params: JudgeParams = DEFAULT_PARAMS,
    *,
    prompt: str = PROMPT,
    final: bool | None = None,
) -> JudgeRequest:
    """`final`: the final round's schema (no need_context) and task. Default: by the round
    (`round >= max_rounds`); True for the retry after `Unrepairable(final=True)`."""
    template = load_prompt(prompt)
    final = inputs.round >= params.max_rounds if final is None else final
    return JudgeRequest(
        prompt_version=template.version,
        schema_version=ANSWER_SCHEMA_VERSION,
        final=final,
        system=template.system,
        user=render_user(inputs, params, template, final=final),
        schema_name="judge_answer_final" if final else "judge_answer",
        schema=answer_schema(final),
        max_tokens=params.max_output_tokens,
    )


RETRY_ERROR_CHARS = 600  # the stated error is cut to this length


def retry_request(request: JudgeRequest, error: str) -> JudgeRequest:
    """The round's one retry (§19: invalid JSON or an unrepairable plan): `request` (built for
    the retry: `final=True` after `Unrepairable(final=True)`) with the error stated after the
    task. The error is one cleaned line, angle brackets replaced (it may quote the model's own
    output, which may quote the stream)."""
    note = _cut(clean_text(error), RETRY_ERROR_CHARS) or "the answer could not be used"
    user = (
        f"{request.user.rstrip()}\n\n"
        f"Retry: your previous answer could not be used: {note}\n"
        "Answer again with one JSON object that follows the schema."
    )
    return replace(request, user=user)


def render_user(
    inputs: JudgeInputs,
    params: JudgeParams = DEFAULT_PARAMS,
    template: PromptTemplate | None = None,
    *,
    final: bool | None = None,
) -> str:
    """The user content: the input document."""
    template = template or load_prompt()
    final = inputs.round >= params.max_rounds if final is None else final
    transcript = Transcript(
        inputs.words,
        inputs.context,
        pause_s=params.sentence_pause_s,
        max_s=params.sentence_max_s,
    )
    doc = _Doc(inputs, params, final=final)
    return template.user.substitute(
        meta=doc.meta(),
        options=doc.options(),
        media=doc.media(),
        transcript=doc.transcript(transcript.sentences),
        bucket=_num(params.chat_bucket_s),
        chat=doc.chat(),
        sound_min=_num(params.sound_min),
        loud_db=_num(params.loud_db),
        music_risk=_num(params.music_risk),
        audio=doc.audio(),
        rounds=doc.rounds(),
        task=doc.task(),
    )


# --------------------------------------------------------------------------- formatting


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _num(x: float) -> str:
    """A parameter in prose: 10, 0.5, 600."""
    return f"{x:g}"


def _quote(text: str) -> str:
    """Untrusted text as a JSON string on one line."""
    return json.dumps(clean_text(text), ensure_ascii=False)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class _Doc:
    def __init__(self, inputs: JudgeInputs, params: JudgeParams, *, final: bool) -> None:
        self.i = inputs
        self.p = params
        self.final = final
        self.t = inputs.anchor
        self.delay = timedelta(seconds=inputs.candidate.chat_delay_s)

    def rel(self, ts: datetime) -> str:
        s = round((ensure_utc(ts) - self.t).total_seconds(), 1)
        return f"{s + 0.0 if s else 0.0:+.1f}"

    def span(self, a: datetime, b: datetime) -> str:
        return f"[{self.rel(a)}→{self.rel(b)}]"

    def spans(self, intervals: Iterable[Interval]) -> str:
        return ", ".join(self.span(iv.start, iv.end) for iv in intervals) or "none"

    # ------------------------------------------------------------------ meta

    def meta(self) -> str:
        c = self.i.candidate
        core = Interval(
            ensure_utc(c.moment_from) - timedelta(seconds=self.p.core_pad_s),
            ensure_utc(c.moment_to) + timedelta(seconds=self.p.core_pad_s),
        )
        lines = [
            f"channel: {clean_text(self.i.channel)}",
            f"stream title: {_quote(self.i.stream_title) if self.i.stream_title else 'none'}",
            f"category: {_quote(self.i.stream_category) if self.i.stream_category else 'none'}",
            f"detector: trigger {clean_text(c.trigger)}, score {c.score:.2f}, "
            f"source {clean_text(c.source)}, moment type {clean_text(c.moment_type)}",
            f"candidate moment: {self.span(c.moment_from, c.moment_to)}, "
            f"core {self.span(core.start, core.end)}",
            f"explicit signals: {self.explicit()}",
            f"chat delay: {c.chat_delay_s:.1f} s ({clean_text(c.delay_source)}); chat below is "
            "shifted to video time",
            f"flags: {', '.join(sorted({clean_text(f) for f in self.i.flags})) or 'none'}",
        ]
        return "\n".join(lines)

    def explicit(self) -> str:
        parts = []
        ex = self.i.explicit
        mentions = ex.get("clip_mentions")
        if isinstance(mentions, Mapping):
            parts.append(f"clip mentions by {_int(mentions.get('users'))} users")
        clips = ex.get("viewer_clips")
        if isinstance(clips, Mapping):
            parts.append(f"viewer clips by {_int(clips.get('creators'))} creators")
        return "; ".join(parts) or "none"

    def options(self) -> str:
        tags = sorted({clean_text(t) for t in self.i.sfx_tags if clean_text(t)})
        return "\n".join(
            [
                f"clip length: {_num(self.p.min_clip_s)}-{_num(self.p.max_clip_s)} s",
                f"sfx tags: {', '.join(tags) if tags else 'none (leave sfx empty)'}",
                f"music moods: {', '.join(m.value for m in MusicMood)}",
                f"caption styles: {', '.join(s.value for s in CaptionStyle)}",
                f"layouts: {', '.join(k.value for k in LayoutKind)}",
            ]
        )

    # ------------------------------------------------------------------ media

    def video(self) -> list[Interval]:
        """The video inside the context, jitter holes bridged (as the resolver cuts)."""
        parts = (iv.intersection(self.i.context) for iv in self.i.video)
        return bridge_jitter(merge(p for p in parts if p is not None))

    def media(self) -> str:
        ctx = self.i.context
        video = self.video()
        holes = uncovered(ctx, video)
        lines = [
            f"context: {self.span(ctx.start, ctx.end)}",
            f"video: {self.spans(video)}",
        ]
        if holes:
            labelled = ", ".join(f"{self.span(h.start, h.end)} {self.hole_label(h)}" for h in holes)
            lines.append(f"no video: {labelled}")
        return "\n".join(lines)

    def hole_label(self, hole: Interval) -> str:
        """Why media is missing: the overlapping gap row's reason, else off air / none."""
        best: tuple[float, datetime, str] | None = None
        for g in self.i.gaps:
            part = hole.intersection(Interval(g.start_utc, g.end_utc))
            if part is not None:
                key = (-part.seconds, ensure_utc(g.start_utc), clean_text(g.reason))
                best = key if best is None or key < best else best
        if best is not None:
            return best[2]
        if hole.end <= ensure_utc(self.i.stream_started_at):
            return "off air: before the stream"
        ended = self.i.stream_ended_at
        if ended is not None and hole.start >= ensure_utc(ended):
            return "off air: after the stream"
        return "not recorded"

    # ------------------------------------------------------------------ transcript

    def sentence_line(self, s: Sentence) -> str:
        words = []
        for w in s.words:
            text = clean_text(w.text)
            if not text:
                continue
            if w.confidence is not None and w.confidence < self.p.low_confidence:
                core = text.rstrip(_TRAILING)
                text = f"{core}(?){text[len(core) :]}" if core else f"{text}(?)"
            words.append(text)
        return f"{s.id} {self.span(s.start_utc, s.end_utc)} {' '.join(words)}"

    def transcript(self, sentences: Sequence[Sentence]) -> str:
        cov = self.i.coverage
        items: list[tuple[datetime, int, str]] = []
        for s in sentences:
            items.append((s.start_utc, 1, self.sentence_line(s)))
        for part in cov.gap:
            label = self.hole_label(part)
            if not label.startswith("off air"):
                label = f"no audio: {label}" if label != "not recorded" else "no audio"
            items.append((part.start, 0, f"[{self.rel(part.start)}→{self.rel(part.end)} {label}]"))
        for part in cov.pending:
            text = f"[{self.rel(part.start)}→{self.rel(part.end)} no transcript yet]"
            items.append((part.start, 0, text))
        spoken = [Interval(s.start_utc, max(s.end_utc, s.start_utc)) for s in sentences]
        for part in cov.transcribed:
            for quiet in uncovered(part, spoken):
                if quiet.seconds >= self.p.silence_note_s:
                    text = f"[{self.rel(quiet.start)}→{self.rel(quiet.end)} no speech]"
                    items.append((quiet.start, 0, text))
        if not items:
            return "(no transcript in the context)"
        return "\n".join(line for _, _, line in sorted(items))

    # ------------------------------------------------------------------ chat

    def chat(self) -> str:
        p, ctx = self.p, self.i.context
        buckets: dict[int, list[tuple[datetime, ChatMessageLike]]] = defaultdict(list)
        for m in self.i.chat:
            if m.deleted or self.i.bots.is_bot(m.user_login):
                continue
            video_t = ensure_utc(m.sent_at) - self.delay
            if not ctx.start <= video_t < ctx.end:
                continue
            k = math.floor((video_t - self.t).total_seconds() / p.chat_bucket_s)
            buckets[k].append((video_t, m))
        lines = []
        for k in sorted(buckets):
            msgs = sorted(buckets[k], key=lambda vm: (vm[0], vm[1].msg_id))
            lines.append(self.chat_line(k, msgs))
        return "\n".join(lines) or "(no chat in the context)"

    def chat_line(self, k: int, msgs: Sequence[tuple[datetime, ChatMessageLike]]) -> str:
        p = self.p
        start = self.t + timedelta(seconds=k * p.chat_bucket_s)
        end = start + timedelta(seconds=p.chat_bucket_s)
        users = {user_key(m) for _, m in msgs} - {None}
        emotes: Counter[str] = Counter()
        texts: Counter[str] = Counter()
        first: dict[str, datetime] = {}
        askers: set[str] = set()
        for video_t, m in msgs:
            for e in m.emotes or ():
                name = clean_text(str(e.get("name", ""))) if isinstance(e, Mapping) else ""
                if name:
                    emotes[name] += max(_int(e.get("count", 1)), 1)
            text = normalised_text(strip_links(m.text))
            if text and not is_command(text) and not _emotes_only(m):
                texts[text] += 1
                first.setdefault(text, video_t)
            if is_mention(m.text):
                askers.add(user_key(m) or m.msg_id)
        parts = [f"{self.span(start, end)} {len(msgs)} msgs, {len(users)} users"]
        top_emotes = sorted(emotes.items(), key=lambda kv: (-kv[1], kv[0]))[: p.top_n]
        if top_emotes:
            parts.append("emotes: " + ", ".join(f"{name} {n}" for name, n in top_emotes))
        top = sorted(texts.items(), key=lambda kv: (-kv[1], first[kv[0]], kv[0]))[: p.top_n]
        if top:
            shown = (f"{_quote(_cut(t, p.message_chars))} {_TIMES}{n}" for t, n in top)
            parts.append("top: " + ", ".join(shown))
        if askers:
            parts.append(f"clip asks: {len(askers)}")
        return " | ".join(parts)

    # ------------------------------------------------------------------ audio

    def audio(self) -> str:
        p, ctx = self.p, self.i.context
        rows = {}
        for r in self.i.audio:
            ts = ensure_utc(r.ts)
            if ctx.start <= ts < ctx.end:
                rows.setdefault(ts, r)
        if not rows:
            return "(no audio metrics in the context)"
        ordered = [rows[ts] for ts in sorted(rows)]
        median = statistics.median(r.loudness_db for r in ordered)
        runs: list[list[tuple[datetime, dict[str, float], float]]] = []
        for r in ordered:
            ts = ensure_utc(r.ts)
            scores = {
                str(k): float(v)
                for k, v in (r.classes or {}).items()
                if _finite(v) and str(k) != SoundGroup.SPEECH
            }
            loud = r.loudness_db - median
            strong = {k: v for k, v in scores.items() if v >= p.sound_min}
            if not strong and loud < p.loud_db:
                continue
            item = (ts, strong, loud)
            if runs and ts - runs[-1][-1][0] == timedelta(seconds=1):
                runs[-1].append(item)
            else:
                runs.append([item])
        if not runs:
            return "(no audio events)"
        return "\n".join(self.audio_line(run) for run in runs)

    def audio_line(self, run: Sequence[tuple[datetime, dict[str, float], float]]) -> str:
        p = self.p
        peak: dict[str, float] = {}
        for _, strong, _ in run:
            for k, v in strong.items():
                peak[k] = max(peak.get(k, 0.0), v)
        start, end = run[0][0], run[-1][0] + timedelta(seconds=1)
        parts = []
        groups = sorted(peak.items(), key=lambda kv: (-kv[1], kv[0]))
        if groups:
            parts.append(", ".join(f"{clean_text(k)} {v:.2f}" for k, v in groups))
        loud = max(item[2] for item in run)
        if loud >= p.loud_db:
            parts.append(f"loud +{loud:.0f} dB")
        if peak.get(SoundGroup.MUSIC, 0.0) >= p.music_risk:
            parts.append("Content ID risk")
        return f"{self.span(start, end)} {' | '.join(parts)}"

    # ------------------------------------------------------------------ rounds, task

    def rounds(self) -> str:
        lines = []
        for r in sorted(self.i.prior_rounds, key=lambda r: r.round):
            ctx = Interval(
                self.t - timedelta(seconds=r.before_s), self.t + timedelta(seconds=r.after_s)
            )
            why = f": {_quote(r.why)}" if r.why.strip() else ""
            lines.append(
                f"round {r.round}: asked for more context, got {self.span(ctx.start, ctx.end)}{why}"
            )
        return "\n".join(lines) or "none"

    def task(self) -> str:
        r, p = self.i.round, self.p
        if self.final:
            return (
                f"Round {r}, the last one: decide clip or reject with what you have. "
                "Answer with the JSON object only."
            )
        left = p.max_rounds - r
        return (
            f"Round {r}: decide clip, reject or need_context. You may ask for more context "
            f"{left} more time{'s' if left != 1 else ''}, at most {_num(p.max_before_s)} s "
            f"before and {_num(p.max_after_s)} s after T. Answer with the JSON object only."
        )


def _emotes_only(m: ChatMessageLike) -> bool:
    """Every token is one of the message's own emotes ("KEKW KEKW"): counted under emotes."""
    names = {str(e.get("name")) for e in m.emotes or () if isinstance(e, Mapping)}
    tokens = m.text.split()
    return bool(tokens) and all(t in names for t in tokens)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _finite(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
