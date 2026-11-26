"""Twitch VOD chat replay via the internal GQL API (anonymous, undocumented, can break).

`ChatReplay` is what the backfill tool depends on; `GqlChatReplay` is the real one and
`FakeChatReplay` replays fixture pages in tests. Both also answer video metadata
(`VideoInfoSource.video`: a plain query with the web client id, through the same pacing), which
the backfill tool needs when there are no Helix credentials.

One operation: the persisted query `VideoCommentsByOffsetOrCursor`. The first page is asked for
by VOD offset (`contentOffsetSeconds`), the next ones by the cursor of the previous page's last
comment, until `hasNextPage` is false. Twitch picks the page size (seen 2026-11-19: 53-56
comments) and starts an offset page a few seconds *before* the offset (asked 41867, got 41864),
so callers dedupe by comment id. Client id and query hash come from `TwitchSettings`
(`CLIPPER_TWITCH_GQL_CLIENT_ID` / `_GQL_COMMENTS_HASH`).

Politeness and failures (DESIGN §17): one request at a time, at least `min_interval_s` apart
(<= 2 req/s); HTTP errors, timeouts and Twitch's HTTP-200 "context deadline exceeded"/service
errors (also when they come with partial data) are retried with linear backoff on the **same**
offset/cursor (nothing is lost). After `max_attempts`: `GqlUnavailable` / `GqlRateLimited`.
A changed API (integrity check, unknown query hash, new response shape, fractional offsets,
rejected client id) raises `GqlBroken` at once: stop the run, fix the config or the parser.

Times: `createdAt` is kept as Twitch sends it (UTC, "Z", ms); `contentOffsetSeconds` is VOD time
in whole seconds. Their relation and known bugs are in DESIGN §3/§17; nothing is corrected here.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol

import httpx

from clipper_core.logs import get_logger
from clipper_core.timeline import ensure_utc
from clipper_twitch.settings import DEFAULT_GQL_WEB_CLIENT_ID, TwitchSettings

log = get_logger(__name__)

GQL_URL = "https://gql.twitch.tv/gql"
OPERATION = "VideoCommentsByOffsetOrCursor"
# Plain query (not persisted): answered anonymously for the web client id (seen 2026-11-19).
VIDEO_QUERY = (
    "query VideoInfo($id: ID!) { video(id: $id) { id title createdAt publishedAt lengthSeconds"
    " status broadcastType owner { id login displayName } game { name } seekPreviewsURL } }"
)
# 200-responses with these error messages are Twitch-side hiccups: retry the same request
TRANSIENT_MESSAGES = ("context deadline exceeded", "service error", "timeout", "unavailable")
# ...and these mean the API changed or refuses this client: retrying won't help
BROKEN_MESSAGES = ("integrity", "persistedquerynotfound", "persistedquerynotsupported")


class GqlError(RuntimeError):
    """Base class of chat-replay failures."""


class GqlBroken(GqlError):
    """The API changed or refuses us (integrity check, unknown query hash, new response shape,
    rejected client id). Not retried: stop the run and fix config or parser."""


class GqlUnavailable(GqlError):
    """Still failing after all retries (HTTP 5xx, timeouts, Twitch's deadline errors)."""


class GqlRateLimited(GqlUnavailable):
    """Still HTTP 429 after all retries."""


class ReplayUnavailable(GqlError):
    """This video has no chat replay for us: the video doesn't exist (any more), or its first
    page keeps coming back without comments."""


class _Retry(Exception):
    """Internal: this attempt failed in a way worth retrying. Never leaves this module:
    `GqlChatReplay.page` retries it, `parse_page` turns it into a public error.

    `kind` picks the error raised when retries run out: transient -> GqlUnavailable,
    rate_limited -> GqlRateLimited, unknown (an error message we don't know) -> GqlBroken,
    no_comments -> ReplayUnavailable. `wait_s` overrides the backoff (Retry-After).
    """

    def __init__(self, reason: str, *, kind: str = "transient", wait_s: float | None = None):
        super().__init__(reason)
        self.kind = kind
        self.wait_s = wait_s


@dataclass(frozen=True, slots=True)
class Fragment:
    """A piece of a message: plain text, or a Twitch emote (its text is the emote's name).

    7TV/BTTV/FFZ emotes are plain text here; match them with `emotes.message_emotes`.
    """

    text: str
    emote_id: str | None = None


@dataclass(frozen=True, slots=True)
class Comment:
    id: str  # message uuid (probably live IRC's `id` tag: unverified)
    # UTC as Twitch reports it (server clock; see DESIGN §17 for bugs); None when the field is
    # missing or null (never seen; the backfill then uses its chat clock)
    created_at: datetime | None
    content_offset_s: int  # VOD time, whole seconds from the VOD start (fractional: GqlBroken)
    commenter_id: str | None  # None: the user was deleted
    commenter_login: str | None
    commenter_display_name: str | None
    fragments: tuple[Fragment, ...]
    badges: tuple[tuple[str, str], ...]  # (set id, version), empty ones skipped
    user_color: str | None
    cursor: str | None  # this edge's cursor: the next page starts after it

    @property
    def text(self) -> str:
        return "".join(f.text for f in self.fragments)

    def twitch_emotes(self) -> list[tuple[str, str, int]]:
        """`(emote_id, name, count)` in order of first use: the input of `message_emotes`."""
        counts: dict[tuple[str, str], int] = {}
        for f in self.fragments:
            if f.emote_id:
                key = (f.emote_id, f.text)
                counts[key] = counts.get(key, 0) + 1
        return [(emote_id, name, n) for (emote_id, name), n in counts.items()]


@dataclass(frozen=True, slots=True)
class CommentPage:
    video_id: str
    comments: tuple[Comment, ...]
    has_next_page: bool
    has_previous_page: bool
    creator_id: str | None  # the channel's user id (data.video.creator.id)

    @property
    def cursor(self) -> str | None:
        """Pass as `cursor=` for the next page (the last comment's cursor)."""
        return self.comments[-1].cursor if self.comments else None


class ChatReplay(Protocol):
    async def page(
        self, video_id: str, *, offset_s: int | None = None, cursor: str | None = None
    ) -> CommentPage:
        """One page of comments: from VOD offset `offset_s`, or after `cursor` (exactly one)."""
        ...


@dataclass(frozen=True, slots=True)
class VideoInfo:
    """A video's metadata from GQL (anonymous; Helix needs app credentials)."""

    id: str
    title: str | None
    created_at: datetime  # UTC (whole seconds); probably Helix's `created_at` (unverified)
    published_at: datetime | None
    length_s: int  # grows while the archived stream is still live
    status: str | None  # RECORDED, or RECORDING while the stream is still live
    broadcast_type: str | None  # ARCHIVE | HIGHLIGHT | UPLOAD
    owner_id: str | None
    owner_login: str | None
    owner_display_name: str | None
    game: str | None
    # storyboard URL; its path names the broadcast (`vod.parse_vod_path`: stream id), also for
    # VODs whose playlist we may not fetch (sub-only)
    seek_previews_url: str | None


class VideoInfoSource(Protocol):
    async def video(self, video_id: str) -> VideoInfo | None:
        """Metadata of one video; None when it doesn't exist (any more) or isn't visible."""
        ...


# --------------------------------------------------------------------------- parsing


def _emote_id(emote: Any) -> str | None:
    if not emote:
        return None
    # `emoteID` is the Twitch emote id; `id` looks like "<emoteID>;<from>;<to>"
    value = emote.get("emoteID") or str(emote.get("id") or "").split(";")[0]
    return str(value) if value else None


def _comment(edge: dict[str, Any]) -> Comment:
    node = edge["node"]
    commenter = node.get("commenter") or {}
    message = node.get("message") or {}
    fragments = tuple(
        Fragment(str(f.get("text") or ""), _emote_id(f.get("emote")))
        for f in message.get("fragments") or []
    )
    badges = tuple(
        (str(b["setID"]), str(b["version"]))
        for b in message.get("userBadges") or []
        if b.get("setID") and b.get("version")
    )
    created = node.get("createdAt")
    return Comment(
        id=str(node["id"]),
        created_at=ensure_utc(datetime.fromisoformat(created)) if created else None,
        content_offset_s=_offset(node["contentOffsetSeconds"]),
        commenter_id=commenter.get("id") or None,
        commenter_login=commenter.get("login") or None,
        commenter_display_name=commenter.get("displayName") or None,
        fragments=fragments,
        badges=badges,
        user_color=message.get("userColor") or None,
        cursor=edge.get("cursor") or None,
    )


def _offset(value: Any) -> int:
    """`contentOffsetSeconds`: whole seconds (JSON int). The T0 estimate relies on that, so a
    fractional value means the API changed: GqlBroken."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise GqlBroken(f"contentOffsetSeconds is not a number: {value!r}")
    if isinstance(value, float) and not value.is_integer():
        raise GqlBroken(f"contentOffsetSeconds is fractional: {value!r}")
    return int(value)


def _messages(body: dict[str, Any]) -> list[str]:
    errors = body.get("errors") or []
    return [str(e.get("message", e) if isinstance(e, dict) else e) for e in errors]


def _error_for(retry: _Retry, reason: str) -> GqlError:
    """The public error for a failure that is no longer retried."""
    error = {
        "rate_limited": GqlRateLimited,
        "unknown": GqlBroken,
        "no_comments": ReplayUnavailable,
    }.get(retry.kind, GqlUnavailable)
    return error(reason)


def _checked_video(body: Any, video_id: str) -> dict[str, Any] | None:
    """`data.video` of a GQL body (None: `video: null`), after the checks every query shares.

    `video: null` *with* errors is not "no such video" but a failure we don't know: retried,
    then GqlBroken (an unknown error must never read as "gone").
    """
    if isinstance(body, list):
        if len(body) != 1:
            raise GqlBroken(f"expected one GQL result, got {len(body)}")
        body = body[0]
    if not isinstance(body, dict):
        raise GqlBroken(f"unexpected GQL body: {str(body)[:200]}")
    errors = _messages(body)
    lowered = " ".join(errors).lower()
    if any(m in lowered for m in BROKEN_MESSAGES) or (body.get("extensions") or {}).get(
        "challenge"
    ):
        raise GqlBroken(f"GQL refused the query: {errors}")
    # A hiccup can come with partial data (`video: null`, `comments: null`): retry before
    # reading anything into it (TwitchDownloader #1647: "context deadline exceeded").
    if any(m in lowered for m in TRANSIENT_MESSAGES):
        raise _Retry(f"GQL error: {errors}")
    data = body.get("data")
    if not data:
        if errors:
            raise _Retry(f"unknown GQL error: {errors}", kind="unknown")
        raise GqlBroken(f"GQL response without data: {str(body)[:200]}")
    if "video" not in data:
        raise GqlBroken(f"GQL data without `video`: {str(data)[:200]}")
    if errors:
        if data["video"] is None:
            raise _Retry(f"`video: null` with GQL errors: {errors}", kind="unknown")
        log.warning("gql partial errors", video_id=video_id, errors=errors[:3])
    return data["video"]


def parse_video(body: Any, video_id: str) -> VideoInfo | None:
    """A `VIDEO_QUERY` response body -> VideoInfo (None: `video: null`, no such video).

    Raises GqlBroken (unexpected shape, API change) or GqlUnavailable (a Twitch-side hiccup).
    """
    try:
        return _parse_video(body, video_id)
    except _Retry as retry:
        raise _error_for(retry, f"video {video_id}: {retry}") from None


def _parse_video(body: Any, video_id: str) -> VideoInfo | None:
    video = _checked_video(body, video_id)
    if video is None:
        return None
    try:
        owner = video.get("owner") or {}
        published = video.get("publishedAt")
        return VideoInfo(
            id=str(video["id"]),
            title=video.get("title") or None,
            created_at=ensure_utc(datetime.fromisoformat(video["createdAt"])),
            published_at=ensure_utc(datetime.fromisoformat(published)) if published else None,
            length_s=int(video["lengthSeconds"]),
            status=video.get("status") or None,
            broadcast_type=video.get("broadcastType") or None,
            owner_id=owner.get("id") or None,
            owner_login=owner.get("login") or None,
            owner_display_name=owner.get("displayName") or None,
            game=(video.get("game") or {}).get("name") or None,
            seek_previews_url=video.get("seekPreviewsURL") or None,
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise GqlBroken(f"unexpected video shape: {type(exc).__name__}: {exc}") from exc


def parse_page(body: Any, video_id: str, *, by_cursor: bool = False) -> CommentPage:
    """A GQL response body (object, or a one-element array) -> CommentPage.

    Raises GqlBroken (unexpected shape or an API-change error), ReplayUnavailable (no such video,
    or no comments for a first page), GqlUnavailable (a Twitch-side hiccup: worth a retry).
    `by_cursor`: the body answers a cursor request (missing comments are then a hiccup).
    """
    try:
        return _parse(body, video_id, by_cursor=by_cursor)
    except _Retry as retry:
        raise _error_for(retry, f"video {video_id}: {retry}") from None


def _parse(body: Any, video_id: str, *, by_cursor: bool) -> CommentPage:
    """parse_page, but failures worth a retry raise the internal `_Retry`."""
    video = _checked_video(body, video_id)
    if video is None:
        raise ReplayUnavailable(f"video {video_id}: not found (deleted, expired or private)")
    comments = video.get("comments")
    if comments is None:
        # TwitchDownloader sees this now and then and retries. Only a first page can mean "this
        # video has no replay"; in the middle of a run it is a hiccup.
        raise _Retry("comments missing", kind="transient" if by_cursor else "no_comments")
    try:
        page_info = comments["pageInfo"]
        page = CommentPage(
            video_id=str(video.get("id") or video_id),
            comments=tuple(_comment(e) for e in comments["edges"] or []),
            has_next_page=bool(page_info["hasNextPage"]),
            has_previous_page=bool(page_info.get("hasPreviousPage")),
            creator_id=((video.get("creator") or {}).get("id")) or None,
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise GqlBroken(f"unexpected comment shape: {type(exc).__name__}: {exc}") from exc
    if page.has_next_page and not page.cursor:
        raise GqlBroken(f"video {video_id}: hasNextPage without a cursor")
    return page


# --------------------------------------------------------------------------- real client


MIN_INTERVAL_S = 0.5  # <= 2 requests/s (DESIGN §17)


class GqlChatReplay:
    """Anonymous chat replay (and video metadata) over gql.twitch.tv (see the module docstring
    for the rules).

    Pacing and the one-request-at-a-time lock live in the instance: share one instance per
    process (several instances would each send up to 2 req/s).
    """

    def __init__(
        self,
        *,
        client_id: str,
        comments_hash: str,
        web_client_id: str = DEFAULT_GQL_WEB_CLIENT_ID,
        http: httpx.AsyncClient | None = None,
        url: str = GQL_URL,
        min_interval_s: float = MIN_INTERVAL_S,
        max_attempts: int = 10,
        backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        timeout_s: float = 20.0,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if min_interval_s < MIN_INTERVAL_S:
            raise ValueError(f"min_interval_s must be >= {MIN_INTERVAL_S} (politeness)")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._client_id = client_id
        self._web_client_id = web_client_id
        self._hash = comments_hash
        self._http = http or httpx.AsyncClient(timeout=timeout_s)
        self._owns_http = http is None
        self._url = url
        self._min_interval_s = min_interval_s
        self._max_attempts = max_attempts
        self._backoff_s = backoff_s
        self._max_backoff_s = max_backoff_s
        self._timeout_s = timeout_s
        self._sleep = sleep
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last_request: float | None = None
        self.requests = 0  # HTTP requests sent, retries included

    @classmethod
    def from_settings(cls, settings: TwitchSettings, **kwargs: Any) -> GqlChatReplay:
        return cls(
            client_id=settings.gql_client_id,
            comments_hash=settings.gql_comments_hash,
            web_client_id=settings.gql_web_client_id,
            **kwargs,
        )

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def body(
        self, video_id: str, *, offset_s: int | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        if (offset_s is None) == (cursor is None):
            raise ValueError("pass exactly one of offset_s and cursor")
        variables: dict[str, Any] = {"videoID": str(video_id)}
        if cursor is not None:
            variables["cursor"] = cursor
        else:
            variables["contentOffsetSeconds"] = max(0, int(offset_s))
        return {
            "operationName": OPERATION,
            "variables": variables,
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": self._hash}},
        }

    async def page(
        self, video_id: str, *, offset_s: int | None = None, cursor: str | None = None
    ) -> CommentPage:
        payload = self.body(video_id, offset_s=offset_s, cursor=cursor)
        return await self._request(
            payload,
            lambda body: _parse(body, video_id, by_cursor=cursor is not None),
            client_id=self._client_id,
            video_id=video_id,
            offset_s=offset_s,
        )

    async def video(self, video_id: str) -> VideoInfo | None:
        """Video metadata (plain query with the web client id; same pacing and retries)."""
        if not str(video_id).isdigit():
            raise ValueError(f"not a Twitch video id: {video_id!r}")
        payload = {"query": VIDEO_QUERY, "variables": {"id": str(video_id)}}
        return await self._request(
            payload,
            lambda body: _parse_video(body, video_id),
            client_id=self._web_client_id,
            video_id=video_id,
            offset_s=None,
        )

    async def _request[T](
        self,
        payload: dict[str, Any],
        parse: Callable[[Any], T],
        *,
        client_id: str,
        video_id: str,
        offset_s: int | None,
    ) -> T:
        async with self._lock:  # one request at a time, retries included
            failures = 0
            while True:
                try:
                    body = await self._post(payload, client_id)
                    return parse(body)
                except _Retry as retry:
                    failures += 1
                    if failures >= self._max_attempts:
                        reason = f"video {video_id}: {failures} failed attempts, last: {retry}"
                        raise _error_for(retry, reason) from None
                    wait = retry.wait_s
                    if wait is None:
                        wait = self._backoff_s * failures
                    wait = min(max(wait, 0.0), self._max_backoff_s)
                    log.warning(
                        "gql retry",
                        video_id=video_id,
                        offset_s=offset_s,
                        attempt=failures,
                        wait_s=round(wait, 2),
                        reason=str(retry)[:200],
                    )
                    if wait > 0:
                        await self._sleep(wait)

    async def _post(self, payload: dict[str, Any], client_id: str) -> Any:
        await self._pace()
        self.requests += 1
        try:
            resp = await self._http.post(
                self._url,
                json=payload,
                headers={"Client-ID": client_id},
                timeout=self._timeout_s,
            )
        except httpx.RequestError as exc:  # timeouts, resets, DNS, broken content encoding
            raise _Retry(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code == 429:
            raise _Retry("HTTP 429", kind="rate_limited", wait_s=_retry_after(resp))
        if resp.status_code >= 500:
            raise _Retry(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise GqlBroken(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise _Retry(f"invalid JSON: {resp.text[:100]!r}") from exc

    async def _pace(self) -> None:
        if self._last_request is not None:
            wait = self._last_request + self._min_interval_s - self._clock()
            if wait > 0:
                await self._sleep(wait)
        self._last_request = self._clock()


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers["Retry-After"])
    except (KeyError, ValueError):
        return None


# --------------------------------------------------------------------------- fake


def _replay_order(c: Comment) -> tuple[int, float, str]:
    """Twitch's order: by offset, then by time (a comment without `createdAt` first)."""
    return (c.content_offset_s, c.created_at.timestamp() if c.created_at else float("-inf"), c.id)


class FakeChatReplay:
    """Test double: serves comments in pages of `page_size`, like Twitch does.

    By offset: from the first comment at or after it (Twitch starts a few seconds earlier, so
    a caller that works here and dedupes by id works there). By cursor: after that comment.
    Build it from parsed comments, or `from_pages` from raw GQL page bodies (fixtures). `errors`
    maps a call number (0-based) to an exception raised by that call instead. `videos`: what
    `video()` answers (unknown ids: None).
    """

    def __init__(
        self,
        comments: Mapping[str, Sequence[Comment]] | None = None,
        *,
        page_size: int = 50,
        creator_id: str | None = None,
        errors: Mapping[int, Exception] | None = None,
        videos: Mapping[str, VideoInfo] | None = None,
    ) -> None:
        self._comments = {
            vid: sorted(cs, key=_replay_order) for vid, cs in (comments or {}).items()
        }
        self._page_size = page_size
        self._creator_id = creator_id
        self._errors = dict(errors or {})
        self._videos = dict(videos or {})
        self.calls: list[tuple[str, int | None, str | None]] = []
        self.video_calls: list[str] = []

    async def video(self, video_id: str) -> VideoInfo | None:
        self.video_calls.append(video_id)
        return self._videos.get(video_id)

    @classmethod
    def from_pages(cls, video_id: str, pages: Iterable[Any], **kwargs: Any) -> FakeChatReplay:
        """Comments of raw GQL page bodies (deduplicated by id) as one video's replay."""
        seen: dict[str, Comment] = {}
        creator_id = kwargs.pop("creator_id", None)
        for body in pages:
            page = parse_page(body, video_id)
            creator_id = creator_id or page.creator_id
            for c in page.comments:
                seen.setdefault(c.id, c)
        return cls({video_id: list(seen.values())}, creator_id=creator_id, **kwargs)

    async def page(
        self, video_id: str, *, offset_s: int | None = None, cursor: str | None = None
    ) -> CommentPage:
        if (offset_s is None) == (cursor is None):
            raise ValueError("pass exactly one of offset_s and cursor")
        call = len(self.calls)
        self.calls.append((video_id, offset_s, cursor))
        if call in self._errors:
            raise self._errors[call]
        if video_id not in self._comments:
            raise ReplayUnavailable(f"video {video_id}: unknown to the fake")
        comments = self._comments[video_id]
        if cursor is not None:
            prefix = f"fake:{video_id}:"
            if not cursor.startswith(prefix):
                raise GqlBroken(f"not a cursor of this fake: {cursor!r}")
            start = int(cursor.removeprefix(prefix)) + 1
        else:
            start = next(
                (i for i, c in enumerate(comments) if c.content_offset_s >= offset_s),
                len(comments),
            )
        chunk = comments[start : start + self._page_size]
        out = tuple(replace(c, cursor=f"fake:{video_id}:{start + i}") for i, c in enumerate(chunk))
        return CommentPage(
            video_id=video_id,
            comments=out,
            has_next_page=start + len(chunk) < len(comments),
            has_previous_page=start > 0,
            creator_id=self._creator_id,
        )
