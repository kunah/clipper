"""`judge` jobs (DESIGN §19 "A judge job = advance this clip"): idempotent, never blocks a worker
while media is missing.

    handler = JudgeHandler(sessions, store, model, settings, guard=UsageGuard(settings))
    runner = JobRunner(sessions, dsn, worker_id, {JobKind.JUDGE: handler, ...})

Per job (ref = candidate; the detector's `judge:{cand}` or the waiter's `judge:{cand}:r{n}`):
1. One transaction: the candidate's clip is created on the first job (context = the candidate's
   window, anchor = T, round 0, waiting since now) and locked. Decided clips (rejected, planned,
   failed) are left alone. A waiting clip without video near the moment and none to come is
   rejected `no_video` without a call (unless `judge_audio_only`); else its readiness (the
   waiter's rule) decides: still waiting -> the job ends (a round job instead checks again
   later: `RetryLater`), past the hard deadline without any transcribed core -> `no_media`,
   ready -> compare-and-set `waiting_for_media -> judging` (same round; flag
   `partial_context` when judged at a deadline). A clip found `judging` (its job died or was
   requeued) is taken over.
2. Inputs from the database and object storage, `build_request`; the call cache by
   `request_hash` (an earlier call of the same request is reused); the usage guard (off by
   default; only before a real call); the call, its `judge_calls` row committed alone
   (`calls.Caller`).
3. Parse + resolve (`resolver.resolve`); invalid JSON or `Unrepairable` -> one retry with the
   error stated (the final round's schema after `Unrepairable(final=True)`), then `failed`.
4. One transaction, compare-and-set on `(judging, round)`: the `clip_plans` row (version + 1)
   and the clip's status; `clip` -> `planned` + the `render` job (`dedupe_key(RENDER, plan)`, at
   the clip's priority; off with `render_jobs=False`); `reject` -> `rejected`; `need_context` ->
   the grown context, round + 1, `request_media(context, JUDGE_CONTEXT, clip_id)`, back to
   `waiting_for_media`, and the next round's job when that context is covered already (else the
   waiter enqueues it). Terminal states mark the clip's own media requests fulfilled.
Provider errors are retried by the job queue; the job's last attempt marks the clip `failed` (in
the round the job handled; a job that failed before it loaded the clip leaves it to the waiter). A
usage limit (refused call, extra usage that isn't allowed and the guard's overage latch after it,
or the guard with caps set) defers the job to the reset time, never past the hard deadline, where
the clip becomes `rejected`/`budget`. A model that switched itself off (`ModelUnavailableError`:
it would bill an API key, rejected credentials) holds the job (its `retry_after_s`, 10 min; no
attempt used, the clip stays `judging`) until the service is restarted with a fix.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_core.contracts import (
    CLIP_MIN,
    CandidateStatus,
    ClipRejectReason,
    ClipStatus,
    JudgePayload,
    Verdict,
    judge_round_key,
)
from clipper_core.jobs import ClaimedJob, PermanentJobError, RetryLater
from clipper_core.logs import get_logger, log_context
from clipper_core.timeline import utc_now
from clipper_judge import reads, writes
from clipper_judge.calls import NOT_COUNTED, Caller, CallOutcome
from clipper_judge.guard import Defer, Reject, UsageGuard
from clipper_judge.model import (
    JudgeModel,
    ModelError,
    ModelUnavailableError,
    PermanentModelError,
    UsageLimitError,
)
from clipper_judge.params import JudgeParams
from clipper_judge.prompt import JudgeRequest, build_request, request_hash, retry_request
from clipper_judge.reads import CandidateInfo, ClipRow, ObjectStore, StreamInfo
from clipper_judge.resolver import (
    NeedsContext,
    Planned,
    Rejected,
    Resolution,
    Unrepairable,
    resolve,
)
from clipper_judge.settings import JudgeSettings
from clipper_judge.waiter import Deadlines, NoMedia, Ready, Wait, check_readiness, deadlines

log = get_logger(__name__)

Clock = Callable[[], datetime]
TERMINAL = frozenset({ClipStatus.REJECTED, ClipStatus.PLANNED, ClipStatus.FAILED})


@dataclass
class RoundStats:
    """Counters since the process started (heartbeat)."""

    rounds: int = 0  # rounds judged (a call or a cached answer)
    cached: int = 0  # answers reused from judge_calls
    retries: int = 0  # the round's one retry (invalid answer / unrepairable)
    deferred: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)  # planned, rejected:<reason>, ...

    def count(self, outcome: str) -> None:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1


@dataclass
class _JobState:
    """What one job learned about its clip (for giving up on the right round)."""

    round: int | None = None  # the clip's round when the job loaded it


class _Budget:
    """The usage guard rejected the clip (`rejected`/`budget`, already committed)."""


BUDGET = _Budget()


class JudgeHandler:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        model: JudgeModel,
        settings: JudgeSettings,
        *,
        guard: UsageGuard | None = None,
        clock: Clock = utc_now,
        params: JudgeParams | None = None,
        stats: RoundStats | None = None,
    ) -> None:
        self.sessions = sessions
        self.store = store
        self.model = model
        self.settings = settings
        self.params = params or settings.params()
        self.guard = guard or UsageGuard(settings)
        self.clock = clock
        self.stats = stats or RoundStats()
        self.caller = Caller(sessions, store, model, settings, clock=clock)

    async def __call__(self, job: ClaimedJob) -> None:
        try:
            payload = JudgePayload.model_validate(job.payload)
        except ValueError as exc:  # a malformed job: retrying can't help
            raise PermanentJobError(f"invalid judge payload: {exc}") from exc
        state = _JobState()
        with log_context(candidate_id=payload.candidate_id):
            try:
                await self.advance(payload.candidate_id, job, state)
            except (RetryLater, PermanentJobError):
                raise
            except Exception as exc:
                if job.attempts >= job.max_attempts and state.round is not None:
                    # the last attempt: the judge gives up on the round this job was handling
                    await self._give_up(
                        payload.candidate_id, f"{type(exc).__name__}: {exc}", state.round
                    )
                raise

    # ------------------------------------------------------------------ step 1

    async def advance(
        self, candidate_id: int, job: ClaimedJob, state: _JobState | None = None
    ) -> None:
        state = state if state is not None else _JobState()
        now = self.clock()
        retry_at: datetime | None = None
        async with self.sessions.begin() as s:
            cand = await reads.candidate(s, candidate_id)
            if cand is None:
                raise PermanentJobError(f"candidate {candidate_id} does not exist")
            if cand.status != CandidateStatus.EMITTED:
                log.info("candidate is not emitted: nothing to judge", status=cand.status)
                return
            stream_info = await reads.stream(s, cand.stream_id)
            clip = await writes.upsert_clip(s, cand, priority=job.priority, now=now)
            state.round = clip.round
            if clip.status in TERMINAL:
                log.info("clip already decided", clip_id=clip.id, status=clip.status)
                return
            if clip.status == ClipStatus.WAITING_FOR_MEDIA:
                woken = await self._wake(s, clip, cand, stream_info, now)
                if isinstance(woken, Wait):
                    key = await reads.job_key(s, job.id)
                    if key == judge_round_key(cand.id, clip.round):
                        # the waiter saw this clip ready, now it isn't (late chunks): check again
                        sweep = now + timedelta(seconds=self.settings.sweep_interval_s)
                        retry_at = min(woken.next_check, sweep)
                    else:
                        log.info("clip waits for media", clip_id=clip.id, round=clip.round)
                        return
                elif woken is None:
                    return
                else:
                    clip = woken
            else:
                log.info("taking over a clip in judging", clip_id=clip.id, round=clip.round)
        if retry_at is not None:
            raise RetryLater(retry_at, "media not complete any more: checking again")
        with log_context(clip_id=clip.id, round=clip.round):
            await self._round(clip, cand, stream_info)

    async def _wake(
        self,
        s: AsyncSession,
        clip: ClipRow,
        cand: CandidateInfo,
        stream_info: StreamInfo,
        now: datetime,
    ) -> ClipRow | Wait | None:
        """A waiting clip: rejected (None), still waiting (`Wait`) or now judging (the clip)."""
        if not self.settings.judge_audio_only and await reads.no_video(
            s, cand, stream_info, now=now, min_s=CLIP_MIN.total_seconds()
        ):
            await writes.reject(
                s, clip, ClipRejectReason.NO_VIDEO, now=now, expect=ClipStatus.WAITING_FOR_MEDIA
            )
            self.stats.count(f"rejected:{ClipRejectReason.NO_VIDEO}")
            log.info("clip rejected: no video near the moment", clip_id=clip.id)
            return None
        state, _ = await check_readiness(
            s,
            stream_id=clip.stream_id,
            context=clip.context,
            moment_from=cand.moment_from,
            moment_to=cand.moment_to,
            waiting_since=clip.waiting_since,
            now=now,
            settings=self.settings,
            params=self.params,
        )
        if isinstance(state, Wait):
            return state
        if isinstance(state, NoMedia):
            await writes.reject(
                s, clip, ClipRejectReason.NO_MEDIA, now=now, expect=ClipStatus.WAITING_FOR_MEDIA
            )
            self.stats.count(f"rejected:{ClipRejectReason.NO_MEDIA}")
            log.info("clip rejected: hard deadline without a transcribed core", clip_id=clip.id)
            return None
        assert isinstance(state, Ready)
        judging = await writes.start_judging(s, clip, partial=state.partial)
        if judging is not None:
            log.info("clip judging", clip_id=clip.id, round=clip.round, partial=state.partial)
        return judging

    # ------------------------------------------------------------------ steps 2-4

    def _deadlines(self, clip: ClipRow) -> Deadlines:
        return deadlines(
            clip.waiting_since,
            clip.context_to,
            soft_wait_s=self.settings.soft_wait_s,
            hard_wait_s=self.settings.hard_wait_s,
        )

    async def _round(self, clip: ClipRow, cand: CandidateInfo, stream_info: StreamInfo) -> None:
        d = self._deadlines(clip)
        async with self.sessions() as s:
            inputs = await reads.load_inputs(
                s,
                self.store,
                clip,
                cand,
                stream_info,
                params=self.params,
                now=self.clock(),
                sfx_tags=self.settings.sfx_tags,
                audio_only=self.settings.judge_audio_only,
            )
        prompt = self.settings.prompt
        request = build_request(inputs, self.params, prompt=prompt)
        error: str | None = None
        retry_final = request.final
        for attempt in range(2):
            if attempt:
                self.stats.retries += 1
                log.info("retrying the round with the error stated", error=(error or "")[:300])
                request = retry_request(
                    build_request(inputs, self.params, prompt=prompt, final=retry_final),
                    error or "",
                )
            outcome = await self._answer(request, clip, cand, d)
            if outcome is BUDGET:
                return
            assert isinstance(outcome, CallOutcome)
            self.stats.rounds += 1
            if outcome.answer is None:
                error, retry_final = outcome.error or "invalid answer", request.final
                continue
            res = resolve(
                outcome.answer,
                inputs,
                self.params,
                model=self.model.name,
                prompt_version=request.prompt_version,
            )
            if isinstance(res, Unrepairable):
                error, retry_final = res.error, request.final or res.final
                continue
            await self._commit(clip, cand, res, outcome.call_id, request)
            return
        await self._finish_failed(clip, f"no usable answer after one retry: {error}")

    async def _answer(
        self, request: JudgeRequest, clip: ClipRow, cand: CandidateInfo, d: Deadlines
    ) -> CallOutcome | _Budget:
        h = request_hash(self.model.name, request, self.model.options)
        cached = await self.caller.cached(h, request)
        if cached is not None:
            self.stats.cached += 1
            log.info("answer reused from an earlier call", call_id=cached.call_id)
            return cached
        held = self.guard.hold(now=self.clock(), hard_deadline=d.hard)
        if isinstance(held, Reject):
            await self._reject_budget(clip, held.reason)
            return BUDGET
        if isinstance(held, Defer):
            self.stats.deferred += 1
            log.info("usage guard holds the clip", until=held.until.isoformat())
            raise RetryLater(held.until, f"usage guard: {held.reason}")
        if self.guard.enabled:
            async with self.sessions() as s:
                counts = await reads.usage_counts(s, clip.stream_id, not_counted=NOT_COUNTED)
                median = (
                    await reads.stream_hour_median(s, clip.stream_id)
                    if self.settings.calls_per_stream_hour is not None
                    else None
                )
            decision = self.guard.check(
                now=self.clock(),
                hard_deadline=d.hard,
                counts=counts,
                score=cand.score,
                hour_median=median,
            )
            if isinstance(decision, Reject):
                await self._reject_budget(clip, decision.reason)
                return BUDGET
            if isinstance(decision, Defer):
                self.stats.deferred += 1
                log.info("usage guard defers the clip", until=decision.until.isoformat())
                raise RetryLater(decision.until, f"usage guard: {decision.reason}")
        try:
            outcome = await self.caller.call(
                request, request_hash=h, clip_id=clip.id, stream_id=clip.stream_id
            )
        except ModelError as exc:
            self.guard.observe(exc.rate_limit)
            if isinstance(exc, ModelUnavailableError):
                until = self.clock() + timedelta(seconds=exc.retry_after_s)
                self.stats.deferred += 1
                log.error(
                    "the model is switched off: job held", until=until.isoformat(), error=str(exc)
                )
                raise RetryLater(until, f"model unavailable: {exc}") from exc
            if isinstance(exc, UsageLimitError):
                return await self._limit(clip, exc, d)
            if isinstance(exc, PermanentModelError):
                await self._finish_failed(clip, f"model: {exc}")
                raise PermanentJobError(str(exc)) from exc
            raise
        except BaseException:
            self.guard.call_done()
            raise
        self.guard.observe(outcome.rate_limit)
        return outcome

    async def _limit(self, clip: ClipRow, exc: UsageLimitError, d: Deadlines) -> _Budget:
        now = self.clock()
        if now >= d.hard:
            await self._reject_budget(clip, f"usage limit at the hard deadline: {exc}")
            return BUDGET
        resets = exc.resets_at if exc.resets_at is not None and exc.resets_at > now else None
        until = min(resets or now + self.guard.retry_after, d.hard)
        self.stats.deferred += 1
        log.warning("call refused for a usage limit: waiting", until=until.isoformat())
        raise RetryLater(until, f"usage limit: {exc}") from exc

    async def _commit(
        self,
        clip: ClipRow,
        cand: CandidateInfo,
        res: Resolution,
        call_id: int,
        request: JudgeRequest,
    ) -> None:
        """Step 4: one transaction, compare-and-set on the clip's (judging, round)."""
        now = self.clock()
        async with self.sessions.begin() as s:
            cur = await reads.clip_for_update(s, clip_id=clip.id)
            if cur is None or cur.status != ClipStatus.JUDGING or cur.round != clip.round:
                log.warning(
                    "clip changed while it was judged: result dropped",
                    status=None if cur is None else cur.status,
                )
                return
            version = await writes.next_version(s, cur.id)
            common = {
                "clip_id": cur.id,
                "version": version,
                "round_": cur.round,
                "model": self.model.name,
                "prompt_version": request.prompt_version,
                "judge_call_id": call_id,
            }
            if isinstance(res, Planned):
                plan = res.plan
                plan_id = await writes.insert_plan(
                    s,
                    **common,
                    verdict=Verdict.CLIP,
                    plan=plan.model_dump(mode="json"),
                    confidence=plan.confidence,
                    reasons=plan.reasons,
                    repairs=plan.repairs,
                )
                await writes.planned(s, cur, version=version, now=now)
                render_job = None
                if self.settings.render_jobs:
                    render_job = await writes.enqueue_render(s, plan_id, priority=cur.priority)
                self.stats.count("planned")
                log.info(
                    "clip planned",
                    plan_id=plan_id,
                    version=version,
                    duration_s=plan.duration.total_seconds(),
                    repairs=len(plan.repairs),
                    render_job=render_job,
                )
            elif isinstance(res, Rejected):
                await writes.insert_plan(
                    s,
                    **common,
                    verdict=Verdict.REJECT,
                    confidence=res.confidence,
                    reasons=res.reasons,
                    repairs=res.repairs,
                )
                await writes.reject(s, cur, res.reason, now=now, expect=ClipStatus.JUDGING)
                self.stats.count(f"rejected:{res.reason}")
                log.info("clip rejected", reason=res.reason, version=version)
            elif isinstance(res, NeedsContext):
                await writes.insert_plan(
                    s,
                    **common,
                    verdict=Verdict.NEED_CONTEXT,
                    context_request=res.context_request(),
                    confidence=res.confidence,
                    reasons=res.reasons,
                    repairs=res.repairs,
                )
                nxt = await writes.next_round(s, cur, context=res.context, now=now)
                assert nxt is not None  # we hold the row lock
                await writes.request_context(s, nxt)
                state, _ = await check_readiness(
                    s,
                    stream_id=nxt.stream_id,
                    context=nxt.context,
                    moment_from=cand.moment_from,
                    moment_to=cand.moment_to,
                    waiting_since=nxt.waiting_since,
                    now=now,
                    settings=self.settings,
                    params=self.params,
                )
                ready = not isinstance(state, Wait)
                if ready:  # often the time before T is transcribed already
                    await writes.enqueue_round(s, nxt)
                self.stats.count("need_context")
                log.info(
                    "clip needs more context",
                    version=version,
                    next_round=nxt.round,
                    before_s=res.before_s,
                    after_s=res.after_s,
                    ready=ready,
                )

    # ------------------------------------------------------------------ endings

    async def _reject_budget(self, clip: ClipRow, reason: str) -> None:
        now = self.clock()
        async with self.sessions.begin() as s:
            cur = await reads.clip_for_update(s, clip_id=clip.id)
            if cur is not None and cur.round == clip.round:
                await writes.reject(
                    s, cur, ClipRejectReason.BUDGET, now=now, expect=ClipStatus.JUDGING
                )
        self.stats.count(f"rejected:{ClipRejectReason.BUDGET}")
        log.warning("clip rejected by the usage guard", reason=reason)

    async def _finish_failed(self, clip: ClipRow, error: str) -> None:
        async with self.sessions.begin() as s:
            cur = await reads.clip_for_update(s, clip_id=clip.id)
            if cur is not None and cur.round == clip.round:
                await writes.fail(s, cur, error)
        self.stats.count("failed")
        log.error("clip failed", error=error[:500])

    async def _give_up(self, candidate_id: int, error: str, round_: int) -> None:
        """The job's last attempt failed: the clip fails (waiting or judging), but only in the
        round the job was handling (a stale job of an earlier round leaves a later round be)."""
        async with self.sessions.begin() as s:
            cur = await reads.clip_for_update(s, candidate_id=candidate_id)
            if cur is None or cur.status in TERMINAL or cur.round != round_:
                return
            await writes.fail(s, cur, error, expect=ClipStatus(cur.status))
        self.stats.count("failed")
        log.error("clip failed: the judge job gave up", clip_id=cur.id, error=error[:500])
