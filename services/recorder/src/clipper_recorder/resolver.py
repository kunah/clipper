"""Channel login -> media playlist URL, via streamlink.

streamlink handles Twitch's access token + usher request (and keeps up when Twitch changes
them). We only take the playlist URL: streamlink drops EXT-X-PROGRAM-DATE-TIME, which we need,
so the recorder fetches the playlist and segments itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from streamlink import Streamlink

from clipper_core.contracts import RecordingMode
from clipper_core.logs import get_logger

log = get_logger(__name__)


class StreamlinkResolver:
    def __init__(self, qualities: Sequence[str] = ("best",)) -> None:
        self.qualities = list(qualities)

    async def resolve(self, login: str, mode: RecordingMode) -> str | None:
        """Media playlist URL, or None if the channel is offline. Raises on API errors."""
        return await asyncio.to_thread(self._resolve, login, mode)

    def _resolve(self, login: str, mode: RecordingMode) -> str | None:
        streams = Streamlink().streams(f"https://www.twitch.tv/{login}")
        if not streams:
            return None
        wanted = ["audio_only"] if mode == RecordingMode.AUDIO_ONLY else self.qualities
        for name in wanted:
            if name in streams:
                return streams[name].url
        fallback = "best" if mode == RecordingMode.FULL else "worst"
        log.warning(
            "wanted quality not available, using fallback",
            channel=login,
            wanted=wanted,
            available=sorted(streams),
            fallback=fallback,
        )
        return streams[fallback].url
