"""Housekeeping the controller runs regardless of Twitch credentials."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_core import jobs
from clipper_core.contracts import JobKind
from clipper_core.logs import get_logger
from clipper_core.partitions import ensure_chat_partitions
from clipper_core.timeline import utc_now

log = get_logger(__name__)

# How long a job may stay "running" before we assume its worker died.
STALE_AFTER = {
    JobKind.TRANSCRIBE: timedelta(minutes=15),
    JobKind.JUDGE: timedelta(minutes=10),  # LLM calls; the judge commits each call right after it
    JobKind.DETECT_OFFLINE: timedelta(minutes=30),  # a whole stream's offline pass
    JobKind.REVISE_PLAN: timedelta(minutes=10),  # one judge call at most (an instruction)
    # pin + two-pass loudnorm + encode; a 3 min clip with x264 on the CPU stays well below it
    JobKind.RENDER: timedelta(minutes=20),
}


async def run_maintenance(sessions: async_sessionmaker[AsyncSession]) -> None:
    today = utc_now().date()
    async with sessions.begin() as session:
        await ensure_chat_partitions(session, today, today + timedelta(days=7))
    async with sessions.begin() as session:
        for kind, after in STALE_AFTER.items():
            requeued = await jobs.requeue_stale(session, kind, after)
            if requeued:
                log.warning("requeued stale jobs", job_kind=kind, count=requeued)
