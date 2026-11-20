"""Controller: stream lifecycle from Helix polls, maintenance, channel CLI (needs Postgres)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from clipper_controller import channels_cli
from clipper_controller.maintenance import run_maintenance
from clipper_controller.poller import StreamPoller
from clipper_core import jobs
from clipper_core.contracts import JobKind
from clipper_core.timeline import utc_now
from clipper_twitch.helix import HelixStream, HelixUser

pytestmark = pytest.mark.integration

T = datetime(2026, 11, 18, 20, 0, tzinfo=UTC)


class FakeHelix:
    def __init__(self) -> None:
        self.users = {"alpha": HelixUser("100", "alpha", "Alpha")}
        self.live: dict[str, HelixStream] = {}

    async def get_users(self, *, logins=(), ids=()):
        return [self.users[login] for login in logins if login in self.users]

    async def get_streams(self, user_ids):
        return [self.live[u] for u in user_ids if u in self.live]

    def go_live(self, stream_id: str, viewers: int = 1000) -> None:
        self.live["100"] = HelixStream(
            stream_id, "100", "alpha", "title", "Just Chatting", viewers, T
        )


async def streams(sessions):
    async with sessions() as s:
        return (
            await s.execute(text("SELECT platform_stream_id, status FROM streams ORDER BY id"))
        ).all()


async def test_stream_lifecycle(sessions):
    async with sessions.begin() as s:
        await channels_cli.add(s, ["Alpha", "ghost"], "full")
    helix = FakeHelix()
    poller = StreamPoller(sessions, helix, end_after_misses=2)

    r = await poller.poll_once(T)
    assert r.unknown_logins == ["ghost"] and r.live == 0
    async with sessions() as s:
        uid = (
            await s.execute(text("SELECT platform_user_id FROM channels WHERE login = 'alpha'"))
        ).scalar_one()
    assert uid == "100"

    helix.go_live("s1", viewers=1000)
    r = await poller.poll_once(T + timedelta(seconds=30))
    assert r.started == ["alpha"]
    helix.go_live("s1", viewers=1500)
    r = await poller.poll_once(T + timedelta(seconds=60))
    assert r.started == []
    async with sessions() as s:
        viewers = (
            (await s.execute(text("SELECT viewers FROM viewer_snapshots ORDER BY ts")))
            .scalars()
            .all()
        )
    assert viewers == [1000, 1500]

    helix.live.clear()  # one missing poll is tolerated...
    assert (await poller.poll_once(T + timedelta(seconds=90))).ended == []
    assert await streams(sessions) == [("s1", "live")]
    helix.go_live("s1")  # ...and it came back
    await poller.poll_once(T + timedelta(seconds=120))
    helix.live.clear()
    await poller.poll_once(T + timedelta(seconds=150))
    r = await poller.poll_once(T + timedelta(seconds=180))  # two misses in a row -> ended
    assert r.ended == ["alpha"]
    assert await streams(sessions) == [("s1", "ended")]

    helix.go_live("s2")
    await poller.poll_once(T + timedelta(seconds=210))
    helix.go_live("s3")  # new broadcast replaces s2 immediately
    r = await poller.poll_once(T + timedelta(seconds=240))
    assert r.started == ["alpha"] and r.ended == ["alpha"]
    assert await streams(sessions) == [("s1", "ended"), ("s2", "ended"), ("s3", "live")]


async def test_disabled_channel_streams_end(sessions):
    async with sessions.begin() as s:
        await channels_cli.add(s, ["alpha"], "full")
    helix = FakeHelix()
    helix.go_live("s1")
    poller = StreamPoller(sessions, helix, end_after_misses=1)
    await poller.poll_once(T)
    async with sessions.begin() as s:
        assert await channels_cli.set_field(s, "alpha", "active", False)
    r = await poller.poll_once(T + timedelta(seconds=30))
    assert r.ended == ["alpha"]


async def test_maintenance_creates_partitions_and_requeues_stale_jobs(sessions):
    async with sessions.begin() as s:
        job_id = await jobs.enqueue(s, JobKind.TRANSCRIBE, ref_id=1)
    async with sessions.begin() as s:
        await jobs.claim(s, [JobKind.TRANSCRIBE], "dead-worker")
        await s.execute(text("UPDATE jobs SET locked_at = now() - interval '1 hour'"))
    await run_maintenance(sessions)
    async with sessions() as s:
        status = (
            await s.execute(text("SELECT status FROM jobs WHERE id = :i"), {"i": job_id})
        ).scalar_one()
        far = utc_now().date() + timedelta(days=7)
        part = (
            await s.execute(
                text("SELECT to_regclass(:n)::text"), {"n": f"chat_messages_p{far:%Y%m%d}"}
            )
        ).scalar_one()
    assert status == "queued" and part is not None


async def test_maintenance_requeues_detector_and_judge_jobs_after_their_own_limits(sessions):
    """judge jobs are stale after 10 min, detect_offline after 30 min (DESIGN §18)."""
    ids = {}
    async with sessions.begin() as s:
        for kind in (JobKind.JUDGE, JobKind.DETECT_OFFLINE):
            ids[kind] = await jobs.enqueue(s, kind, ref_id=1)
    async with sessions.begin() as s:
        for kind in ids:
            await jobs.claim(s, [kind], "dead-worker")
        await s.execute(text("UPDATE jobs SET locked_at = now() - interval '20 minutes'"))
    await run_maintenance(sessions)
    async with sessions() as s:
        rows = (await s.execute(text("SELECT kind, status FROM jobs"))).all()
    assert dict(rows) == {"judge": "queued", "detect_offline": "running"}


async def test_channels_cli_commands(sessions):
    async with sessions.begin() as s:
        assert await channels_cli.add(s, ["@XQC", "kai"], "audio_only") == [
            "xqc (added, audio_only)",
            "kai (added, audio_only)",
        ]
        assert await channels_cli.add(s, ["xqc"], "full") == ["xqc (updated, full)"]
        assert await channels_cli.set_field(s, "kai", "recording_mode", "off")
        assert not await channels_cli.set_field(s, "nobody", "active", False)
        listing = await channels_cli.list_channels(s)
    assert listing[1].split()[:3] == ["kai", "yes", "off"]
    assert listing[2].split()[:3] == ["xqc", "yes", "full"]


def test_cli_argument_parsing():
    args = channels_cli.parse_args(["add", "a", "b", "--mode", "audio_only"])
    assert (args.cmd, args.logins, args.mode) == ("add", ["a", "b"], "audio_only")
    with pytest.raises(SystemExit):
        channels_cli.parse_args(["mode", "a", "loud"])
