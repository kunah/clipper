"""clipper_twitch.gql: chat replay parsing, the GQL client (pacing, retries, errors), the fake.

Fixture pages are real `VideoCommentsByOffsetOrCursor` answers (xqc archive 2932113307, smoke
test 2026-11-19) with commenter ids/names replaced by placeholders: one from offset 0, one from
offset 41867 (Twitch started it at 41864).
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from clipper_twitch.emotes import message_emotes
from clipper_twitch.gql import (
    Comment,
    FakeChatReplay,
    Fragment,
    GqlBroken,
    GqlChatReplay,
    GqlRateLimited,
    GqlUnavailable,
    ReplayUnavailable,
    VideoInfo,
    parse_page,
    parse_video,
)
from clipper_twitch.settings import (
    DEFAULT_GQL_CLIENT_ID,
    DEFAULT_GQL_COMMENTS_HASH,
    DEFAULT_GQL_WEB_CLIENT_ID,
    TwitchSettings,
)

FIXTURES = Path(__file__).parent / "fixtures"
VIDEO = "2932113307"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


START = fixture("gql_comments_start.json")
MID = fixture("gql_comments_mid.json")


# --------------------------------------------------------------------------- parsing


def test_parse_real_first_page():
    page = parse_page(START, VIDEO)
    assert page.video_id == VIDEO and page.creator_id == "71092938"
    assert len(page.comments) == 53 and page.has_next_page and not page.has_previous_page
    first = page.comments[0]
    assert first.id == "dc6ab89f-bb3a-4d40-8f3b-474d2360e7ff"
    assert first.created_at == datetime(2026, 11, 18, 6, 28, 8, 553000, tzinfo=UTC)
    assert first.created_at.tzinfo is UTC
    assert first.content_offset_s == 14 and type(first.content_offset_s) is int
    assert (first.commenter_id, first.commenter_login, first.commenter_display_name) == (
        "900000", "user1", "User1"
    )  # fmt: skip
    assert first.text == "The fuck were you setting up for 3 days? @user102"
    assert first.badges == (("no_audio", "1"),) and first.user_color == "#1E90FF"
    assert page.cursor == page.comments[-1].cursor and page.cursor.startswith("eyJ")
    offsets = [c.content_offset_s for c in page.comments]
    assert offsets == sorted(offsets)


def test_parse_skips_empty_badges_and_reads_twitch_emotes():
    comments = parse_page(START, VIDEO).comments + parse_page(MID, VIDEO).comments
    by_text = {c.text: c for c in comments}
    assert by_text["WE BACKKKKKKKK"].badges == (("squirtle", "1"),)  # ("", "") dropped
    emote = by_text["sukivtWow ACTUALLY LIVE"]
    assert emote.fragments[0] == Fragment("sukivtWow", "emotesv2_877bfa2e4c684e22a69dd917f24e02e4")
    assert emote.twitch_emotes() == [("emotesv2_877bfa2e4c684e22a69dd917f24e02e4", "sukivtWow", 1)]
    resub = next(c for c in comments if c.text.startswith("xqcL"))
    assert resub.twitch_emotes() == [("1035663", "xqcL", 4)]
    # 7TV/BTTV emotes are plain text: message_emotes matches them by name
    pog = by_text["PogU LIVE"]
    assert pog.twitch_emotes() == []
    assert message_emotes(pog.text, pog.twitch_emotes(), {}, {"PogU": "7tv"}) == [
        {"name": "PogU", "count": 1, "source": "7tv"}
    ]


def test_parse_mid_page_and_one_element_arrays():
    page = parse_page([MID], VIDEO)
    assert page.has_previous_page and page.comments[0].content_offset_s == 41864


def test_deleted_commenter_is_kept_without_user():
    body = copy.deepcopy(START)
    body["data"]["video"]["comments"]["edges"][0]["node"]["commenter"] = None
    c = parse_page(body, VIDEO).comments[0]
    assert (c.commenter_id, c.commenter_login, c.commenter_display_name) == (None, None, None)


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"data": {"video": None}}, ReplayUnavailable),
        ({"errors": [{"message": "failed integrity check"}], "extensions": {}}, GqlBroken),
        (
            {"errors": [{"message": "x"}], "extensions": {"challenge": {"type": "integrity"}}},
            GqlBroken,
        ),
        ({"errors": [{"message": "PersistedQueryNotFound"}]}, GqlBroken),
        ({"data": {"user": {}}}, GqlBroken),
        ({"data": {"video": {"comments": {"edges": []}}}}, GqlBroken),  # no pageInfo
        ({"data": {"video": {"comments": {"edges": [{"node": {}}], "pageInfo": {}}}}}, GqlBroken),
        ({}, GqlBroken),
        ([START, START], GqlBroken),
        ("<html>", GqlBroken),
    ],
)
def test_parse_errors(body, error):
    with pytest.raises(error):
        parse_page(body, VIDEO)


def offset_page(value) -> dict:
    body = copy.deepcopy(START)
    body["data"]["video"]["comments"]["edges"][0]["node"]["contentOffsetSeconds"] = value
    return body


def test_offsets_are_whole_seconds():
    assert parse_page(offset_page(14.0), VIDEO).comments[0].content_offset_s == 14
    for bad in (14.7, "14", None, True):
        with pytest.raises(GqlBroken, match="contentOffsetSeconds"):
            parse_page(offset_page(bad), VIDEO)


@pytest.mark.parametrize(
    ("body", "by_cursor", "error"),
    [
        ({"errors": [{"message": "context deadline exceeded"}]}, False, GqlUnavailable),
        (  # a hiccup with partial data is a hiccup, not "no such video"
            {"errors": [{"message": "context deadline exceeded"}], "data": {"video": None}},
            False,
            GqlUnavailable,
        ),
        ({"errors": [{"message": "something new"}]}, False, GqlBroken),
        ({"data": {"video": {"id": VIDEO, "comments": None}}}, False, ReplayUnavailable),
        ({"data": {"video": {"id": VIDEO, "comments": None}}}, True, GqlUnavailable),
    ],
)
def test_parse_page_raises_only_public_errors(body, by_cursor, error):
    with pytest.raises(error) as err:
        parse_page(body, VIDEO, by_cursor=by_cursor)
    assert type(err.value) is error


def test_has_next_page_without_cursor_is_broken():
    body = {
        "data": {"video": {"comments": {"edges": [], "pageInfo": {"hasNextPage": True}}}},
    }
    with pytest.raises(GqlBroken, match="without a cursor"):
        parse_page(body, VIDEO)


# --------------------------------------------------------------------------- client


class FakeGql:
    """MockTransport handler: answers with the queued responses, then with `default`.

    An item is a Response, an exception to raise, or a function making the Response (built in
    the transport, so errors while reading it surface like on the wire).
    """

    def __init__(self, *responses, default=None) -> None:
        self.queue = list(responses)
        self.default = default if default is not None else httpx.Response(200, json=START)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.queue.pop(0) if self.queue else self.default
        if isinstance(item, Exception):
            raise item
        return item() if callable(item) else item

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


def client(gql: FakeGql, clock: FakeTime, **kwargs) -> GqlChatReplay:
    http = httpx.AsyncClient(transport=httpx.MockTransport(gql))
    return GqlChatReplay.from_settings(
        TwitchSettings(_env_file=None), http=http, sleep=clock.sleep, clock=clock.clock, **kwargs
    )


def deadline() -> httpx.Response:
    # TwitchDownloader #1647: HTTP 200, no data
    return httpx.Response(200, json={"errors": [{"message": "context deadline exceeded"}]})


async def test_request_shape_and_settings_defaults():
    gql, t = FakeGql(), FakeTime()
    replay = client(gql, t)
    await replay.page(VIDEO, offset_s=0)
    await replay.page(VIDEO, cursor="abc")
    first, second = gql.bodies()
    assert first == {
        "operationName": "VideoCommentsByOffsetOrCursor",
        "variables": {"videoID": VIDEO, "contentOffsetSeconds": 0},
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": DEFAULT_GQL_COMMENTS_HASH}},
    }
    assert second["variables"] == {"videoID": VIDEO, "cursor": "abc"}
    assert gql.requests[0].headers["Client-ID"] == DEFAULT_GQL_CLIENT_ID
    assert str(gql.requests[0].url) == "https://gql.twitch.tv/gql"
    assert "Authorization" not in gql.requests[0].headers  # anonymous


def test_settings_override_client_id_and_hash(monkeypatch):
    monkeypatch.setenv("CLIPPER_TWITCH_GQL_CLIENT_ID", "other")
    monkeypatch.setenv("CLIPPER_TWITCH_GQL_COMMENTS_HASH", "f" * 64)
    replay = GqlChatReplay.from_settings(TwitchSettings(_env_file=None), http=httpx.AsyncClient())
    body = replay.body(VIDEO, offset_s=5)
    assert body["extensions"]["persistedQuery"]["sha256Hash"] == "f" * 64
    assert replay._client_id == "other"


async def test_offset_xor_cursor():
    replay = client(FakeGql(), FakeTime())
    with pytest.raises(ValueError):
        await replay.page(VIDEO)
    with pytest.raises(ValueError):
        await replay.page(VIDEO, offset_s=0, cursor="c")


async def test_requests_are_paced_to_two_per_second():
    gql, t = FakeGql(), FakeTime()
    replay = client(gql, t)
    await replay.page(VIDEO, offset_s=0)
    await replay.page(VIDEO, offset_s=10)
    t.now += 0.2
    await replay.page(VIDEO, offset_s=20)
    t.now += 5
    await replay.page(VIDEO, offset_s=30)
    assert t.sleeps == [0.5, 0.3]  # none before the first, none after a long pause
    assert replay.requests == 4


async def test_deadline_exceeded_and_5xx_retry_the_same_cursor():
    gql, t = FakeGql(deadline(), httpx.Response(503), httpx.ConnectTimeout("slow")), FakeTime()
    replay = client(gql, t)
    page = await replay.page(VIDEO, cursor="c-17")
    assert len(page.comments) == 53
    assert [b["variables"] for b in gql.bodies()] == [{"videoID": VIDEO, "cursor": "c-17"}] * 4
    assert t.sleeps == [1.0, 2.0, 3.0]  # linear backoff (>= the 0.5 s pacing)


async def test_429_waits_retry_after():
    gql, t = FakeGql(httpx.Response(429, headers={"Retry-After": "7"})), FakeTime()
    replay = client(gql, t)
    await replay.page(VIDEO, offset_s=0)
    assert t.sleeps == [7.0] and replay.requests == 2


async def test_persistent_429_raises_rate_limited():
    gql, t = FakeGql(default=httpx.Response(429)), FakeTime()
    with pytest.raises(GqlRateLimited):
        await client(gql, t, max_attempts=3).page(VIDEO, offset_s=0)
    assert len(gql.requests) == 3


async def test_persistent_deadline_raises_unavailable():
    gql, t = FakeGql(default=deadline()), FakeTime()
    with pytest.raises(GqlUnavailable) as err:
        await client(gql, t, max_attempts=4).page(VIDEO, offset_s=0)
    assert type(err.value) is GqlUnavailable and len(gql.requests) == 4
    assert max(t.sleeps) <= 30.0


async def test_backoff_is_capped():
    gql, t = FakeGql(default=httpx.Response(500)), FakeTime()
    with pytest.raises(GqlUnavailable):
        await client(gql, t, max_attempts=6, backoff_s=10.0, max_backoff_s=25.0).page(
            VIDEO, offset_s=0
        )
    assert t.sleeps == [10.0, 20.0, 25.0, 25.0, 25.0]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            200,
            json={
                "errors": [{"message": "failed integrity check"}],
                "extensions": {"challenge": {"type": "integrity"}},
            },
        ),
        httpx.Response(200, json={"errors": [{"message": "PersistedQueryNotFound"}]}),
        httpx.Response(400, json={"error": "Bad Request", "message": "The Client-ID is invalid"}),
        httpx.Response(200, json={"data": {"video": {"comments": {"edges": "?"}}}}),
    ],
)
async def test_api_changes_raise_broken_at_once(response):
    gql, t = FakeGql(response), FakeTime()
    with pytest.raises(GqlBroken):
        await client(gql, t).page(VIDEO, offset_s=0)
    assert len(gql.requests) == 1


async def test_unknown_errors_are_retried_then_broken():
    unknown = httpx.Response(200, json={"errors": [{"message": "something new"}]})
    gql, t = FakeGql(unknown), FakeTime()
    assert (await client(gql, t).page(VIDEO, offset_s=0)).comments  # once: retried
    gql, t = FakeGql(default=unknown), FakeTime()
    with pytest.raises(GqlBroken, match="something new"):
        await client(gql, t, max_attempts=3).page(VIDEO, offset_s=0)


async def test_missing_video_and_missing_comments():
    gone = httpx.Response(200, json={"data": {"video": None}})
    gql, t = FakeGql(gone), FakeTime()
    with pytest.raises(ReplayUnavailable):
        await client(gql, t).page("1", offset_s=0)
    assert len(gql.requests) == 1  # not retried
    no_comments = httpx.Response(200, json={"data": {"video": {"id": VIDEO, "comments": None}}})
    gql, t = FakeGql(no_comments), FakeTime()
    assert (await client(gql, t).page(VIDEO, offset_s=0)).comments
    assert t.sleeps == [1.0]  # linear backoff
    gql, t = FakeGql(default=no_comments), FakeTime()
    with pytest.raises(ReplayUnavailable):  # a first page: the video has no replay
        await client(gql, t, max_attempts=3).page(VIDEO, offset_s=0)
    assert t.sleeps == [1.0, 2.0]
    gql, t = FakeGql(default=no_comments), FakeTime()
    with pytest.raises(GqlUnavailable) as err:  # mid-run: a hiccup, not "no replay"
        await client(gql, t, max_attempts=3).page(VIDEO, cursor="c-9")
    assert type(err.value) is GqlUnavailable


async def test_deadline_with_partial_data_is_retried_not_unavailable():
    body = {
        "errors": [{"message": "context deadline exceeded", "path": ["video", "comments"]}],
        "data": {"video": None},
    }
    gql, t = FakeGql(httpx.Response(200, json=body)), FakeTime()
    assert (await client(gql, t).page(VIDEO, cursor="c-3")).comments
    assert [b["variables"]["cursor"] for b in gql.bodies()] == ["c-3", "c-3"]
    gql, t = FakeGql(default=httpx.Response(200, json=body)), FakeTime()
    with pytest.raises(GqlUnavailable) as err:
        await client(gql, t, max_attempts=3).page(VIDEO, offset_s=0)
    assert type(err.value) is GqlUnavailable and len(gql.requests) == 3


async def test_broken_content_encoding_is_retried():
    def bad_gzip():
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=b"not gzip")

    gql, t = FakeGql(bad_gzip), FakeTime()
    assert (await client(gql, t).page(VIDEO, offset_s=0)).comments
    assert len(gql.requests) == 2
    gql, t = FakeGql(default=bad_gzip), FakeTime()
    with pytest.raises(GqlUnavailable):
        await client(gql, t, max_attempts=2).page(VIDEO, offset_s=0)


def test_pacing_cannot_be_set_below_two_requests_per_second():
    with pytest.raises(ValueError, match="min_interval_s"):
        GqlChatReplay(client_id="x", comments_hash="h", min_interval_s=0.1)
    with pytest.raises(ValueError, match="max_attempts"):
        GqlChatReplay(client_id="x", comments_hash="h", max_attempts=0)


async def test_invalid_json_is_retried():
    gql, t = FakeGql(httpx.Response(200, text="<html>gateway</html>")), FakeTime()
    assert (await client(gql, t).page(VIDEO, offset_s=0)).comments
    assert len(gql.requests) == 2


# --------------------------------------------------------------------------- fake


def make_comment(i: int, offset: int) -> Comment:
    return Comment(
        id=f"c{i}",
        created_at=datetime(2026, 11, 18, 6, 28, tzinfo=UTC),
        content_offset_s=offset,
        commenter_id=str(i),
        commenter_login=f"u{i}",
        commenter_display_name=f"U{i}",
        fragments=(Fragment(f"msg {i}"),),
        badges=(),
        user_color=None,
        cursor=None,
    )


async def test_fake_pages_by_offset_and_cursor():
    comments = [make_comment(i, i // 2) for i in range(7)]  # offsets 0,0,1,1,2,2,3
    fake = FakeChatReplay({VIDEO: comments}, page_size=3)
    page = await fake.page(VIDEO, offset_s=1)
    assert [c.id for c in page.comments] == ["c2", "c3", "c4"]
    assert page.has_next_page and page.has_previous_page
    page = await fake.page(VIDEO, cursor=page.cursor)
    assert [c.id for c in page.comments] == ["c5", "c6"] and not page.has_next_page
    empty = await fake.page(VIDEO, offset_s=99)
    assert empty.comments == () and not empty.has_next_page and empty.cursor is None
    assert fake.calls == [(VIDEO, 1, None), (VIDEO, None, f"fake:{VIDEO}:4"), (VIDEO, 99, None)]


async def test_fake_from_fixture_pages_reads_everything_once():
    fake = FakeChatReplay.from_pages(VIDEO, [START, MID, START], page_size=40)
    seen: list[str] = []
    page = await fake.page(VIDEO, offset_s=0)
    seen += [c.id for c in page.comments]
    while page.has_next_page:
        page = await fake.page(VIDEO, cursor=page.cursor)
        seen += [c.id for c in page.comments]
    assert len(seen) == len(set(seen)) == 53 + 56
    assert page.creator_id == "71092938"
    mid = await fake.page(VIDEO, offset_s=41867)
    assert mid.comments[0].content_offset_s == 41867


async def test_fake_errors_and_unknown_videos():
    fake = FakeChatReplay({VIDEO: [make_comment(0, 0)]}, errors={0: GqlUnavailable("down")})
    with pytest.raises(GqlUnavailable):
        await fake.page(VIDEO, offset_s=0)
    assert (await fake.page(VIDEO, offset_s=0)).comments
    with pytest.raises(ReplayUnavailable):
        await fake.page("404", offset_s=0)
    with pytest.raises(GqlBroken):
        await fake.page(VIDEO, cursor="eyJ-real-cursor")


# --------------------------------------------------------------------------- video metadata

# The anonymous answer for the smoke VOD (2026-11-19), title shortened.
VIDEO_BODY = {
    "data": {
        "video": {
            "id": VIDEO,
            "title": "LIVE GTA 6",
            "createdAt": "2026-11-18T06:27:53Z",
            "publishedAt": "2026-11-18T06:27:53Z",
            "lengthSeconds": 83735,
            "status": "RECORDED",
            "broadcastType": "ARCHIVE",
            "owner": {"id": "71092938", "login": "xqc", "displayName": "xQc"},
            "game": {"name": "Just Chatting"},
            "seekPreviewsURL": "https://d2vi6trrdongqn.cloudfront.net/8173ac3b86530f54c2d4_xqc"
            "_321206426716_1794983267/storyboards/2932113307-info.json",
        }
    },
    "extensions": {"durationMilliseconds": 45},
}


def test_parse_video():
    info = parse_video(VIDEO_BODY, VIDEO)
    assert info == VideoInfo(
        id=VIDEO,
        title="LIVE GTA 6",
        created_at=datetime(2026, 11, 18, 6, 27, 53, tzinfo=UTC),
        published_at=datetime(2026, 11, 18, 6, 27, 53, tzinfo=UTC),
        length_s=83735,
        status="RECORDED",
        broadcast_type="ARCHIVE",
        owner_id="71092938",
        owner_login="xqc",
        owner_display_name="xQc",
        game="Just Chatting",
        seek_previews_url=VIDEO_BODY["data"]["video"]["seekPreviewsURL"],
    )
    assert parse_video({"data": {"video": None}}, VIDEO) is None
    bare = {"data": {"video": {"id": "1", "createdAt": "2026-11-18T06:27:53Z", "lengthSeconds": 5}}}
    assert parse_video(bare, "1").owner_login is None and parse_video(bare, "1").game is None
    with pytest.raises(GqlBroken):
        parse_video({"data": {"video": {"id": "1"}}}, "1")
    with pytest.raises(GqlBroken):
        parse_video({"errors": [{"message": "PersistedQueryNotFound"}]}, "1")
    with pytest.raises(GqlUnavailable):
        parse_video({"errors": [{"message": "service error"}]}, "1")
    # `video: null` WITH an unknown error is not "gone" (retried, then broken)
    with pytest.raises(GqlBroken):
        parse_video({"data": {"video": None}, "errors": [{"message": "forbidden"}]}, "1")


async def test_null_video_with_errors_is_retried_not_read_as_gone():
    odd = httpx.Response(200, json={"data": {"video": None}, "errors": [{"message": "odd"}]})
    gql, t = FakeGql(odd, httpx.Response(200, json=VIDEO_BODY)), FakeTime()
    assert (await client(gql, t).video(VIDEO)).id == VIDEO
    gql, t = FakeGql(default=odd), FakeTime()
    with pytest.raises(GqlBroken):
        await client(gql, t, max_attempts=2).video(VIDEO)
    gql, t = FakeGql(default=odd), FakeTime()
    with pytest.raises(GqlBroken):  # the same for chat pages: not ReplayUnavailable
        await client(gql, t, max_attempts=2).page(VIDEO, offset_s=0)


async def test_video_uses_a_plain_query_with_the_web_client_and_shares_pacing():
    gql, t = FakeGql(deadline(), httpx.Response(200, json=VIDEO_BODY)), FakeTime()
    replay = client(gql, t)
    info = await replay.video(VIDEO)
    assert info.owner_login == "xqc" and replay.requests == 2  # the deadline was retried
    body = gql.bodies()[0]
    assert body["variables"] == {"id": VIDEO} and "video(id: $id)" in body["query"]
    assert "seekPreviewsURL" in body["query"] and "extensions" not in body
    assert gql.requests[0].headers["Client-ID"] == DEFAULT_GQL_WEB_CLIENT_ID
    await replay.page(VIDEO, offset_s=0)  # same pacer: waits like any other request
    assert gql.requests[-1].headers["Client-ID"] == DEFAULT_GQL_CLIENT_ID
    assert t.sleeps.count(0.5) >= 1
    with pytest.raises(ValueError):
        await replay.video("../1")


async def test_fake_video():
    fake = FakeChatReplay({}, videos={VIDEO: parse_video(VIDEO_BODY, VIDEO)})
    assert (await fake.video(VIDEO)).owner_id == "71092938"
    assert await fake.video("404") is None
    assert fake.video_calls == [VIDEO, "404"]
