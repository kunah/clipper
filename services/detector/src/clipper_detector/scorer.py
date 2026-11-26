"""Stage 1: per-second chat stats -> suspects and episodes (DESIGN §18.1).

`ChannelScorer.step(second)` is a pure, clock-free state machine: time comes only from the rows,
so the live loop, the offline pass and the backtest give the same output for the same rows.

Per second t (chat time, UTC):
- activity `A(t)` = sum of `unique_chatters` over the last `activity_window_s` (10) seconds;
- baseline `B(t)` = median of A over `[t - 20 min, t - 60 s]` (own history), scale
  `S = max(1.4826 * MAD, sqrt(B'), 2)`;
- viewers normalise (never trigger): `B' = B * clamp(V_now / V_median, 0.67, 2)` with `V_median`
  over the same window; no viewer data (None, e.g. a backfilled stream) = no scaling;
- `z = (A - B') / S`, `rho = A / max(B', 3)`, floor `A >= max(8, 0.005 * V_now)`;
- an episode opens at `z >= 4 and rho >= 2 and floor` (warm-up: `z >= 6 and rho >= 3`), stays open
  while `z >= 2`, closes after 10 s below (bursts < 10 s apart merge) and force-closes at 180 s;
- a *suspect* (`z >= 3 and rho >= 1.5 and floor`, re-armed once z < 2) lets the live service
  start stage 2 early.

Time of stream: warm-up for the first 10 min after `started_at` and 5 min after a room-mode
toggle (`Second.toggles`, from `event_counts["roomstate"]`); until 20 min of in-stream history
exist `B' = max(own, prior)`. Seconds before `started_at` are neither scored nor part of the
baseline (offline chat is another regime). Missing chat: an in-stream zero run >= 60 s while
B >= 5 is missing data, kept out of the baseline (also every A whose window touches it).

Decisions beyond §18 (see DESIGN §18 build item 2): no scoring while the own baseline holds fewer
than `min_baseline_values` (300) values, prior or not: the prior only raises the bar (a prior
from a quieter stream would otherwise make every early second a spike); viewer scaling applies to
the own baseline and its MAD, the prior has its own ratio (vs. the previous stream's median
viewers, same clamp), so the two aren't scaled twice.

Seconds must come in order; skipped seconds count as zero (no row = 0), viewers carried forward.
`advance(until)` steps empty seconds (the live loop, when no rows arrived), `flush()` ends the
data (offline/backtest) and closes an open episode.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right, insort
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NamedTuple

from clipper_core.chat import MetricRow
from clipper_core.timeline import ensure_utc
from clipper_detector.params import DetectorParams

MAD_TO_SIGMA = 1.4826


@dataclass(frozen=True, slots=True)
class Second:
    """One second of a channel's chat stats (a `chat_metrics_1s` row or an empty second)."""

    ts: datetime  # UTC start of the second (chat time)
    unique_chatters: int = 0
    messages: int = 0
    viewers: int | None = None  # latest viewer count at ts; None = unknown
    toggles: int = 0  # room-mode changes (roomstate events) in this second

    @classmethod
    def from_metric(cls, row: MetricRow, viewers: int | None = None) -> Second:
        events = row.event_counts or {}
        return cls(
            row.ts, row.unique_chatters, row.messages, viewers, int(events.get("roomstate", 0))
        )


@dataclass(frozen=True, slots=True)
class PriorBaseline:
    """The channel's previous stream: the start-of-stream floor of the baseline."""

    activity: float  # median A of the previous stream
    viewers: float | None = None  # its median viewer count; None = unknown (no scaling)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What the scorer saw at one second."""

    ts: datetime
    activity: int  # A
    baseline: float  # B' (A is compared with this)
    own_baseline: float  # median of own history, before viewer scaling
    prior_baseline: float | None  # the scaled prior, while it was in use
    scale: float  # S
    z: float
    rho: float
    viewers: int | None
    viewer_ratio: float | None  # clamped V_now / V_median; None = unknown, no scaling


@dataclass(frozen=True, slots=True)
class Suspect:
    ts: datetime
    snapshot: Snapshot


@dataclass(frozen=True, slots=True)
class Episode:
    """A closed chat episode (all chat times, UTC)."""

    opened_at: datetime  # first second meeting the open condition
    onset: datetime  # first second the smoothed uniques reach a quarter of the rise
    peak: datetime  # argmax of the centred mean of unique_chatters
    end: datetime  # last second with z >= z_stay (the force-close second when forced)
    decided_at: datetime  # second at which the scorer closed it
    forced: bool  # force-closed at force_close_s
    truncated: bool  # closed by flush() (end of data)
    warmup: bool  # opened under the warm-up thresholds
    prior_used: bool  # the prior was part of the baseline at the open
    peak_uniques: float  # smoothed unique chatters per second at the peak
    base_per_s: float  # B' / activity_window_s at the open (the per-second level risen from)
    at_open: Snapshot
    strongest: Snapshot  # the second with the highest z

    @property
    def duration_s(self) -> int:
        return int((self.end - self.opened_at).total_seconds()) + 1


