"""Postgres job queue with priorities that can change after a job is queued.

- enqueue() inside the same transaction as the data it refers to, so a job can't get lost.
- Workers claim the highest-priority job with FOR UPDATE SKIP LOCKED.
- pg_notify wakes idle workers instantly (sent on commit); they also poll as a fallback.
- Priority can only go up: re-enqueueing or bumping uses GREATEST(old, new).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg
from sqlalchemy import bindparam
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_core.contracts import JobStatus

log = logging.getLogger(__name__)

NOTIFY_CHANNEL = "clipper_jobs"


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: int
    kind: str
    ref_id: int | None
    priority: int
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


async def notify(session: AsyncSession, kind: str) -> None:
    await session.execute(
        sql_text("SELECT pg_notify(:ch, :kind)"), {"ch": NOTIFY_CHANNEL, "kind": kind}
    )


async def enqueue(
    session: AsyncSession,
    kind: str,
    *,
    payload: Mapping[str, Any] | None = None,
    priority: int = 0,
    ref_id: int | None = None,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 5,
) -> int:
    """Insert a job. With a dedupe_key that already exists, only raise that job's priority."""
    stmt = sql_text(
        """
        INSERT INTO jobs (kind, ref_id, dedupe_key, priority, payload, run_after, max_attempts)
        VALUES (:kind, :ref_id, :dedupe_key, :priority, :payload,
                COALESCE(:run_after, now()), :max_attempts)
        ON CONFLICT (dedupe_key) DO UPDATE
           SET priority = GREATEST(jobs.priority, EXCLUDED.priority), updated_at = now()
        RETURNING id
        """
    ).bindparams(bindparam("payload", type_=JSONB))
    job_id = (
        await session.execute(
            stmt,
            {
                "kind": kind,
                "ref_id": ref_id,
                "dedupe_key": dedupe_key,
                "priority": int(priority),
                "payload": dict(payload or {}),
                "run_after": run_after,
                "max_attempts": max_attempts,
            },
        )
    ).scalar_one()
    await notify(session, kind)
    return job_id


async def bump_priority(
    session: AsyncSession, kind: str, ref_ids: Iterable[int], priority: int
) -> int:
    """Raise the priority of queued jobs for these objects. Returns how many changed."""
    ids = list(ref_ids)
    if not ids:
        return 0
    result = await session.execute(
        sql_text(
            """
            UPDATE jobs SET priority = :p, updated_at = now()
             WHERE kind = :kind AND status = 'queued' AND ref_id = ANY(:ids) AND priority < :p
            """
        ),
        {"kind": kind, "ids": ids, "p": int(priority)},
    )
    if result.rowcount:
        await notify(session, kind)
    return result.rowcount


async def claim(session: AsyncSession, kinds: Sequence[str], worker_id: str) -> ClaimedJob | None:
    """Take the most important runnable job. Commit right after, to release the row lock."""
    row = (
        await session.execute(
            sql_text(
                """
                UPDATE jobs
                   SET status = 'running', locked_by = :worker, locked_at = now(),
                       attempts = attempts + 1, updated_at = now()
                 WHERE id = (
                       SELECT id FROM jobs
                        WHERE status = 'queued' AND kind = ANY(:kinds) AND run_after <= now()
                        ORDER BY priority DESC, id
                        LIMIT 1
                          FOR UPDATE SKIP LOCKED)
                RETURNING id, kind, ref_id, priority, payload, attempts, max_attempts
                """
            ),
            {"kinds": list(kinds), "worker": worker_id},
        )
    ).first()
    return ClaimedJob(**row._mapping) if row else None


async def complete(session: AsyncSession, job_id: int) -> None:
    await session.execute(
        sql_text(
            """
            UPDATE jobs SET status = 'done', finished_at = now(), updated_at = now(),
                            locked_by = NULL, last_error = NULL
             WHERE id = :id
            """
        ),
        {"id": job_id},
    )


def retry_delay(attempts: int) -> timedelta:
    return timedelta(seconds=min(5 * 2 ** max(attempts - 1, 0), 600))


async def fail(session: AsyncSession, job: ClaimedJob, error: str) -> JobStatus:
    """Retry later with backoff, or give up after max_attempts."""
    final = job.attempts >= job.max_attempts
    status = JobStatus.FAILED if final else JobStatus.QUEUED
    await session.execute(
        sql_text(
            """
            UPDATE jobs SET status = :status, last_error = :error, locked_by = NULL,
                            run_after = now() + :delay, updated_at = now(),
                            finished_at = CASE WHEN :final THEN now() END
             WHERE id = :id
            """
        ),
        {
            "id": job.id,
            "status": str(status),
            "error": error[-4000:],
            "delay": retry_delay(job.attempts),
            "final": final,
        },
    )
    return status


async def requeue_stale(session: AsyncSession, kind: str, older_than: timedelta) -> int:
    """Put back jobs whose worker died mid-job (locked too long). Run periodically."""
    result = await session.execute(
        sql_text(
            """
            UPDATE jobs SET status = 'queued', locked_by = NULL, updated_at = now(),
                            last_error = 'requeued: worker lock expired'
             WHERE kind = :kind AND status = 'running' AND locked_at < now() - :older_than
            """
        ),
        {"kind": kind, "older_than": older_than},
    )
    if result.rowcount:
        await notify(session, kind)
    return result.rowcount


Handler = Callable[[ClaimedJob], Awaitable[None]]


class JobRunner:
    """Loop: claim -> handle -> complete/fail. Sleeps until NOTIFY or `poll_interval`."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        dsn: str,
        worker_id: str,
        handlers: Mapping[str, Handler],
        poll_interval: float = 10.0,
    ) -> None:
        self._sessions = sessions
        self._dsn = dsn
        self._worker_id = worker_id
        self._handlers = dict(handlers)
        self._kinds = list(self._handlers)
        self._poll_interval = poll_interval
        self._wakeup = asyncio.Event()

    async def run(self, stop: asyncio.Event) -> None:
        listener = asyncio.create_task(self._listen(stop))
        try:
            while not stop.is_set():
                # Clear *before* looking for work, so a NOTIFY that arrives while we check
                # still wakes us up instead of being lost until the next poll.
                self._wakeup.clear()
                if not await self.run_once():
                    await _wait_any(stop, self._wakeup, wait_s=self._poll_interval)
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener

    async def run_once(self) -> bool:
        """Process at most one job. Returns False when there was nothing to do."""
        async with self._sessions.begin() as session:
            job = await claim(session, self._kinds, self._worker_id)
        if job is None:
            return False
        log.info(
            "job %s %s (priority %s, attempt %s)", job.id, job.kind, job.priority, job.attempts
        )
        try:
            await self._handlers[job.kind](job)
        except Exception as exc:
            log.exception("job %s %s failed", job.id, job.kind)
            async with self._sessions.begin() as session:
                await fail(session, job, f"{type(exc).__name__}: {exc}")
        else:
            async with self._sessions.begin() as session:
                await complete(session, job.id)
        return True

    async def _listen(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                async with await psycopg.AsyncConnection.connect(
                    self._dsn, autocommit=True
                ) as conn:
                    await conn.execute(f"LISTEN {NOTIFY_CHANNEL}")
                    async for note in conn.notifies():
                        if note.payload in self._kinds:
                            self._wakeup.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("job listener disconnected; retrying in 5s", exc_info=True)
                await _wait_any(stop, wait_s=5)


async def _wait_any(*events: asyncio.Event, wait_s: float) -> None:
    waiters = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(waiters, timeout=wait_s, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            w.cancel()
