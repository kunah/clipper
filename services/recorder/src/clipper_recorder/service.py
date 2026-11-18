"""recorder service: picks channels to record, runs one ChannelRecorder task per channel.

Every `sync_interval_s`:
- live streams of active channels with recording_mode != off are read from the DB (the
  controller writes them);
- a channel is recorded only while this process holds its lease (`record:channel:{id}`), so
  several recorder processes/machines can share the work later without code changes;
- recorders whose stream ended, whose mode changed, or whose lease was lost are stopped (they
  close their chunk first); a new broadcast gets a new recorder.
On start, chunks left in the spool by a previous run are finished first.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_core import heartbeat, leases
from clipper_core.contracts import RecordingMode, recording_lease
from clipper_core.logs import get_logger
from clipper_core.timeline import utc_now
from clipper_recorder.channel import Assignment, ChannelRecorder, Resolver
from clipper_recorder.finisher import Finisher
from clipper_recorder.settings import RecorderSettings
from clipper_recorder.spool import Spool
from clipper_recorder.tracker import SegmentTracker

log = get_logger(__name__)

LEASE_TTL = timedelta(seconds=60)
STOP_TIMEOUT_S = 20.0  # a recorder may be in the middle of a 10 s download


@dataclass
class Running:
    assignment: Assignment
    recorder: ChannelRecorder
    task: asyncio.Task
    stop: asyncio.Event = field(default_factory=asyncio.Event)


class Recorder:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        settings: RecorderSettings,
        worker_id: str,
        http: httpx.AsyncClient,
        resolver: Resolver,
        spool: Spool,
        finisher: Finisher,
        recorder_factory: Callable[..., ChannelRecorder] = ChannelRecorder,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self.worker_id = worker_id
        self.http = http
        self.resolver = resolver
        self.spool = spool
        self.finisher = finisher
        self.recorder_factory = recorder_factory
        self.running: dict[int, Running] = {}  # channel id -> recorder
        self._started_at = utc_now()

    # ------------------------------------------------------------------ lifecycle

    async def run(self, stop: asyncio.Event) -> None:
        recovered = self.recover_spool()
        if recovered:
            log.info("finishing chunks left in the spool", count=recovered)
        self.finisher.start()
        loops = [
            asyncio.create_task(self._every(stop, self.settings.sync_interval_s, self.sync)),
            asyncio.create_task(self._every(stop, 15.0, self.beat)),
        ]
        try:
            await stop.wait()
        finally:
            for task in loops:
                task.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            await self.stop_all()
            await self.finisher.drain(self.settings.drain_timeout_s)
            await self._release_all()
            log.info("recorder stopped", chunks_saved=self.finisher.done)

    def recover_spool(self) -> int:
        count = 0
        for state in self.spool.pending():
            recovered = self.spool.recover(state)
            if recovered:
                self.finisher.submit(recovered)
                count += 1
        return count

    async def stop_all(self) -> None:
        for r in self.running.values():
            r.stop.set()
        tasks = [r.task for r in self.running.values()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=STOP_TIMEOUT_S)
            for task in pending:  # the chunk is still in the spool; next start finishes it
                task.cancel()
        self._reap()

    # ------------------------------------------------------------------ sync

    async def wanted(self) -> dict[int, Assignment]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT DISTINCT ON (c.id) c.id, c.login, c.recording_mode,
                               s.id AS stream_id, s.started_at
                          FROM channels c
                          JOIN streams s ON s.channel_id = c.id AND s.status = 'live'
                         WHERE c.active AND c.platform = 'twitch' AND c.recording_mode <> 'off'
                         ORDER BY c.id, s.started_at DESC
                        """
                    )
                )
            ).all()
        return {
            r.id: Assignment(
                r.id, r.login, RecordingMode(r.recording_mode), r.stream_id, r.started_at
            )
            for r in rows
        }

    async def sync(self) -> None:
        self._reap()
        wanted = await self.wanted()
        async with self.sessions.begin() as session:
            for channel_id, r in self.running.items():
                if r.stop.is_set():
                    continue
                if wanted.get(channel_id) != r.assignment:
                    log.info("stopping recorder", channel=r.assignment.login, reason="changed")
                    r.stop.set()
                elif not await leases.acquire(
                    session, recording_lease(channel_id), self.worker_id, LEASE_TTL
                ):
                    log.warning("recording lease lost", channel=r.assignment.login)
                    r.stop.set()
            for channel_id, assignment in wanted.items():
                if channel_id in self.running:  # running, or still stopping
                    continue
                if len(self.running) >= self.settings.max_channels:
                    log.warning("max channels reached", channel=assignment.login)
                    continue
                if not await leases.acquire(
                    session, recording_lease(channel_id), self.worker_id, LEASE_TTL
                ):
                    continue  # another recorder has it
                self._start(assignment, await self._tracker_and_seq(session, assignment))

    async def _tracker_and_seq(
        self, session: AsyncSession, a: Assignment
    ) -> tuple[SegmentTracker, int]:
        """Continue after what was recorded before (DB or spool), or start fresh."""
        row = (
            await session.execute(
                text(
                    "SELECT max(seq) AS seq, max(end_utc) AS end FROM chunks WHERE stream_id = :s"
                ),
                {"s": a.stream_id},
            )
        ).one()
        last_seq, last_end = row.seq, row.end
        spooled = self.spool.latest(a.stream_id)
        if spooled:
            last_seq = max(last_seq if last_seq is not None else -1, spooled.seq)
            last_end = max(last_end, spooled.end) if last_end else spooled.end
        if last_seq is None:
            return SegmentTracker.fresh(a.started_at), 0
        return SegmentTracker.resume(last_end), last_seq + 1

    def _start(self, a: Assignment, tracker_and_seq: tuple[SegmentTracker, int]) -> None:
        tracker, next_seq = tracker_and_seq
        recorder = self.recorder_factory(
            a,
            http=self.http,
            resolver=self.resolver,
            spool=self.spool,
            submit=self.finisher.submit,
            tracker=tracker,
            next_seq=next_seq,
            chunk_seconds=self.settings.chunk_seconds,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(recorder.run(stop), name=f"record-{a.login}")
        self.running[a.channel_id] = Running(a, recorder, task, stop)

    def _reap(self) -> list[int]:
        """Forget finished recorders. Their lease simply expires unless the next sync renews it."""
        done = [cid for cid, r in self.running.items() if r.task.done()]
        for cid in done:
            r = self.running.pop(cid)
            if not r.task.cancelled() and r.task.exception():
                log.error(
                    "recorder crashed, restarting on next sync",
                    channel=r.assignment.login,
                    exc_info=r.task.exception(),
                )
        return done

    async def _release_all(self) -> None:
        try:
            async with self.sessions.begin() as session:
                await session.execute(
                    text("DELETE FROM leases WHERE owner = :o AND resource LIKE 'record:%'"),
                    {"o": self.worker_id},
                )
        except Exception:
            log.warning("could not release recording leases (they expire)", exc_info=True)

    # ------------------------------------------------------------------ status

    async def beat(self) -> None:
        recording = [
            {
                "channel": r.assignment.login,
                "stream_id": r.assignment.stream_id,
                "mode": r.assignment.mode,
                "next_seq": r.recorder.next_seq,
            }
            for r in self.running.values()
        ]
        async with self.sessions.begin() as session:
            await heartbeat.beat(
                session,
                worker_id=self.worker_id,
                service="recorder",
                started_at=self._started_at,
                info={
                    "recording": recording,
                    "finish_queue": self.finisher.queue.qsize(),
                    "chunks_saved": self.finisher.done,
                },
            )

    @staticmethod
    async def _every(stop: asyncio.Event, interval: float, fn) -> None:
        while not stop.is_set():
            try:
                await fn()
            except Exception:
                log.exception("recorder loop failed", loop=getattr(fn, "__name__", "?"))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
