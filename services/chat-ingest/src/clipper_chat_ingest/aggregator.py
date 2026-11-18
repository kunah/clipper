"""Per-second chat stats per channel -> chat_metrics_1s (what the live detector reads).

Buckets are keyed by the message's server time (tmi-sent-ts) truncated to the second, and
flushed once `grace` has passed, to wait for slightly late messages. Anything arriving after its
second was flushed is still stored raw, just not counted (tracked in `late_dropped`).
A second with no row means 0 messages.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(slots=True)
class MetricRow:
    channel_id: int
    ts: datetime
    stream_id: int | None
    messages: int
    unique_chatters: int
    emote_counts: dict[str, int]
    event_counts: dict[str, int]


@dataclass(slots=True)
class _Bucket:
    stream_id: int | None = None
    messages: int = 0
    chatters: set[str] = field(default_factory=set)
    emotes: Counter[str] = field(default_factory=Counter)
    events: Counter[str] = field(default_factory=Counter)


def second_of(ts: datetime) -> datetime:
    return ts.replace(microsecond=0)


class SecondAggregator:
    def __init__(self, grace: timedelta = timedelta(seconds=5)) -> None:
        self.grace = grace
        self.late_dropped = 0
        self._buckets: dict[tuple[int, datetime], _Bucket] = {}
        self._flushed_until: dict[int, datetime] = {}  # per channel: seconds < this are flushed

    def _bucket(self, channel_id: int, ts: datetime, stream_id: int | None) -> _Bucket | None:
        sec = second_of(ts)
        if sec < self._flushed_until.get(channel_id, datetime.min.replace(tzinfo=sec.tzinfo)):
            self.late_dropped += 1
            return None
        bucket = self._buckets.setdefault((channel_id, sec), _Bucket())
        if stream_id is not None:
            bucket.stream_id = stream_id
        return bucket

    def add_message(
        self,
        channel_id: int,
        stream_id: int | None,
        sent_at: datetime,
        user_key: str | None,
        emotes: Iterable[tuple[str, int]] = (),
    ) -> None:
        bucket = self._bucket(channel_id, sent_at, stream_id)
        if bucket is None:
            return
        bucket.messages += 1
        if user_key:
            bucket.chatters.add(user_key)
        for name, count in emotes:
            bucket.emotes[name] += count

    def add_event(
        self, channel_id: int, stream_id: int | None, sent_at: datetime, kind: str
    ) -> None:
        bucket = self._bucket(channel_id, sent_at, stream_id)
        if bucket is not None:
            bucket.events[kind] += 1

    def pop_ready(self, now: datetime) -> list[MetricRow]:
        cutoff = second_of(now - self.grace)
        return self._pop(lambda sec: sec < cutoff, cutoff)

    def pop_all(self) -> list[MetricRow]:
        return self._pop(lambda sec: True, None)

    def _pop(self, ready, cutoff: datetime | None) -> list[MetricRow]:
        rows: list[MetricRow] = []
        for key in sorted(k for k in self._buckets if ready(k[1])):
            channel_id, sec = key
            b = self._buckets.pop(key)
            rows.append(
                MetricRow(
                    channel_id,
                    sec,
                    b.stream_id,
                    b.messages,
                    len(b.chatters),
                    dict(b.emotes),
                    dict(b.events),
                )
            )
        if cutoff is not None:
            for channel_id in {r.channel_id for r in rows} | set(self._flushed_until):
                self._flushed_until[channel_id] = max(
                    self._flushed_until.get(channel_id, cutoff), cutoff
                )
        return rows

    @property
    def pending(self) -> int:
        return len(self._buckets)
