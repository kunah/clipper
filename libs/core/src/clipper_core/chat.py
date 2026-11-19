"""Chat rows, per-second chat stats and their batch writer, shared by live and backfill.

chat-ingest (live IRC) and the backfill tool (VOD chat replay) map their sources to the same
rows and aggregate them with the same `SecondAggregator`, so `chat_metrics_1s` means the same
for both. Each writes only its own streams' rows (one writer per table and source, DESIGN §17).

Per-second stats: buckets are keyed by the message's server time (`tmi-sent-ts` live, the
replay's `createdAt` for backfill) truncated to the second, and flushed once `grace` has passed,
to wait for slightly late messages. Anything arriving after its second was flushed is still
stored raw, just not counted (tracked in `late_dropped`). A second with no row means 0 messages.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from clipper_core.models import ChatEvent, ChatMessage, ChatMetricSecond
from clipper_core.partitions import ensure_chat_partitions
from clipper_core.timeline import utc_now

# --------------------------------------------------------------------------- rows


@dataclass(slots=True)
class MessageRow:
    """One `chat_messages` row."""

    sent_at: datetime
    msg_id: str
    channel_id: int
    stream_id: int | None
    received_at: datetime | None  # None for backfill (we didn't receive it live)
    user_id: str | None
    user_login: str | None
    text: str
    emotes: list[dict[str, Any]] | None
    badges: dict[str, str] | None
    deleted: bool = False
    source: str = "live"


@dataclass(slots=True)
class EventRow:
    """One `chat_events` row (live only: chat replay has no events)."""

    channel_id: int
    stream_id: int | None
    sent_at: datetime
    type: str
    msg_id: str | None = None
    user_login: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MetricRow:
    """One `chat_metrics_1s` row."""

    channel_id: int
    ts: datetime
    stream_id: int | None
    messages: int
    unique_chatters: int
    emote_counts: dict[str, int]
    event_counts: dict[str, int]


# --------------------------------------------------------------------------- per-second stats


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


# --------------------------------------------------------------------------- writer


class ChatWriter:
    """Batch writes of chat_messages / chat_events / chat_metrics_1s.

    Messages: `ON CONFLICT DO NOTHING` (PK `sent_at, msg_id`), so re-writing a batch is safe.
    Metrics: upserted per `(channel_id, ts)`. Events: plain inserts. Creates the chat partitions
    of the days it writes (remembered per writer instance).
    """

    def __init__(self) -> None:
        self._partition_days: set[date] = set()

    async def write(
        self,
        session: AsyncSession,
        messages: list[MessageRow],
        events: list[EventRow],
        metrics: list[MetricRow],
        deleted_msg_ids: list[str],
    ) -> None:
        """Everything in one transaction (the caller commits)."""
        if messages:
            await self._ensure_partitions(session, {m.sent_at.date() for m in messages})
            await session.execute(
                insert(ChatMessage).on_conflict_do_nothing(), [asdict(m) for m in messages]
            )
        if events:
            await session.execute(insert(ChatEvent), [asdict(e) for e in events])
        if metrics:
            stmt = insert(ChatMetricSecond)
            stmt = stmt.on_conflict_do_update(
                index_elements=["channel_id", "ts"],
                set_={
                    "stream_id": stmt.excluded.stream_id,
                    "messages": stmt.excluded.messages,
                    "unique_chatters": stmt.excluded.unique_chatters,
                    "emote_counts": stmt.excluded.emote_counts,
                    "event_counts": stmt.excluded.event_counts,
                },
            )
            await session.execute(stmt, [asdict(m) for m in metrics])
        if deleted_msg_ids:
            # Deletions refer to recent messages; the time bound keeps it to 1-2 partitions.
            newest = min((m.sent_at for m in messages), default=utc_now())
            await session.execute(
                text(
                    "UPDATE chat_messages SET deleted = true "
                    "WHERE msg_id = ANY(:ids) AND sent_at >= :since"
                ),
                {"ids": deleted_msg_ids, "since": newest - timedelta(days=1)},
            )

    async def _ensure_partitions(self, session: AsyncSession, days: set[date]) -> None:
        missing = days - self._partition_days
        if missing:
            await ensure_chat_partitions(session, min(missing), max(missing))
            self._partition_days |= missing
