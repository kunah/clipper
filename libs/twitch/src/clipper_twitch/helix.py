"""Minimal Twitch Helix API client (app access token via client credentials).

Users, live streams, archives (`get_videos`) and viewer clips (`get_clips`). Times are parsed into
timezone-aware UTC datetimes; list endpoints follow Helix's `pagination.cursor`.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from clipper_core.logs import get_logger
from clipper_core.timeline import ensure_utc
from clipper_twitch.settings import TwitchSettings

log = get_logger(__name__)

HELIX_URL = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
MAX_IDS = 100  # Helix accepts up to 100 ids/logins per request
MAX_PAGE = 100  # `first` on list endpoints: 1..100
_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


class HelixError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HelixUser:
    id: str
    login: str
    display_name: str


@dataclass(frozen=True, slots=True)
class HelixStream:
    id: str
    user_id: str
    user_login: str
    title: str
    game_name: str
    viewer_count: int
    started_at: datetime


@dataclass(frozen=True, slots=True)
class MutedSegment:
    """A muted part of a video, in VOD time (seconds from its start)."""

    offset_s: int
    duration_s: int


@dataclass(frozen=True, slots=True)
class HelixVideo:
    """Get Videos item. For archives, `stream_id` is the broadcast's stream id (else None)."""

    id: str
    stream_id: str | None
    user_id: str
    user_login: str
    user_name: str
    title: str
    description: str
    created_at: datetime  # UTC; about the broadcast start, not exactly VOD offset 0 (DESIGN §17)
    published_at: datetime | None  # UTC; can be missing
    url: str
    thumbnail_url: str
    viewable: str
    view_count: int
    language: str
    type: str  # archive | highlight | upload
    duration_s: int  # from "1h0m5s"; still growing while the archived stream is live
    # None = not reported: Helix always sends null with an app access token (docs), so muted
    # parts come from the playlist ("N-muted.ts") instead.
    muted_segments: tuple[MutedSegment, ...] | None


@dataclass(frozen=True, slots=True)
class HelixClip:
    """Get Clips item (a viewer-made clip)."""

    id: str
    url: str
    embed_url: str
    broadcaster_id: str
    broadcaster_name: str
    creator_id: str
    creator_name: str
    video_id: str | None  # the archive it was cut from; Helix "" (no VOD) -> None
    game_id: str
    language: str
    title: str
    view_count: int
    created_at: datetime  # UTC; when the clip was made, not when the moment happened
    thumbnail_url: str
    duration: float  # seconds, 0.1 precision
    # Where the clip starts in the VOD (s). None while the VOD is unavailable and for a few
    # minutes after the clip was made. Reportedly off by about the clip length (DESIGN §17).
    vod_offset: int | None
    is_featured: bool


@dataclass(frozen=True, slots=True)
class HelixPage[T]:
    """Items of one or more pages, and the cursor to continue after them (None: no more)."""

    items: list[T] = field(default_factory=list)
    cursor: str | None = None


def parse_duration(value: str) -> int:
    """Helix video duration ("3m21s", "1h0m5s"; zero leading units left out) -> seconds."""
    m = _DURATION.match(value.strip())
    if not value.strip() or not m:
        raise ValueError(f"not a Helix duration: {value!r}")
    hours, minutes, seconds = (int(g or 0) for g in m.groups())
    return hours * 3600 + minutes * 60 + seconds


def _time(value: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(value))


def _rfc3339(dt: datetime) -> str:
    return ensure_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _video(v: dict[str, Any]) -> HelixVideo:
    muted = v.get("muted_segments")
    return HelixVideo(
        id=v["id"],
        stream_id=v.get("stream_id") or None,
        user_id=v["user_id"],
        user_login=v["user_login"],
        user_name=v.get("user_name") or "",
        title=v.get("title") or "",
        description=v.get("description") or "",
        created_at=_time(v["created_at"]),
        published_at=_time(v["published_at"]) if v.get("published_at") else None,
        url=v.get("url") or "",
        thumbnail_url=v.get("thumbnail_url") or "",
        viewable=v.get("viewable") or "",
        view_count=int(v.get("view_count") or 0),
        language=v.get("language") or "",
        type=v.get("type") or "",
        duration_s=parse_duration(v["duration"]),
        muted_segments=None
        if muted is None
        else tuple(MutedSegment(int(m["offset"]), int(m["duration"])) for m in muted),
    )


