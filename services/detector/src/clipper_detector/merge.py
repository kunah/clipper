"""Episode + stage 2 -> candidate draft (DESIGN §18.5). Pure: no database, no clock.

- **Delay** `d`: `channels.chat_delay_s` (manual override) -> median of the last 5 valid
  `chat_delay_estimates` (the caller orders them by their stream's `started_at`, newest first,
  and skips backfilled streams flagged `timeline_uncertain_s`) -> 8 s. Fixed per stream.
- **Times**: `peak_utc = T = chat_peak - d` (video time); the moment is `[onset - d, end - d]`;
  the media window `[min(T - 90 s, onset - d - 30 s), max(T + 30 s, end - d + 15 s)]`, at most
  5 min: the larger side beyond `[T - 90 s, T + 30 s]` is trimmed first, the moment is clamped
  into the window.
- **Cooldown 60 s**: an emitted-to-be episode opening < 60 s after an *emitted* candidate's chat
  end folds into it when it scores no higher (`FoldIn`, recorded as `features.followups`); a
  stronger one is emitted with `features.prev_candidate_id`. A fold moves the candidate's chat end
  to the folded episode's end (`FoldIn.extended` replaces it in `recent`), so the cooldown runs
  from the last close: a burst every ~45 s stays one candidate instead of every other burst being
  emitted. Suppressed rows don't block (a real moment right after a raid must not vanish into
  the raid's row).
- **Budget**: at most 8 emitted per stream in a rolling hour (by chat peak); beyond that only
  above the lowest of them, else `suppressed` with reason `budget`.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from clipper_core.contracts import (
    CandidateSource,
    CandidateStatus,
    DelaySource,
    MomentType,
    SuppressReason,
)
from clipper_core.timeline import Interval
from clipper_detector.params import DetectorParams
from clipper_detector.scorer import Episode, Snapshot
from clipper_detector.stage2 import Stage2Result


@dataclass(frozen=True, slots=True)
class Delay:
    seconds: float
    source: DelaySource

    @property
    def td(self) -> timedelta:
        return timedelta(seconds=self.seconds)


def resolve_delay(
    override_s: float | None, learned_s: Sequence[float], params: DetectorParams
) -> Delay:
    """`learned_s`: delays of valid estimates, newest stream first (only the first
    `learned_delay_count` are used)."""
    if override_s is not None:
        return Delay(float(override_s), DelaySource.OVERRIDE)
    recent = list(learned_s)[: params.learned_delay_count]
    if recent:
        return Delay(float(statistics.median(recent)), DelaySource.LEARNED)
    return Delay(params.default_delay_s, DelaySource.DEFAULT)


@dataclass(frozen=True, slots=True)
class PriorCandidate:
    """A candidate already decided for the stream (cooldown and budget look at these)."""

    candidate_id: int | None
    status: CandidateStatus
    chat_peak: datetime
    chat_end: datetime
    score: float


@dataclass(frozen=True, slots=True)
class CandidateDraft:
    """A `candidates` row before its insert (no id, media request or timestamps yet)."""

    stream_id: int
    channel_id: int
    source: CandidateSource
    status: CandidateStatus
    suppress_reason: SuppressReason | None
    trigger: str
    peak_utc: datetime
    chat_peak_utc: datetime | None
    chat_delay_s: float
    delay_source: DelaySource
    moment_from: datetime
    moment_to: datetime
    window_from: datetime
    window_to: datetime
    score: float
    moment_type: MomentType
    features: dict[str, Any]
    detector_version: str
    chat_end_utc: datetime | None = None  # not a column: the episode end (cooldown)

    @property
    def emitted(self) -> bool:
        return self.status is CandidateStatus.EMITTED

    @property
    def window(self) -> Interval:
        return Interval(self.window_from, self.window_to)

    def row(self) -> dict[str, Any]:
        """Column values for the insert into `candidates`."""
        return {
            "stream_id": self.stream_id,
            "channel_id": self.channel_id,
            "source": self.source.value,
            "status": self.status.value,
            "suppress_reason": self.suppress_reason.value if self.suppress_reason else None,
            "trigger": self.trigger,
            "peak_utc": self.peak_utc,
            "chat_peak_utc": self.chat_peak_utc,
            "chat_delay_s": self.chat_delay_s,
            "delay_source": self.delay_source.value,
            "moment_from": self.moment_from,
            "moment_to": self.moment_to,
            "window_from": self.window_from,
            "window_to": self.window_to,
            "score": self.score,
            "moment_type": self.moment_type.value,
            "features": self.features,
            "detector_version": self.detector_version,
        }

    def as_prior(self, candidate_id: int | None) -> PriorCandidate:
        return PriorCandidate(
            candidate_id,
            self.status,
            self.chat_peak_utc or self.peak_utc,
            self.chat_end_utc or self.moment_to,
            self.score,
        )


@dataclass(frozen=True, slots=True)
class FoldIn:
    """No new candidate: a weaker follow-up within the cooldown of `into`. `extended` is `into`
    with its chat end moved to the follow-up's end: it replaces `into` in the caller's `recent`."""

    into: PriorCandidate
    extended: PriorCandidate
    followup: dict[str, Any] = field(default_factory=dict)


Decision = CandidateDraft | FoldIn


@dataclass(frozen=True, slots=True)
class Times:
    peak_utc: datetime
    moment_from: datetime
    moment_to: datetime
    window_from: datetime
    window_to: datetime


def candidate_times(
    chat_peak: datetime,
    chat_onset: datetime,
    chat_end: datetime,
    delay: Delay,
    params: DetectorParams,
) -> Times:
    """Video-time peak, moment and media window of a chat episode (§18.5, §7)."""
    p = params
    d = delay.td
    peak = chat_peak - d
    moment_from, moment_to = chat_onset - d, max(chat_end, chat_peak) - d
    core_from = peak - timedelta(seconds=p.window_before_s)
    core_to = peak + timedelta(seconds=p.window_after_s)
    window_from = min(core_from, moment_from - timedelta(seconds=p.window_onset_pad_s))
    window_to = max(core_to, moment_to + timedelta(seconds=p.window_close_pad_s))
    left = (core_from - window_from).total_seconds()
    right = (window_to - core_to).total_seconds()
    room = p.window_max_s - p.window_before_s - p.window_after_s
    excess = left + right - room
    if excess > 0:
        if left >= right:
            cut = min(excess, left - right)
            left, excess = left - cut, excess - cut
        else:
            cut = min(excess, right - left)
            right, excess = right - cut, excess - cut
        left, right = left - excess / 2, right - excess / 2
        window_from = core_from - timedelta(seconds=left)
        window_to = core_to + timedelta(seconds=right)
    moment_from = min(max(moment_from, window_from), peak)
    moment_to = max(min(moment_to, window_to), peak)
    return Times(peak, moment_from, moment_to, window_from, window_to)


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 3)


def _snapshot(s: Snapshot) -> dict[str, Any]:
    return {
        "ts": s.ts.isoformat(),
        "activity": s.activity,
        "baseline": _r(s.baseline),
        "own_baseline": _r(s.own_baseline),
        "prior_baseline": _r(s.prior_baseline),
        "scale": _r(s.scale),
        "z": _r(s.z),
        "rho": _r(s.rho),
        "viewers": s.viewers,
        "viewer_ratio": _r(s.viewer_ratio),
    }


def episode_features(episode: Episode) -> dict[str, Any]:
    s = episode.strongest
    return {
        "z": _r(s.z),
        "rho": _r(s.rho),
        "baseline": _r(s.baseline),
        "uniques": s.activity,
        "viewers": s.viewers,
        "viewer_ratio": _r(s.viewer_ratio),
        "chat": {
            "opened_at": episode.opened_at.isoformat(),
            "onset": episode.onset.isoformat(),
            "peak": episode.peak.isoformat(),
            "end": episode.end.isoformat(),
            "duration_s": episode.duration_s,
            "peak_uniques": _r(episode.peak_uniques),
            "base_per_s": _r(episode.base_per_s),
            "forced": episode.forced,
            "truncated": episode.truncated,
            "warmup": episode.warmup,
            "prior_used": episode.prior_used,
        },
        "at_open": _snapshot(episode.at_open),
        "strongest": _snapshot(s),
    }


def plan_candidate(
    episode: Episode,
    stage2: Stage2Result,
    *,
    stream_id: int,
    channel_id: int,
    delay: Delay,
    recent: Sequence[PriorCandidate],
    params: DetectorParams,
    detector_version: str,
    source: CandidateSource = CandidateSource.LIVE,
    trigger: str = "chat",
    extra_features: dict[str, Any] | None = None,
) -> Decision:
    """The decision for one closed episode. `recent`: this stream's candidates decided so far
    (at least the last `budget_window_s` + `cooldown_s`, any order)."""
    p = params
    status, reason = stage2.status, stage2.reason
    features: dict[str, Any] = {
        **episode_features(episode),
        **stage2.features(),
        "delay": {"seconds": delay.seconds, "source": delay.source.value},
        **(extra_features or {}),
    }

    if status is CandidateStatus.EMITTED:
        emitted = [c for c in recent if c.status is CandidateStatus.EMITTED]
        cooldown = timedelta(seconds=p.cooldown_s)
        before = [
            c
            for c in emitted
            if c.chat_peak < episode.peak and episode.opened_at - c.chat_end < cooldown
        ]
        if before:
            prev = max(before, key=lambda c: (c.chat_end, c.chat_peak))
            if stage2.score <= prev.score:
                return FoldIn(
                    prev,
                    replace(prev, chat_end=max(prev.chat_end, episode.end)),
                    {
                        "opened_at": episode.opened_at.isoformat(),
                        "peak": episode.peak.isoformat(),
                        "end": episode.end.isoformat(),
                        "score": round(stage2.score, 3),
                        "z": _r(episode.strongest.z),
                        "moment_type": stage2.moment_type.value,
                    },
                )
            features["prev_candidate_id"] = prev.candidate_id
        hour = timedelta(seconds=p.budget_window_s)
        in_hour = [c for c in emitted if episode.peak - hour < c.chat_peak <= episode.peak]
        lowest = min((c.score for c in in_hour), default=0.0)
        if len(in_hour) >= p.budget_per_hour and stage2.score <= lowest:
            status, reason = CandidateStatus.SUPPRESSED, SuppressReason.BUDGET
            features["budget"] = {"in_window": len(in_hour), "lowest": round(lowest, 3)}

    times = candidate_times(episode.peak, episode.onset, episode.end, delay, p)
    return CandidateDraft(
        stream_id=stream_id,
        channel_id=channel_id,
        source=source,
        status=status,
        suppress_reason=reason,
        trigger=trigger,
        peak_utc=times.peak_utc,
        chat_peak_utc=episode.peak,
        chat_delay_s=delay.seconds,
        delay_source=delay.source,
        moment_from=times.moment_from,
        moment_to=times.moment_to,
        window_from=times.window_from,
        window_to=times.window_to,
        score=stage2.score,
        moment_type=stage2.moment_type,
        features=features,
        detector_version=detector_version,
        chat_end_utc=episode.end,
    )
