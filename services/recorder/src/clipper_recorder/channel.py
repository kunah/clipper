"""Records one channel's live stream: follow the media playlist, write segments into chunks.

Loop: resolve the playlist URL -> reload it every segment duration -> download new live segments
in order -> append to the open chunk -> close the chunk at `chunk_seconds`, on a gap, on an ad, or
when nothing arrives for `idle_close_s` -> hand closed chunks to the finisher.
Playlist errors: 3 failures in a row -> resolve again; offline -> retry every `offline_retry_s`
until the controller ends the stream (the service then stops this recorder).
fMP4 streams: each chunk file starts with the init section (EXT-X-MAP) its fragments belong to;
a different init section starts a new chunk.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx

from clipper_core.contracts import GapReason, RecordingMode
from clipper_core.logs import get_logger, log_context
from clipper_recorder.spool import ChunkState, ChunkWriter, GapState, Spool
from clipper_recorder.tracker import Gap, SegmentTracker
from clipper_twitch.hls import MediaPlaylist, Segment, parse_media_playlist

log = get_logger(__name__)


class Resolver(Protocol):
    async def resolve(self, login: str, mode: RecordingMode) -> str | None: ...


@dataclass(frozen=True, slots=True)
class Assignment:
    """What to record: one channel's current live stream, in one mode."""

    channel_id: int
    login: str
    mode: RecordingMode
    stream_id: int
    started_at: datetime


