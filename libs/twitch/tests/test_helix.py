from datetime import UTC, datetime

import httpx
import pytest

from clipper_twitch.helix import HelixClient, HelixError
from clipper_twitch.settings import TwitchSettings


class FakeTwitch:
    def __init__(self) -> None:
        self.token_requests = 0
        self.calls: list[httpx.Request] = []
        self.reject_next_with_401 = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            self.token_requests += 1
            return httpx.Response(
                200, json={"access_token": f"t{self.token_requests}", "expires_in": 5000000}
            )
        self.calls.append(request)
        assert request.headers["Client-Id"] == "cid"
        if self.reject_next_with_401:
            self.reject_next_with_401 = False
            return httpx.Response(401, json={"message": "invalid token"})
        params = request.url.params
        if request.url.path == "/helix/users":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": f"id-{login}", "login": login, "display_name": login.upper()}
                        for login in params.get_list("login")
                    ]
                },
            )
        if request.url.path == "/helix/streams":
            ids = params.get_list("user_id")
            page = params.get("after")
            data = [
                {
                    "id": f"s-{uid}",
                    "user_id": uid,
                    "user_login": uid,
                    "type": "live",
                    "title": "t",
                    "game_name": "g",
                    "viewer_count": 5,
                    "started_at": "2026-11-18T19:00:00Z",
                }
                for uid in ids
                if uid.endswith("live")
            ]
            # two pages: first half, then the rest
            half = len(data) // 2 or len(data)
            if page is None and len(data) > 1:
                return httpx.Response(
                    200, json={"data": data[:half], "pagination": {"cursor": "next"}}
                )
            return httpx.Response(
                200, json={"data": data[half:] if page else data, "pagination": {}}
            )
        return httpx.Response(404)


@pytest.fixture
def twitch():
    return FakeTwitch()


@pytest.fixture
def helix(twitch):
    http = httpx.AsyncClient(transport=httpx.MockTransport(twitch.handler))
    return HelixClient("cid", "secret", http=http)


async def test_get_users_batches_by_100_and_reuses_token(helix, twitch):
    logins = [f"user{i}" for i in range(150)]
    users = await helix.get_users(logins=logins)
    assert len(users) == 150 and users[0].id == "id-user0" and users[0].display_name == "USER0"
    assert len(twitch.calls) == 2
    assert twitch.token_requests == 1


async def test_get_streams_follows_pagination(helix):
    streams = await helix.get_streams(["a-live", "b", "c-live", "d-live"])
    assert sorted(s.user_id for s in streams) == ["a-live", "c-live", "d-live"]
    assert streams[0].started_at == datetime(2026, 11, 18, 19, 0, tzinfo=UTC)
    assert streams[0].viewer_count == 5


async def test_expired_token_is_refreshed(helix, twitch):
    await helix.get_users(logins=["a"])
    twitch.reject_next_with_401 = True
    users = await helix.get_users(logins=["b"])
    assert [u.login for u in users] == ["b"]
    assert twitch.token_requests == 2


def test_missing_credentials():
    with pytest.raises(HelixError):
        HelixClient.from_settings(TwitchSettings(_env_file=None))
    assert TwitchSettings(client_id="x", client_secret="y", _env_file=None).has_credentials
