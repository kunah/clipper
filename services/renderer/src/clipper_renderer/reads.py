"""What the renderer reads from the database (it writes only `renders` and its jobs)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class PlanContext:
    plan_id: int
    clip_id: int
    verdict: str
    plan: dict[str, Any] | None
    shadow: bool
    stream_id: int
    channel_id: int
    login: str
    category: str | None
    recording_mode: str


async def plan_context(session: AsyncSession, plan_id: int) -> PlanContext | None:
    row = (
        await session.execute(
            text(
                "SELECT cp.id AS plan_id, cp.clip_id, cp.verdict, cp.plan, cp.shadow,"
                " c.stream_id, c.channel_id, ch.login, s.category, ch.recording_mode"
                " FROM clip_plans cp JOIN clips c ON c.id = cp.clip_id"
                " JOIN streams s ON s.id = c.stream_id JOIN channels ch ON ch.id = c.channel_id"
                " WHERE cp.id = :id"
            ),
            {"id": plan_id},
        )
    ).first()
    return PlanContext(**row._mapping) if row else None


@dataclass(frozen=True, slots=True)
class ChunkRow:
    id: int
    seq: int
    start_utc: datetime
    end_utc: datetime
    video_key: str | None
    audio_key: str | None


async def chunks_overlapping(
    session: AsyncSession, stream_id: int, start: datetime, end: datetime
) -> list[ChunkRow]:
    rows = await session.execute(
        text(
            "SELECT id, seq, start_utc, end_utc, video_key, audio_key FROM chunks"
            " WHERE stream_id = :s AND end_utc > :a AND start_utc < :b ORDER BY start_utc, seq"
        ),
        {"s": stream_id, "a": start, "b": end},
    )
    return [ChunkRow(**r._mapping) for r in rows]


async def queued_renders(session: AsyncSession) -> int:
    return (
        await session.execute(
            text("SELECT count(*) FROM jobs WHERE kind = 'render' AND status = 'queued'")
        )
    ).scalar_one()
