"""The contract between services: job kinds, priorities, payloads, enum values.

Changing anything here means rebuilding every service that uses it. Keep changes additive.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import IntEnum, StrEnum
from itertools import pairwise
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from clipper_core.timeline import ensure_utc

# A timezone-aware datetime, converted to UTC; naive datetimes (and ISO strings without an
# offset) are rejected. For pydantic fields of contracts and stored models.
UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]

# pg_notify channel: the transcriber sends the stream id (as text) when a chunk's transcript is
# committed. The judge LISTENs to re-check `transcripts.coverage()` of waiting clips.
TRANSCRIPTS_NOTIFY_CHANNEL = "clipper_transcripts"

# Neighbouring chunks don't line up to the millisecond: a chunk ends at its last segment's
# PDT + duration, the next starts at the next segment's PDT. Holes up to this long are jitter,
# not missing media: the recorder writes gap rows only for longer holes (its gap tolerance), and
# `transcripts.coverage()` doesn't wait for them.
CHUNK_JITTER = timedelta(seconds=0.5)

# `chunks.seq` of the chunks a `clipper-backfill vod --fill-gaps` run adds to a LIVE stream
# (`source='backfill'`, DESIGN §17) start here, in time order; the recorder's seqs stay below it.
# So their storage keys (stream id + seq) never collide, and no fill chunk is ever a seq neighbour
# (`seq ± 1`, the transcriber's handover) of a live chunk.
FILL_SEQ_BASE = 1_000_000


class Priority(IntEnum):
    """Higher runs first. Effective priority of a job = max of everything that asked for it."""

    BACKFILL = 0  # old VODs, run whenever the GPU is idle
    LIVE = 10  # "transcribe everything" from live streams
    TRIGGER = 80  # window around a freshly detected moment
    JUDGE_CONTEXT = 100  # judge asked for more context; a clip is already waiting


class JobKind(StrEnum):
    TRANSCRIBE = "transcribe"  # ref_id = chunks.id; recorder / backfill tool, with the chunk
    # ref_id = candidates.id; enqueued by the detector with the candidate (`judge:{candidate}`),
    # again by the judge's waiter per context round (`judge:{candidate}:r{round}`, DESIGN §19)
    JUDGE = "judge"
    DETECT_OFFLINE = "detect_offline"  # ref_id = streams.id; enqueued by the detector's sweep
    # ref_id = clip_plans.id; the judge with each `clip` plan (first plans and revisions), review
    # for a re-render (§21); `dedupe_key(RENDER, plan_id)`. Run by the renderer (§20).
    RENDER = "render"
    # §21's code-only contract (here first, since the judge and renderer enqueue these before
    # review exists):
    REVIEW = "review"  # ref_id = renders.id; the renderer enqueues it with the finished render
    REVISE_PLAN = "revise_plan"  # ref_id = review_events.id; review enqueues, the judge runs it
    NOTIFY = "notify"  # ref_id = what the notice is about; anyone enqueues, review runs it


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class RecordingMode(StrEnum):
    FULL = "full"  # video + audio
    AUDIO_ONLY = "audio_only"  # audio + chat; cheap way to watch many channels
    OFF = "off"  # chat only


class StreamStatus(StrEnum):
    LIVE = "live"
    ENDED = "ended"


class DataSource(StrEnum):
    LIVE = "live"
    BACKFILL = "backfill"


class GapReason(StrEnum):
    AD = "ad"  # Twitch replaced the stream with an ad break
    RECONNECT = "reconnect"  # playlist/network trouble, or the stream was briefly offline
    NOT_RECORDED = "not_recorded"  # no recorder was running (joined late, restart, crash)
    UNKNOWN = "unknown"  # the media timeline jumped without a known cause
    # VOD audio silenced by the platform (copyrighted music): no usable audio there. Video may
    # still exist as a video-only chunk. Written by the backfill tool only.
    MUTED = "muted"


class T0Method(StrEnum):
    """How a backfilled VOD's `vods.t0_utc` (UTC of VOD offset 0) was found (DESIGN §17)."""

    # `audio_only` playlist EXT-X-PROGRAM-DATE-TIME minus the segment's VOD offset (the default:
    # the same clock as live media times; the chat anchor sits ~4 s later)
    PDT = "pdt"
    CHAT = "chat"  # median of chat replay `createdAt - contentOffsetSeconds` (no PDT)
    # the platform's `created_at` (Helix Get Videos; GQL `createdAt` without Helix credentials):
    # fallback without PDT and chat, or when the chat anchor is > 30 s off it
    HELIX = "helix"
    AUDIO = "audio"  # audio correlation with a live recording of the same stream


