"""Twitch VOD (archive) media playlists: video id + rendition -> media playlist URL.

`VodSource` is what the backfill tool depends on; `StreamlinkVodSource` is the real one and
`FakeVodSource` serves tests. streamlink does Twitch's access token + usher request (and keeps up
when Twitch changes them), like the recorder's live resolver; the caller then fetches the
playlist and segments itself and parses them with `hls.parse_vod_playlist`.

Rendition names are streamlink's: `audio_only`, `160p30` ... `1080p60`, plus the synonyms `best`
(source) and `worst`. streamlink asks usher for H.264 only, and while resolving it fetches every
rendition's media playlist once (a few hundred KB each for a long VOD), so resolve once per VOD
(`renditions`) rather than once per rendition.
Seen 2026-11-19: an unknown video id gets no playback token -> `VodGone`. Sub-only VODs (usher
403 `vod_manifest_restricted` -> `VodRestricted`) follow the research notes, not seen yet.

Needs the `vod` extra (streamlink); this module imports it only when a playlist is resolved.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from clipper_core.logs import get_logger

log = get_logger(__name__)

VIDEO_URL = "https://www.twitch.tv/videos/{video_id}"
USHER_HOST = "usher.ttvnw.net"
# usher's `error_code` for VODs a viewer may not watch (sub-only, ...): research 2026-11-19
RESTRICTED_CODES = frozenset({"vod_manifest_restricted", "unauthorized_entitlements"})


class VodError(RuntimeError):
    """Resolving a VOD failed for another reason (API error, network): worth a later retry."""


class VodRestricted(VodError):
    """Not watchable anonymously (sub-only: usher 403 `vod_manifest_restricted`). Skip media."""


class VodGone(VodError):
    """The VOD doesn't exist (any more): deleted or expired."""


class VodRenditionMissing(VodError):
    """The VOD exists but not in the wanted rendition; `available` lists what it has."""

    def __init__(self, video_id: str, rendition: str, available: list[str]) -> None:
        super().__init__(f"video {video_id} has no rendition {rendition!r} (has {available})")
        self.available = available


class VodSource(Protocol):
    async def renditions(self, video_id: str) -> dict[str, str]:
        """{rendition name: media playlist URL}. Raises VodRestricted / VodGone / VodError."""
        ...

    async def playlist_url(self, video_id: str, rendition: str = "audio_only") -> str:
        """Media playlist URL of one rendition. Raises VodRestricted / VodGone /
        VodRenditionMissing / VodError."""
        ...


def _pick(video_id: str, streams: Mapping[str, str], rendition: str) -> str:
    if rendition not in streams:
        raise VodRenditionMissing(video_id, rendition, sorted(streams))
    return streams[rendition]


@dataclass(slots=True)
class UsherResponse:
    """An HTTP response streamlink got from usher while resolving (kept to explain failures)."""

    status: int
    body: str


def usher_error_codes(body: str) -> set[str]:
    """`error_code`s in usher's JSON error body: `[{"type": "error", "error_code": ...}]`."""
    try:
        data = json.loads(body)
    except ValueError:
        return set()
    items = data if isinstance(data, list) else [data]
    return {str(i["error_code"]) for i in items if isinstance(i, dict) and i.get("error_code")}


def classify_failure(video_id: str, usher: list[UsherResponse]) -> VodError:
    """streamlink returned no streams: tell restricted from gone from anything else.

    streamlink returns no streams both when the access token is empty (no such VOD: it never asks
    usher) and when usher answers 4xx/5xx (logged, not raised), so the usher responses decide.
    """
    if not usher:
        return VodGone(f"video {video_id}: no playback token (deleted, expired or never existed)")
    last = usher[-1]
    codes = usher_error_codes(last.body)
    if last.status == 403 and (codes & RESTRICTED_CODES or not codes):
        return VodRestricted(f"video {video_id}: usher {last.status} {sorted(codes)}")
    if last.status in (404, 410):
        return VodGone(f"video {video_id}: usher {last.status} {sorted(codes)}")
    return VodError(f"video {video_id}: usher {last.status} {sorted(codes)} {last.body[:200]}")


def _default_session() -> Any:
    from streamlink import Streamlink  # the `vod` extra

    return Streamlink()


class StreamlinkVodSource:
    """Resolves VOD renditions with streamlink (blocking calls run in a worker thread).

    `session_factory` returns a streamlink session (a new one per call: sessions aren't
    thread-safe); tests pass a stand-in with `.http.hooks` and `.streams(url)`.
    """

    def __init__(self, session_factory: Callable[[], Any] = _default_session) -> None:
        self._session_factory = session_factory

    async def renditions(self, video_id: str) -> dict[str, str]:
        return await asyncio.to_thread(self._renditions, video_id)

    async def playlist_url(self, video_id: str, rendition: str = "audio_only") -> str:
        return _pick(video_id, await self.renditions(video_id), rendition)

    def _renditions(self, video_id: str) -> dict[str, str]:
        if not video_id.isdigit():
            raise ValueError(f"not a Twitch video id: {video_id!r}")
        session = self._session_factory()
        usher: list[UsherResponse] = []

        def on_response(resp: Any, *args: Any, **kwargs: Any) -> None:
            # requests response hook: record usher's answers (status + error body)
            if urlsplit(resp.url).hostname == USHER_HOST:
                body = resp.text[:2000] if resp.status_code >= 400 else ""
                usher.append(UsherResponse(resp.status_code, body))

        session.http.hooks.setdefault("response", []).append(on_response)
        try:
            streams = session.streams(VIDEO_URL.format(video_id=video_id))
        except Exception as exc:  # streamlink's PluginError and friends
            if usher and usher[-1].status >= 400:  # usher said why: restricted / gone / other
                raise classify_failure(video_id, usher) from exc
            raise VodError(f"video {video_id}: {type(exc).__name__}: {exc}") from exc
        if not streams:
            err = classify_failure(video_id, usher)
            log.info(
                "vod not resolvable", video_id=video_id, kind=type(err).__name__, error=str(err)
            )
            raise err
        urls = {name: stream.url for name, stream in streams.items()}
        log.debug("vod resolved", video_id=video_id, renditions=sorted(urls))
        return urls


@dataclass
class FakeVodSource:
    """Test double: fixed renditions per video id, or an exception to raise."""

    videos: dict[str, dict[str, str] | Exception] = field(default_factory=dict)
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    async def renditions(self, video_id: str) -> dict[str, str]:
        self.calls.append((video_id, None))
        return self._lookup(video_id)

    async def playlist_url(self, video_id: str, rendition: str = "audio_only") -> str:
        self.calls.append((video_id, rendition))
        return _pick(video_id, self._lookup(video_id), rendition)

    def _lookup(self, video_id: str) -> dict[str, str]:
        found = self.videos.get(video_id)
        if found is None:
            raise VodGone(f"video {video_id}: unknown to the fake")
        if isinstance(found, Exception):
            raise found
        return dict(found)
