"""chunks.commit_chunk: chunk + gaps + transcription job in the caller's transaction (Postgres)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from clipper_core import media
from clipper_core.chunks import Gap, commit_chunk
from clipper_core.contracts import DataSource, GapReason, Priority
from clipper_core.timeline import Interval

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 1, 18, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


async def make_stream(sessions) -> int:
    async with sessions.begin() as s:
        cid = (
            await s.execute(text("INSERT INTO channels (login) VALUES ('vodder') RETURNING id"))
        ).scalar_one()
        return (
            await s.execute(
                text(
                    "INSERT INTO streams (channel_id, platform_stream_id, started_at, status,"
                    " source) VALUES (:c, 'v1', :t, 'ended', 'backfill') RETURNING id"
                ),
                {"c": cid, "t": T0},
            )
        ).scalar_one()


async def rows(sessions, sql):
    async with sessions() as s:
        return (await s.execute(text(sql))).all()


async def commit(sessions, stream_id, *, seq=2, gaps=(), audio=True, default=Priority.BACKFILL):
    async with sessions.begin() as s:
        return await commit_chunk(
            s,
            stream_id=stream_id,
            seq=seq,
            interval=Interval(at(60), at(120)),
            video_key=None,
            audio_key=f"streams/{stream_id}/audio/{seq:06d}.aac" if audio else None,
            video_bytes=None,
            audio_bytes=1000 if audio else None,
            source=DataSource.BACKFILL,
            gaps_before=gaps,
            default_priority=default,
        )


async def test_new_chunk_gets_its_gaps_and_a_job_at_the_requested_priority(sessions):
    stream_id = await make_stream(sessions)
    async with sessions.begin() as s:  # someone needs [90 s, 100 s) at TRIGGER
        await media.request_media(
            s,
            stream_id=stream_id,
            window=Interval(at(90), at(100)),
            priority=Priority.TRIGGER,
            requested_by="test",
        )
    gaps = [Gap(at(20), at(40), GapReason.MUTED), Gap(at(40), at(60), GapReason.UNKNOWN)]

    result = await commit(sessions, stream_id, gaps=gaps)

    assert result.created and result.priority == Priority.TRIGGER
    (chunk,) = await rows(sessions, "SELECT * FROM chunks")
    assert (chunk.id, chunk.seq, chunk.source) == (result.chunk_id, 2, "backfill")
    assert (chunk.start_utc, chunk.end_utc) == (at(60), at(120))
    got = await rows(sessions, "SELECT start_utc, end_utc, reason FROM stream_gaps ORDER BY 1")
    assert [tuple(g) for g in got] == [(at(20), at(40), "muted"), (at(40), at(60), "unknown")]
    (job,) = await rows(sessions, "SELECT * FROM jobs")
    assert (job.kind, job.ref_id, job.priority) == ("transcribe", result.chunk_id, 80)
    assert job.payload == {"chunk_id": result.chunk_id}


async def test_recommit_is_idempotent(sessions):
    stream_id = await make_stream(sessions)
    gaps = [Gap(at(0), at(60), GapReason.NOT_RECORDED)]
    first = await commit(sessions, stream_id, gaps=gaps)
    again = await commit(sessions, stream_id, gaps=gaps)

    assert (again.chunk_id, again.created) == (first.chunk_id, False)
    assert len(await rows(sessions, "SELECT * FROM chunks")) == 1
    assert len(await rows(sessions, "SELECT * FROM stream_gaps")) == 1
    (job,) = await rows(sessions, "SELECT priority FROM jobs")
    assert job.priority == Priority.BACKFILL


async def test_chunk_without_audio_gets_no_job(sessions):
    stream_id = await make_stream(sessions)
    result = await commit(sessions, stream_id, audio=False)
    assert result.created and result.priority is None
    assert await rows(sessions, "SELECT * FROM jobs") == []


async def test_nothing_is_written_when_the_callers_transaction_rolls_back(sessions):
    stream_id = await make_stream(sessions)
    with pytest.raises(RuntimeError):
        async with sessions.begin() as s:
            await commit_chunk(
                s,
                stream_id=stream_id,
                seq=0,
                interval=Interval(at(0), at(60)),
                video_key=None,
                audio_key="a.aac",
                video_bytes=None,
                audio_bytes=1,
                source=DataSource.BACKFILL,
                gaps_before=[Gap(at(-5), at(0), GapReason.UNKNOWN)],
                default_priority=Priority.BACKFILL,
            )
            raise RuntimeError("caller failed after commit_chunk")
    for table in ("chunks", "stream_gaps", "jobs"):
        assert await rows(sessions, f"SELECT * FROM {table}") == []


async def test_recommit_with_another_interval_is_refused_and_changes_nothing(sessions):
    stream_id = await make_stream(sessions)
    first = await commit(sessions, stream_id)
    with pytest.raises(ValueError, match="exists as"):
        async with sessions.begin() as s:
            await commit_chunk(
                s,
                stream_id=stream_id,
                seq=2,
                interval=Interval(at(60), at(120.5)),  # same seq, other media
                video_key="other.ts",
                audio_key="other.aac",
                video_bytes=5,
                audio_bytes=5,
                source=DataSource.BACKFILL,
                default_priority=Priority.TRIGGER,
            )
    (chunk,) = await rows(sessions, "SELECT * FROM chunks")
    assert (chunk.id, chunk.end_utc, chunk.video_key) == (first.chunk_id, at(120), None)
    assert chunk.audio_key == f"streams/{stream_id}/audio/000002.aac"
    (job,) = await rows(sessions, "SELECT priority FROM jobs")
    assert job.priority == Priority.BACKFILL


async def test_adding_video_later_keeps_the_audio(sessions):
    stream_id = await make_stream(sessions)
    first = await commit(sessions, stream_id)
    async with sessions.begin() as s:  # e.g. `--video-range` over an audio_only chunk
        again = await commit_chunk(
            s,
            stream_id=stream_id,
            seq=2,
            interval=Interval(at(60), at(120)),
            video_key="v.ts",
            audio_key=None,
            video_bytes=4000,
            audio_bytes=None,
            source=DataSource.BACKFILL,
            default_priority=Priority.BACKFILL,
        )
    assert (again.chunk_id, again.created, again.priority) == (first.chunk_id, False, None)
    (chunk,) = await rows(sessions, "SELECT * FROM chunks")
    assert (chunk.video_key, chunk.video_bytes) == ("v.ts", 4000)
    assert (chunk.audio_key, chunk.audio_bytes) == (f"streams/{stream_id}/audio/000002.aac", 1000)
    assert len(await rows(sessions, "SELECT * FROM jobs")) == 1