class VodMediaStatus(StrEnum):
    """`vods.media_status`."""

    PENDING = "pending"  # not (fully) fetched yet; `vods.media_done_s` says how far
    DONE = "done"
    RESTRICTED = "restricted"  # sub-only or otherwise not playable for us
    GONE = "gone"  # the platform deleted the VOD (data we already have is kept)
    OFF = "off"  # not requested (`--no-media`)


class VodChatStatus(StrEnum):
    """`vods.chat_status`."""

    PENDING = "pending"  # not (fully) fetched yet; `vods.chat_done_s` says how far
    DONE = "done"
    SKIPPED = "skipped"  # the channel has live chat in the VOD's range (live data wins)
    UNAVAILABLE = "unavailable"  # no chat replay for this VOD
    OFF = "off"  # not requested (`--no-chat`)


class ClipTimeSource(StrEnum):
    """`viewer_clips.time_source`: which T0 `start_utc = T0 + vod_offset_s` was computed from."""

    VOD_T0 = "vod_t0"  # our backfilled VOD's `vods.t0_utc` (PDT clock, like live media)
    # the VOD's Helix `created_at` (a stream we recorded live, or a VOD we didn't backfill): a few
    # seconds off T0 (GQL createdAt was T0_pdt + 3.2 s on the smoke VOD), so `slack_s` is larger
    HELIX_CREATED_AT = "helix_created_at"


class SoundGroup(StrEnum):
    """Sound-class groups: the keys of `audio_metrics_1s.classes` (written by the transcriber,
    read by the detector). Each value is a score 0..1 for one second: the max over the group's
    AudioSet labels (the label mapping lives in the transcriber)."""

    LAUGHTER = "laughter"
    SCREAMING = "screaming"
    SHOUTING = "shouting"
    CHEERING = "cheering"
    GASP = "gasp"
    CRYING = "crying"
    MUSIC = "music"
    SINGING = "singing"
    SPEECH = "speech"
    GUNFIRE_EXPLOSION = "gunfire_explosion"


class CandidateSource(StrEnum):
    """`candidates.source`: which detector pass found the moment (DESIGN §18)."""

    LIVE = "live"  # live scorer on chat_metrics_1s + raw chat
    OFFLINE = "offline"  # `detect_offline` pass over an ended stream (chat, audio, transcript)


class CandidateStatus(StrEnum):
    """`candidates.status` (writer: detector). The lifecycle after that lives in the judge's
    `clips` (DESIGN §7, §19)."""

    # a media request was made for its window; a judge job comes with it in the same
    # transaction, except while the detector runs with CLIPPER_DETECTOR_JUDGE_JOBS=0 (until
    # the judge exists): then emitted rows have no judge job
    EMITTED = "emitted"
    SUPPRESSED = "suppressed"  # fake-spike rule or budget; see `candidates.suppress_reason`


