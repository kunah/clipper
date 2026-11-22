"""The `JudgeModel` protocol (DESIGN §19): one call per `JudgeRequest`, provider-neutral.

An adapter turns a `JudgeRequest` (system prompt, user content, JSON schema, max tokens) into
one provider call and returns a `JudgeResponse`: the answer's JSON text, usage (input / cached
input / output tokens), latency, the provider's request id, its own cost estimate if it has one,
the last usage-window reading (the Claude Code CLI's `rate_limit_event`, build item 6d) and
`extras`: whatever else the provider reported (CLI version, init event, raw result line). The
extras go into the call's S3 record only, never into the request hash, so a provider update
doesn't re-judge anything.

Errors: `ModelError` (the call failed: provider error, timeout, a run without an answer; the job
retries with backoff), `UsageLimitError` (a rate or usage limit: the job waits for `resets_at`),
`PermanentModelError` (retrying can't help: wrong credentials, a misconfigured adapter). An answer
that arrives but isn't valid JSON or doesn't fit the schema is *not* an error here: the service
parses it (`answer.parse_answer`) and spends the round's one retry on it.

Adapters: the fakes (`clipper_judge.fakes`) now; `ClaudeCliModel` in 6d; API adapters later.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from clipper_judge.prompt import JudgeRequest


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts of one call. `input_tokens` excludes cached input (as the Anthropic API and
    the CLI's `usage` count them: cache reads are `cached_input_tokens`)."""

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
    adapter saw it (wall clock); `status` is the provider's (`allowed`, ...)."""

    observed_at: datetime
    status: str = "allowed"
    resets_at: datetime | None = None
    five_hour: UsageWindow | None = None
    seven_day: UsageWindow | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

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


class ModelError(Exception):
    """The call failed (provider error, timeout, no answer): the judge job retries with backoff;
    its last attempt marks the clip `failed`. What is known of the failed call (latency, request
    id, provider output) goes into the call's error row and S3 record."""

    def __init__(
        self,
        message: str,
        *,
        latency_s: float = 0.0,
        request_id: str | None = None,
        rate_limit: RateLimitReading | None = None,
        extras: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.latency_s = latency_s
        self.request_id = request_id
        self.rate_limit = rate_limit
        self.extras = dict(extras or {})


class UsageLimitError(ModelError):
    """The provider refused the call for a rate or usage limit: the clip waits until
    `resets_at` (else the settings' `limit_retry_s`), at most until its hard deadline (§19)."""

    def __init__(self, message: str, *, resets_at: datetime | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.resets_at = resets_at


class PermanentModelError(ModelError):
    """Retrying can't help (wrong credentials, an adapter misconfiguration): the clip fails at
    once."""
