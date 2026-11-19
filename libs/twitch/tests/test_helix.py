import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from clipper_twitch.helix import HelixClient, HelixError, MutedSegment, parse_duration
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


# --------------------------------------------------------------------------- videos and clips

FIXTURES = Path(__file__).parent / "fixtures"


class PagedHelix:
    """Serves fixture items (shaped like the Helix docs) in pages of `first`, cursor "c<index>".

    Like Helix, a non-empty last page still carries a cursor; the page after it is empty.
    """

    def __init__(self) -> None:
        self.videos = json.loads((FIXTURES / "helix_videos.json").read_text())["data"]
        self.clips = json.loads((FIXTURES / "helix_clips.json").read_text())["data"]
        self.requests: list[httpx.QueryParams] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 5000000})
        params = request.url.params
        self.requests.append(params)
        if request.url.path == "/helix/videos":
            kind = params["type"]
            items = [v for v in self.videos if kind == "all" or v["type"] == kind]
        elif request.url.path == "/helix/clips":
            items = self.clips
        else:
            return httpx.Response(404)
        start = int(params.get("after", "c0")[1:])
        page = items[start : start + int(params["first"])]
        cursor = {"cursor": f"c{start + len(page)}"} if page else {}
        return httpx.Response(200, json={"data": page, "pagination": cursor})


@pytest.fixture
def paged():
    return PagedHelix()


@pytest.fixture
def paged_helix(paged):
    http = httpx.AsyncClient(transport=httpx.MockTransport(paged.handler))
    return HelixClient("cid", "secret", http=http)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("1h0m5s", 3605), ("3m21s", 201), ("45s", 45), ("2h", 7200), ("23h15m35s", 83735), ("0s", 0)],
)
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "1d", "5m1h", "abc", "1h 5m", "-3s"])
def test_parse_duration_rejects_other_formats(text):
    with pytest.raises(ValueError):
        parse_duration(text)


async def test_get_videos_parses_archives(paged_helix, paged):
    page = await paged_helix.get_videos("71092938")
    assert [v.id for v in page.items] == ["2932113307", "2929044358", "2926188923", "2924308181"]
    assert page.cursor is None
    first, second = page.items[:2]
    assert first.stream_id == "321206426716" and first.type == "archive"
    assert first.created_at == datetime(2026, 11, 18, 6, 27, 53, tzinfo=UTC)
    assert first.created_at.tzinfo is UTC and first.published_at == first.created_at
    assert first.duration_s == 83735 and first.view_count == 51234
    assert first.muted_segments is None  # app tokens always get null
    assert second.published_at is None
    assert second.muted_segments == (MutedSegment(120, 30), MutedSegment(7200, 360))
    req = paged.requests[0]
    assert (req["user_id"], req["type"], req["sort"], req["first"]) == (
        "71092938", "archive", "time", "100"
    )  # fmt: skip


async def test_get_videos_follows_cursors_until_an_empty_page(paged_helix, paged):
    page = await paged_helix.get_videos("71092938", type="all", first=2)
    assert len(page.items) == 5 and page.cursor is None
    assert page.items[2].stream_id is None and page.items[2].type == "upload"
    # 2 + 2 + 1 items, then the empty page after the last cursor
    assert [r.get("after") for r in paged.requests] == [None, "c2", "c4", "c5"]


async def test_get_videos_limit_asks_only_for_what_is_wanted_and_resumes(paged_helix, paged):
    page = await paged_helix.get_videos("71092938", first=2, limit=3)
    assert [v.id for v in page.items] == ["2932113307", "2929044358", "2926188923"]
    assert [r["first"] for r in paged.requests] == ["2", "1"]
    assert page.cursor == "c3"
    rest = await paged_helix.get_videos("71092938", first=2, after=page.cursor)
    assert [v.id for v in rest.items] == ["2924308181"] and rest.cursor is None


async def test_get_videos_rejects_bad_page_sizes(paged_helix):
    with pytest.raises(ValueError):
        await paged_helix.get_videos("1", first=101)
    with pytest.raises(ValueError):
        await paged_helix.get_videos("1", limit=0)


async def test_get_clips_window_and_fields(paged_helix, paged):
    cest = timezone(timedelta(hours=2))
    page = await paged_helix.get_clips(
        "71092938",
        started_at=datetime(2026, 11, 18, 8, 27, 53, tzinfo=cest),
        ended_at=datetime(2026, 11, 19, 5, 44, tzinfo=UTC),
    )
    req = paged.requests[0]
    assert req["broadcaster_id"] == "71092938"
    assert (req["started_at"], req["ended_at"]) == ("2026-11-18T06:27:53Z", "2026-11-19T05:44:00Z")
    top, fresh, no_vod = page.items
    assert top.video_id == "2932113307" and top.vod_offset == 1980 and top.duration == 30.1
    assert top.is_featured and top.view_count == 9001 and top.creator_name == "Clipper1"
    assert top.created_at == datetime(2026, 11, 18, 7, 1, 12, tzinfo=UTC)
    assert fresh.vod_offset is None and fresh.duration == 60.0
    assert no_vod.video_id is None and not no_vod.is_featured


async def test_get_clips_refuses_naive_times(paged_helix):
    with pytest.raises(ValueError, match="naive"):
        await paged_helix.get_clips(
            "1", started_at=datetime(2026, 11, 18), ended_at=datetime(2026, 11, 19, tzinfo=UTC)
        )


async def test_a_repeated_cursor_raises_instead_of_looping():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 5000})
        calls += 1
        name = "helix_videos.json" if request.url.path.endswith("videos") else "helix_clips.json"
        item = json.loads((FIXTURES / name).read_text())["data"][0]
        return httpx.Response(200, json={"data": [item], "pagination": {"cursor": "same"}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    helix = HelixClient("cid", "secret", http=http)
    day = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(HelixError, match="cursor repeated"):
        await helix.get_clips("1", started_at=day, ended_at=day + timedelta(days=1))
    assert calls == 2
    with pytest.raises(HelixError, match="cursor repeated"):  # also when resuming from it
        await helix.get_videos("1", after="same")
