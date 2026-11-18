"""Demand-driven priority: "I need transcripts for this time range, at this priority".

Used by the detector (window around a trigger) and the judge (more context before/after).
- Already-queued transcription jobs overlapping the range are bumped right away.
- Chunks recorded later inside the range get the requested priority when they are enqueued
  (the recorder calls priority_for_range), which covers context *after* the trigger.
"""

from __future__ import annotations

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from clipper_core import jobs
from clipper_core.contracts import JobKind
from clipper_core.timeline import Interval


async def request_media(
    session: AsyncSession,
    *,
    stream_id: int,
    window: Interval,
    priority: int,
    requested_by: str,
    clip_id: int | None = None,
) -> int:
    request_id = (
        await session.execute(
            sql_text(
                """
                INSERT INTO media_requests
                       (stream_id, from_utc, to_utc, priority, requested_by, clip_id)
                VALUES (:stream_id, :from_utc, :to_utc, :priority, :requested_by, :clip_id)
                RETURNING id
                """
            ),
            {
                "stream_id": stream_id,
                "from_utc": window.start,
                "to_utc": window.end,
                "priority": int(priority),
                "requested_by": requested_by,
                "clip_id": clip_id,
            },
        )
    ).scalar_one()
    chunk_ids = (
        (
            await session.execute(
                sql_text(
                    """
                SELECT id FROM chunks
                 WHERE stream_id = :stream_id AND start_utc < :to_utc AND end_utc > :from_utc
                """
                ),
                {"stream_id": stream_id, "from_utc": window.start, "to_utc": window.end},
            )
        )
        .scalars()
        .all()
    )
    await jobs.bump_priority(session, JobKind.TRANSCRIBE, chunk_ids, priority)
    return request_id


async def priority_for_range(
    session: AsyncSession, *, stream_id: int, window: Interval, default: int
) -> int:
    """Priority a newly recorded chunk should get: max(default, open requests overlapping it)."""
    return (
        await session.execute(
            sql_text(
                """
                SELECT GREATEST(COALESCE(MAX(priority), :default), :default)
                  FROM media_requests
                 WHERE stream_id = :stream_id AND fulfilled_at IS NULL
                   AND from_utc < :to_utc AND to_utc > :from_utc
                """
            ),
            {
                "stream_id": stream_id,
                "from_utc": window.start,
                "to_utc": window.end,
                "default": int(default),
            },
        )
    ).scalar_one()


async def mark_fulfilled(session: AsyncSession, request_ids: list[int]) -> None:
    if request_ids:
        await session.execute(
            sql_text("UPDATE media_requests SET fulfilled_at = now() WHERE id = ANY(:ids)"),
            {"ids": request_ids},
        )
