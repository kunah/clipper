"""The waiter (DESIGN §19 step 3): wakes waiting clips when their media is there.

Each judge process runs one: LISTEN `clipper_transcripts` (the transcriber sends the stream id
when a chunk's transcript commits) plus a sweep every 30 s. It claims the `waiting_for_media`
clips (of the notified streams, or all of them on a sweep) one at a time, `FOR UPDATE SKIP
LOCKED`, skips those whose round job is already queued or running, and enqueues
`judge:{cand}:r{round}` at the clip's priority for every clip that is no longer waiting
(`readiness`): the job does the state change (`rounds.JudgeHandler`). The waiter's only write to
`clips`: a clip whose round job ended while it still waits in that round (the job's last attempt
failed before it loaded the clip; its key can't be enqueued again) is marked `failed`.

`readiness` (pure; the job applies the same rule):
- the context's coverage is complete -> ready;
- past the **soft deadline** (`max(waiting_since, context_to) + soft_wait_s`, wall clock) with
  nothing pending in the core `[moment_from - 5 s, moment_to + 5 s]` -> ready, flag
  `partial_context`;
- past the **hard deadline** (the same + `hard_wait_s`) -> ready (`partial_context`) when any of
  the core is transcribed, else rejected `no_media`;
- else it waits. A stream that ended is no reason to wait: `coverage()` makes time after its last
  chunk a gap.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_core import transcripts
from clipper_core.contracts import TRANSCRIPTS_NOTIFY_CHANNEL, ClipStatus, judge_round_key
from clipper_core.logs import get_logger
from clipper_core.timeline import Interval, ensure_utc
from clipper_core.transcripts import Coverage
from clipper_judge import writes
from clipper_judge.params import JudgeParams
from clipper_judge.reads import CLIP_COLUMNS, clip_row
from clipper_judge.settings import JudgeSettings

log = get_logger(__name__)

Clock = Callable[[], datetime]
CHECK_RETRY_S = 5.0  # after a failed check (database down): the next sweep this much later


# --------------------------------------------------------------------------- pure


@dataclass(frozen=True, slots=True)
class Deadlines:
    soft: datetime
    hard: datetime


def deadlines(
    waiting_since: datetime, context_to: datetime, *, soft_wait_s: float, hard_wait_s: float
) -> Deadlines:
    base = max(ensure_utc(waiting_since), ensure_utc(context_to))
    return Deadlines(base + timedelta(seconds=soft_wait_s), base + timedelta(seconds=hard_wait_s))


@dataclass(frozen=True, slots=True)
class Ready:
    partial: bool = False  # judged at a deadline with part of the context missing


@dataclass(frozen=True, slots=True)
class Wait:
    next_check: datetime  # the next deadline (a wake-up comes earlier when media lands)


@dataclass(frozen=True, slots=True)
class NoMedia:
    """The hard deadline passed and nothing of the core is transcribed: `rejected/no_media`."""


Readiness = Ready | Wait | NoMedia


def readiness(context: Coverage, core: Coverage, now: datetime, d: Deadlines) -> Readiness:
    """Whether a waiting clip can be judged now (see the module docstring)."""
    if context.complete:
        return Ready()
    if now >= d.soft and core.complete:
        return Ready(partial=True)
    if now >= d.hard:
        return Ready(partial=True) if core.transcribed else NoMedia()
    return Wait(d.soft if now < d.soft else d.hard)


def core_interval(
    moment_from: datetime, moment_to: datetime, context: Interval, params: JudgeParams
) -> Interval:
    """The core moment inside the context (never empty: the anchor lies in the context)."""
    pad = timedelta(seconds=params.core_pad_s)
    core = Interval(ensure_utc(moment_from) - pad, ensure_utc(moment_to) + pad)
    hit = core.intersection(context)
    if hit is not None:
        return hit
    mid = min(max(ensure_utc(moment_from), context.start), context.end)
    return Interval(mid, mid)


async def check_readiness(
    session: AsyncSession,
    *,
    stream_id: int,
    context: Interval,
    moment_from: datetime,
    moment_to: datetime,
    waiting_since: datetime,
    now: datetime,
    settings: JudgeSettings,
    params: JudgeParams,
) -> tuple[Readiness, Deadlines]:
    """`readiness` from the database: coverage of the context and of the core."""
    d = deadlines(
        waiting_since,
        context.end,
        soft_wait_s=settings.soft_wait_s,
        hard_wait_s=settings.hard_wait_s,
    )
    ctx_cov = await transcripts.coverage(session, stream_id, context, now=now)
    if ctx_cov.complete:
        return Ready(), d
    core = core_interval(moment_from, moment_to, context, params)
    core_cov = await transcripts.coverage(session, stream_id, core, now=now)
    return readiness(ctx_cov, core_cov, now, d), d


# --------------------------------------------------------------------------- the loop


class Waiter:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: JudgeSettings,
        *,
        dsn: str,
        clock: Clock,
        params: JudgeParams | None = None,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self.params = params or settings.params()
        self.dsn = dsn
        self.clock = clock
        self._dirty: set[int] = set()
        self._wakeup = asyncio.Event()
        self.enqueued = 0  # round jobs enqueued since start (heartbeat)
        self.listening = False

    async def run(self, stop: asyncio.Event) -> None:
        listener = asyncio.create_task(self._listen(stop), name="waiter-listen")
        next_sweep = 0.0
        try:
            while not stop.is_set():
                self._wakeup.clear()
                try:
                    if time.monotonic() >= next_sweep:
                        self._dirty.clear()
                        await self.check()
                        next_sweep = time.monotonic() + self.settings.sweep_interval_s
                    elif self._dirty:
                        dirty, self._dirty = self._dirty, set()
                        await self.check(dirty)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("waiter check failed", retry_in_s=CHECK_RETRY_S)
                    next_sweep = time.monotonic() + CHECK_RETRY_S
                    self._dirty.clear()  # the sweep covers them
                if self._dirty:
                    continue
                wait_s = max(next_sweep - time.monotonic(), 0.0)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wait(stop), timeout=wait_s)
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener

    async def _wait(self, stop: asyncio.Event) -> None:
        waiters = [asyncio.create_task(stop.wait()), asyncio.create_task(self._wakeup.wait())]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()

    def wake(self, stream_ids: Iterable[int]) -> None:
        self._dirty.update(stream_ids)
        self._wakeup.set()

    async def _listen(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                async with await psycopg.AsyncConnection.connect(self.dsn, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {TRANSCRIPTS_NOTIFY_CHANNEL}")
                    self.listening = True
                    async for note in conn.notifies():
                        try:
                            self.wake([int(note.payload)])
                        except ValueError:
                            log.warning("odd transcripts notification", payload=note.payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("waiter listener disconnected, retrying", retry_in_s=5, exc_info=True)
            finally:
                self.listening = False
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=5)

    async def check(self, stream_ids: Iterable[int] | None = None) -> int:
        """One pass over the waiting clips (of `stream_ids`, else all): enqueue the round job of
        every clip that is ready or past its hard deadline. Each clip is claimed and checked in a
        transaction of its own (`FOR UPDATE SKIP LOCKED`), so a judge job never waits behind a
        long sweep. Returns how many round jobs were enqueued."""
        ids = None if stream_ids is None else sorted(set(stream_ids))
        where = "" if ids is None else " AND stream_id = ANY(:ids)"
        async with self.sessions() as session:
            clip_ids = list(
                (
                    await session.execute(
                        text(
                            "SELECT id FROM clips WHERE status = 'waiting_for_media'"
                            f"{where} ORDER BY priority DESC, id"
                        ),
                        {} if ids is None else {"ids": ids},
                    )
                ).scalars()
            )
        n = 0
        for clip_id in clip_ids:
            n += await self._check_clip(clip_id)
        self.enqueued += n
        return n

    async def _check_clip(self, clip_id: int) -> int:
        now = self.clock()
        async with self.sessions.begin() as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {', '.join('c.' + col for col in CLIP_COLUMNS.split(', '))},"
                        " k.moment_from, k.moment_to"
                        " FROM clips c JOIN candidates k ON k.id = c.candidate_id"
                        " WHERE c.id = :id AND c.status = 'waiting_for_media'"
                        " FOR UPDATE OF c SKIP LOCKED"
                    ),
                    {"id": clip_id},
                )
            ).first()
            if row is None:  # another process holds it, or it moved on
                return 0
            clip = clip_row(row)
            key = judge_round_key(clip.candidate_id, clip.round)
            status = (
                await session.execute(
                    text("SELECT status FROM jobs WHERE dedupe_key = :k"), {"k": key}
                )
            ).scalar_one_or_none()
            if status in ("queued", "running"):
                return 0  # its round job will decide
            if status is not None:
                # A round job ends only once its clip has left that round (judged, rejected, or
                # deferred: still queued); one that ended while the clip waits (its last attempt
                # failed before it loaded the clip) can't be enqueued again: the clip fails.
                await writes.fail(
                    session,
                    clip,
                    f"the round {clip.round} judge job ended ({status}) while the clip waited",
                    expect=ClipStatus.WAITING_FOR_MEDIA,
                )
                log.error(
                    "clip failed: its round job ended while it waited",
                    clip_id=clip.id,
                    round=clip.round,
                    job_status=status,
                )
                return 0
            state, _ = await check_readiness(
                session,
                stream_id=clip.stream_id,
                context=clip.context,
                moment_from=row.moment_from,
                moment_to=row.moment_to,
                waiting_since=clip.waiting_since,
                now=now,
                settings=self.settings,
                params=self.params,
            )
            if isinstance(state, Wait):
                return 0
            await writes.enqueue_round(session, clip)
        log.info(
            "clip ready",
            clip_id=clip.id,
            candidate_id=clip.candidate_id,
            round=clip.round,
            partial=isinstance(state, Ready) and state.partial,
            no_media=isinstance(state, NoMedia),
        )
        return 1
