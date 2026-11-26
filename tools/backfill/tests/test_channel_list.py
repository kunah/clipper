"""`channel <login>` lists a channel's archives and what we have of each; it writes nothing
(backfill runs only on a VOD the user names, DESIGN §17). Helix on httpx.MockTransport, Postgres.

"Writes nothing" is enforced, not just observed: the listing runs on a read-only engine (every
INSERT/UPDATE/DELETE raises, so a write undone later, like a lease taken and released, fails too),
the backfill machinery (object store, temp dirs, Backfiller, ...) raises when touched, and every
table's content is compared before and after."""

import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from clipper_backfill import cli
from clipper_core.db import create_engine, session_factory
from clipper_core.models import Base
from clipper_twitch.helix import HelixClient
from clipper_twitch.settings import TwitchSettings

pytestmark = pytest.mark.integration

LIVE_VIDEO, LIVE_STREAM = "2000000003", "777000333"  # newest: a broadcast we recorded live
LIVE_START = datetime(2026, 11, 20, 14, 2, 5, tzinfo=UTC)
LIVE_TITLE = "big   stream\ntoday " + "x" * 50  # whitespace squeezed, cut to 40 chars
BF_VIDEO, BF_STREAM = "2000000002", "777000222"  # backfilled (vods row)
BF_T0 = datetime(2026, 11, 19, 6, 27, 49, 784000, tzinfo=UTC)
NEW_VIDEO, NEW_STREAM = "2000000001", "777000111"  # nothing of ours
NEW_CREATED = datetime(2026, 11, 18, 18, 0, 3, tzinfo=UTC)
OLD_VIDEO = "2000000000"  # before --since
CREDENTIALS = {"client_id": "cid", "client_secret": "secret"}
READ_ONLY = {"options": "-c default_transaction_read_only=on"}  # every transaction READ ONLY
# What `vod` and the backfill use; `channel` must touch none of it.
MACHINERY = (
    "_backfill",
    "Backfiller",
    "BackfillSettings",
    "ObjectStore",
    "remove_stale_workdirs",
    "StreamlinkVodSource",
    "GqlChatReplay",
)


def thumbnail(stream_id: str) -> str:
    """A Helix archive thumbnail: the VOD's CDN path carries the broadcast's stream id."""
    return (
        "https://static-cdn.jtvnw.net/cf_vods/d2nvs31859zcd8/"
        f"8173ac3b86530f54c2d4_streamer_{stream_id}_1795000000//thumb/thumb0-%{{width}}x%{{height}}.jpg"
    )


