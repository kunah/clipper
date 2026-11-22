"""Model calls with a cache (DESIGN §19 "Cache, idempotency, cost").

Every call gets a `judge_calls` row keyed by `request_hash` (sha256 of model, provider options and
the canonical request), **committed alone, right after the call**, so a crash or a retry after
the call reuses the paid answer instead of calling again. The full I/O (request, raw response,
provider extras) is uploaded first to `storage.keys.judge_io(request_hash, clip_id)`; an upload
failure is logged and leaves `io_key` NULL (the row still saves the answer).

`judge_calls.error` starts with its kind:
- `answer: ...` the model answered, but not with a valid answer (invalid JSON, wrong schema).
  That is the request's outcome: a repeat of the same request reuses it (the round's one retry is
  a different request, with the error stated). `answer` holds the decoded object when the text
  was a JSON object.
- `provider: ...` the call failed (a provider error): not an outcome, sent again.
- `timeout: ...` the call timed out: sent once more at most; the request's second timeout
  (`TIMEOUT_ATTEMPTS`) fails the clip (raised as `PermanentModelError`).
- `usage_limit: ...` refused for a rate or usage limit (or extra usage that isn't allowed): sent
  again after the reset; not counted by the usage guard.
- `permanent: ...` retrying can't help (configuration, a budget cap).
- `unavailable: ...` the adapter switched itself off on this call (it would bill an API key,
  rejected credentials): the jobs are held. Later refusals start nothing and write no row.
The failed calls record the usage and cost the provider reported for them (the CLI reports them
for a run that was paid but failed), else 0, and each gets its own S3 record
(`judge_io(..., suffix="error-<id>")`), so the request's next call doesn't overwrite it;
`answer:` rows were answered (and paid): they record the call's usage and cost and share the
request's record like a success.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_core.logs import get_logger
from clipper_core.storage import keys
from clipper_judge import reads, writes
from clipper_judge.answer import AnswerError, JudgeAnswer, parse_answer
from clipper_judge.model import (
    CallTimeoutError,
    JudgeModel,
    JudgeResponse,
    ModelError,
    ModelUnavailableError,
    PermanentModelError,
    RateLimitReading,
    Usage,
    UsageLimitError,
)
from clipper_judge.prompt import JudgeRequest
from clipper_judge.reads import ObjectStore
from clipper_judge.settings import JudgeSettings

log = get_logger(__name__)

ANSWER_ERROR = "answer"
PROVIDER_ERROR = "provider"
TIMEOUT = "timeout"
USAGE_LIMIT = "usage_limit"
PERMANENT_ERROR = "permanent"
UNAVAILABLE = "unavailable"
TIMEOUT_ATTEMPTS = 2  # timeouts of one request before the clip fails
REUSABLE = (ANSWER_ERROR,)  # error kinds that are a request's outcome (cached)
NOT_COUNTED = (USAGE_LIMIT,)  # error kinds the usage guard doesn't count as calls

Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class CallOutcome:
    call_id: int
    answer: JudgeAnswer | None  # None: `error` says why the answer can't be used
    error: str | None
    cached: bool = False
    rate_limit: RateLimitReading | None = None


def error_kind(error: str | None) -> str | None:
    return None if error is None else error.split(":", 1)[0]


def _strip_kind(error: str) -> str:
    return error.split(":", 1)[1].strip() if ":" in error else error


def _decode(raw: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _parse(obj: Mapping[str, Any] | None, raw: str | None, *, final: bool) -> JudgeAnswer:
    if obj is not None:
        return parse_answer(obj, final=final)
    return parse_answer(raw if raw is not None else "", final=final)


class Caller:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        model: JudgeModel,
        settings: JudgeSettings,
        *,
        clock: Clock,
    ) -> None:
        self.sessions = sessions
        self.store = store
        self.model = model
        self.settings = settings
        self.clock = clock
        self.calls = 0
        self.errors = 0

    async def cached(self, request_hash: str, request: JudgeRequest) -> CallOutcome | None:
        """The outcome of an earlier call of this exact request, if there is one."""
        async with self.sessions() as session:
            row = await reads.cached_call(session, request_hash, reusable=REUSABLE)
        if row is None:
            return None
        if row.error is not None and row.answer is None:
            return CallOutcome(row.id, None, _strip_kind(row.error), cached=True)
        try:
            answer = _parse(row.answer, None, final=request.final)
        except AnswerError as exc:
            return CallOutcome(row.id, None, str(exc), cached=True)
        return CallOutcome(row.id, answer, None, cached=True)

    async def call(
        self,
        request: JudgeRequest,
        *,
        request_hash: str,
        clip_id: int | None,
        stream_id: int,
    ) -> CallOutcome:
        """One model call, recorded (S3 record, then the `judge_calls` row, committed alone).
        Model errors are recorded too, then raised again."""
        try:
            response = await self.model.judge(request)
        except ModelUnavailableError as exc:
            if not exc.called:  # refused without starting anything: no call, no row
                raise
            self.calls += 1
            self.errors += 1
            await self._record_error(request, request_hash, clip_id, stream_id, exc, UNAVAILABLE)
            raise
        except ModelError as exc:
            self.calls += 1
            self.errors += 1
            kind = (
                USAGE_LIMIT
                if isinstance(exc, UsageLimitError)
                else PERMANENT_ERROR
                if isinstance(exc, PermanentModelError)
                else TIMEOUT
                if isinstance(exc, CallTimeoutError)
                else PROVIDER_ERROR
            )
            await self._record_error(request, request_hash, clip_id, stream_id, exc, kind)
            if kind == TIMEOUT:
                async with self.sessions() as s:
                    n = await reads.error_count(s, request_hash, TIMEOUT)
                if n >= TIMEOUT_ATTEMPTS:
                    raise PermanentModelError(
                        f"the request timed out {n} times: {exc}",
                        latency_s=exc.latency_s,
                        request_id=exc.request_id,
                        rate_limit=exc.rate_limit,
                    ) from exc
            raise
        self.calls += 1
        obj = _decode(response.text)
        try:
            answer: JudgeAnswer | None = _parse(obj, response.text, final=request.final)
            error = None
        except AnswerError as exc:
            answer, error = None, str(exc)
        call_id = await self._record(
            request,
            request_hash,
            clip_id,
            stream_id,
            response=response,
            answer=obj,
            error=None if error is None else f"{ANSWER_ERROR}: {error}",
            latency_s=response.latency_s,
            request_id=response.request_id,
            extras=response.extras,
        )
        return CallOutcome(call_id, answer, error, rate_limit=response.rate_limit)

    async def _record_error(
        self,
        request: JudgeRequest,
        request_hash: str,
        clip_id: int | None,
        stream_id: int,
        exc: ModelError,
        kind: str,
    ) -> int:
        return await self._record(
            request,
            request_hash,
            clip_id,
            stream_id,
            response=None,
            answer=None,
            error=f"{kind}: {exc}",
            latency_s=exc.latency_s,
            request_id=exc.request_id,
            extras={**exc.extras, "rate_limit": _reading(exc.rate_limit)},
            suffix=f"error-{uuid.uuid4().hex[:12]}",
            usage=exc.usage,
            cost_usd=exc.cost_usd,
        )

    async def _record(
        self,
        request: JudgeRequest,
        request_hash: str,
        clip_id: int | None,
        stream_id: int,
        *,
        response: JudgeResponse | None,
        answer: Mapping[str, Any] | None,
        error: str | None,
        latency_s: float,
        request_id: str | None,
        extras: Mapping[str, Any],
        suffix: str | None = None,
        usage: Usage | None = None,
        cost_usd: float | None = None,
    ) -> int:
        """`usage`/`cost_usd`: of a failed call (no response) the provider reported them for."""
        io_key = await self._upload(
            request,
            request_hash,
            clip_id,
            stream_id,
            response=response,
            error=error,
            extras=extras,
            suffix=suffix,
        )
        if response is not None:
            usage, cost_usd = response.usage, response.cost_usd
        tokens = (
            (usage.input_tokens, usage.cached_input_tokens, usage.output_tokens)
            if usage is not None
            else (0, 0, 0)
        )
        if cost_usd is not None:
            cost = cost_usd
        elif usage is not None:
            cost = self.settings.cost_usd(*tokens)
        else:
            cost = 0.0
        async with self.sessions.begin() as session:
            call_id = await writes.insert_call(
                session,
                clip_id=clip_id,
                stream_id=stream_id,
                model=self.model.name,
                prompt_version=request.prompt_version,
                request_hash=request_hash,
                input_tokens=tokens[0],
                cached_input_tokens=tokens[1],
                output_tokens=tokens[2],
                cost_usd=cost,
                latency_s=latency_s,
                answer=answer,
                error=error,
                io_key=io_key,
                provider_request_id=request_id,
            )
        log.info(
            "judge call",
            call_id=call_id,
            request_hash=request_hash[:12],
            model=self.model.name,
            final=request.final,
            input_tokens=tokens[0],
            cached_input_tokens=tokens[1],
            output_tokens=tokens[2],
            latency_s=round(latency_s, 3),
            error=error,
        )
        return call_id

    async def _upload(
        self,
        request: JudgeRequest,
        request_hash: str,
        clip_id: int | None,
        stream_id: int,
        *,
        response: JudgeResponse | None,
        error: str | None,
        extras: Mapping[str, Any],
        suffix: str | None = None,
    ) -> str | None:
        key = keys.judge_io(request_hash, clip_id, suffix)
        record = {
            "request_hash": request_hash,
            "model": self.model.name,
            "options": dict(self.model.options),
            "clip_id": clip_id,
            "stream_id": stream_id,
            "at": self.clock().isoformat(),
            "request": request.as_dict(),
            "response": None
            if response is None
            else {
                "text": response.text,
                "usage": {
                    "input_tokens": response.usage.input_tokens,
                    "cached_input_tokens": response.usage.cached_input_tokens,
                    "output_tokens": response.usage.output_tokens,
                },
                "latency_s": response.latency_s,
                "request_id": response.request_id,
                "cost_usd": response.cost_usd,
                "rate_limit": _reading(response.rate_limit),
            },
            "extras": dict(extras),
            "error": error,
        }
        data = json.dumps(record, ensure_ascii=False, default=str).encode("utf-8")
        try:
            await asyncio.to_thread(self.store.put_bytes, key, data, "application/json")
        except Exception:
            log.warning("judge call I/O not uploaded", key=key, exc_info=True)
            return None
        return key


def _reading(r: RateLimitReading | None) -> dict[str, Any] | None:
    return None if r is None else r.as_dict()
