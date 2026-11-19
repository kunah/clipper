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
    TRANSCRIBE = "transcribe"


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


class Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TranscribePayload(Payload):
    chunk_id: int


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


def dedupe_key(kind: JobKind, ref_id: int) -> str:
    """One job per (kind, object): enqueuing again only raises its priority."""
    return f"{kind}:{ref_id}"