class SuppressReason(StrEnum):
    """`candidates.suppress_reason` (plain text, no CHECK; DESIGN §18.3). Written by the detector,
    read by the backtest, the judge and review (§19, §21). The first matching rule, in this order
    (raid ... greeting), is stored; `features.rules` lists every match."""

    RAID = "raid"  # a raid notice from onset - 30 s (unless a strong reaction follows)
    SUBS = "subs"  # a mass gift (submysterygift >= 5) or >= 10 sub/resub/subgift notices
    BITS = "bits"  # cheering messages >= 30 %
    MODE_TOGGLE = "mode_toggle"  # a roomstate change (emote-/subs-only, slow...) from onset - 10 s
    SPAM_WAVE = "spam_wave"  # >= 30 % deleted, timed-out/banned users' or cleared messages
    COMMANDS = "commands"  # `!command` messages >= 30 % (giveaways, bot games)
    VOTE = "vote"  # one number/letter or believe/doubt >= 40 % (polls, predictions)
    COPYPASTA = "copypasta"  # the most common free-text message >= 40 %
    BOTS = "bots"  # bot accounts' messages >= 30 %
    GREETING = "greeting"  # greetings that aren't reactions >= 40 % ("hi"/"bye" floods)
    WEAK = "weak"  # no rule, but z_clean below the emit bar (bots/commands/deleted mixed)
    BUDGET = "budget"  # over the per-stream-hour budget of emitted candidates


class DelaySource(StrEnum):
    """`candidates.delay_source` (plain text): where the chat delay of a candidate came from."""

    OVERRIDE = "override"  # channels.chat_delay_s, set by the operator
    LEARNED = "learned"  # median of recent valid chat_delay_estimates
    DEFAULT = "default"  # the detector's default (8 s)


class MomentType(StrEnum):
    """`candidates.moment_type`: dominant emote/word category of a moment (DESIGN §18). Also the
    edit plan's moment type (§19)."""

    FUNNY = "funny"
    HYPE = "hype"
    SHOCK = "shock"
    TENSE = "tense"
    SAD = "sad"
    CRINGE = "cringe"
    WHOLESOME = "wholesome"
    MIXED = "mixed"  # no category reached the top share
    UNKNOWN = "unknown"  # too few votes


# --------------------------------------------------------------------------- judge (§19)


class ClipStatus(StrEnum):
    """`clips.status` (writer: judge; CHECK). The later §7 states come from the renderer's,
    review's and publisher's tables: `clipper_core.lifecycle.lifecycle_state()`."""

    WAITING_FOR_MEDIA = "waiting_for_media"  # transcripts don't cover the context yet
    JUDGING = "judging"  # a judge job holds it (compare-and-set from waiting_for_media)
    REJECTED = "rejected"  # see `clips.reject_reason` (ClipRejectReason)
    PLANNED = "planned"  # a `clip` plan exists (`clips.plan_version`)
    FAILED = "failed"  # the model or plan validation kept failing; `clips.error`


class ClipRejectReason(StrEnum):
    """`clips.reject_reason` (plain text, no CHECK on the values; set iff `rejected`). Review's
    labels for rejected renders are a separate set (`ReviewLabel`, §21)."""

    MODEL = "model"  # the model said no
    NO_VIDEO = "no_video"  # nothing renderable (audio_only), no LLM call
    NO_MEDIA = "no_media"  # hard deadline passed without a transcribed core
    BUDGET = "budget"  # the usage guard (was: cost guard), §19


class Verdict(StrEnum):
    """`clip_plans.verdict` (CHECK): what one judge round decided."""

    CLIP = "clip"  # with an EditPlan (`clip_plans.plan`)
    REJECT = "reject"
    NEED_CONTEXT = "need_context"  # with `clip_plans.context_request`


class PlanAuthor(StrEnum):
    """`clip_plans.author` (CHECK)."""

    JUDGE = "judge"  # the model (first plans, instructed revisions)
    OPERATOR = "operator"  # a review PlanPatch, resolved


class LifecycleState(StrEnum):
    """DESIGN §7's state, derived by `clipper_core.lifecycle.lifecycle_state()`; never stored.
    Every `ClipStatus` value is one of these."""

    CANDIDATE = "candidate"  # an emitted candidate without a clip row yet
    WAITING_FOR_MEDIA = "waiting_for_media"
    JUDGING = "judging"
    REJECTED = "rejected"
    PLANNED = "planned"
    RENDERING = "rendering"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    DISCARDED = "discarded"  # review rejected, expired or withdrawn
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"  # judge or renderer gave up
    UNRENDERABLE = "unrenderable"  # §20: no video / video expired