@dataclass(frozen=True, slots=True)
class MissingChat:
    """A run of seconds without chat taken as missing data (not quiet chat)."""

    start: datetime
    end: datetime  # exclusive: the first second with chat again (or the end of data)


ScorerEvent = Suspect | Episode | MissingChat


# --------------------------------------------------------------------------- helpers


class _Rec:
    __slots__ = ("activity", "base_added", "missing", "t", "uniques", "viewers", "viewers_added")

    def __init__(self, t: int, uniques: int, viewers: int | None) -> None:
        self.t = t
        self.uniques = uniques
        self.viewers = viewers
        self.activity = 0
        self.missing = False
        self.base_added = False
        self.viewers_added = False


def _median(sorted_vals: Sequence[int]) -> float:
    n = len(sorted_vals)
    mid = n // 2
    return float(sorted_vals[mid]) if n % 2 else (sorted_vals[mid - 1] + sorted_vals[mid]) / 2


def _mad(sorted_vals: Sequence[int], med: float) -> float:
    """Median absolute deviation of integer values around `med` (an integer or x.5): the
    deviations live on a half-integer grid, so each order statistic is a binary search."""
    n = len(sorted_vals)
    m2 = round(med * 2)

    def kth(k: int) -> float:  # k-th smallest |x - med| (0-based)
        lo, hi = 0, 2 * (sorted_vals[-1] - sorted_vals[0]) + 2
        while lo < hi:
            d2 = (lo + hi) // 2  # 2 * deviation
            inside = bisect_right(sorted_vals, (m2 + d2) / 2) - bisect_left(
                sorted_vals, (m2 - d2) / 2
            )
            if inside >= k + 1:
                hi = d2
            else:
                lo = d2 + 1
        return lo / 2

    mid = n // 2
    return kth(mid) if n % 2 else (kth(mid - 1) + kth(mid)) / 2


def _remove(sorted_vals: list[int], value: int) -> None:
    del sorted_vals[bisect_left(sorted_vals, value)]


def _epoch(ts: datetime) -> int:
    return math.floor(ensure_utc(ts).timestamp())


def _dt(t: int) -> datetime:
    return datetime.fromtimestamp(t, tz=UTC)


class _Vals(NamedTuple):
    activity: int
    baseline: float
    own: float | None
    prior: float | None
    scale: float
    z: float
    rho: float
    viewers: int | None
    ratio: float | None
    floor: bool
    prior_used: bool


@dataclass(slots=True)
class _Open:
    opened_at: int
    last_above: int
    warmup: bool
    prior_used: bool
    at_open: Snapshot
    strongest: Snapshot


# --------------------------------------------------------------------------- scorer


