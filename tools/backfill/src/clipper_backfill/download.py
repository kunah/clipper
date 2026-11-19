"""Playlist and segment downloads for VODs: a few at a time, long retries.

Unlike the live recorder (which gives up fast: the stream moves on), a VOD can be fetched again,
so failures are retried with backoff (`attempts`, Retry-After honoured). What stays broken:
- 404/410 on a segment: the segment is *lost* (`None`): the planner turns it into a
  `not_recorded` gap and the run goes on.
- anything else (network, 5xx, 403, other 4xx): `DownloadError`; the run stops, everything
  committed so far stays, and a re-run resumes.
At most `concurrency` (<= 4) requests are in flight.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

from clipper_core.logs import get_logger
from clipper_twitch.hls import VodSegment

log = get_logger(__name__)

MAX_CONCURRENCY = 4
LOST_STATUSES = (404, 410)


class DownloadError(RuntimeError):
    """A download failed for good (after retries); the run should stop and resume later."""


class Downloader:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        concurrency: int = MAX_CONCURRENCY,
        attempts: int = 6,
        backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        if not 1 <= concurrency <= MAX_CONCURRENCY:
            raise ValueError(f"concurrency must be 1..{MAX_CONCURRENCY} (politeness)")
        self.http = http
        self.attempts = max(1, attempts)
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self._sleep = sleep
        self._slots = asyncio.Semaphore(concurrency)
        self._init: tuple[str, bytes] | None = None  # the current fMP4 init section
        self.bytes = 0  # downloaded, all requests
        self.requests = 0

    async def text(self, url: str) -> str:
        """A playlist. Any failure (also 404) is a DownloadError."""
        data = await self._get(url, what="playlist")
        if data is None:
            raise DownloadError(f"playlist not found: {url}")
        return data.decode("utf-8", errors="replace")

    async def init_section(self, uri: str) -> bytes | None:
        """fMP4 init section (cached: consecutive chunks share it). None: lost."""
        if self._init and self._init[0] == uri:
            return self._init[1]
        data = await self._get(uri, what="init section")
        if data is not None:
            self._init = (uri, data)
        return data

    async def segments(self, segments: Sequence[VodSegment]) -> dict[int, bytes | None]:
        """{segment index: bytes, or None when lost}. Raises DownloadError after all finished."""
        results = await asyncio.gather(
            *(self._get(s.uri, what="segment", index=s.index) for s in segments),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise errors[0]
        return {s.index: r for s, r in zip(segments, results, strict=True)}

    async def _get(self, url: str, *, what: str, **fields: Any) -> bytes | None:
        failures = 0
        while True:
            async with self._slots:
                self.requests += 1
                try:
                    resp = await self.http.get(url)
                    status = resp.status_code
                    if status == 200:
                        self.bytes += len(resp.content)
                        return resp.content
                    reason = f"HTTP {status}"
                    wait = _retry_after(resp)
                except httpx.HTTPError as exc:  # timeouts, resets, DNS
                    status, reason, wait = None, f"{type(exc).__name__}: {exc}", None
            failures += 1
            lost = status in LOST_STATUSES
            retryable = status is None or status == 429 or status >= 500 or lost
            # a 404 is asked twice (a CDN hiccup), the rest up to `attempts` times
            if not retryable or failures >= (min(2, self.attempts) if lost else self.attempts):
                if lost:
                    log.warning("lost", what=what, url=url, status=status, **fields)
                    return None
                raise DownloadError(f"{what} {url}: {reason} after {failures} attempts")
            if wait is None:
                wait = self.backoff_s * 2 ** (failures - 1)
            wait = min(wait, self.max_backoff_s)
            log.warning(
                "download retry", what=what, reason=reason, attempt=failures, wait_s=wait, **fields
            )
            await self._sleep(wait)


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return max(0.0, float(resp.headers["Retry-After"]))
    except (KeyError, ValueError):
        return None