class LayoutKind(StrEnum):
    AUTO = "auto"  # the renderer decides from the channel's layout config
    FACECAM_GAMEPLAY = "facecam_gameplay"
    FULL_CROP = "full_crop"
    BLURRED_FIT = "blurred_fit"


class CaptionStyle(StrEnum):
    """Caption presets; defined (look, fonts) in the renderer."""

    BOLD_POP = "bold_pop"
    CLEAN = "clean"
    KARAOKE = "karaoke"


class MusicMood(StrEnum):
    HYPE = "hype"
    FUNNY = "funny"
    CHILL = "chill"
    SUSPENSE = "suspense"
    SAD = "sad"
    EPIC = "epic"
    WHOLESOME = "wholesome"


class HookMode(StrEnum):
    NONE = "none"  # the in-point itself is the hook
    TEXT = "text"  # overlay text over the first seconds
    TEASER = "teaser"  # the hook range (1-3 s, inside the clip) plays first, then the clip


class NoticeKind(StrEnum):
    """`NotifyPayload.kind` (§21's code-only contract; §22 adds the publisher's kinds)."""

    REVISION_REFUSED = "revision_refused"  # judge; ref = review event
    RENDER_FAILED = "render_failed"  # renderer; ref = render
    # judge: its Claude credential expires within 14 days; ref = the expiry (epoch seconds), so
    # each token gets one notice
    CREDENTIAL_EXPIRING = "credential_expiring"
    # judge: its model adapter switched itself off (an API key would be billed, rejected or
    # expired credentials): judge jobs are held; ref = the UTC day (epoch days), one per day
    MODEL_OFF = "model_off"


class Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TranscribePayload(Payload):
    chunk_id: int


class JudgePayload(Payload):
    candidate_id: int


class DetectOfflinePayload(Payload):
    stream_id: int
    detector_version: str  # also in the job's dedupe key: a new version may run again


class RenderPayload(Payload):
    plan_id: int  # = the job's ref_id; a `clip` plan
    profile: str = "short_1080x1920"  # output profile (renderer, §20)


class ReviewPayload(Payload):  # §21
    render_id: int


class RevisePlanPayload(Payload):  # §21; review enqueues, the judge runs it
    review_event_id: int
    clip_id: int
    base_plan_id: int  # the plan of the render the reviewer saw (refused if not the latest)


class NotifyPayload(Payload):  # §21; `dedupe_key(NOTIFY, ref_id, kind)`
    kind: NoticeKind
    text: str
    clip_id: int | None = None
    publication_id: int | None = None
    severity: Literal["info", "warning", "error"] = "info"


# --------------------------------------------------------------------------- words files


class StoredModel(BaseModel):
    """Data kept in object storage for a long time (transcripts are kept forever).

    Unknown fields are ignored, so readers built before an additive change still read newer
    files. Incompatible changes bump the file's `version` instead, which old readers reject.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")


class Word(StoredModel):
    """One recognised word. Times are seconds relative to the words file's `t0`."""

    text: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1)  # engine's word probability

    @model_validator(mode="after")
    def _end_after_start(self) -> Word:
        if self.end < self.start:
            raise ValueError(f"word ends before it starts: {self.start} > {self.end}")
        return self


WORDS_FILE_VERSION = 1


class WordsFile(StoredModel):
    """`storage.keys.chunk_words(stream_id, seq)`: all words the transcriber recognised in a
    chunk's window. Written by the transcriber; read through `clipper_core.transcripts.words()`,
    which applies the ownership rule (only `[owned_from, owned_to)` of the chunk's
    `chunk_transcripts` row counts).

    `t0` = UTC start of the transcribed window: the chunk's `start_utc`, or earlier when this
    chunk took over the previous chunk's provisional tail (then the first words lie before the
    chunk's own start). It equals the `owned_from` of the chunk's `chunk_transcripts` row. The
    writer clamps words to the window (no word starts before `t0` or after the chunk's end).
    """

    version: Literal[1] = WORDS_FILE_VERSION
    chunk_id: int
    stream_id: int
    seq: int
    model: str
    t0: datetime
    words: list[Word] = Field(default_factory=list)

    @field_validator("t0")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


