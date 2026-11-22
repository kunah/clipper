"""Usage guard (DESIGN §19, decided 2026-11-21): keeps the judge from using up the plan limits it
shares with the user's own Claude use. **Off by default**: every reading is logged and in the
heartbeat, and with all caps unset (`None`) nothing blocks.

Inputs: the last `RateLimitReading` (five-hour and seven-day utilization, `resetsAt`, status; in
memory) and local call counts (`reads.usage_counts`: the stream's calls in the last 60 min, all
calls of the UTC day). Before a call (`check`), in this order:
1. `calls_per_day` reached -> reject (`budget`) and a heartbeat warning;
2. `calls_per_stream_hour` reached -> only candidates above that hour's median score go on, the
   others are rejected (`budget`);
3. a usage cap set: no reading yet or one older than `usage_stale_s` -> one call goes through to
   refresh it (others wait a little, rejected at the hard deadline); a fresh reading at or over
   `usage_five_hour_max` /
   `usage_seven_day_max`, or a status other than `allowed` -> the clip waits until the window
   resets (`resetsAt`, else `limit_retry_s`), never past its hard deadline; at the hard
   deadline it is rejected (`budget`).
A call the provider refuses for a limit waits for `resetsAt` whether caps are set or not (the
service handles that: `UsageLimitError`). Operator revisions are counted, never blocked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from clipper_core.logs import get_logger
from clipper_judge.model import RateLimitReading, UsageWindow
from clipper_judge.reads import UsageCounts
from clipper_judge.settings import JudgeSettings

log = get_logger(__name__)

REFRESH_WAIT = timedelta(seconds=30)  # while another call refreshes a stale reading
ALLOWED = "allowed"


@dataclass(frozen=True, slots=True)
class Allow:
    refresh: bool = False  # this call refreshes a missing or stale reading


@dataclass(frozen=True, slots=True)
class Defer:
    until: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class Reject:
    reason: str


Decision = Allow | Defer | Reject


class UsageGuard:
    def __init__(self, settings: JudgeSettings) -> None:
        self.settings = settings
        self.reading: RateLimitReading | None = None
        self.warnings: dict[str, str] = {}  # name -> since (ISO), for the heartbeat
        self._refreshing = False
        self.blocked = 0  # deferred or rejected calls since start

    @property
    def enabled(self) -> bool:
        return self.settings.usage_caps

    def observe(self, reading: RateLimitReading | None) -> None:
        """A provider reading (after a call, success or error): kept and logged."""
        self._refreshing = False
        if reading is None:
            return
        self.reading = reading
        log.info("usage reading", guard=self.enabled, **_fields(reading))

    def call_done(self) -> None:
        """The call this guard let through finished without a reading."""
        self._refreshing = False

    def check(
        self,
        *,
        now: datetime,
        hard_deadline: datetime,
        counts: UsageCounts,
        score: float,
        hour_median: float | None,
    ) -> Decision:
        s = self.settings
        if s.calls_per_day is not None and counts.today >= s.calls_per_day:
            self.warnings.setdefault("calls_per_day", now.isoformat())
            return self._blocked(Reject(f"calls_per_day {s.calls_per_day} reached"))
        if (
            s.calls_per_stream_hour is not None
            and counts.stream_last_hour >= s.calls_per_stream_hour
            and hour_median is not None
            and score <= hour_median
        ):
            return self._blocked(
                Reject(
                    f"calls_per_stream_hour {s.calls_per_stream_hour} reached and score "
                    f"{score:.2f} is not above the hour's median {hour_median:.2f}"
                )
            )
        if s.usage_five_hour_max is None and s.usage_seven_day_max is None:
            return Allow()
        r = self.reading
        if r is None or (now - r.observed_at).total_seconds() > s.usage_stale_s:
            if self._refreshing:
                if now >= hard_deadline:
                    return self._blocked(Reject("usage reading refreshing at the hard deadline"))
                return self._blocked(
                    Defer(min(now + REFRESH_WAIT, hard_deadline), "usage reading refreshing")
                )
            self._refreshing = True
            return Allow(refresh=True)
        over = _over("five_hour", r.five_hour, s.usage_five_hour_max) or _over(
            "seven_day", r.seven_day, s.usage_seven_day_max
        )
        if over is None and r.status == ALLOWED:
            return Allow()
        if now >= hard_deadline:
            return self._blocked(Reject("usage limit at the hard deadline"))
        name, window = over if over is not None else ("status", None)
        resets = (window.resets_at if window is not None else None) or r.resets_at
        until = resets if resets is not None and resets > now else now + self.retry_after
        reason = f"usage {name} over the cap" if over is not None else f"usage status {r.status}"
        return self._blocked(Defer(min(until, hard_deadline), reason))

    @property
    def retry_after(self) -> timedelta:
        return timedelta(seconds=self.settings.limit_retry_s)

    def _blocked(self, decision: Decision) -> Decision:
        self.blocked += 1
        return decision

    def info(self) -> dict[str, Any]:
        """For the heartbeat."""
        s = self.settings
        return {
            "enabled": self.enabled,
            "caps": {
                "five_hour": s.usage_five_hour_max,
                "seven_day": s.usage_seven_day_max,
                "calls_per_stream_hour": s.calls_per_stream_hour,
                "calls_per_day": s.calls_per_day,
            },
            "reading": None if self.reading is None else self.reading.as_dict(),
            "blocked": self.blocked,
            "warnings": dict(self.warnings),
        }


def _over(
    name: str, window: UsageWindow | None, cap: float | None
) -> tuple[str, UsageWindow] | None:
    if cap is None or window is None or window.utilization is None:
        return None
    return (name, window) if window.utilization >= cap else None


def _fields(r: RateLimitReading) -> dict[str, Any]:
    return {
        "status": r.status,
        "five_hour": None if r.five_hour is None else r.five_hour.utilization,
        "seven_day": None if r.seven_day is None else r.seven_day.utilization,
        "resets_at": None if r.resets_at is None else r.resets_at.isoformat(),
    }
