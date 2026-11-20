"""chat-ingest: one chat connection for all active channels, buffered writes every second.

Loops:
- sync (30 s): active channels + their live stream from the DB -> join/part, stream_id
- flush (1 s): messages, events, per-second stats, deletions in one transaction
- emotes: global third-party emotes every 6 h, per channel every hour (needs twitch id)
- heartbeat (15 s)
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_chat_ingest.mapping import ChannelState, map_message
from clipper_core import heartbeat
from clipper_core.chat import ChatWriter, EventRow, MessageRow, SecondAggregator
from clipper_core.logs import get_logger
from clipper_core.timeline import utc_now
from clipper_twitch.emotes import fetch_channel_emotes, fetch_global_emotes
from clipper_twitch.irc import IrcMessage

log = get_logger(__name__)

MAX_BUFFERED = 200_000  # safety valve if the DB is down for a long time


class ChatIngest:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        worker_id: str,
        http: httpx.AsyncClient | None = None,
        sync_interval_s: float = 30.0,
        flush_interval_s: float = 1.0,
        fetch_emotes: bool = True,
    ) -> None:
        self.sessions = sessions
        self.worker_id = worker_id
        self.http = http
        self.sync_interval_s = sync_interval_s
        self.flush_interval_s = flush_interval_s
        self.fetch_emotes = fetch_emotes
        self.channels: dict[str, ChannelState] = {}  # login -> state
        self.global_emotes: dict[str, str] = {}
        self.aggregator = SecondAggregator()
        self.writer = ChatWriter()
        self.messages: list[MessageRow] = []
        self.events: list[EventRow] = []
        self.deleted: list[str] = []
        self.stats = {"messages": 0, "events": 0, "flush_errors": 0, "dropped": 0}
        self._emote_fetched: dict[str, float] = {}
        self._started_at = utc_now()

    # ------------------------------------------------------------------ inbound

    def on_message(self, msg: IrcMessage, received_at: datetime) -> None:
        """Called for every chat line. Must stay fast: only buffers."""
        channel = self.channels.get(msg.channel or "")
        if channel is None:
            if msg.command == "NOTICE" and msg.text:
                log.warning(
                    "chat notice",
                    channel=msg.channel,
                    notice=msg.text,
                    notice_id=msg.tags.get("msg-id"),
                )
            return
        mapped = map_message(msg, received_at, channel, self.global_emotes)
        if mapped.room_id and mapped.room_id != channel.twitch_id:
            channel.twitch_id = mapped.room_id
        if mapped.message:
            m = mapped.message
            self.messages.append(m)
            self.stats["messages"] += 1
            self.aggregator.add_row(m)  # channel_id/stream_id: map_message took them from `channel`
        for event in mapped.events:
            self.events.append(event)
            self.stats["events"] += 1
            self.aggregator.add_event(
                channel.channel_id, channel.stream_id, event.sent_at, event.type
            )
        if mapped.delete_msg_id:
            self.deleted.append(mapped.delete_msg_id)

    # ------------------------------------------------------------------ loops

    async def sync_channels(self) -> set[str]:
        """Reload active channels and live streams; returns the logins to be joined."""
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT c.id, c.login, c.platform_user_id, s.id AS stream_id
                          FROM channels c
                          LEFT JOIN LATERAL (
                                SELECT id FROM streams
                                 WHERE channel_id = c.id AND status = 'live'
                                 ORDER BY started_at DESC LIMIT 1) s ON true
                         WHERE c.active AND c.platform = 'twitch'
                        """
                    )
                )
            ).all()
        wanted = {}
        for row in rows:
            state = self.channels.get(row.login) or ChannelState(row.id, row.login)
            state.channel_id = row.id
            state.stream_id = row.stream_id
            state.twitch_id = row.platform_user_id or state.twitch_id
            wanted[row.login] = state
        for login in set(self.channels) - set(wanted):
            log.info("channel removed", channel=login)
        for login in set(wanted) - set(self.channels):
            log.info("channel added", channel=login)
        self.channels = wanted
        return set(wanted)

    async def refresh_emotes(self, now_mono: float | None = None) -> None:
        if not self.fetch_emotes or self.http is None:
            return
        now_mono = now_mono or time.monotonic()
        if now_mono - self._emote_fetched.get("", -1e9) > 6 * 3600:
            self.global_emotes = await fetch_global_emotes(self.http)
            self._emote_fetched[""] = now_mono
            log.info("global emotes loaded", count=len(self.global_emotes))
        due = [
            s
            for s in self.channels.values()
            if s.twitch_id and now_mono - self._emote_fetched.get(s.login, -1e9) > 3600
        ]
        limit = asyncio.Semaphore(5)

        async def load(state: ChannelState) -> None:
            async with limit:
                state.emotes = await fetch_channel_emotes(self.http, state.twitch_id)
            self._emote_fetched[state.login] = now_mono
            log.info("channel emotes loaded", channel=state.login, count=len(state.emotes))

        await asyncio.gather(*(load(s) for s in due))

    async def flush(self, final: bool = False) -> None:
        now = utc_now()
        metrics = self.aggregator.pop_all() if final else self.aggregator.pop_ready(now)
        messages, events, deleted = self.messages, self.events, self.deleted
        if not (messages or events or metrics or deleted):
            return
        self.messages, self.events, self.deleted = [], [], []
        try:
            async with self.sessions.begin() as session:
                await self.writer.write(session, messages, events, metrics, deleted)
        except Exception:
            self.stats["flush_errors"] += 1
            log.exception(
                "chat flush failed, will retry",
                messages=len(messages),
                events=len(events),
                metrics=len(metrics),
            )
            # Put rows back (metrics for those seconds are lost; raw rows are kept).
            self.messages = messages + self.messages
            self.events = events + self.events
            self.deleted = deleted + self.deleted
            overflow = len(self.messages) - MAX_BUFFERED
            if overflow > 0:
                self.stats["dropped"] += overflow
                del self.messages[:overflow]
                log.error("chat buffer full, dropped oldest messages", dropped=overflow)

    async def run(self, stop: asyncio.Event, client_factory: Callable[..., object]) -> None:
        client = client_factory(self.on_message)
        client.set_channels(await self.sync_channels())
        tasks = [
            asyncio.create_task(client.run(stop), name="chat-client"),
            asyncio.create_task(
                self._every(stop, self.sync_interval_s, lambda: self._sync(client)), name="sync"
            ),
            asyncio.create_task(self._every(stop, self.flush_interval_s, self.flush), name="flush"),
            # every 20 s so channels get their emote sets soon after ROOMSTATE gives their id
            asyncio.create_task(self._every(stop, 20.0, self.refresh_emotes), name="emotes"),
            asyncio.create_task(self._every(stop, 15.0, lambda: self._beat(client)), name="beat"),
        ]
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            await self.flush(final=True)
            log.info("chat-ingest stopped", **self.stats)

    async def _sync(self, client) -> None:
        client.set_channels(await self.sync_channels())

    async def _beat(self, client) -> None:
        async with self.sessions.begin() as session:
            await heartbeat.beat(
                session,
                worker_id=self.worker_id,
                service="chat-ingest",
                started_at=self._started_at,
                info={
                    "connected": getattr(client, "connected", None),
                    "channels": len(self.channels),
                    "joined": len(getattr(client, "joined", ())),
                    "late_dropped": self.aggregator.late_dropped,
                    "buffered": len(self.messages),
                    **self.stats,
                },
            )

    @staticmethod
    async def _every(
        stop: asyncio.Event, interval_s: float, fn: Callable[[], Awaitable[None]]
    ) -> None:
        while not stop.is_set():
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("periodic task failed", task=getattr(fn, "__name__", "?"))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