class ChannelRecorder:
    def __init__(
        self,
        assignment: Assignment,
        *,
        http: httpx.AsyncClient,
        resolver: Resolver,
        spool: Spool,
        submit: Callable[[ChunkState], None],
        tracker: SegmentTracker,
        next_seq: int = 0,
        chunk_seconds: float = 60.0,
        idle_close_s: float = 15.0,
        offline_retry_s: float = 10.0,
        playlist_failures_before_resolve: int = 3,
        reload_interval_s: float | None = None,  # None: follow the playlist (tests set 0)
    ) -> None:
        self.a = assignment
        self.http = http
        self.resolver = resolver
        self.spool = spool
        self.submit = submit
        self.tracker = tracker
        self.next_seq = next_seq
        self.chunk_seconds = chunk_seconds
        self.idle_close_s = idle_close_s
        self.offline_retry_s = offline_retry_s
        self.playlist_failures_before_resolve = playlist_failures_before_resolve
        self.reload_interval_s = reload_interval_s
        self.writer: ChunkWriter | None = None
        self.chunks = 0
        self._last_append = time.monotonic()
        self._offline_logged = False
        self._inits: dict[str, bytes] = {}  # fMP4 init sections by URI (tiny, ~1 KB each)

    async def run(self, stop: asyncio.Event) -> None:
        with log_context(channel=self.a.login, stream_id=self.a.stream_id):
            log.info("recording started", mode=self.a.mode, next_seq=self.next_seq)
            try:
                while not stop.is_set():
                    url = await self._resolve()
                    if url is None:
                        self._close_chunk()
                        self.tracker.interrupted(GapReason.RECONNECT)
                        await _wait(stop, self.offline_retry_s)
                        continue
                    await self._follow(url, stop)
                    self.tracker.interrupted(GapReason.RECONNECT)
            finally:
                self._close_chunk()
                log.info("recording stopped", chunks=self.chunks)

    async def _resolve(self) -> str | None:
        try:
            url = await self.resolver.resolve(self.a.login, self.a.mode)
        except Exception:
            log.warning("resolving the stream failed", exc_info=True)
            return None
        if url is None and not self._offline_logged:
            log.info("stream offline, waiting")
        self._offline_logged = url is None
        return url

    async def _follow(self, url: str, stop: asyncio.Event) -> None:
        """Follow one playlist URL until it stops working or ends."""
        failures = 0
        while not stop.is_set():
            try:
                playlist = await self._load_playlist(url)
                failures = 0
            except (httpx.HTTPError, ValueError) as exc:
                failures += 1
                log.warning("playlist reload failed", error=str(exc), failures=failures)
                if failures >= self.playlist_failures_before_resolve:
                    return
                await _wait(stop, 1.0 if self.reload_interval_s is None else self.reload_interval_s)
                continue

            new = 0
            for seg in playlist.segments:
                if stop.is_set():
                    break
                if self.tracker.is_new(seg):
                    new += 1
                    await self._handle(seg)

            if self.writer and time.monotonic() - self._last_append > self.idle_close_s:
                self._close_chunk()
            if playlist.ended:  # broadcast over (or restarting): wait before asking again
                log.info("playlist ended")
                self._close_chunk()
                await _wait(stop, self.offline_retry_s)
                return
            await _wait(stop, self._reload_delay(playlist, new))

    async def _handle(self, seg: Segment) -> None:
        if seg.ad:
            if self.tracker.ad(seg):
                log.info("ad break started", at=seg.start.isoformat())
            self._close_chunk()
            return
        init = await self._init_section(seg.init_uri) if seg.init_uri else None
        if seg.init_uri and init is None:
            data = None  # a fragment is useless without its init section
        else:
            data = await self._download(seg)
        if data is None:
            self.tracker.lost(seg)
            return
        plan = self.tracker.live(seg)
        if plan.gap:
            log.info(
                "gap in stream",
                reason=plan.gap.reason,
                seconds=round(plan.gap.seconds, 3),
                start=plan.gap.start.isoformat(),
            )
        if plan.split or (self.writer and self.writer.init != init):
            self._close_chunk()
        if self.writer is None:
            self.writer = ChunkWriter.open(
                self.spool,
                stream_id=self.a.stream_id,
                channel=self.a.login,
                seq=self.next_seq,
                mode=self.a.mode,
                first=seg,
                gap_before=_gap_state(plan.gap),
                init=init,
            )
            self.next_seq += 1
        await self.writer.append(seg, data)
        self._last_append = time.monotonic()
        if self.writer.seconds >= self.chunk_seconds:
            self._close_chunk()

    def _close_chunk(self) -> None:
        if self.writer is None:
            return
        state = self.writer.close()
        self.writer = None
        self.chunks += 1
        self.submit(state)

    async def _load_playlist(self, url: str) -> MediaPlaylist:
        resp = await self.http.get(url)
        resp.raise_for_status()
        return parse_media_playlist(resp.text, str(resp.url))

    async def _download(self, seg: Segment) -> bytes | None:
        return await self._get(seg.uri, what="segment", start=seg.start.isoformat())

    async def _init_section(self, uri: str) -> bytes | None:
        if uri not in self._inits:
            data = await self._get(uri, what="init section")
            if data is None:
                return None
            self._inits = {uri: data}  # keep only the current one
        return self._inits[uri]

    async def _get(self, url: str, *, what: str, attempts: int = 3, **fields) -> bytes | None:
        for attempt in range(1, attempts + 1):
            try:
                resp = await self.http.get(url)
                resp.raise_for_status()
                return resp.content
            except httpx.HTTPError as exc:
                if attempt == attempts:
                    log.warning("download failed", what=what, error=str(exc) or repr(exc), **fields)
                    return None
                await asyncio.sleep(0.5 * attempt)
        return None

    def _reload_delay(self, playlist: MediaPlaylist, new: int) -> float:
        if self.reload_interval_s is not None:
            return self.reload_interval_s
        last = playlist.segments[-1].duration if playlist.segments else playlist.target_duration
        last = last or 2.0
        # HLS rule of thumb: reload after one segment; sooner if nothing new appeared.
        return max(0.5, last if new else last / 2)


def _gap_state(gap: Gap | None) -> GapState | None:
    return GapState(start=gap.start, end=gap.end, reason=gap.reason) if gap else None


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    if seconds <= 0:
        await asyncio.sleep(0)
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