def _clip(c: dict[str, Any]) -> HelixClip:
    offset = c.get("vod_offset")
    return HelixClip(
        id=c["id"],
        url=c.get("url") or "",
        embed_url=c.get("embed_url") or "",
        broadcaster_id=c["broadcaster_id"],
        broadcaster_name=c.get("broadcaster_name") or "",
        creator_id=c.get("creator_id") or "",
        creator_name=c.get("creator_name") or "",
        video_id=c.get("video_id") or None,
        game_id=c.get("game_id") or "",
        language=c.get("language") or "",
        title=c.get("title") or "",
        view_count=int(c.get("view_count") or 0),
        created_at=_time(c["created_at"]),
        thumbnail_url=c.get("thumbnail_url") or "",
        duration=float(c.get("duration") or 0.0),
        vod_offset=None if offset is None else int(offset),
        is_featured=bool(c.get("is_featured")),
    )


def _chunks(items: Sequence[str], size: int = MAX_IDS) -> Iterable[Sequence[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class HelixClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        http: httpx.AsyncClient | None = None,
        base_url: str = HELIX_URL,
        token_url: str = TOKEN_URL,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http or httpx.AsyncClient(timeout=15.0)
        self._owns_http = http is None
        self._base_url = base_url.rstrip("/")
        self._token_url = token_url
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, settings: TwitchSettings, **kwargs: Any) -> HelixClient:
        if not settings.has_credentials:
            raise HelixError("CLIPPER_TWITCH_CLIENT_ID / CLIPPER_TWITCH_CLIENT_SECRET are not set")
        return cls(settings.client_id, settings.client_secret.get_secret_value(), **kwargs)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _get_token(self, force: bool = False) -> str:
        async with self._token_lock:
            if force or not self._token or time.monotonic() > self._token_expires - 300:
                resp = await self._http.post(
                    self._token_url,
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "grant_type": "client_credentials",
                    },
                )
                if resp.status_code != 200:
                    raise HelixError(f"token request failed: {resp.status_code} {resp.text[:200]}")
                body = resp.json()
                self._token = body["access_token"]
                self._token_expires = time.monotonic() + float(body.get("expires_in", 3600))
                log.info("helix token acquired", expires_in_s=body.get("expires_in"))
            return self._token

    async def get(self, path: str, params: list[tuple[str, str]]) -> dict[str, Any]:
        for attempt in range(4):
            token = await self._get_token(force=attempt > 0 and self._token is None)
            resp = await self._http.get(
                f"{self._base_url}/{path.lstrip('/')}",
                params=params,
                headers={"Client-Id": self._client_id, "Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401:
                self._token = None  # expired/revoked: fetch a new one
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                reset = resp.headers.get("Ratelimit-Reset")
                wait = max(0.5, float(reset) - time.time()) if reset else 2.0**attempt
                log.warning(
                    "helix retry", path=path, status=resp.status_code, wait_s=round(wait, 1)
                )
                await asyncio.sleep(min(wait, 30.0))
                continue
            raise HelixError(f"GET {path} failed: {resp.status_code} {resp.text[:200]}")
        raise HelixError(f"GET {path} failed after retries")

    async def get_users(
        self, *, logins: Sequence[str] = (), ids: Sequence[str] = ()
    ) -> list[HelixUser]:
        keys = [("login", login.lower()) for login in logins] + [("id", i) for i in ids]
        users: list[HelixUser] = []
        for chunk in _chunks(keys):  # type: ignore[arg-type]
            body = await self.get("users", list(chunk))
            users += [HelixUser(u["id"], u["login"], u["display_name"]) for u in body["data"]]
        return users

    async def get_streams(self, user_ids: Sequence[str]) -> list[HelixStream]:
        """Currently live streams among these users."""
        streams: list[HelixStream] = []
        for chunk in _chunks(list(user_ids)):
            cursor: str | None = None
            while True:
                params = [("user_id", uid) for uid in chunk] + [("first", "100")]
                if cursor:
                    params.append(("after", cursor))
                body = await self.get("streams", params)
                for s in body["data"]:
                    if s.get("type", "live") != "live":
                        continue
                    streams.append(
                        HelixStream(
                            id=s["id"],
                            user_id=s["user_id"],
                            user_login=s["user_login"],
                            title=s.get("title") or "",
                            game_name=s.get("game_name") or "",
                            viewer_count=int(s.get("viewer_count") or 0),
                            started_at=datetime.fromisoformat(s["started_at"]),
                        )
                    )
                cursor = (body.get("pagination") or {}).get("cursor")
                if not cursor or not body["data"]:
                    break
        return streams

    async def get_videos(
        self,
        user_id: str,
        *,
        type: str = "archive",
        first: int = MAX_PAGE,
        after: str | None = None,
        limit: int | None = None,
    ) -> HelixPage[HelixVideo]:
        """The user's videos, newest first (`type`: archive | highlight | upload | all).

        `limit` caps the items (None: all pages); continue with `after=page.cursor`. An archive
        of a stream that is still live is included, with a growing duration.
        """
        params = [("user_id", user_id), ("type", type), ("sort", "time")]
        return await self._pages("videos", params, _video, first=first, after=after, limit=limit)

    async def get_clips(
        self,
        broadcaster_id: str,
        *,
        started_at: datetime,
        ended_at: datetime,
        first: int = MAX_PAGE,
        after: str | None = None,
        limit: int | None = None,
    ) -> HelixPage[HelixClip]:
        """Clips created in [started_at, ended_at], **most viewed first**.

        Helix truncates both bounds to the minute and returns about 1,000 clips at most over
        all pages of one query, so split long ranges. Clips can be made from the VOD long after
        the moment: match them to a VOD by `video_id`, not by `created_at`.
        """
        params = [
            ("broadcaster_id", broadcaster_id),
            ("started_at", _rfc3339(started_at)),
            ("ended_at", _rfc3339(ended_at)),
        ]
        return await self._pages("clips", params, _clip, first=first, after=after, limit=limit)

    async def _pages[T](
        self,
        path: str,
        params: list[tuple[str, str]],
        parse: Callable[[dict[str, Any]], T],
        *,
        first: int,
        after: str | None,
        limit: int | None,
    ) -> HelixPage[T]:
        """Follow `pagination.cursor` until `limit` items or the end (an empty page, or none).

        Each page asks for at most the items still wanted, so the returned cursor continues
        exactly after the last item (a resume skips nothing).
        """
        if not 1 <= first <= MAX_PAGE:
            raise ValueError(f"first must be 1..{MAX_PAGE}, got {first}")
        if limit is not None and limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        items: list[T] = []
        cursor = after
        seen = {after} if after else set()
        while limit is None or len(items) < limit:
            want = first if limit is None else min(first, limit - len(items))
            query = [*params, ("first", str(want))] + ([("after", cursor)] if cursor else [])
            body = await self.get(path, query)
            data = body.get("data") or []
            items += [parse(item) for item in data]
            cursor = (body.get("pagination") or {}).get("cursor") or None
            if not data or not cursor:
                # "The last page may still include a cursor; the page after it is empty."
                return HelixPage(items, None)
            if cursor in seen:  # Helix handing out the same page again: would loop forever
                raise HelixError(f"GET {path}: cursor repeated after {len(items)} items")
            seen.add(cursor)
        return HelixPage(items, cursor)
