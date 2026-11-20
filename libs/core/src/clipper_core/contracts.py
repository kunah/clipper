"""The contract between services: job kinds, priorities, payloads, enum values.

Changing anything here means rebuilding every service that uses it. Keep changes additive.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import IntEnum, StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from clipper_core.timeline import ensure_utc

# pg_notify channel: the transcriber sends the stream id (as text) when a chunk's transcript is
# committed. The judge LISTENs to re-check `transcripts.coverage()` of waiting clips.
TRANSCRIPTS_NOTIFY_CHANNEL = "clipper_transcripts"

# Neighbouring chunks don't line up to the millisecond: a chunk ends at its last segment's
# PDT + duration, the next starts at the next segment's PDT. Holes up to this long are jitter,
# not missing media: the recorder writes gap rows only for longer holes (its gap tolerance), and
# `transcripts.coverage()` doesn't wait for them.
CHUNK_JITTER = timedelta(seconds=0.5)


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

    EMITTED = "emitted"  # judge job enqueued
    SUPPRESSED = "suppressed"  # fake-spike rule or budget; see `candidates.suppress_reason`


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


class Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TranscribePayload(Payload):
    chunk_id: int


class JudgePayload(Payload):
    candidate_id: int


class DetectOfflinePayload(Payload):
    stream_id: int
    detector_version: str  # also in the job's dedupe key: a new version may run again


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
