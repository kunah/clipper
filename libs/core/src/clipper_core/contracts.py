"""The contract between services: job kinds, priorities, payloads, enum values.

Changing anything here means rebuilding every service that uses it. Keep changes additive.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict


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
    AD = "ad"
    RECONNECT = "reconnect"
    UNKNOWN = "unknown"


class Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TranscribePayload(Payload):
    chunk_id: int


def dedupe_key(kind: JobKind, ref_id: int) -> str:
    """One job per (kind, object): enqueuing again only raises its priority."""
    return f"{kind}:{ref_id}"
