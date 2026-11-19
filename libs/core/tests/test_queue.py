"""Job queue, demand-driven priorities, leases, heartbeats (needs Postgres)."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import text

from clipper_core import heartbeat, jobs, leases, media
from clipper_core.contracts import JobKind, JobStatus, Priority, TranscribePayload, dedupe_key
from clipper_core.timeline import Interval, utc_now

pytestmark = pytest.mark.integration

T0 = utc_now().replace(microsecond=0)


def at(seconds: float):
    return T0 + timedelta(seconds=seconds)


async def make_stream(session) -> int:
    channel_id = (
        await session.execute(text("INSERT INTO channels (login) VALUES ('test') RETURNING id"))
    ).scalar_one()
    return (
        await session.execute(
            text(
                "INSERT INTO streams (channel_id, platform_stream_id, started_at) "
                "VALUES (:c, 'stream-1', :t) RETURNING id"
            ),
            {"c": channel_id, "t": T0},
        )
    ).scalar_one()


async def add_chunk(session, stream_id: int, seq: int) -> int:
    """Chunk `seq` covers [seq*60, seq*60+60) seconds after T0."""
    return (
        await session.execute(
            text(
                "INSERT INTO chunks (stream_id, seq, start_utc, end_utc) "
                "VALUES (:s, :q, :a, :b) RETURNING id"
            ),
            {"s": stream_id, "q": seq, "a": at(seq * 60), "b": at(seq * 60 + 60)},
        )
    ).scalar_one()


async def enqueue_transcribe(session, chunk_id: int, priority: int = Priority.LIVE, **kw) -> int:
    return await jobs.enqueue(
        session,
        JobKind.TRANSCRIBE,
        payload=TranscribePayload(chunk_id=chunk_id).model_dump(),
        ref_id=chunk_id,
        dedupe_key=dedupe_key(JobKind.TRANSCRIBE, chunk_id),
        priority=priority,
        **kw,
    )


async def claim_one(sessions, worker="w1"):
    async with sessions.begin() as session:
        return await jobs.claim(session, [JobKind.TRANSCRIBE], worker)


async def job_row(sessions, job_id: int):
    async with sessions() as session:
        return (
            await session.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id})
        ).one()


async def test_claims_highest_priority_first_then_fifo(sessions):
    async with sessions.begin() as session:
        stream_id = await make_stream(session)
        c = [await add_chunk(session, stream_id, i) for i in range(3)]
        await enqueue_transcribe(session, c[0], Priority.LIVE)
        await enqueue_transcribe(session, c[1], Priority.TRIGGER)
        await enqueue_transcribe(session, c[2], Priority.LIVE)

    order = [(await claim_one(sessions)).ref_id for _ in range(3)]
    assert order == [c[1], c[0], c[2]]
    assert await claim_one(sessions) is None


async def test_enqueue_same_key_only_raises_priority(sessions):
    async with sessions.begin() as session:
        stream_id = await make_stream(session)
        chunk = await add_chunk(session, stream_id, 0)
        first = await enqueue_transcribe(session, chunk, Priority.LIVE)
        second = await enqueue_transcribe(session, chunk, Priority.TRIGGER)
        third = await enqueue_transcribe(session, chunk, Priority.BACKFILL)
    assert first == second == third
    assert (await job_row(sessions, first)).priority == Priority.TRIGGER


async def test_judge_context_request_bumps_queued_chunks(sessions):
    async with sessions.begin() as session:
        stream_id = await make_stream(session)
        c = [await add_chunk(session, stream_id, i) for i in range(5)]
        for chunk in c:
            await enqueue_transcribe(session, chunk, Priority.LIVE)

    # Judge wants more context: 70s..150s touches chunks 1 and 2.
    async with sessions.begin() as session:
        await media.request_media(
            session,
            stream_id=stream_id,
            window=Interval(at(70), at(150)),
            priority=Priority.JUDGE_CONTEXT,
            requested_by="judge",
        )

    first, second, third = [await claim_one(sessions) for _ in range(3)]
    assert [first.ref_id, second.ref_id] == [c[1], c[2]]
    assert first.priority == second.priority == Priority.JUDGE_CONTEXT
    assert third.ref_id == c[0] and third.priority == Priority.LIVE


async def test_request_for_future_range_sets_priority_of_new_chunks(sessions):
    async with sessions.begin() as session:
        stream_id = await make_stream(session)
        # Trigger at 360s: want 270s..390s, but chunks 4..6 aren't recorded yet.
        await media.request_media(
            session,
            stream_id=stream_id,
            window=Interval(at(270), at(390)),
            priority=Priority.TRIGGER,
            requested_by="detector",
        )

    async with sessions() as session:
        inside = await media.priority_for_range(
            session, stream_id=stream_id, window=Interval(at(360), at(420)), default=Priority.LIVE
        )
        outside = await media.priority_for_range(
            session, stream_id=stream_id, window=Interval(at(420), at(480)), default=Priority.LIVE
        )
    assert inside == Priority.TRIGGER
    assert outside == Priority.LIVE


async def test_running_jobs_are_not_bumped(sessions):
    async with sessions.begin() as session:
        stream_id = await make_stream(session)
        chunk = await add_chunk(session, stream_id, 0)
        job_id = await enqueue_transcribe(session, chunk, Priority.LIVE)
    await claim_one(sessions)
    async with sessions.begin() as session:
        changed = await jobs.bump_priority(session, JobKind.TRANSCRIBE, [chunk], Priority.TRIGGER)
    assert changed == 0
    assert (await job_row(sessions, job_id)).status == JobStatus.RUNNING


async def test_failed_job_retries_with_backoff_then_gives_up(sessions):
    async with sessions.begin() as session:
        stream_id = await make_stream(session)
        chunk = await add_chunk(session, stream_id, 0)
        job_id = await enqueue_transcribe(session, chunk, max_attempts=2)

    job = await claim_one(sessions)
    async with sessions.begin() as session:
        assert await jobs.fail(session, job, "boom 1") == JobStatus.QUEUED
    assert await claim_one(sessions) is None  # waiting for backoff

    async with sessions.begin() as session:
        await session.execute(
            text("UPDATE jobs SET run_after = now() WHERE id = :id"), {"id": job_id}
        )
    job = await claim_one(sessions)
    assert job.attempts == 2
    async with sessions.begin() as session:
        assert await jobs.fail(session, job, "boom 2") == JobStatus.FAILED
    row = await job_row(sessions, job_id)
    assert row.status == JobStatus.FAILED and row.last_error == "boom 2" and row.finished_at


async def test_two_workers_never_get_the_same_job(sessions):
    async with sessions.begin() as session:
        stream_id = await make_stream(session)
        for i in range(2):
            await enqueue_transcribe(session, await add_chunk(session, stream_id, i))

    async with sessions.begin() as a, sessions.begin() as b:
        job_a = await jobs.claim(a, [JobKind.TRANSCRIBE], "a")  # row lock held until commit
        job_b = await jobs.claim(b, [JobKind.TRANSCRIBE], "b")
    assert job_a and job_b and job_a.id != job_b.id


async def test_stale_running_jobs_are_requeued(sessions):
    async with sessions.begin() as session:
        stream_id = await make_stream(session)
        job_id = await enqueue_transcribe(session, await add_chunk(session, stream_id, 0))
    await claim_one(sessions)
    async with sessions.begin() as session:
        await session.execute(
            text("UPDATE jobs SET locked_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": job_id},
        )
        assert await jobs.requeue_stale(session, JobKind.TRANSCRIBE, timedelta(minutes=10)) == 1
    assert (await claim_one(sessions)).id == job_id


async def test_runner_wakes_on_notify_and_records_failures(sessions, settings):
    async with sessions.begin() as session:
        stream_id = await make_stream(session)
        good = await add_chunk(session, stream_id, 0)
        bad = await add_chunk(session, stream_id, 1)

    handled: list[int] = []
    done = asyncio.Event()

    async def handler(job: jobs.ClaimedJob) -> None:
        if job.ref_id == bad:
            raise RuntimeError("bad chunk")
        handled.append(job.payload["chunk_id"])
        done.set()

    runner = jobs.JobRunner(
        sessions, settings.database_dsn, "runner-1", {JobKind.TRANSCRIBE: handler}, poll_interval=60
    )
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run(stop))
    try:
        await asyncio.sleep(1)  # runner is idle and LISTENing; only NOTIFY can wake it now
        async with sessions.begin() as session:
            bad_job = await enqueue_transcribe(session, bad, Priority.TRIGGER)
            await enqueue_transcribe(session, good, Priority.LIVE)
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    assert handled == [good]
    row = await job_row(sessions, bad_job)
    assert row.status == JobStatus.QUEUED and row.attempts == 1
    assert row.last_error == "RuntimeError: bad chunk"


class BrokenInput(jobs.PermanentJobError):
    pass


async def test_permanent_error_fails_the_job_at_once(sessions):
    async with sessions.begin() as session:
        stream_id = await make_stream(session)
        broken = await add_chunk(session, stream_id, 0)
        flaky = await add_chunk(session, stream_id, 1)
        broken_job = await enqueue_transcribe(session, broken, Priority.TRIGGER)
        flaky_job = await enqueue_transcribe(session, flaky, Priority.LIVE)

    async def handler(job: jobs.ClaimedJob) -> None:
        if job.ref_id == broken:
            raise BrokenInput("file can't be decoded")
        raise RuntimeError("try again")

    runner = jobs.JobRunner(sessions, "unused", "runner-1", {JobKind.TRANSCRIBE: handler})
    assert await runner.run_once() and await runner.run_once()
    row = await job_row(sessions, broken_job)
    assert row.status == JobStatus.FAILED and row.attempts == 1 < row.max_attempts
    assert row.last_error == "BrokenInput: file can't be decoded"
    assert row.finished_at is not None and row.locked_by is None
    row = await job_row(sessions, flaky_job)  # ordinary errors still retry with backoff
    assert row.status == JobStatus.QUEUED and row.attempts == 1 and row.finished_at is None


async def test_fail_permanent_ignores_remaining_attempts(sessions):
    async with sessions.begin() as session:
        stream_id = await make_stream(session)
        await enqueue_transcribe(session, await add_chunk(session, stream_id, 0))
    job = await claim_one(sessions)
    async with sessions.begin() as session:
        assert await jobs.fail(session, job, "broken", permanent=True) == JobStatus.FAILED
    assert (await job_row(sessions, job.id)).status == JobStatus.FAILED


async def test_leases(sessions):
    ttl = timedelta(seconds=30)
    async with sessions.begin() as session:
        assert await leases.acquire(session, "record:channel:1", "a", ttl)
        assert not await leases.acquire(session, "record:channel:1", "b", ttl)
        assert await leases.acquire(session, "record:channel:1", "a", ttl)  # renew
        assert await leases.holder(session, "record:channel:1") == "a"
        await session.execute(text("UPDATE leases SET expires_at = now() - interval '1 second'"))
        assert await leases.holder(session, "record:channel:1") is None
        assert await leases.acquire(session, "record:channel:1", "b", ttl)  # take over
        await leases.release(session, "record:channel:1", "b")
        assert await leases.holder(session, "record:channel:1") is None


async def test_heartbeat_upsert(sessions):
    started = utc_now()
    async with sessions.begin() as session:
        await heartbeat.beat(session, worker_id="w1", service="recorder", started_at=started)
        await heartbeat.beat(
            session, worker_id="w1", service="recorder", started_at=started, info={"channels": 3}
        )
    async with sessions() as session:
        rows = (await session.execute(text("SELECT * FROM worker_heartbeats"))).all()
    assert len(rows) == 1 and rows[0].info == {"channels": 3}
