"""Recorder service: which channels get recorded, leases, resume after restart (Postgres)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from clipper_core import leases
from clipper_core.contracts import GapReason, RecordingMode, recording_lease
from clipper_recorder.finisher import Finisher
from clipper_recorder.service import Recorder
from clipper_recorder.settings import RecorderSettings
from clipper_recorder.spool import ChunkState, Spool

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 18, 20, 0, tzinfo=UTC)


class FakeChannelRecorder:
    def __init__(self, assignment, **kwargs) -> None:
        self.a = assignment
        self.tracker = kwargs["tracker"]
        self.next_seq = kwargs["next_seq"]

    async def run(self, stop: asyncio.Event) -> None:
        await stop.wait()


async def add_channel(sessions, login, *, mode="full", active=True, live=True) -> tuple[int, int]:
    async with sessions.begin() as s:
        cid = (
            await s.execute(
                text(
                    "INSERT INTO channels (login, recording_mode, active) "
                    "VALUES (:l, :m, :a) RETURNING id"
                ),
                {"l": login, "m": mode, "a": active},
            )
        ).scalar_one()
        sid = None
        if live:
            sid = (
                await s.execute(
                    text(
                        "INSERT INTO streams (channel_id, platform_stream_id, started_at) "
                        "VALUES (:c, :p, :t) RETURNING id"
                    ),
                    {"c": cid, "p": f"b-{login}", "t": T0},
                )
            ).scalar_one()
    return cid, sid


def make_recorder(sessions, tmp_path, worker="rec-1") -> Recorder:
    spool = Spool(tmp_path)
    return Recorder(
        sessions,
        settings=RecorderSettings(spool_dir=tmp_path, _env_file=None),
        worker_id=worker,
        http=None,
        resolver=None,
        spool=spool,
        finisher=Finisher(sessions, store=None, spool=spool),
        recorder_factory=FakeChannelRecorder,
    )


async def test_records_live_channels_it_can_lease(sessions, tmp_path):
    full, _ = await add_channel(sessions, "full")
    audio, _ = await add_channel(sessions, "audio", mode="audio_only")
    taken, _ = await add_channel(sessions, "taken")
    await add_channel(sessions, "off", mode="off")
    await add_channel(sessions, "offline", live=False)
    await add_channel(sessions, "inactive", active=False)
    async with sessions.begin() as s:  # another recorder already records "taken"
        await leases.acquire(s, recording_lease(taken), "rec-2", timedelta(minutes=1))

    rec = make_recorder(sessions, tmp_path)
    try:
        await rec.sync()
        assert set(rec.running) == {full, audio}
        assert rec.running[audio].assignment.mode == RecordingMode.AUDIO_ONLY
        r = rec.running[full].recorder
        assert r.next_seq == 0
        assert r.tracker.media_end == T0 and r.tracker.reason == GapReason.NOT_RECORDED
        async with sessions() as s:
            assert await leases.holder(s, recording_lease(full)) == "rec-1"

        # stream ends -> the recorder is told to stop, and forgotten once it has
        async with sessions.begin() as s:
            await s.execute(
                text("UPDATE streams SET status = 'ended' WHERE channel_id = :c"), {"c": full}
            )
        await rec.sync()
        assert rec.running[full].stop.is_set()
        await asyncio.wait_for(rec.running[full].task, timeout=1)
        await rec.sync()
        assert set(rec.running) == {audio}
    finally:
        await rec.stop_all()


async def test_resumes_after_chunks_in_db_and_spool(sessions, tmp_path):
    cid, sid = await add_channel(sessions, "dallas")
    async with sessions.begin() as s:
        for seq in range(3):
            await s.execute(
                text(
                    "INSERT INTO chunks (stream_id, seq, start_utc, end_utc) "
                    "VALUES (:s, :q, :a, :b)"
                ),
                {
                    "s": sid,
                    "q": seq,
                    "a": T0 + timedelta(minutes=seq),
                    "b": T0 + timedelta(minutes=seq + 1),
                },
            )
    rec = make_recorder(sessions, tmp_path)
    # a chunk the previous run closed but never uploaded
    rec.spool.save(
        ChunkState(
            stream_id=sid,
            channel="dallas",
            seq=3,
            mode=RecordingMode.FULL,
            start=T0 + timedelta(minutes=3),
            end=T0 + timedelta(minutes=3, seconds=40),
            closed=True,
        )
    )
    try:
        await rec.sync()
        r = rec.running[cid].recorder
        assert r.next_seq == 4
        assert r.tracker.seen_until == T0 + timedelta(minutes=3, seconds=40)
    finally:
        await rec.stop_all()
