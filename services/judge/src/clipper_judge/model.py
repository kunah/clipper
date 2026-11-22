"""The `JudgeModel` protocol (DESIGN §19): one call per `JudgeRequest`, provider-neutral.

An adapter turns a `JudgeRequest` (system prompt, user content, JSON schema, max tokens) into
one provider call and returns a `JudgeResponse`: the answer's JSON text, usage (input / cached
input / output tokens), latency, the provider's request id, its own cost estimate if it has one,
the last usage-window reading (the Claude Code CLI's `rate_limit_event`, build item 6d) and
`extras`: whatever else the provider reported (CLI version, init event, raw result line). The
extras go into the call's S3 record only, never into the request hash, so a provider update
doesn't re-judge anything.

Errors: `ModelError` (the call failed: provider error, a run without an answer; the job
retries with backoff), `CallTimeoutError` (the call timed out: one more attempt, then the clip
fails), `UsageLimitError` (a rate or usage limit, or extra usage that isn't allowed: the job
waits for `resets_at`), `PermanentModelError` (retrying can't help: a misconfiguration, a
budget cap: the clip fails), `ModelUnavailableError` (the adapter switched itself off, e.g. it
would bill an API key or its credentials were rejected: no further calls until a restart, the
jobs are held and the clips keep their state). Every error carries what is known of the call
(latency, request id, reading, usage and cost when the provider reported them: a failed run can
be paid). An answer that arrives but isn't valid JSON or doesn't fit the schema is *not* an
error here: the service parses it (`answer.parse_answer`) and spends the round's one retry on
it.

Adapters: the fakes (`clipper_judge.fakes`), `ClaudeCliModel` (`clipper_judge.claude_cli`, 6d);
API adapters later. An adapter with something to show in the heartbeat (CLI version, credential
expiry, warnings) also implements `ReportsStatus`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from clipper_judge.prompt import JudgeRequest

# Reading statuses that let calls through (`rate_limit_info.status`, window statuses).
ALLOWED_STATUSES = frozenset({"allowed", "allowed_warning"})


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts of one call. `input_tokens` is the input not read from a prompt cache (cache
    writes included), `cached_input_tokens` the input read from one; together the whole prompt.
    The CLI's `usage` (as the Anthropic API counts): `input_tokens + cache_creation_input_tokens`
    and `cache_read_input_tokens`."""

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class UsageWindow:
    """One usage window of the plan (`unifiedWindows.five_hour|seven_day` of the CLI)."""

    utilization: float | None = None  # 0..1
    resets_at: datetime | None = None
    status: str | None = None


@dataclass(frozen=True, slots=True)
class RateLimitReading:
    """The provider's last usage reading (DESIGN §19 "Usage guard"). `observed_at` is when the
    adapter saw it (wall clock); `status` is the provider's (`allowed`, ...). `overage`: the call
    is billed as extra usage beyond the plan (`isUsingOverage`, or the plan refusing while
    `overageStatus` allows); `overage_status` as reported."""

    observed_at: datetime
    status: str = "allowed"
    resets_at: datetime | None = None
    five_hour: UsageWindow | None = None
    seven_day: UsageWindow | None = None
    overage: bool = False
    overage_status: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def refusal_resets_at(self) -> datetime | None:
        """When a refused (or exhausted) plan allows again: the window that refuses or is full,
        else the reading's own reset when its status refuses; None when the reading shows no
        refusal (a short 429 while the windows still allow: the caller's default wait)."""
        for w in (self.five_hour, self.seven_day):
            if w is None or w.resets_at is None:
                continue
            if (w.status is not None and w.status not in ALLOWED_STATUSES) or (
                w.utilization is not None and w.utilization >= 1.0
            ):
                return w.resets_at
        if self.status not in ALLOWED_STATUSES | {"unknown"}:
            return self.resets_at
        return None

    def as_dict(self) -> dict[str, Any]:
        def window(w: UsageWindow | None) -> dict[str, Any] | None:
            if w is None:
                return None
            return {
                "utilization": w.utilization,
                "resets_at": None if w.resets_at is None else w.resets_at.isoformat(),
                "status": w.status,
            }

        return {
            "observed_at": self.observed_at.isoformat(),
            "status": self.status,
            "resets_at": None if self.resets_at is None else self.resets_at.isoformat(),
            "five_hour": window(self.five_hour),
            "seven_day": window(self.seven_day),
            "overage": self.overage,
            "overage_status": self.overage_status,
        }


@dataclass(frozen=True, slots=True)
class JudgeResponse:
    text: str  # the answer's JSON text
    usage: Usage = Usage()
    latency_s: float = 0.0
    request_id: str | None = None  # the provider's id (CLI: session_id)
    cost_usd: float | None = None  # the provider's own estimate; None: the settings' price table
    rate_limit: RateLimitReading | None = None
    extras: Mapping[str, Any] = field(default_factory=dict)  # S3 record only (JSON values)


class JudgeModel(Protocol):
    """`name`: the model id recorded in `judge_calls.model` and the plans, part of the request
    hash. `options`: the provider settings that change the answer (temperature, effort, ...;
    JSON values), also hashed (`prompt.request_hash`)."""

    @property
    def name(self) -> str: ...

    @property
    def options(self) -> Mapping[str, Any]: ...

    async def judge(self, request: JudgeRequest) -> JudgeResponse: ...


@runtime_checkable
class ReportsStatus(Protocol):
    """An adapter's state for the heartbeat (JSON values; `warnings`: name -> detail)."""

    def status(self) -> Mapping[str, Any]: ...


class ModelError(Exception):
    """The call failed (provider error, no answer): the judge job retries with backoff; its last
    attempt marks the clip `failed`. What is known of the failed call (latency, request id,
    provider output, and `usage`/`cost_usd` when the provider reported them: the run was paid)
    goes into the call's error row and S3 record."""

    def __init__(
        self,
        message: str,
        *,
        latency_s: float = 0.0,
        request_id: str | None = None,
        rate_limit: RateLimitReading | None = None,
        extras: Mapping[str, Any] | None = None,
        usage: Usage | None = None,
        cost_usd: float | None = None,
    ) -> None:
        super().__init__(message)
        self.latency_s = latency_s
        self.request_id = request_id
        self.rate_limit = rate_limit
        self.extras = dict(extras or {})
        self.usage = usage
        self.cost_usd = cost_usd


class CallTimeoutError(ModelError):
    """The call ran past its timeout (the provider's process was killed): one more attempt of
    the same request at most, then the clip fails (`calls.TIMEOUT_ATTEMPTS`)."""


class UsageLimitError(ModelError):
    """The provider refused the call for a rate or usage limit: the clip waits until
    `resets_at` (else the settings' `limit_retry_s`), at most until its hard deadline (§19)."""

    def __init__(self, message: str, *, resets_at: datetime | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.resets_at = resets_at


class PermanentModelError(ModelError):
    """Retrying can't help (an adapter misconfiguration, a run over its budget cap): the clip
    fails at once."""


class ModelUnavailableError(ModelError):
    """The adapter switched itself off (latched): it would bill an API key instead of the plan,
    its output didn't start as expected, or its credentials were rejected. It makes no further
    calls until the service restarts; judge jobs are held (deferred by `retry_after_s`), clips keep
    their state, the heartbeat shows the reason. `called`: this error came from a real call (its
    row is recorded); False: refused without starting anything."""

    def __init__(
        self, message: str, *, called: bool = False, retry_after_s: float = 600.0, **kw: Any
    ) -> None:
        super().__init__(message, **kw)
        self.called = called
        self.retry_after_s = retry_after_s
