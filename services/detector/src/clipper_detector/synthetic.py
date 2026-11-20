"""Seeded synthetic chat for tests and experiments (no real data needed).

- `poisson_seconds`: per-second stats only (fast; scorer tests).
- `ChatSim`: messages and events of one channel, aggregated into per-second stats by the same
  `clipper_core.chat.SecondAggregator` that chat-ingest and the backfill tool use.
- `bump`: a raised-cosine burst shape (peak in the middle).
- `episode`: a hand-made stage-1 episode (stage 2 and merge tests).
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from clipper_core.chat import EventRow, MessageRow, SecondAggregator
from clipper_detector.scorer import Episode, Second, Snapshot

Rate = Callable[[int], float]


def poisson(rng: random.Random, lam: float) -> int:
    """A Poisson draw (Knuth below 30, a rounded normal above)."""
    if lam <= 0:
        return 0
    if lam >= 30:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    limit, k, prod = math.exp(-lam), 0, rng.random()
    while prod > limit:
        k += 1
        prod *= rng.random()
    return k


def bump(i: float, center: float, width: float, factor: float) -> float:
    """Multiplier 1 outside `[center - width/2, center + width/2]`, rising smoothly to `factor`
    at `center`."""
    half = width / 2
    if abs(i - center) >= half:
        return 1.0
    return 1.0 + (factor - 1.0) * 0.5 * (1.0 + math.cos(math.pi * (i - center) / half))


def poisson_seconds(
    rng: random.Random,
    start: datetime,
    duration_s: int,
    rate: Rate | float,
    *,
    viewers: Callable[[int], int | None] | None = None,
    toggles: Sequence[int] = (),
) -> list[Second]:
    """One `Second` per second; unique chatters ~ Poisson(rate(i))."""
    rate_fn = rate if callable(rate) else (lambda _i, r=float(rate): r)
    toggle_set = set(toggles)
    out = []
    for i in range(duration_s):
        u = poisson(rng, rate_fn(i))
        out.append(
            Second(
                start + timedelta(seconds=i),
                u,
                u,
                viewers(i) if viewers is not None else None,
                1 if i in toggle_set else 0,
            )
        )
    return out


@dataclass
class ChatSim:
    """Synthetic chat of one channel from `start` on (offsets in seconds)."""

    rng: random.Random
    start: datetime
    channel_id: int = 1
    stream_id: int | None = 1
    pool: int = 3000  # regular chatters user0..userN
    emote_names: frozenset[str] = frozenset()  # recognised emotes (go into `emotes`)
    messages: list[MessageRow] = field(default_factory=list)
    events: list[EventRow] = field(default_factory=list)
    _n: int = 0

    def at(self, offset_s: float) -> datetime:
        return self.start + timedelta(seconds=offset_s)

    def message(
        self, offset_s: float, text: str, user: str, *, deleted: bool = False
    ) -> MessageRow:
        self._n += 1
        counts: dict[str, int] = {}
        for token in text.split():
            if token in self.emote_names:
                counts[token] = counts.get(token, 0) + 1
        row = MessageRow(
            sent_at=self.at(offset_s),
            msg_id=f"m{self._n}",
            channel_id=self.channel_id,
            stream_id=self.stream_id,
            received_at=None,
            user_id=None,
            user_login=user,
            text=text,
            emotes=[{"name": k, "count": v} for k, v in counts.items()] or None,
            badges=None,
            deleted=deleted,
        )
        self.messages.append(row)
        return row

    def chatter(
        self,
        from_s: int,
        duration_s: int,
        rate: Rate | float,
        texts: Sequence[str] | Callable[[random.Random], str] = ("hello there", "nice", "ok"),
        *,
        users: Sequence[str] | None = None,
        deleted: float = 0.0,
    ) -> list[MessageRow]:
        """Messages at Poisson(rate(i)) per second (i relative to `from_s`), each from a random
        user of `users` (default: the pool) with a random text."""
        rate_fn = rate if callable(rate) else (lambda _i, r=float(rate): r)
        pick = texts if callable(texts) else (lambda rng, t=tuple(texts): rng.choice(t))
        rows = []
        for i in range(duration_s):
            for _ in range(poisson(self.rng, rate_fn(i))):
                user = self.rng.choice(users) if users else f"user{self.rng.randrange(self.pool)}"
                rows.append(
                    self.message(
                        from_s + i + self.rng.random(),
                        pick(self.rng),
                        user,
                        deleted=self.rng.random() < deleted,
                    )
                )
        return rows

    def event(
        self,
        offset_s: float,
        type_: str,
        *,
        user_login: str | None = None,
        msg_id: str | None = None,
        **data,
    ) -> EventRow:
        row = EventRow(
            channel_id=self.channel_id,
            stream_id=self.stream_id,
            sent_at=self.at(offset_s),
            type=type_,
            msg_id=msg_id,
            user_login=user_login,
            data=dict(data),
        )
        self.events.append(row)
        return row

    def seconds(self, *, viewers: Callable[[int], int | None] | None = None) -> list[Second]:
        """Per-second stats of everything so far (no row for an empty second, like the table)."""
        agg = SecondAggregator()
        for m in sorted(self.messages, key=lambda m: m.sent_at):
            agg.add_row(m)
        for e in self.events:
            agg.add_event(e.channel_id, e.stream_id, e.sent_at, e.type)
        out = []
        for row in agg.pop_all():
            offset = int((row.ts - self.start).total_seconds())
            out.append(Second.from_metric(row, viewers(offset) if viewers else None))
        return out


def episode(
    onset: datetime,
    *,
    peak_after_s: float = 4,
    end_after_s: float = 6,
    activity: int = 150,
    baseline: float = 50.0,
    scale: float = 7.0,
    viewers: int | None = None,
    warmup: bool = False,
) -> Episode:
    """An episode with onset, peak (onset + peak_after_s) and end (peak + end_after_s); its
    strongest second is the peak with the given A, B' and S."""
    peak = onset + timedelta(seconds=peak_after_s)
    end = peak + timedelta(seconds=end_after_s)
    snap = Snapshot(
        ts=peak,
        activity=activity,
        baseline=baseline,
        own_baseline=baseline,
        prior_baseline=None,
        scale=scale,
        z=(activity - baseline) / scale,
        rho=activity / max(baseline, 3.0),
        viewers=viewers,
        viewer_ratio=None,
    )
    return Episode(
        opened_at=min(onset + timedelta(seconds=2), peak),
        onset=onset,
        peak=peak,
        end=end,
        decided_at=end + timedelta(seconds=10),
        forced=False,
        truncated=False,
        warmup=warmup,
        prior_used=False,
        peak_uniques=activity / 10,
        base_per_s=baseline / 10,
        at_open=snap,
        strongest=snap,
    )
