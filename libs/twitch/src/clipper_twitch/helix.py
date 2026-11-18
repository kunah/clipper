"""Minimal Twitch Helix API client (app access token via client credentials)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from clipper_core.logs import get_logger
from clipper_twitch.settings import TwitchSettings

log = get_logger(__name__)

HELIX_URL = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
MAX_IDS = 100  # Helix accepts up to 100 ids/logins per request


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