class ChannelScorer:
    """Stage-1 state of one channel during one stream (in memory only; rebuilt by replay)."""

    def __init__(
        self,
        params: DetectorParams,
        *,
        started_at: datetime | None = None,
        prior: PriorBaseline | None = None,
    ) -> None:
        self.p = params
        self.prior = prior
        self._start = _epoch(started_at) if started_at is not None else None
        self._hist: deque[_Rec] = deque()
        self._base: list[int] = []  # sorted A values of the baseline window
        self._vbase: list[int] = []  # sorted viewer counts of the baseline window
        self._asum = 0
        self._last: int | None = None
        self._first: int | None = None
        self._last_viewers: int | None = None
        self._last_toggle: int | None = None
        self._zero_start: int | None = None
        self._zero_missing = False
        self._open: _Open | None = None
        self._armed = True
        p = params
        # seconds of history kept: the baseline window, an episode's peak search, A's window
        need = p.force_close_s + p.onset_lookback_s + p.close_after_s + p.peak_smooth_s + 2
        self._horizon = max(p.baseline_window_s, need, p.activity_window_s) + 1

    # ------------------------------------------------------------------ public

    @property
    def last_ts(self) -> datetime | None:
        return None if self._last is None else _dt(self._last)

    @property
    def in_episode(self) -> bool:
        return self._open is not None

    @property
    def missing(self) -> bool:
        """The current second belongs to a missing-chat run."""
        return self._zero_missing

    @property
    def baseline_values(self) -> int:
        return len(self._base)

    def step(self, second: Second) -> list[ScorerEvent]:
        t = _epoch(second.ts)
        if self._last is not None and t <= self._last:
            raise ValueError(f"seconds must be in order: {second.ts} after {_dt(self._last)}")
        events: list[ScorerEvent] = []
        if self._last is not None:
            for gap_t in range(self._last + 1, t):
                self._step(gap_t, 0, self._last_viewers, 0, events)
        self._step(t, max(0, second.unique_chatters), second.viewers, second.toggles, events)
        return events

    def advance(self, until: datetime) -> list[ScorerEvent]:
        """Step empty seconds up to `until` (exclusive): time passed without rows."""
        events: list[ScorerEvent] = []
        if self._last is None:
            return events
        for t in range(self._last + 1, _epoch(until)):
            self._step(t, 0, self._last_viewers, 0, events)
        return events

    def flush(self) -> list[ScorerEvent]:
        """End of data: close an open episode (truncated) and a missing run."""
        events: list[ScorerEvent] = []
        if self._last is None:
            return events
        if self._open is not None:
            events.append(self._close(self._open, self._open.last_above, self._last, False, True))
            self._open = None
        if self._zero_missing and self._zero_start is not None:
            events.append(MissingChat(_dt(self._zero_start), _dt(self._last + 1)))
            self._zero_start, self._zero_missing = None, False
        return events

    # ------------------------------------------------------------------ one second

    def _step(
        self, t: int, uniques: int, viewers: int | None, toggles: int, out: list[ScorerEvent]
    ) -> None:
        p = self.p
        if self._first is None:
            self._first = t
        self._last = t
        self._last_viewers = viewers
        if toggles > 0:
            self._last_toggle = t

        rec = _Rec(t, uniques, viewers)
        self._hist.append(rec)
        self._asum += uniques
        if len(self._hist) > p.activity_window_s:
            self._asum -= self._hist[-1 - p.activity_window_s].uniques
        rec.activity = self._asum
        self._expire(t)

        self._track_zero_run(t, uniques, out)
        self._feed_baseline(t)

        in_stream = self._start is None or t >= self._start
        vals = self._score(t, rec) if in_stream and not self._zero_missing else None
        self._advance_episode(t, vals, out)

    def _expire(self, t: int) -> None:
        old = self._rec_at(t - self.p.baseline_window_s - 1)  # just left the baseline window
        if old is not None:
            if old.base_added:
                _remove(self._base, old.activity)
                old.base_added = False
            if old.viewers_added and old.viewers is not None:
                _remove(self._vbase, old.viewers)
                old.viewers_added = False
        while self._hist and self._hist[0].t < t - self._horizon:
            self._hist.popleft()

    def _rec_at(self, t: int) -> _Rec | None:
        if not self._hist:
            return None
        idx = t - self._hist[0].t
        if 0 <= idx < len(self._hist):
            rec = self._hist[idx]
            if rec.t == t:
                return rec
        return None

    def _raw_level(self) -> float:
        """B before viewer scaling, for the missing-chat test."""
        if len(self._base) >= self.p.min_baseline_values:
            return _median(self._base)
        return self.prior.activity if self.prior is not None else 0.0

    def _track_zero_run(self, t: int, uniques: int, out: list[ScorerEvent]) -> None:
        p = self.p
        if self._start is not None and t < self._start:
            return  # offline chat: quiet stretches there are no missing data
        if uniques > 0:
            if self._zero_missing and self._zero_start is not None:
                out.append(MissingChat(_dt(self._zero_start), _dt(t)))
            self._zero_start, self._zero_missing = None, False
            return
        if self._zero_start is None:
            self._zero_start = t
        if self._zero_missing:
            self._hist[-1].missing = True
            return
        if t - self._zero_start + 1 >= p.missing_chat_s and (
            self._raw_level() >= p.missing_chat_min_baseline
        ):
            self._zero_missing = True
            # mark the run; drop A values already in the baseline whose window touches it
            for rec in reversed(self._hist):
                if rec.t < self._zero_start:
                    break
                rec.missing = True
                if rec.base_added:
                    _remove(self._base, rec.activity)
                    rec.base_added = False

    def _feed_baseline(self, t: int) -> None:
        p = self.p
        s = t - p.baseline_exclude_s
        rec = self._rec_at(s)
        if rec is None or (self._start is not None and s < self._start):
            return
        if rec.viewers is not None:
            insort(self._vbase, rec.viewers)
            rec.viewers_added = True
        for back in range(p.activity_window_s):
            r = self._rec_at(s - back)
            if r is not None and r.missing:
                return
        if self._start is not None and s - p.activity_window_s + 1 < self._start:
            return  # its activity window reaches back before the stream
        insort(self._base, rec.activity)
        rec.base_added = True

    def _score(self, t: int, rec: _Rec) -> _Vals | None:
        p = self.p
        if len(self._base) < p.min_baseline_values:
            return None
        own = _median(self._base)
        mad = _mad(self._base, own)
        v_now = rec.viewers
        ratio = None
        if v_now is not None and self._vbase:
            v_med = _median(self._vbase)
            if v_med > 0:
                ratio = min(max(v_now / v_med, p.viewer_ratio_min), p.viewer_ratio_max)
        prior_scaled = None
        first = self._first if self._first is not None else t
        history_start = first if self._start is None else max(first, self._start)
        if self.prior is not None and t - history_start < p.prior_until_s:
            prior_ratio = 1.0
            if v_now is not None and self.prior.viewers:
                prior_ratio = min(
                    max(v_now / self.prior.viewers, p.viewer_ratio_min), p.viewer_ratio_max
                )
            prior_scaled = self.prior.activity * prior_ratio
        r = ratio if ratio is not None else 1.0
        own_scaled = own * r
        baseline = own_scaled if prior_scaled is None else max(own_scaled, prior_scaled)
        scale = max(MAD_TO_SIGMA * mad * r, math.sqrt(max(baseline, 0.0)), p.scale_floor)
        a = rec.activity
        z = (a - baseline) / scale
        rho = a / max(baseline, p.ratio_min_baseline)
        floor = a >= max(p.floor_activity, p.floor_viewer_fraction * (v_now or 0))
        prior_used = prior_scaled is not None and prior_scaled >= own_scaled
        return _Vals(a, baseline, own, prior_scaled, scale, z, rho, v_now, ratio, floor, prior_used)

    @staticmethod
    def _snapshot(t: int, v: _Vals) -> Snapshot:
        return Snapshot(
            _dt(t), v.activity, v.baseline, v.own, v.prior, v.scale, v.z, v.rho, v.viewers, v.ratio
        )

    def _warm(self, t: int) -> bool:
        p = self.p
        if self._start is not None and t < self._start + p.warmup_stream_s:
            return True
        return self._last_toggle is not None and t < self._last_toggle + p.warmup_toggle_s

    def _advance_episode(self, t: int, vals: _Vals | None, out: list[ScorerEvent]) -> None:
        p = self.p
        z = vals.z if vals is not None else None
        if z is None or z < p.z_stay:
            self._armed = True

        ep = self._open
        if ep is not None:
            if z is not None and z >= p.z_stay:
                ep.last_above = t
                if z > ep.strongest.z:
                    ep.strongest = self._snapshot(t, vals)
            if t - ep.opened_at + 1 >= p.force_close_s:
                out.append(self._close(ep, t, t, True, False))
                self._open = None
            elif t - ep.last_above >= p.close_after_s:
                out.append(self._close(ep, ep.last_above, t, False, False))
                self._open = None
            return

        if vals is None or not vals.floor:
            return
        if self._armed and vals.z >= p.z_suspect and vals.rho >= p.rho_suspect:
            self._armed = False
            out.append(Suspect(_dt(t), self._snapshot(t, vals)))
        warm = self._warm(t)
        z_on, rho_on = (p.z_open_warmup, p.rho_open_warmup) if warm else (p.z_open, p.rho_open)
        if vals.z >= z_on and vals.rho >= rho_on:
            snap = self._snapshot(t, vals)
            self._armed = False
            self._open = _Open(t, t, warm, vals.prior_used, snap, snap)

    # ------------------------------------------------------------------ closing

    def _close(self, ep: _Open, end: int, decided: int, forced: bool, truncated: bool) -> Episode:
        p = self.p
        first = max(ep.opened_at - p.onset_lookback_s, self._hist[0].t)
        lo_off = (p.peak_smooth_s - 1) // 2
        hi_off = p.peak_smooth_s // 2

        def smooth(x: int) -> float:
            vals = [
                r.uniques
                for y in range(x - lo_off, x + hi_off + 1)
                if y <= self._last and (r := self._rec_at(y)) is not None
            ]
            return sum(vals) / len(vals) if vals else 0.0

        means = {x: smooth(x) for x in range(first, end + 1)}
        peak = max(means, key=lambda x: (means[x], -x))  # first maximum
        base_per_s = ep.at_open.baseline / p.activity_window_s
        rise = means[peak] - base_per_s
        onset = peak
        if rise > 0:
            bar = base_per_s + p.onset_fraction * rise
            onset = next(x for x in range(first, peak + 1) if means[x] >= bar)
        return Episode(
            opened_at=_dt(ep.opened_at),
            onset=_dt(onset),
            peak=_dt(peak),
            end=_dt(end),
            decided_at=_dt(decided),
            forced=forced,
            truncated=truncated,
            warmup=ep.warmup,
            prior_used=ep.prior_used,
            peak_uniques=means[peak],
            base_per_s=base_per_s,
            at_open=ep.at_open,
            strongest=ep.strongest,
        )
