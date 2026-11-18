"""Daily partitions for chat_messages (UTC days).

There is deliberately no DEFAULT partition: rows landing there would block creating the real
partition later. Instead, writers call ensure_chat_partitions for the days they write (the
migrate container pre-creates yesterday..+7 days; backfill creates the days of old VODs).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import text as sql_text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

_LOCK_KEY = "clipper:chat_partitions"


def partition_name(day: date) -> str:
    return f"chat_messages_p{day:%Y%m%d}"


def partition_ddl(day: date) -> str:
    start = datetime.combine(day, time(), tzinfo=UTC)
    end = start + timedelta(days=1)
    return (
        f"CREATE TABLE IF NOT EXISTS {partition_name(day)} PARTITION OF chat_messages "
        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    )


def days(start: date, end: date) -> list[date]:
    """Inclusive range of days."""
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def ensure_chat_partitions_sync(conn: Connection, start: date, end: date) -> None:
    conn.execute(sql_text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": _LOCK_KEY})
    for day in days(start, end):
        conn.execute(sql_text(partition_ddl(day)))


async def ensure_chat_partitions(
    conn: AsyncConnection | AsyncSession, start: date, end: date
) -> None:
    await conn.execute(sql_text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": _LOCK_KEY})
    for day in days(start, end):
        await conn.execute(sql_text(partition_ddl(day)))
