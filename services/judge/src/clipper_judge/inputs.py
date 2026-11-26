"""Everything one judge round reads, in memory (DESIGN §19). The service (build item 6c) loads it
from Postgres and object storage; the input builder and the resolver are pure functions of it.

Rows are taken as protocols, so ORM rows (`clipper_core.models`), `clipper_core.chat.MessageRow`
and test stubs all fit. Times are timezone-aware UTC everywhere.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from clipper_core.chat import BotFilter, NoBots
from clipper_core.contracts import CHUNK_JITTER
from clipper_core.timeline import Interval, ensure_utc, merge
from clipper_core.transcripts import Coverage
from clipper_judge.transcript import WordLike


class CandidateLike(Protocol):
    """A `candidates` row (`clipper_core.models.Candidate`)."""

    id: int
    stream_id: int
    source: str
    trigger: str
    peak_utc: datetime  # T, video time
    chat_delay_s: float
    delay_source: str
    moment_from: datetime
    moment_to: datetime
    score: float
    moment_type: str
    features: dict[str, Any]


class GapLike(Protocol):
    """A `stream_gaps` row."""

    start_utc: datetime
    end_utc: datetime
    reason: str


class ChunkLike(Protocol):
    """A `chunks` row (for `media_ranges`)."""

    start_utc: datetime
    end_utc: datetime
    video_key: str | None
    audio_key: str | None


class ChatMessageLike(Protocol):
    """A `chat_messages` row (chat time: `sent_at` is Twitch's server time)."""

    sent_at: datetime
    msg_id: str
    user_id: str | None
    user_login: str | None
    text: str
    emotes: list[dict[str, Any]] | None
    deleted: bool


class AudioSecondLike(Protocol):
    """An `audio_metrics_1s` row."""

    ts: datetime
    loudness_db: float
    speech_ratio: float
    classes: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PriorRound:
    """An earlier round of this clip that asked for more context (`clip_plans` row)."""

    round: int
    before_s: float  # as granted (clamped), relative to T
    after_s: float
    why: str = ""


@dataclass(frozen=True, slots=True)
class JudgeInputs:
    """One round of one clip.

    - `context`: the clip's context (`clips.context_from/to`); `words`, `chat`, `audio` may hold
      more, the builder and the resolver look only inside it (chat after the delay shift).
    - `words`: `transcripts.words()` over `words_interval` (ownership applied), which must cover
      the context and `WORDS_MARGIN` (5 s) more on each side (`words_interval_for(context)`):
      the resolver needs the words cut by the context's edges to keep cuts out of speech. Only
      words starting inside the context are shown to the model.
    - `coverage`: `transcripts.coverage()` of the context; `gaps`: the stream's `stream_gaps`
      rows there (reasons of the holes).
    - `video`: where renderable video exists (`media_ranges(chunks)`); cuts are clamped to it.
    - `chat`: raw `chat_messages` over the context shifted by `+ chat_delay_s` (chat time).
    - `sfx_tags`: the tags of the asset manifest (§20); other SFX tags are dropped.
    - `flags`: set by the service, copied into the plan (e.g. `partial_context`).
    """

    clip_id: int
    candidate: CandidateLike
    channel: str
    stream_started_at: datetime
    stream_ended_at: datetime | None
    context: Interval
    words: Sequence[WordLike]
    words_interval: Interval  # what `words` were read over: covers context +- WORDS_MARGIN
    coverage: Coverage
    video: Sequence[Interval]
    stream_title: str | None = None
    stream_category: str | None = None
    round: int = 0
    gaps: Sequence[GapLike] = ()
    chat: Sequence[ChatMessageLike] = ()
    audio: Sequence[AudioSecondLike] = ()
    prior_rounds: Sequence[PriorRound] = ()
    sfx_tags: Sequence[str] = ()
    bots: BotFilter = field(default_factory=NoBots)
    flags: Sequence[str] = ()

    def __post_init__(self) -> None:
        anchor = ensure_utc(self.candidate.peak_utc)
        if not self.context.start <= anchor <= self.context.end:
            raise ValueError("the anchor (candidate peak) must lie inside the context")
        if self.round < 0:
            raise ValueError("rounds start at 0")
        need = words_interval_for(self.context)
        if not (self.words_interval.start <= need.start and need.end <= self.words_interval.end):
            raise ValueError(
                "words must be read over the context and 5 s more on each side "
                "(words_interval_for(context))"
            )

    @property
    def anchor(self) -> datetime:
        return ensure_utc(self.candidate.peak_utc)

    @property
    def explicit(self) -> Mapping[str, Any]:
        value = (self.candidate.features or {}).get("explicit")
        return value if isinstance(value, Mapping) else {}


WORDS_MARGIN = timedelta(seconds=5)


def words_interval_for(context: Interval) -> Interval:
    """The interval to read `transcripts.words()` over for a context."""
    return context.expand(WORDS_MARGIN, WORDS_MARGIN)


def media_ranges(chunks: Iterable[ChunkLike], *, video: bool = True) -> list[Interval]:
    """Merged ranges of the chunks that have video (`video=False`: audio, for `judge_audio_only`).
    Touching chunks merge; jitter holes between chunks stay (the resolver bridges them)."""
    return merge(
        Interval(c.start_utc, c.end_utc)
        for c in chunks
        if (c.video_key if video else c.audio_key) is not None and c.end_utc > c.start_utc
    )


def bridge_jitter(ranges: Sequence[Interval], jitter: timedelta = CHUNK_JITTER) -> list[Interval]:
    """Sorted, merged ranges with holes up to `jitter` bridged (chunk-boundary jitter, §16)."""
    out: list[Interval] = []
    for v in ranges:
        if out and v.start - out[-1].end <= jitter:
            out[-1] = Interval(out[-1].start, max(out[-1].end, v.end))
        else:
            out.append(v)
    return out