# --------------------------------------------------------------------------- edit plan (§19)
# `clip_plans.plan` (jsonb). StoredModel: unknown fields are ignored, `version` gates breaking
# changes. All times are UTC video time; the renderer maps them to output time.

EDIT_PLAN_VERSION = 1
CLIP_MIN = timedelta(seconds=3)  # contract bounds of the segments' total length; the judge's
CLIP_MAX = timedelta(seconds=180)  # own defaults are narrower (10-60 s, CLIPPER_JUDGE_*)
TEASER_MAX = timedelta(seconds=3)  # a teaser hook's length


class PlanSegment(StoredModel):
    start_utc: UtcDatetime
    end_utc: UtcDatetime  # > start_utc

    @model_validator(mode="after")
    def _positive(self) -> PlanSegment:
        if self.end_utc <= self.start_utc:
            raise ValueError(f"segment is empty or reversed: {self.start_utc} >= {self.end_utc}")
        return self


class Hook(StoredModel):
    mode: HookMode = HookMode.NONE
    start_utc: UtcDatetime | None = None  # TEASER: the range that plays first (<= 3 s)
    end_utc: UtcDatetime | None = None
    text: str | None = Field(default=None, max_length=40)  # TEXT: the overlay

    @model_validator(mode="after")
    def _mode_fields(self) -> Hook:
        if self.mode == HookMode.TEASER:
            if self.start_utc is None or self.end_utc is None:
                raise ValueError("a teaser hook needs start_utc and end_utc")
            if not timedelta(0) < self.end_utc - self.start_utc <= TEASER_MAX:
                raise ValueError(f"a teaser hook lasts more than 0 and at most {TEASER_MAX}")
        elif self.mode == HookMode.TEXT and not (self.text and self.text.strip()):
            raise ValueError("a text hook needs text (not only whitespace)")
        return self


class EmphasisWord(StoredModel):
    start_utc: UtcDatetime  # start of the word as transcripts.words() returns it
    text: str = Field(min_length=1)
    kind: Literal["highlight", "big"] = "highlight"


class WordFix(StoredModel):
    """A caption text fix (review editor); the renderer shows `text` for that word."""

    start_utc: UtcDatetime  # the word's start as transcripts.words() returns it
    text: str = Field(min_length=1, max_length=40)


class Zoom(StoredModel):
    start_utc: UtcDatetime
    end_utc: UtcDatetime  # > start_utc
    scale: float = Field(ge=1.0, le=1.6)
    target: Literal["face", "gameplay", "center"] = "center"

    @model_validator(mode="after")
    def _positive(self) -> Zoom:
        if self.end_utc <= self.start_utc:
            raise ValueError(f"zoom is empty or reversed: {self.start_utc} >= {self.end_utc}")
        return self


class SfxCue(StoredModel):
    at_utc: UtcDatetime
    tag: str = Field(min_length=1)  # a tag of the asset manifest (clipper_core.assets, §20)
    gain_db: float = Field(default=0.0, ge=-30, le=6)


class MusicPlan(StoredModel):
    mood: MusicMood
    gain_db: float = Field(default=-20.0, ge=-40, le=-6)
    duck: bool = True  # duck under the stream audio


class LayoutHint(StoredModel):
    kind: LayoutKind = LayoutKind.AUTO
    focus_x: float | None = Field(default=None, ge=0, le=1)  # full_crop centre, 0 = left


