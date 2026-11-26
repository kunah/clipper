"""Guard 1 (DESIGN §17): the controller never updates a stream the backfill tool wrote."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from clipper_controller import channels_cli
from clipper_controller.poller import StreamPoller
from clipper_twitch.helix import HelixStream, HelixUser

pytestmark = pytest.mark.integration

T = datetime(2026, 11, 18, 20, 0, tzinfo=UTC)


class FakeHelix:
    def __init__(self) -> None:
        self.live: dict[str, HelixStream] = {}

    async def get_users(self, *, logins=(), ids=()):
        return [HelixUser("100", "alpha", "Alpha")] if "alpha" in logins else []

    async def get_streams(self, user_ids):
        return [self.live[u] for u in user_ids if u in self.live]

    def go_live(self, stream_id: str) -> None:
        self.live["100"] = HelixStream(stream_id, "100", "alpha", "new title", "Chess", 500, T)


async def add_stream(sessions, platform_stream_id: str, *, status: str, source: str) -> int:
    async with sessions.begin() as s:
        return (
            await s.execute(
                text(
                    "INSERT INTO streams (channel_id, platform_stream_id, title, started_at,"
                    " ended_at, status, source)"
                    " SELECT id, :sid, 'vod title', :t, :end, :status, :source"
                    " FROM channels WHERE login = 'alpha' RETURNING id"
                ),
                {
                    "sid": platform_stream_id,
                    "t": T - timedelta(hours=5),
                    "end": T - timedelta(hours=1) if status == "ended" else None,
                    "status": status,
                    "source": source,
                },
            )
        ).scalar_one()


async def stream_rows(sessions):
    async with sessions() as s:
        return (
            await s.execute(
                text("SELECT platform_stream_id, title, status, source FROM streams ORDER BY id")
            )
        ).all()


async def test_backfilled_stream_is_never_updated_by_the_poller(sessions):
    async with sessions.begin() as s:
        await channels_cli.add(s, ["alpha"], "full")
    await add_stream(sessions, "s1", status="ended", source="backfill")
    helix = FakeHelix()
    poller = StreamPoller(sessions, helix, end_after_misses=1)

    helix.go_live("s1")  # Helix reports the backfilled stream's id as live
    for i in range(2):
        r = await poller.poll_once(T + timedelta(seconds=30 * i))
        assert r.started == [] and r.ended == []

    assert [tuple(r) for r in await stream_rows(sessions)] == [
        ("s1", "vod title", "ended", "backfill")
    ]
    async with sessions() as s:
        assert (await s.execute(text("SELECT count(*) FROM viewer_snapshots"))).scalar() == 0

    helix.go_live("s2")  # a real new broadcast is tracked as usual
    r = await poller.poll_once(T + timedelta(seconds=90))
    assert r.started == ["alpha"]
    assert [tuple(r) for r in await stream_rows(sessions)][1] == ("s2", "new title", "live", "live")


async def test_a_backfilled_stream_can_never_be_live(sessions):
    """The recorder and chat-ingest follow every `status='live'` row: the DB keeps backfilled
    streams `ended` (CHECK `ck_streams_backfill_ended`), and `source` is live or backfill."""
    async with sessions.begin() as s:
        await channels_cli.add(s, ["alpha"], "full")
    with pytest.raises(IntegrityError, match="ck_streams_backfill_ended"):
        await add_stream(sessions, "s1", status="live", source="backfill")
    with pytest.raises(IntegrityError, match="ck_streams_source"):
        await add_stream(sessions, "s1", status="ended", source="vod")
    sid = await add_stream(sessions, "s1", status="ended", source="backfill")
    with pytest.raises(IntegrityError, match="ck_streams_backfill_ended"):
        async with sessions.begin() as s:
            await s.execute(text("UPDATE streams SET status = 'live' WHERE id = :i"), {"i": sid})
    assert [tuple(r) for r in await stream_rows(sessions)] == [
        ("s1", "vod title", "ended", "backfill")
    ]
