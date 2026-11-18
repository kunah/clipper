"""Exclusive ownership with expiry, e.g. which recorder process records which channel.

A holder must renew before `ttl` runs out; if it dies, another process can take over.
This is what lets us add a second machine later without code changes.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession


async def acquire(session: AsyncSession, resource: str, owner: str, ttl: timedelta) -> bool:
    """Take or renew a lease. True if `owner` holds it afterwards."""
    holder = (
        await session.execute(
            sql_text(
                """
                INSERT INTO leases (resource, owner, expires_at)
                VALUES (:resource, :owner, now() + :ttl)
                ON CONFLICT (resource) DO UPDATE
                   SET owner = EXCLUDED.owner,
                       expires_at = EXCLUDED.expires_at,
                       acquired_at = CASE WHEN leases.owner = EXCLUDED.owner
                                          THEN leases.acquired_at ELSE now() END
                 WHERE leases.owner = EXCLUDED.owner OR leases.expires_at < now()
                RETURNING owner
                """
            ),
            {"resource": resource, "owner": owner, "ttl": ttl},
        )
    ).scalar_one_or_none()
    return holder == owner


async def release(session: AsyncSession, resource: str, owner: str) -> None:
    await session.execute(
        sql_text("DELETE FROM leases WHERE resource = :resource AND owner = :owner"),
        {"resource": resource, "owner": owner},
    )


async def holder(session: AsyncSession, resource: str) -> str | None:
    return (
        await session.execute(
            sql_text("SELECT owner FROM leases WHERE resource = :r AND expires_at > now()"),
            {"r": resource},
        )
    ).scalar_one_or_none()