def video(vid: str, stream_id: str | None, created: datetime, duration: str, title: str) -> dict:
    return {
        "id": vid,
        "stream_id": stream_id,
        "user_id": "4242",
        "user_login": "streamer",
        "user_name": "Streamer",
        "title": title,
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


class FakeHelix:
    """Users by login; the channel's archives, newest first (Helix sort=time)."""

    def __init__(self) -> None:
        self.videos = [
            video(LIVE_VIDEO, LIVE_STREAM, LIVE_START, "3h27m12s", LIVE_TITLE),
            video(BF_VIDEO, BF_STREAM, BF_T0 + timedelta(seconds=3), "2m40s", "backfilled one"),
            video(NEW_VIDEO, NEW_STREAM, NEW_CREATED, "45m0s", "never seen"),
            video(OLD_VIDEO, "777000000", datetime(2026, 10, 1, tzinfo=UTC), "1h0m0s", "old"),
        ]
        self.calls: list[httpx.URL] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        assert request.method == "GET"  # Helix reads only
        self.calls.append(request.url)
        params = request.url.params
        if request.url.path == "/helix/users":
            user = {"id": "4242", "login": "streamer", "display_name": "Streamer"}
            found = [user] if params["login"] == "streamer" else []
            return httpx.Response(200, json={"data": found})
        if request.url.path == "/helix/videos":
            assert params["user_id"] == "4242"
            assert (params["type"], params["sort"]) == ("archive", "time")
            return httpx.Response(200, json={"data": self.videos[: int(params["first"])]})
        return httpx.Response(404)


@contextlib.asynccontextmanager
async def helix(fake: FakeHelix):
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as http:
        yield HelixClient("cid", "secret", http=http)


def read_only_engine(settings):
    return create_engine(settings, connect_args=READ_ONLY)


@pytest.fixture
async def read_only(settings, sessions):
    """Sessions on the test database (after `sessions` cleaned it) that can't write."""
    engine = read_only_engine(settings)
    yield session_factory(engine)
    await engine.dispose()


@pytest.fixture(autouse=True)
def no_backfill_machinery(monkeypatch):
    def forbidden(name: str):
        def use(*args, **kwargs):
            raise AssertionError(f"`channel` must not use {name}")

        return use

    for name in MACHINERY:
        monkeypatch.setattr(cli, name, forbidden(name))


async def setup_rows(sessions) -> dict[str, int]:
    async with sessions.begin() as s:

        async def one(sql: str, **values) -> int:
            return (await s.execute(text(sql + " RETURNING id"), values)).scalar_one()

        cid = await one(
            "INSERT INTO channels (login, platform_user_id, recording_mode, active) "
            "VALUES ('streamer', '4242', 'audio_only', true)"
        )
        other = await one("INSERT INTO channels (login, platform_user_id) VALUES ('other', '9999')")
        live = await one(
            "INSERT INTO streams (channel_id, platform_stream_id, started_at, ended_at, status) "
            "VALUES (:c, :p, :t, :e, 'ended')",
            c=cid, p=LIVE_STREAM, t=LIVE_START, e=LIVE_START + timedelta(hours=3),
        )  # fmt: skip
        for seq in range(2):
            start = LIVE_START + timedelta(seconds=10 + 60 * seq)
            await one(
                "INSERT INTO chunks (stream_id, seq, start_utc, end_utc, audio_key) "
                "VALUES (:s, :q, :a, :b, 'k')",
                s=live, q=seq, a=start, b=start + timedelta(seconds=60),
            )  # fmt: skip
        backfilled = await one(
            "INSERT INTO streams (channel_id, platform_stream_id, started_at, ended_at, status, "
            "source) VALUES (:c, :p, :t, :e, 'ended', 'backfill')",
            c=cid, p=BF_STREAM, t=BF_T0, e=BF_T0 + timedelta(seconds=160),
        )  # fmt: skip
        await one(
            "INSERT INTO vods (channel_id, stream_id, platform_video_id, platform_created_at, "
            "duration_s, t0_utc, t0_method, media_status, media_done_s, chat_status, "
            "chat_done_s) VALUES (:c, :s, :v, :t, 160, :t, 'pdt', 'pending', 40, 'done', 160)",
            c=cid, s=backfilled, v=BF_VIDEO, t=BF_T0,
        )  # fmt: skip
        # another channel's stream with NEW_VIDEO's stream id: not ours for this channel
        await one(
            "INSERT INTO streams (channel_id, platform_stream_id, started_at, status) "
            "VALUES (:c, :p, :t, 'ended')",
            c=other, p=NEW_STREAM, t=NEW_CREATED,
        )  # fmt: skip
        await one("INSERT INTO jobs (kind, payload, priority) VALUES ('transcribe', '{}', 10)")
    return {"channel": cid, "live": live, "backfilled": backfilled}


async def snapshot(sessions) -> dict[str, tuple[int, str]]:
    """Every table's row count and a hash of its rows: an insert, update or delete shows."""
    sql = "SELECT count(*), md5(COALESCE(string_agg(x::text, '|' ORDER BY x::text), '')) FROM {} x"
    async with sessions() as s:
        return {
            t.name: tuple((await s.execute(text(sql.format(t.name)))).one())
            for t in Base.metadata.sorted_tables
        }


def twitch(**values) -> TwitchSettings:
    return TwitchSettings(_env_file=None, **{"client_id": None, "client_secret": None, **values})


async def listing(args: list[str], sessions, fake: FakeHelix) -> int:
    async with helix(fake) as client:
        return await cli._channel(
            cli.parse_args(args), sessions, twitch(**CREDENTIALS), helix=client
        )


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO leases (resource, owner, expires_at) VALUES ('backfill:vod:1', 'me', now())",
        "UPDATE channels SET active = false",
        "DELETE FROM leases",
    ],
)
async def test_the_read_only_sessions_refuse_every_write(read_only, sql):
    async with read_only() as s:  # rolled back on close
        with pytest.raises(DBAPIError, match="read-only transaction"):
            await s.execute(text(sql))