class EditPlan(StoredModel):
    """What to render. Written by the judge (resolved, snapped to words, clamped to video); read
    by the renderer, review and publisher. All times are UTC video time; the renderer maps them
    to output time.

    Validators: segments sorted, disjoint and not touching (the resolver merges touching ones),
    their total length within CLIP_MIN..CLIP_MAX; emphasis, word fixes, sfx and the thumbnail lie
    inside a segment (`start <= t < end`); zooms and a teaser hook lie inside one segment; zooms
    are disjoint; one word fix per word; hashtags are bare words (no '#', no whitespace); the
    title is not blank.
    """

    version: Literal[1] = EDIT_PLAN_VERSION
    clip_id: int
    stream_id: int
    anchor_utc: UtcDatetime  # T, the candidate's peak
    segments: list[PlanSegment] = Field(min_length=1)  # v1 judge writes one; jump cuts later
    hook: Hook = Field(default_factory=Hook)
    title: str = Field(min_length=1, max_length=100)  # title band + default post title (§22)
    hashtags: list[str] = Field(default_factory=list, max_length=5)  # without '#'; §22 merges
    caption_style: CaptionStyle = CaptionStyle.BOLD_POP
    emphasis: list[EmphasisWord] = Field(default_factory=list)
    word_fixes: list[WordFix] = Field(default_factory=list)
    zooms: list[Zoom] = Field(default_factory=list)
    sfx: list[SfxCue] = Field(default_factory=list)
    music: MusicPlan | None = None
    layout: LayoutHint = Field(default_factory=LayoutHint)
    thumbnail_utc: UtcDatetime | None = None
    moment_type: MomentType = MomentType.UNKNOWN
    confidence: float = Field(ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)  # reasons[0] = the one-liner review shows
    flags: list[str] = Field(default_factory=list)  # partial_context, copyright_music_risk, ...
    repairs: list[str] = Field(default_factory=list)  # resolver changes: snap_in, split_gap, ...
    model: str
    prompt_version: str

    @property
    def in_utc(self) -> datetime:
        return self.segments[0].start_utc

    @property
    def out_utc(self) -> datetime:
        return self.segments[-1].end_utc

    @property
    def duration(self) -> timedelta:
        """Length of the clip itself (the segments); a teaser hook plays on top of it."""
        return sum((s.end_utc - s.start_utc for s in self.segments), timedelta(0))

    @field_validator("title")
    @classmethod
    def _title(cls, title: str) -> str:
        return _not_blank(title)

    @field_validator("hashtags")
    @classmethod
    def _bare_hashtags(cls, tags: list[str]) -> list[str]:
        for tag in tags:
            if not tag or tag.startswith("#") or any(ch.isspace() for ch in tag):
                raise ValueError(f"a hashtag is a bare word without '#': {tag!r}")
        return tags

    @model_validator(mode="after")
    def _consistent(self) -> EditPlan:
        for prev, seg in pairwise(self.segments):
            if seg.start_utc <= prev.end_utc:
                raise ValueError("segments must be sorted and must not overlap or touch")
        if not CLIP_MIN <= self.duration <= CLIP_MAX:
            raise ValueError(f"clip length {self.duration} is outside {CLIP_MIN}..{CLIP_MAX}")

        def at(t: datetime) -> bool:
            return any(s.start_utc <= t < s.end_utc for s in self.segments)

        def within(start: datetime, end: datetime) -> bool:
            return any(s.start_utc <= start and end <= s.end_utc for s in self.segments)

        for what, times in (
            ("emphasis word", [e.start_utc for e in self.emphasis]),
            ("word fix", [f.start_utc for f in self.word_fixes]),
            ("sfx cue", [c.at_utc for c in self.sfx]),
            ("thumbnail", [] if self.thumbnail_utc is None else [self.thumbnail_utc]),
        ):
            for t in times:
                if not at(t):
                    raise ValueError(f"{what} at {t} is outside the segments")
        if len({f.start_utc for f in self.word_fixes}) < len(self.word_fixes):
            raise ValueError("more than one word fix for the same word")
        zooms = sorted(self.zooms, key=lambda z: z.start_utc)
        for z in zooms:
            if not within(z.start_utc, z.end_utc):
                raise ValueError(f"zoom {z.start_utc}..{z.end_utc} is not inside one segment")
        for prev, z in pairwise(zooms):
            if z.start_utc < prev.end_utc:
                raise ValueError("zooms must not overlap")
        hook = self.hook
        if (
            hook.mode == HookMode.TEASER
            and hook.start_utc is not None
            and hook.end_utc is not None
            and not within(hook.start_utc, hook.end_utc)
        ):
            raise ValueError("the teaser hook is not inside one segment")
        return self


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty or only whitespace")
    return value


