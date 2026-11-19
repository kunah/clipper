"""Commit one media chunk: chunk row + the gaps before it + its transcription job.

Shared by the recorder (live streams) and the backfill tool (backfilled streams), so both write
chunks, gaps and jobs the same way. The caller owns the transaction: everything here commits
with whatever else the caller writes (the backfill tool adds its `vods` progress), and a crash
before the commit leaves nothing behind.

Idempotent: committing the same `(stream_id, seq)` again (a retry after a commit whose
acknowledgement was lost, a re-run) adds no second gap and only raises the transcription job's
priority (dedupe key). It must cover exactly the same interval (else ValueError, nothing is
changed); media keys/bytes it passes replace the stored ones, None keeps them (so adding video to
an audio chunk later doesn't drop its audio).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from clipper_core import jobs, media
from clipper_core.contracts import JobKind, TranscribePayload, dedupe_key
from clipper_core.timeline import Interval, ensure_utc


class GapLike(Protocol):
    """A hole in the media before a chunk (e.g. the recorder's spool `GapState`)."""

    @property
    def start(self) -> datetime: ...

    @property
    def end(self) -> datetime: ...

    @property
    def reason(self) -> str: ...  # a contracts.GapReason


@dataclass(frozen=True, slots=True)
class Gap:
    """A plain `GapLike` for callers without their own gap type."""

    start: datetime
    end: datetime
    reason: str  # a contracts.GapReason


class CommittedChunk(NamedTuple):
    chunk_id: int
    created: bool  # False: the chunk existed already (nothing but its media keys changed)
    priority: int | None  # of the transcription job; None when this commit had no audio


async def commit_chunk(
    session: AsyncSession,
    *,
    stream_id: int,
    seq: int,
    interval: Interval,
    video_key: str | None,
    audio_key: str | None,
    video_bytes: int | None,
    audio_bytes: int | None,
    source: str,
    gaps_before: Sequence[GapLike] = (),
    default_priority: int,
) -> CommittedChunk:
    """Upsert the chunk, insert `gaps_before` only when the chunk is new, and enqueue its
    `transcribe` job when this commit has audio, at `media.priority_for_range(default=
    default_priority)` (open media requests overlapping the chunk raise it). `source`: a
    `contracts.DataSource`. Raises ValueError when `(stream_id, seq)` exists with another
    interval (the caller's numbering is off; its transaction should roll back)."""
    row = (
        await session.execute(
            text(
                """
                INSERT INTO chunks (stream_id, seq, start_utc, end_utc, video_key,
                                    audio_key, video_bytes, audio_bytes, source)
                VALUES (:stream_id, :seq, :start, :end, :video_key, :audio_key,
                        :video_bytes, :audio_bytes, :source)
                ON CONFLICT (stream_id, seq) DO UPDATE
                   SET video_key = COALESCE(EXCLUDED.video_key, chunks.video_key),
                       audio_key = COALESCE(EXCLUDED.audio_key, chunks.audio_key),
                       video_bytes = COALESCE(EXCLUDED.video_bytes, chunks.video_bytes),
                       audio_bytes = COALESCE(EXCLUDED.audio_bytes, chunks.audio_bytes)
                 WHERE chunks.start_utc = EXCLUDED.start_utc
                   AND chunks.end_utc = EXCLUDED.end_utc
                RETURNING id, (xmax = 0) AS created
                """
            ),
            {
                "stream_id": stream_id,
                "seq": seq,
                "start": interval.start,
                "end": interval.end,
                "video_key": video_key,
                "audio_key": audio_key,
                "video_bytes": video_bytes,
                "audio_bytes": audio_bytes,
                "source": str(source),
            },
        )
    ).first()
    if row is None:  # the chunk exists with another interval: the WHERE skipped the update
        stored = (
            await session.execute(
                text("SELECT start_utc, end_utc FROM chunks WHERE stream_id = :s AND seq = :q"),
                {"s": stream_id, "q": seq},
            )
        ).one()
        raise ValueError(
            f"chunk {stream_id}/{seq} exists as [{stored.start_utc.isoformat()}, "
            f"{stored.end_utc.isoformat()}), not [{interval.start.isoformat()}, "
            f"{interval.end.isoformat()})"
        )
    chunk_id, created = row.id, row.created
    if created and gaps_before:  # a re-commit must not add them twice
        await session.execute(
            text(
                "INSERT INTO stream_gaps (stream_id, start_utc, end_utc, reason) "
                "VALUES (:s, :start, :end, :reason)"
            ),
            [
                {
                    "s": stream_id,
                    "start": ensure_utc(gap.start),
                    "end": ensure_utc(gap.end),
                    "reason": str(gap.reason),
                }
                for gap in gaps_before
            ],
        )
    priority = None
    if audio_key:
        priority = await media.priority_for_range(
            session, stream_id=stream_id, window=interval, default=default_priority
        )
        await jobs.enqueue(
            session,
            JobKind.TRANSCRIBE,
            payload=TranscribePayload(chunk_id=chunk_id).model_dump(),
            priority=priority,
            ref_id=chunk_id,
            dedupe_key=dedupe_key(JobKind.TRANSCRIBE, chunk_id),
        )
    return CommittedChunk(chunk_id, created, priority)
