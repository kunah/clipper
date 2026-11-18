"""Batch writes for chat-ingest (the only writer of chat_messages/chat_events/chat_metrics_1s)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from clipper_chat_ingest.aggregator import MetricRow
from clipper_chat_ingest.mapping import EventRow, MessageRow
from clipper_core.models import ChatEvent, ChatMessage, ChatMetricSecond
from clipper_core.partitions import ensure_chat_partitions
from clipper_core.timeline import utc_now


class ChatWriter:
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