async def test_lists_archives_newest_first_with_what_we_have_and_writes_nothing(
    sessions, read_only, capsys
):
    ids = await setup_rows(sessions)
    before = await snapshot(sessions)
    fake = FakeHelix()
    assert await listing(["channel", "Streamer", "--since", "2026-11-01"], read_only, fake) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["video", "created", "(UTC)", "duration", "title", "have"]
    assert [line.split()[0] for line in out[1:-1]] == [LIVE_VIDEO, BF_VIDEO, NEW_VIDEO]
    live, bf, new = out[1:-1]
    assert live.startswith(f"{LIVE_VIDEO}   2026-11-20T14:02:05Z  3:27:12  big stream today xxx")
    assert "..." in live and "\n" not in live
    assert live.endswith(f"live stream {ids['live']}: 2 chunks 14:02:15-14:04:15Z")
    assert bf.endswith(
        f"backfill stream {ids['backfilled']}: 0 chunks; vod: media pending 25%, chat done 100%"
    )
    assert "0:02:40" in bf
    assert new.endswith("never seen" + " " * 32 + "-")  # another channel's stream isn't ours
    assert out[-1] == "backfill one with: clipper-backfill vod <id>"
    assert OLD_VIDEO not in "\n".join(out)  # before --since
    assert [u.path for u in fake.calls] == ["/helix/users", "/helix/videos"]
    assert fake.calls[1].params["first"] == "10"  # default --limit
    assert await snapshot(sessions) == before  # streams, vods, channels, jobs, leases, ...


async def test_a_null_helix_stream_id_falls_back_to_the_thumbnail_path(sessions, read_only, capsys):
    ids = await setup_rows(sessions)
    fake = FakeHelix()
    fake.videos[0].update(stream_id=None, thumbnail_url=thumbnail(LIVE_STREAM))
    processing = "https://vod-secure.twitch.tv/_404/404_processing_%{width}x%{height}.png"
    fake.videos[2].update(stream_id=None, thumbnail_url=processing)  # no CDN path: no stream id
    assert await listing(["channel", "streamer", "--limit", "3"], read_only, fake) == 0
    live, _, new = capsys.readouterr().out.splitlines()[1:-1]
    assert live.endswith(f"live stream {ids['live']}: 2 chunks 14:02:15-14:04:15Z")
    assert new.endswith("never seen" + " " * 32 + "-")


async def test_limit_and_a_channel_we_have_never_seen(sessions, read_only, capsys):
    before = await snapshot(sessions)
    fake = FakeHelix()
    assert await listing(["channel", "streamer", "--limit", "2"], read_only, fake) == 0
    out = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in out[1:-1]] == [LIVE_VIDEO, BF_VIDEO]
    assert all(line.endswith("  -") for line in out[1:-1])  # no channel row: nothing of ours
    assert await snapshot(sessions) == before

    fake.videos = []
    assert await listing(["channel", "streamer"], read_only, fake) == 0
    assert capsys.readouterr().out == "streamer: no archives found\n"


@pytest.mark.parametrize(
    ("key", "value", "error"),
    [("duration", "a while", "ValueError"), ("created_at", None, "KeyError")],  # None: missing
)
async def test_malformed_helix_data_fails_cleanly(read_only, capsys, key, value, error):
    fake = FakeHelix()
    if value is None:
        del fake.videos[1][key]
    else:
        fake.videos[1][key] = value
    assert await listing(["channel", "streamer"], read_only, fake) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith(f"[failed] streamer: {error}: ")


async def test_unknown_login_and_no_credentials_exit_2(read_only, capsys):
    fake = FakeHelix()
    assert await listing(["channel", "nobody"], read_only, fake) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "[error] nobody: unknown channel 'nobody'\n"

    async with helix(fake) as client:
        args = cli.parse_args(["channel", "nobody"])
        assert await cli._channel(args, read_only, twitch(), helix=client) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "CLIPPER_TWITCH_CLIENT_ID" in captured.err
    assert len(fake.calls) == 1  # no credentials: Helix not even asked


async def test_amain_lists_on_a_read_only_engine(sessions, settings, monkeypatch, capsys):
    ids = await setup_rows(sessions)
    before = await snapshot(sessions)
    monkeypatch.setattr(cli, "setup_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "Settings", lambda **kwargs: settings)  # the throwaway database
    monkeypatch.setattr(cli, "create_engine", read_only_engine)
    monkeypatch.setattr(cli, "TwitchSettings", twitch)  # never the worktree's .env
    assert await cli.amain(["channel", "streamer"]) == 2
    assert "lists archives with Helix" in capsys.readouterr().err

    monkeypatch.setattr(cli, "TwitchSettings", lambda: twitch(**CREDENTIALS))
    async with helix(FakeHelix()) as client:
        monkeypatch.setattr(cli, "HelixClient", SimpleNamespace(from_settings=lambda _: client))
        assert await cli.amain(["channel", "streamer", "--limit", "1"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1].endswith(f"live stream {ids['live']}: 2 chunks 14:02:15-14:04:15Z")
    assert await snapshot(sessions) == before