class PlanPatch(BaseModel):
    """An operator's edit of a plan (§21; `review_events.details.patch`), resolved by the judge
    into a new plan version. None = keep; lists replace the whole list.

    Operator input, not a stored file: unknown fields are rejected, also inside the nested hook,
    word fixes, layout and music (a typo must not be dropped silently). An older judge refuses
    a patch with fields it doesn't know (the revision is refused, the operator is notified).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    in_utc: UtcDatetime | None = None  # moves segments[0].start_utc
    out_utc: UtcDatetime | None = None  # moves segments[-1].end_utc
    hook: Hook | None = None
    title: str | None = Field(default=None, min_length=1, max_length=100)  # the title band
    caption_style: CaptionStyle | None = None
    word_fixes: list[WordFix] | None = None
    layout: LayoutHint | None = None
    music: MusicPlan | None = None
    no_music: bool = False  # drop the music (music=None means "keep"; not both)

    @model_validator(mode="before")
    @classmethod
    def _no_unknown_nested_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            nested = [(key, data.get(key), model) for key, model in _PATCH_NESTED.items()]
            nested += [
                (f"word_fixes[{i}]", fix, WordFix)
                for i, fix in enumerate(data.get("word_fixes") or ())
            ]
            for where, value, model in nested:
                if isinstance(value, dict) and (unknown := set(value) - set(model.model_fields)):
                    raise ValueError(f"unknown fields in {where}: {sorted(unknown)}")
        return data

    @field_validator("title")
    @classmethod
    def _title(cls, title: str | None) -> str | None:
        return None if title is None else _not_blank(title)

    @model_validator(mode="after")
    def _music_or_no_music(self) -> PlanPatch:
        if self.music is not None and self.no_music:
            raise ValueError("set music or no_music, not both")
        return self


_PATCH_NESTED: dict[str, type[BaseModel]] = {"hook": Hook, "layout": LayoutHint, "music": MusicPlan}


def recording_lease(channel_id: int) -> str:
    """Lease a recorder holds while it records a channel (only one recorder per channel)."""
    return f"record:channel:{channel_id}"


def backfill_lease(video_id: str) -> str:
    """Lease a backfill run holds while it works on one platform VOD (keeps runs apart)."""
    return f"backfill:vod:{video_id}"


def detect_lease(channel_id: int) -> str:
    """Lease a detector process holds while it scores a channel live."""
    return f"detect:channel:{channel_id}"


def dedupe_key(kind: JobKind, ref_id: int, *parts: str | int) -> str:
    """One job per (kind, object[, parts]): enqueuing again only raises its priority.

    Extra parts are for jobs that may run again for the same object, e.g. per detector version
    (`detect_offline:12:<version>`) or per judge round (`judge:7:r1`). Without parts the key
    stays `kind:ref_id`, as before.
    """
    return ":".join(str(p) for p in (kind, ref_id, *parts))


def judge_round_key(candidate_id: int, round_: int) -> str:
    """Dedupe key of the judge job the judge's waiter enqueues when a clip is ready for round
    `round_` (`judge:{candidate}:r{round}`, DESIGN §19). The detector's first job for the
    candidate is `dedupe_key(JUDGE, candidate_id)` (`judge:{candidate}`)."""
    if round_ < 0:
        raise ValueError(f"judge rounds start at 0: {round_}")
    return dedupe_key(JobKind.JUDGE, candidate_id, f"r{round_}")
