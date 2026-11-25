"""The judge service (DESIGN §19): `concurrency` job runners on `judge` and `revise_plan`, the
waiter and a heartbeat, in one process.

    service = JudgeService(sessions, store, model, settings, worker_id=..., dsn=...)
    await service.run(stop)

Runner i has worker id `<worker_id>-<i>` (`jobs.locked_by`); the usage guard and the counters are
shared by the process. The heartbeat (`worker_heartbeats`, service `judge`) carries the model,
prompt version, the usage guard's last reading and warnings, the round outcomes since start,
the number of waiting clips and, for an adapter that reports one (`model.ReportsStatus`), the
model's status (the CLI adapter: version, auth method, credential expiry, warnings).

Notices (`notify` jobs for review, §21; they queue until review exists), from the adapter's
warnings, in the heartbeat's transaction: `credential_expiring` (the credential ends within 14
days; one per expiry value) and `model_off` (the adapter switched itself off, judge jobs are held;
one per UTC day). Deduped by `(ref, kind)` in the queue, and per process so a notice isn't
re-sent every heartbeat.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_core import heartbeat
from clipper_core.contracts import JobKind, NoticeKind
from clipper_core.jobs import JobRunner
from clipper_core.logs import get_logger
from clipper_core.timeline import ensure_utc, utc_now
from clipper_judge import writes
from clipper_judge.adapters import Shadow
from clipper_judge.guard import UsageGuard
from clipper_judge.model import JudgeModel, ReportsStatus
from clipper_judge.prompt import load_prompt
from clipper_judge.reads import ObjectStore, waiting_count
from clipper_judge.revisions import ReviseHandler, RevisionSource
from clipper_judge.rounds import JudgeHandler, RoundStats
from clipper_judge.settings import JudgeSettings
from clipper_judge.waiter import Waiter

log = get_logger(__name__)

SERVICE = "judge"
STOP_TIMEOUT_S = 20.0  # on shutdown, jobs in progress get this long to finish (then cancelled)
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class Notice:
    kind: NoticeKind
    ref_id: int
    text: str
    severity: Literal["info", "warning", "error"]


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


class JudgeService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        model: JudgeModel,
        settings: JudgeSettings,
        *,
        worker_id: str,
        dsn: str,
        clock: Clock = utc_now,
        revisions: RevisionSource | None = None,
        shadows: Sequence[Shadow] = (),
    ) -> None:
        self.sessions = sessions
        self.model = model
        self.settings = settings
        self.worker_id = worker_id
        self.clock = clock
        self.params = settings.params()
        self.prompt_version = load_prompt(settings.prompt).version
        self.started_at = clock()
        self.stats = RoundStats()
        self.guard = UsageGuard(settings)
        self.judge = JudgeHandler(
            sessions,
            store,
            model,
            settings,
            guard=self.guard,
            clock=clock,
            params=self.params,
            stats=self.stats,
            shadows=shadows,
        )
        self.shadow_labels = [s.label for s in shadows]
        self.revise = ReviseHandler(
            sessions,
            store,
            settings,
            source=revisions,
            clock=clock,
            params=self.params,
            model=model,
            guard=self.guard,
        )
        self.waiter = Waiter(sessions, settings, dsn=dsn, clock=clock, params=self.params)
        self.noticed: set[tuple[NoticeKind, int]] = set()  # notices queued by this process
        handlers = {JobKind.JUDGE: self.judge, JobKind.REVISE_PLAN: self.revise}
        n = settings.concurrency
        self.runners = [
            JobRunner(
                sessions,
                dsn,
                worker_id if n == 1 else f"{worker_id}-{i}",
                handlers,
                poll_interval=settings.poll_interval_s,
            )
            for i in range(n)
        ]

    async def run(self, stop: asyncio.Event) -> int:
        """Until `stop` is set: 0. If a job runner or the waiter ends on its own (a runner's
        claim failing on a database error ends it), the service stops and returns 1, so the
        container restarts instead of running on with fewer workers."""
        s = self.settings
        log.info(
            "judge starting",
            model=self.model.name,
            prompt_version=self.prompt_version,
            concurrency=s.concurrency,
            render_jobs=s.render_jobs,
            judge_audio_only=s.judge_audio_only,
            usage_guard=self.guard.enabled,
            shadows=self.shadow_labels,
        )
        workers = {
            asyncio.create_task(r.run(stop), name=f"runner-{i}"): f"runner-{i}"
            for i, r in enumerate(self.runners)
        }
        workers[asyncio.create_task(self.waiter.run(stop), name="waiter")] = "waiter"
        beat = asyncio.create_task(
            self._every(stop, s.heartbeat_interval_s, self.beat), name="beat"
        )
        stopper = asyncio.create_task(stop.wait(), name="stop")
        exit_code = 0
        try:
            done, _ = await asyncio.wait([stopper, *workers], return_when=asyncio.FIRST_COMPLETED)
            ended = [t for t in done if t is not stopper]
            if ended and not stop.is_set():
                exit_code = 1
                for task in ended:
                    exc = None if task.cancelled() else task.exception()
                    log.error(
                        "judge task ended unexpectedly: stopping the service",
                        task=workers[task],
                        error=None if exc is None else f"{type(exc).__name__}: {exc}",
                        exc_info=exc,
                    )
        finally:
            stop.set()  # the other loops end too
            # runners finish the job in progress (JobRunner.run returns once stop is set); a
            # cancelled call leaves its job running until the controller requeues it
            tasks = [*workers, beat, stopper]
            _, pending = await asyncio.wait(tasks, timeout=STOP_TIMEOUT_S)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            log.info("judge stopped", exit_code=exit_code, **self.stats.outcomes)
        return exit_code

    def info(self, waiting: int) -> dict[str, Any]:
        s = self.settings
        return {
            "model": self.model.name,
            "prompt_version": self.prompt_version,
            "concurrency": s.concurrency,
            "render_jobs": s.render_jobs,
            "judge_audio_only": s.judge_audio_only,
            "usage_guard": self.guard.info(),
            "calls": self.judge.caller.calls,
            "call_errors": self.judge.caller.errors,
            "rounds": self.stats.rounds,
            "cached": self.stats.cached,
            "retries": self.stats.retries,
            "deferred": self.stats.deferred,
            "outcomes": dict(self.stats.outcomes),
            "shadows": self.shadow_labels,
            "shadow": dict(self.stats.shadow),
            "revisions": {"revised": self.revise.revised, "refused": self.revise.refused},
            "waiting_clips": waiting,
            "waiter": {"listening": self.waiter.listening, "enqueued": self.waiter.enqueued},
            "model_status": (
                dict(self.model.status()) if isinstance(self.model, ReportsStatus) else None
            ),
        }

    async def beat(self) -> None:
        sent: list[tuple[NoticeKind, int]] = []
        async with self.sessions.begin() as session:
            waiting = await waiting_count(session)
            await heartbeat.beat(
                session,
                worker_id=self.worker_id,
                service=SERVICE,
                started_at=self.started_at,
                info=self.info(waiting),
            )
            for notice in self.notices():
                key = (notice.kind, notice.ref_id)
                if key in self.noticed:
                    continue
                await writes.notify(
                    session,
                    notice.kind,
                    ref_id=notice.ref_id,
                    text_=notice.text,
                    severity=notice.severity,
                )
                sent.append(key)
                log.warning("notice queued", kind=notice.kind, ref_id=notice.ref_id)
        self.noticed.update(sent)  # only once the transaction committed

    def notices(self) -> list[Notice]:
        """The notices the adapter's warnings call for now (see the module docstring)."""
        if not isinstance(self.model, ReportsStatus):
            return []
        status = self.model.status()
        warnings = status.get("warnings") or {}
        out: list[Notice] = []
        expiring = _parse_iso(warnings.get("credential_expiring"))
        if expiring is not None:
            days = max((expiring - self.clock()).total_seconds() / 86400, 0.0)
            out.append(
                Notice(
                    NoticeKind.CREDENTIAL_EXPIRING,
                    int(expiring.timestamp()),
                    f"The judge's Claude credential expires {expiring:%Y-%m-%d %H:%M} UTC (in "
                    f"{days:.0f} days). Run `claude setup-token`, put the new token in .env as "
                    "CLIPPER_JUDGE_CLAUDE_OAUTH_TOKEN (and its date as "
                    "CLIPPER_JUDGE_CLAUDE_TOKEN_EXPIRES), then restart the judge.",
                    "warning",
                )
            )
        off = warnings.get("model_off")
        if off:
            since = _parse_iso(status.get("off_since")) or self.clock()
            out.append(
                Notice(
                    NoticeKind.MODEL_OFF,
                    int(since.timestamp() // 86400),
                    f"The judge's model is switched off: {off}. Judge jobs are held (clips keep "
                    "their state) until the judge is restarted with a fix.",
                    "error",
                )
            )
        return out

    @staticmethod
    async def _every(
        stop: asyncio.Event, interval_s: float, fn: Callable[[], Awaitable[None]]
    ) -> None:
        while not stop.is_set():
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("periodic task failed", task=getattr(fn, "__name__", "?"))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
