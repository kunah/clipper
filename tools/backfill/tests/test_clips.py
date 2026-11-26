"""Viewer clips -> viewer_clips: Helix Get Clips/Get Videos on httpx.MockTransport, Postgres."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from clipper_backfill import rows as bf_rows
from clipper_backfill.clips import CLIP_SLACK_S, HELIX_T0_ERROR_S, ClipCollector
from clipper_twitch.helix import HelixClient

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 18, 6, 27, 49, 784000, tzinfo=UTC)  # our backfilled VOD's T0
VIDEO, STREAM = "2000000001", "777000111"  # backfilled
LIVE_VIDEO, LIVE_STREAM = "2000000002", "777000222"  # a VOD of a stream we recorded live
LIVE_CREATED = datetime(2026, 11, 19, 18, 0, 3, tzinfo=UTC)


def video(vid: str, stream_id: str, created: datetime, duration: str) -> dict:
    return {
        "id": vid,
        "stream_id": stream_id,
        "user_id": "4242",
        "user_login": "streamer",
        "user_name": "Streamer",
        "title": "t",
        "description": "",
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "published_at": None,
        "url": "",
        "thumbnail_url": "",
        "viewable": "public",
        "view_count": 1,
        "language": "en",
        "type": "archive",
        "duration": duration,
        "muted_segments": None,
    }


def clip(cid: str, vid: str | None, offset: int | None, views: int, created: datetime) -> dict:
    return {
        "id": cid,
        "url": f"https://clips.twitch.tv/{cid}",
        "embed_url": "",
        "broadcaster_id": "4242",
        "broadcaster_name": "Streamer",
        "creator_id": "1",
        "creator_name": "Clipper",
        "video_id": vid or "",
        "game_id": "1",
        "language": "en",
        "title": f"clip {cid}",
        "view_count": views,
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "thumbnail_url": "",
        "duration": 30.1,
        "vod_offset": offset,
        "is_featured": False,
    }


class FakeHelix:
    """Videos by id; clips filtered by the asked window (by created_at), most viewed first."""

    def __init__(self) -> None:
        self.videos = {
            VIDEO: video(VIDEO, STREAM, T0 + timedelta(seconds=3), "2m40s"),
            LIVE_VIDEO: video(LIVE_VIDEO, LIVE_STREAM, LIVE_CREATED, "1h0m0s"),
        }
        self.clips: list[dict] = []
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        params = request.url.params
        self.calls.append(request.url.path)
        if request.url.path == "/helix/videos":
            found = self.videos.get(params["id"])
            return httpx.Response(200, json={"data": [found] if found else []})
        if request.url.path == "/helix/clips":
            a = datetime.fromisoformat(params["started_at"])
            b = datetime.fromisoformat(params["ended_at"])
            data = [c for c in self.clips if a <= datetime.fromisoformat(c["created_at"]) <= b]
            data.sort(key=lambda c: -c["view_count"])
            return httpx.Response(200, json={"data": data[: int(params["first"])]})
        if request.url.path == "/helix/users":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404)


def helix(fake: FakeHelix) -> HelixClient:
    return HelixClient(
        "cid", "secret", http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    )


async def setup_rows(sessions) -> dict[str, int]:
    async with sessions.begin() as s:
        cid = (
            await s.execute(
                text(
                    "INSERT INTO channels (login, platform_user_id, recording_mode, active) "
                    "VALUES ('streamer', '4242', 'off', false) RETURNING id"
                )
            )
        ).scalar_one()
        backfilled = (
            await s.execute(
                text(
                    "INSERT INTO streams (channel_id, platform_stream_id, started_at, ended_at, "
                    "status, source) VALUES (:c, :p, :t, :e, 'ended', 'backfill') RETURNING id"
                ),
                {"c": cid, "p": STREAM, "t": T0, "e": T0 + timedelta(seconds=160)},
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO vods (channel_id, stream_id, platform_video_id, platform_created_at, "
                "duration_s, t0_utc, t0_method) VALUES (:c, :s, :v, :t, 160, :t, 'pdt')"
            ),
            {"c": cid, "s": backfilled, "v": VIDEO, "t": T0},
        )
        live = (
            await s.execute(
                text(
                    "INSERT INTO streams (channel_id, platform_stream_id, started_at, ended_at, "
                    "status) VALUES (:c, :p, :t, :e, 'ended') RETURNING id"
                ),
                {
                    "c": cid,
                    "p": LIVE_STREAM,
                    "t": LIVE_CREATED,
                    "e": LIVE_CREATED + timedelta(hours=1),
                },
            )
        ).scalar_one()
    return {"channel": cid, "backfilled": backfilled, "live": live}


async def clip_rows(sessions) -> dict[str, dict]:
    async with sessions() as s:
        found = (await s.execute(text("SELECT * FROM viewer_clips ORDER BY clip_id"))).all()
    return {r.clip_id: dict(r._mapping) for r in found}


async def test_clips_of_a_backfilled_vod_map_to_t0_plus_offset(sessions):
    ids = await setup_rows(sessions)
    fake = FakeHelix()
    made = (T0 + timedelta(seconds=60)).replace(microsecond=0)  # Helix: whole seconds
    fake.clips = [
        clip("A", VIDEO, 30, 900, made),
        clip("B", VIDEO, None, 500, made),  # fresh: no offset yet
        clip("C", None, None, 300, made),  # no VOD at all
        clip("D", "999", 10, 100, made),  # another video's clip in the window: not ours
        clip("A", VIDEO, 30, 900, made),  # Helix repeating a clip (across pages)
    ]
    collector = ClipCollector(sessions, helix(fake), top=10)
    result = await collector.for_video(VIDEO)
    assert (result.fetched, result.kept, result.new, result.with_span) == (5, 3, 3, 1)
    assert result.stream_id == ids["backfilled"]
    assert fake.calls.count("/helix/videos") == 1  # VIDEO itself; video 999 never looked up

    got = await clip_rows(sessions)
    assert set(got) == {"A", "B", "C"}
    a = got["A"]
    assert a["start_utc"] == T0 + timedelta(seconds=30)
    assert a["end_utc"] == T0 + timedelta(seconds=60.1)
    assert (a["slack_s"], a["time_source"]) == (30.1 + CLIP_SLACK_S, "vod_t0")
    assert (a["stream_id"], a["channel_id"], a["platform_video_id"]) == (
        ids["backfilled"], ids["channel"], VIDEO
    )  # fmt: skip
    assert (a["vod_offset_s"], a["view_count"], a["platform_created_at"]) == (30, 900, made)
    assert got["B"]["start_utc"] is None and got["B"]["stream_id"] == ids["backfilled"]
    assert got["C"]["platform_video_id"] is None and got["C"]["stream_id"] == ids["backfilled"]

    # later: B has its offset, A's is (briefly) missing, the counts grew
    fake.clips[0] = clip("A", VIDEO, None, 950, made)
    fake.clips[1] = clip("B", VIDEO, 100, 800, made)
    collector = ClipCollector(sessions, helix(fake), top=10)
    result = await collector.for_video(VIDEO)
    assert (result.new, result.refreshed, result.filled) == (0, 3, 1)
    got = await clip_rows(sessions)
    assert got["A"]["vod_offset_s"] == 30 and got["A"]["view_count"] == 950  # kept, refreshed
    assert got["A"]["start_utc"] == T0 + timedelta(seconds=30)
    assert got["B"]["start_utc"] == T0 + timedelta(seconds=100)
    assert got["B"]["time_source"] == "vod_t0" and got["B"]["view_count"] == 800
    async with sessions() as s:
        (channel,) = (await s.execute(text("SELECT recording_mode, active FROM channels"))).all()
    assert tuple(channel) == ("off", False)  # never changed


async def test_clips_of_a_channel_cover_live_streams_with_helix_created_at(sessions):
    ids = await setup_rows(sessions)
    fake = FakeHelix()
    fake.clips = [
        clip("L1", LIVE_VIDEO, 600, 70, LIVE_CREATED + timedelta(minutes=11)),
        clip("L2", "gone", 5, 60, LIVE_CREATED + timedelta(minutes=20)),  # VOD Helix lost
        clip("A", VIDEO, 30, 900, T0 + timedelta(seconds=60)),
    ]
    results = await ClipCollector(sessions, helix(fake)).for_channel("streamer")
    assert [r.stream_id for r in results] == [ids["backfilled"], ids["live"]]
    assert [r.kept for r in results] == [1, 2]
    got = await clip_rows(sessions)
    live = got["L1"]
    assert live["stream_id"] == ids["live"] and live["time_source"] == "helix_created_at"
    assert live["start_utc"] == LIVE_CREATED + timedelta(seconds=600)
    assert live["slack_s"] == pytest.approx(30.1 + CLIP_SLACK_S + HELIX_T0_ERROR_S)
    assert got["L2"]["stream_id"] == ids["live"] and got["L2"]["start_utc"] is None
    assert got["A"]["time_source"] == "vod_t0"
    assert fake.calls.count("/helix/videos") == 2  # LIVE_VIDEO and "gone", once each


async def test_unknown_channel_or_video(sessions):
    fake = FakeHelix()
    with pytest.raises(LookupError, match="no channel"):
        await ClipCollector(sessions, helix(fake)).for_channel("nobody")
    with pytest.raises(LookupError, match="doesn't know video"):
        await ClipCollector(sessions, helix(fake)).for_video("12345")


async def test_a_clip_keeps_the_stream_it_was_first_linked_to(sessions):
    ids = await setup_rows(sessions)
    made = LIVE_CREATED + timedelta(minutes=5)

    def row(stream_id, offset=None):
        return {
            "platform": "twitch",
            "clip_id": "N",
            "channel_id": ids["channel"],
            "stream_id": stream_id,
            "platform_video_id": None,
            "vod_offset_s": offset,
            "duration_s": 30.0,
            "start_utc": None,
            "end_utc": None,
            "slack_s": None,
            "time_source": None,
            "title": "no vod",
            "creator_login": "Clipper",
            "view_count": 5,
            "is_featured": False,
            "platform_created_at": made,
        }

    for stream_id in (None, ids["live"], ids["backfilled"], None):
        async with sessions.begin() as s:
            await bf_rows.upsert_clips(s, [row(stream_id)])
    (stored,) = (await clip_rows(sessions)).values()
    assert stored["stream_id"] == ids["live"]  # filled once, then never taken over
