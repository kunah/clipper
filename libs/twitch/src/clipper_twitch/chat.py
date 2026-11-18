"""Read-only Twitch chat client over WebSocket (anonymous, no credentials needed).

- One connection joins many channels; JOINs are rate-limited (Twitch: 20 per 10 s).
- Answers PING, reconnects with backoff on errors/RECONNECT/silence, re-joins everything.
- `set_channels()` can be called any time; the client joins/parts the difference.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from clipper_core.logs import get_logger
from clipper_core.timeline import utc_now
from clipper_twitch.irc import IrcMessage, parse

log = get_logger(__name__)

IRC_URL = "wss://irc-ws.chat.twitch.tv:443"
CAPABILITIES = "twitch.tv/tags twitch.tv/commands"

OnMessage = Callable[[IrcMessage, datetime], Awaitable[None] | None]


class JoinRateLimiter:
    def __init__(self, limit: int = 20, period_s: float = 10.0) -> None:
        self.limit, self.period_s = limit, period_s
        self._sent: deque[float] = deque()

    async def acquire(self) -> None:
        while True:
            now = time.monotonic()
            while self._sent and now - self._sent[0] >= self.period_s:
                self._sent.popleft()
            if len(self._sent) < self.limit:
                self._sent.append(now)
                return
            await asyncio.sleep(self.period_s - (now - self._sent[0]) + 0.05)


class TwitchChatClient:
    def __init__(
        self,
        on_message: OnMessage,
        *,
        url: str = IRC_URL,
        nick: str | None = None,
        idle_timeout_s: float = 360.0,
        join_limit: tuple[int, float] = (20, 10.0),
    ) -> None:
        self._on_message = on_message
        self._url = url
        self._nick = nick or f"justinfan{random.randint(10000, 99999)}"
        self._idle_timeout_s = idle_timeout_s
        self._limiter = JoinRateLimiter(*join_limit)
        self._desired: set[str] = set()
        self._joined: set[str] = set()
        self._membership_changed = asyncio.Event()
        self.connected = False

    @property
    def joined(self) -> frozenset[str]:
        return frozenset(self._joined)

    def set_channels(self, logins: Iterable[str]) -> None:
        desired = {login.lower().lstrip("#") for login in logins}
        if desired != self._desired:
            self._desired = desired
            self._membership_changed.set()

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            started = time.monotonic()
            try:
                await self._session(stop)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("chat connection lost", error=f"{type(exc).__name__}: {exc}")
            finally:
                self.connected = False
                self._joined.clear()
            if stop.is_set():
                break
            if time.monotonic() - started > 60:
                backoff = 1.0
            log.info("chat reconnecting", in_s=round(backoff, 1))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            backoff = min(backoff * 2, 60.0)

    async def _session(self, stop: asyncio.Event) -> None:
        async with connect(self._url, ping_interval=60, max_size=2**22) as ws:
            await ws.send(f"CAP REQ :{CAPABILITIES}")
            await ws.send("PASS SCHMOOPIIE")  # any password works for anonymous login
            await ws.send(f"NICK {self._nick}")
            self.connected = True
            log.info("chat connected", nick=self._nick)
            self._membership_changed.set()
            membership = asyncio.create_task(self._sync_membership(ws, stop))
            stopper = asyncio.create_task(stop.wait())
            try:
                while not stop.is_set():
                    recv = asyncio.create_task(ws.recv())
                    done, _ = await asyncio.wait(
                        {recv, stopper, membership},
                        timeout=self._idle_timeout_s,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if membership in done:
                        recv.cancel()
                        membership.result()  # re-raise its error
                    if recv not in done:
                        recv.cancel()
                        if stop.is_set():
                            return
                        raise TimeoutError(f"no data from chat for {self._idle_timeout_s}s")
                    frame = recv.result()
                    received_at = utc_now()
                    if isinstance(frame, bytes):
                        frame = frame.decode("utf-8", "replace")
                    for line in frame.split("\r\n"):
                        if line and await self._handle(ws, line, received_at):
                            return  # server asked us to reconnect
            finally:
                for task in (membership, stopper):
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError, ConnectionClosed, Exception):
                    await membership

    async def _handle(self, ws, line: str, received_at: datetime) -> bool:
        try:
            msg = parse(line)
        except ValueError:
            log.warning("unparseable chat line", line=line[:500])
            return False
        if msg.command == "PING":
            await ws.send(f"PONG :{msg.text or (msg.params[0] if msg.params else 'tmi.twitch.tv')}")
            return False
        if msg.command == "RECONNECT":
            log.info("chat server requested reconnect")
            return True
        if msg.command == "JOIN" and msg.nick == self._nick and msg.channel:
            self._joined.add(msg.channel)
        elif msg.command == "PART" and msg.nick == self._nick and msg.channel:
            self._joined.discard(msg.channel)
        result = self._on_message(msg, received_at)
        if asyncio.iscoroutine(result):
            await result
        return False

    async def _sync_membership(self, ws, stop: asyncio.Event) -> None:
        requested: set[str] = set()  # sent JOIN (confirmation arrives as a JOIN echo)
        while not stop.is_set():
            await self._membership_changed.wait()
            self._membership_changed.clear()
            to_join = sorted(self._desired - requested)
            to_part = sorted(requested - self._desired)
            for login in to_part:
                await ws.send(f"PART #{login}")
                requested.discard(login)
            for login in to_join:
                if login not in self._desired:
                    continue
                await self._limiter.acquire()
                await ws.send(f"JOIN #{login}")
                requested.add(login)
            if to_join or to_part:
                log.info(
                    "chat membership updated",
                    joined=len(to_join),
                    parted=len(to_part),
                    channels=len(requested),
                )
