"""The judge service against Postgres (DESIGN §19 build item 6c "Done"), with fake models.

The world (conftest): a live stream with 60 s chunks around T (video + audio, queued transcribe
jobs at LIVE), the sample transcript, a candidate as the detector emits it (window [T - 90,
T + 30], moment [T - 6, T + 4], `judge:{cand}` at TRIGGER). The clock is injected: it starts at
T + 40 s, so a clip created then waits since T + 40 (soft deadline T + 280, hard T + 1840).
"""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from typing import ClassVar

import pytest
from sqlalchemy import text

from clipper_core import jobs
from clipper_core.contracts import (
    ClipRejectReason,
    ClipStatus,
    EditPlan,
    JobKind,
    JobStatus,
    NoticeKind,
    Priority,
    RevisePlanPayload,
    dedupe_key,
    judge_round_key,
)
from clipper_core.jobs import PermanentJobError
from clipper_core.storage import keys
from clipper_judge import reads, writes
from clipper_judge.fakes import FakeJudgeModel, ScriptedJudgeModel, response_for
from clipper_judge.model import (
    ModelError,
    PermanentModelError,
    RateLimitReading,
    UsageLimitError,
    UsageWindow,
)
from clipper_judge.prompt import request_hash
from clipper_judge.revisions import ReviewEventsSource, Revision

pytestmark = pytest.mark.integration


def clip_answer(in_s: float = -11.5, out_s: float = 4.0, title: str = "He jumped off the map"):
    return {
        "reasons": ["he jumps off the map"],
        "verdict": "clip",
        "confidence": 0.8,
        "need_context": None,
        "plan": {"in_s": in_s, "out_s": out_s, "title": title, "hashtags": ["#xqc", "jumpking"]},
    }


def context_answer(before_s: float, after_s: float, why: str = "the setup before the jump"):
    return {
        "reasons": ["can't tell without the setup"],
        "verdict": "need_context",
        "confidence": 0.5,
        "need_context": {"before_s": before_s, "after_s": after_s, "why": why},
        "plan": None,
    }


def with_reading(answer, reading: RateLimitReading):
    """A scripted call whose response carries a usage reading."""

    def make(request):
        return replace(response_for(request, json.dumps(answer), prefix="s"), rate_limit=reading)

    return make


def reading(world, five_hour: float, *, resets_in_s: float = 3600) -> RateLimitReading:
    resets = world.clock.now + timedelta(seconds=resets_in_s)
    return RateLimitReading(
        observed_at=world.clock.now,
        status="allowed",
        resets_at=resets,
        five_hour=UsageWindow(utilization=five_hour, resets_at=resets, status="allowed"),
        seven_day=UsageWindow(utilization=0.1),
    )


async def round_job(world, cand: int, round_: int):
    rows = await world.fetch(
        "SELECT *, xmin::text AS x FROM jobs WHERE dedupe_key = :k", k=judge_round_key(cand, round_)
    )
    return rows[0] if rows else None


# --------------------------------------------------------------------------- waiting, waking


async def test_partial_transcripts_wait(world):
    await world.transcribe_range(-120, 0)  # [0, 60) not transcribed yet: the context's end
    cand = await world.candidate()
    model = FakeJudgeModel()
    svc = world.service(model)

    assert await world.run_jobs(svc) == 1  # the detector's job: creates the clip, which waits
    clip = await world.clip(cand)
    assert (clip.status, clip.round, clip.reject_reason) == (ClipStatus.WAITING_FOR_MEDIA, 0, None)
    assert (clip.context_from, clip.context_to) == (world.at(-90), world.at(30))
    assert clip.anchor_utc == world.at(0) and clip.priority == Priority.TRIGGER
    assert clip.waiting_since == world.at(40) and clip.flags == {}
    (job,) = await world.jobs(JobKind.JUDGE)
    assert job.status == JobStatus.DONE and job.dedupe_key == dedupe_key(JobKind.JUDGE, cand)
    assert await svc.waiter.check() == 0  # not ready, no deadline passed
    assert not model.requests and await world.plans(clip.id) == []
    assert await round_job(world, cand, 0) is None


async def test_transcript_and_notify_plan_with_render_job_in_one_transaction(world):
    await world.transcribe_range(-120, 0)
    await world.chat([(-5.0, "viewer1", "KEKW"), (-4.0, "nightbot", "Follow the channel!")])
    cand = await world.candidate()
    model = FakeJudgeModel()
    svc = world.service(model, sweep_interval_s=3600)  # only the NOTIFY can wake the waiter
    assert await world.run_jobs(svc) == 1
    clip_id = (await world.clip(cand)).id

    stop = asyncio.Event()
    task = asyncio.create_task(svc.waiter.run(stop))
    try:
        for _ in range(100):
            if svc.waiter.listening:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)  # the start-up sweep found nothing ready
        assert svc.waiter.enqueued == 0
        await world.transcribe_range(0, 60, notify=True)  # the transcriber's commit + NOTIFY
        for _ in range(100):
            if await round_job(world, cand, 0) is not None:
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)
    job = await round_job(world, cand, 0)
    assert job is not None and job.priority == Priority.TRIGGER and job.ref_id == cand

    assert await world.run_jobs(svc) == 1
    clip = await world.clip(cand)
    assert clip.status == ClipStatus.PLANNED and clip.plan_version == 1
    assert clip.judged_at is not None and clip.error is None
    (plan,) = await world.plans(clip_id)
    assert (plan.version, plan.round, plan.verdict, plan.author) == (1, 0, "clip", "judge")
    edit = EditPlan.model_validate(plan.plan)
    assert edit.clip_id == clip_id and edit.anchor_utc == world.at(0)
    assert edit.model == "fake" and plan.prompt_version == edit.prompt_version
    assert edit.title == "so I went in there and he just"  # the fake: first core sentence
    (render,) = await world.jobs(JobKind.RENDER)
    assert render.ref_id == plan.id and render.dedupe_key == dedupe_key(JobKind.RENDER, plan.id)
    assert render.payload == {"plan_id": plan.id, "profile": "short_1080x1920"}
    assert render.priority == clip.priority and render.status == JobStatus.QUEUED
    # one transaction: the plan, the clip's status and the render job
    assert plan.x == clip.x == render.x

    (call,) = await world.calls()
    assert call.x != plan.x  # committed alone, right after the call
    assert plan.judge_call_id == call.id and call.clip_id == clip_id and call.error is None
    (request,) = model.requests
    assert call.request_hash == request_hash("fake", request, model.options)
    assert call.io_key == keys.judge_io(call.request_hash, clip_id)
    record = json.loads(world.store.objects[call.io_key])
    assert record["request"]["user"] == request.user and record["response"]["text"]
    assert call.answer["verdict"] == "clip" and call.input_tokens > 0
    assert "Follow the channel" not in request.user  # bots left out of the chat summary
    assert "1 msgs, 1 users" in request.user


async def test_waiter_skips_locked_clips_and_queued_rounds(world, sessions):
    cand = await world.candidate()
    svc = world.service(FakeJudgeModel())
    await world.run_jobs(svc)
    await world.transcribe_range(-120, 60)
    clip_id = (await world.clip(cand)).id
    async with sessions.begin() as s:  # another judge process holds the clip
        await s.execute(text("SELECT 1 FROM clips WHERE id = :id FOR UPDATE"), {"id": clip_id})
        assert await svc.waiter.check() == 0
    assert await svc.waiter.check() == 1
    assert await svc.waiter.check() == 0  # its round job is queued already
    async with sessions.begin() as s:  # its round job failed before it loaded the clip
        await s.execute(
            text("UPDATE jobs SET status = 'failed' WHERE dedupe_key = :k"),
            {"k": judge_round_key(cand, 0)},
        )
    assert await svc.waiter.check() == 0  # the key can't be enqueued again: the clip fails
    assert len(await world.jobs(JobKind.JUDGE)) == 2
    clip = await world.clip(cand)
    assert clip.status == ClipStatus.FAILED and "round 0 judge job ended (failed)" in clip.error


# --------------------------------------------------------------------------- context rounds


async def test_need_context_bumps_transcription_and_round_2_must_decide(world):
    await world.transcribe_range(-90, 30)
    cand = await world.candidate()
    model = ScriptedJudgeModel(
        [
            context_answer(300, 60),
            context_answer(400, 90, "what happens next"),
            context_answer(500, 100),  # not allowed in the last round: the one retry
            clip_answer(),
        ]
    )
    svc = world.service(model)

    assert await world.run_jobs(svc) == 1  # ready at once: round 0 asks for more
    clip = await world.clip(cand)
    assert (clip.status, clip.round) == (ClipStatus.WAITING_FOR_MEDIA, 1)
    assert (clip.context_from, clip.context_to) == (world.at(-300), world.at(60))
    (p1,) = await world.plans(clip.id)
    assert (p1.version, p1.round, p1.verdict, p1.plan) == (1, 0, "need_context", None)
    assert p1.context_request == {
        "before_s": 300.0,
        "after_s": 60.0,
        "why": "the setup before the jump",
    }
    assert p1.x == clip.x
    (req,) = await world.fetch(
        "SELECT *, xmin::text AS x FROM media_requests WHERE requested_by = 'judge'"
    )
    assert (req.priority, req.clip_id, req.fulfilled_at) == (Priority.JUDGE_CONTEXT, clip.id, None)
    assert req.x == p1.x  # the round's plan, the clip and its media request: one transaction
    assert (req.from_utc, req.to_utc) == (world.at(-300), world.at(60))
    # request_media(100) bumped the queued transcriptions inside the new context, only those
    prio = {
        r.ref_id: r.priority
        for r in await world.fetch("SELECT ref_id, priority FROM jobs WHERE kind = 'transcribe'")
    }
    for c in world.chunks:
        inside = c.start < 60 and c.end > -300 and not -120 <= c.start < 60
        assert prio[c.id] == (Priority.JUDGE_CONTEXT if inside else Priority.LIVE), c
    assert await round_job(world, cand, 1) is None  # not covered yet: the waiter will
    assert await svc.waiter.check() == 0

    await world.transcribe_range(-300, 60)
    assert await svc.waiter.check() == 1
    assert await world.run_jobs(svc) == 1  # round 1 asks again
    assert "round 0: asked for more context, got [-300.0→+60.0]" in model.requests[1].user
    clip = await world.clip(cand)
    assert (clip.round, clip.context_from, clip.context_to) == (2, world.at(-400), world.at(90))
    reqs = await world.fetch(
        "SELECT * FROM media_requests WHERE requested_by = 'judge' ORDER BY id"
    )
    assert [r.fulfilled_at is None for r in reqs] == [False, True]  # the new one covers the old

    await world.transcribe_range(-400, 90)
    assert await svc.waiter.check() == 1
    assert await world.run_jobs(svc) == 1
    final, retry = model.requests[2], model.requests[3]
    assert final.final and "need_context" not in final.schema["properties"]
    assert "the last one: decide clip or reject" in final.user
    assert retry.final and "Retry: your previous answer could not be used" in retry.user
    clip = await world.clip(cand)
    assert (clip.status, clip.plan_version, clip.round) == (ClipStatus.PLANNED, 3, 2)
    plans = await world.plans(clip.id)
    assert [(p.version, p.round, p.verdict) for p in plans] == [
        (1, 0, "need_context"),
        (2, 1, "need_context"),
        (3, 2, "clip"),
    ]
    calls = await world.calls()
    assert len(calls) == 4 and calls[2].error.startswith("answer: ")
    assert plans[2].judge_call_id == calls[3].id
    reqs = await world.fetch("SELECT * FROM media_requests WHERE requested_by = 'judge'")
    assert all(r.fulfilled_at is not None for r in reqs)
    (render,) = await world.jobs(JobKind.RENDER)
    assert render.ref_id == plans[2].id


async def test_next_round_enqueued_at_once_when_its_context_is_covered(world):
    await world.transcribe_range(-480, 180)  # everything transcribed already
    cand = await world.candidate()
    model = ScriptedJudgeModel([context_answer(300, 60), clip_answer()])
    svc = world.service(model)
    assert await world.run_jobs(svc) == 2  # round 0, then round 1 without the waiter
    clip = await world.clip(cand)
    assert (clip.status, clip.plan_version, clip.round) == (ClipStatus.PLANNED, 2, 1)
    job = await round_job(world, cand, 1)
    assert job.status == JobStatus.DONE and job.priority == clip.priority


# --------------------------------------------------------------------------- the call cache


async def test_crash_between_call_and_commit_does_not_call_again(world, monkeypatch):
    await world.transcribe_range(-120, 60)
    cand = await world.candidate()
    model = ScriptedJudgeModel([clip_answer()])  # one answer only: a second call would fail
    svc = world.service(model)
    real = svc.judge._commit
    crashed: list[bool] = []

    async def crash_once(*args, **kwargs):
        if not crashed:
            crashed.append(True)
            raise RuntimeError("killed before the plan commit")
        return await real(*args, **kwargs)

    monkeypatch.setattr(svc.judge, "_commit", crash_once)
    assert await world.run_jobs(svc) == 1
    (job,) = await world.jobs(JobKind.JUDGE)
    assert job.status == JobStatus.QUEUED and job.attempts == 1
    assert "killed before the plan commit" in job.last_error
    assert (await world.clip(cand)).status == ClipStatus.JUDGING
    (call,) = await world.calls()  # the paid answer is saved

    await world.make_runnable()
    assert await world.run_jobs(svc) == 1  # takes the judging clip over
    clip = await world.clip(cand)
    assert clip.status == ClipStatus.PLANNED
    assert len(model.requests) == 1 and len(await world.calls()) == 1
    (plan,) = await world.plans(clip.id)
    assert plan.judge_call_id == call.id
    assert svc.stats.cached == 1


async def test_invalid_answers_retry_once_then_fail(world):
    await world.transcribe_range(-120, 60)
    cand = await world.candidate()
    model = ScriptedJudgeModel(["this is not json", '{"verdict": "maybe"}'])
    svc = world.service(model)
    assert await world.run_jobs(svc) == 1
    clip = await world.clip(cand)
    assert clip.status == ClipStatus.FAILED and "no usable answer after one retry" in clip.error
    assert "Retry: your previous answer could not be used" in model.requests[1].user
    calls = await world.calls()
    assert [c.error.split(":")[0] for c in calls] == ["answer", "answer"]
    assert calls[0].answer is None and calls[1].answer == {"verdict": "maybe"}
    assert await world.plans(clip.id) == []
    (job,) = await world.jobs(JobKind.JUDGE)
    assert job.status == JobStatus.DONE  # the clip failed; the job did its work


async def test_provider_errors_retry_and_the_last_attempt_fails_the_clip(world):
    await world.transcribe_range(-120, 60)
    cand = await world.candidate(max_attempts=2)
    model = ScriptedJudgeModel([ModelError("overloaded"), ModelError("overloaded again")])
    svc = world.service(model)
    assert await world.run_jobs(svc) == 1
    assert (await world.clip(cand)).status == ClipStatus.JUDGING
    (job,) = await world.jobs(JobKind.JUDGE)
    assert job.status == JobStatus.QUEUED and job.attempts == 1
    await world.make_runnable()
    assert await world.run_jobs(svc) == 1
    clip = await world.clip(cand)
    assert clip.status == ClipStatus.FAILED and "overloaded again" in clip.error
    (job,) = await world.jobs(JobKind.JUDGE)
    assert job.status == JobStatus.FAILED
    calls = await world.calls()
    assert [c.error for c in calls] == ["provider: overloaded", "provider: overloaded again"]
    assert all((c.input_tokens, c.output_tokens, c.cost_usd) == (0, 0, 0.0) for c in calls)


# --------------------------------------------------------------------------- deadlines


async def test_soft_deadline_judges_with_partial_context(make_world):
    world = await make_world(offset=15)  # chunks [-45, 15), [15, 75), ...
    await world.transcribe_range(-90, 15)  # the core [-11, +9] is in, [15, 30) is not
    cand = await world.candidate()
    model = FakeJudgeModel()
    svc = world.service(model)
    assert await world.run_jobs(svc) == 1
    assert (await world.clip(cand)).status == ClipStatus.WAITING_FOR_MEDIA
    world.clock.now = world.at(279)
    assert await svc.waiter.check() == 0
    world.clock.now = world.at(280)  # max(waiting_since T+40, context_to T+30) + 240 s
    assert await svc.waiter.check() == 1
    assert await world.run_jobs(svc) == 1
    clip = await world.clip(cand)
    assert clip.status == ClipStatus.PLANNED and clip.flags == {"partial_context": True}
    (plan,) = await world.plans(clip.id)
    assert "partial_context" in plan.plan["flags"]
    assert "flags: partial_context" in model.requests[0].user
    assert "[+15.0→+30.0 no transcript yet]" in model.requests[0].user


async def test_hard_deadline_without_a_transcribed_core_is_no_media(world):
    await world.transcribe_range(-120, -60)  # nothing of the core [-11, +9]
    cand = await world.candidate()
    model = FakeJudgeModel()
    svc = world.service(model)
    await world.run_jobs(svc)
    world.clock.now = world.at(280)
    assert await svc.waiter.check() == 0  # soft deadline, but the core is pending
    world.clock.now = world.at(1839)
    assert await svc.waiter.check() == 0
    world.clock.now = world.at(1840)  # + 1800 s
    assert await svc.waiter.check() == 1
    assert await world.run_jobs(svc) == 1
    clip = await world.clip(cand)
    assert (clip.status, clip.reject_reason) == (ClipStatus.REJECTED, ClipRejectReason.NO_MEDIA)
    assert not model.requests and await world.plans(clip.id) == []


async def test_hard_deadline_with_part_of_the_core_judges(world):
    await world.transcribe_range(-120, 0)  # the core's first half [-11, 0)
    cand = await world.candidate()
    svc = world.service(FakeJudgeModel())
    await world.run_jobs(svc)
    world.clock.now = world.at(280)
    assert await svc.waiter.check() == 0
    world.clock.now = world.at(1840)
    assert await svc.waiter.check() == 1
    await world.run_jobs(svc)
    clip = await world.clip(cand)
    assert clip.status == ClipStatus.PLANNED and clip.flags == {"partial_context": True}


# --------------------------------------------------------------------------- usage guard


async def test_calls_per_day_rejects_as_budget(world):
    await world.transcribe_range(-120, 120)
    first = await world.candidate()
    second = await world.candidate(peak=60.0, window=(-30.0, 30.0))
    model = FakeJudgeModel()
    svc = world.service(model, calls_per_day=1)
    assert await world.run_jobs(svc) == 2
    assert (await world.clip(first)).status == ClipStatus.PLANNED
    clip = await world.clip(second)
    assert (clip.status, clip.reject_reason) == (ClipStatus.REJECTED, ClipRejectReason.BUDGET)
    assert len(model.requests) == 1
    assert "calls_per_day" in svc.info(0)["usage_guard"]["warnings"]


async def test_nothing_blocks_with_caps_unset(world):
    await world.transcribe_range(-120, 120)
    high = reading(world, 0.99)
    high = replace(high, status="allowed_warning")
    model = ScriptedJudgeModel([with_reading(clip_answer(), high)] * 2)
    first = await world.candidate()
    second = await world.candidate(peak=60.0, window=(-30.0, 30.0))
    svc = world.service(model)
    assert not svc.guard.enabled
    assert await world.run_jobs(svc) == 2
    assert {(await world.clip(c)).status for c in (first, second)} == {ClipStatus.PLANNED}
    assert svc.guard.reading == high and svc.guard.blocked == 0
    info = svc.info(0)["usage_guard"]
    assert info["enabled"] is False and info["reading"]["five_hour"]["utilization"] == 0.99


async def test_usage_cap_defers_to_the_reset_then_budget_at_the_hard_deadline(world):
    await world.transcribe_range(-120, 120)
    model = ScriptedJudgeModel([with_reading(clip_answer(), reading(world, 0.9))])
    first = await world.candidate()
    svc = world.service(model, usage_five_hour_max=0.8, usage_stale_s=7200)
    assert await world.run_jobs(svc) == 1  # no reading yet: the first call refreshes it
    assert (await world.clip(first)).status == ClipStatus.PLANNED

    second = await world.candidate(peak=60.0, window=(-30.0, 30.0))
    assert await world.run_jobs(svc) == 1
    clip = await world.clip(second)
    assert clip.status == ClipStatus.JUDGING and len(model.requests) == 1
    job = next(j for j in await world.jobs(JobKind.JUDGE) if j.ref_id == second)
    # resets in an hour, the hard deadline comes first: max(T+40, T+90) + 1800 s
    assert job.status == JobStatus.QUEUED and job.attempts == 0
    assert job.run_after == world.at(1890) and "usage guard" in job.last_error

    world.clock.now = world.at(1890)
    await world.make_runnable()
    assert await world.run_jobs(svc) == 1
    clip = await world.clip(second)
    assert (clip.status, clip.reject_reason) == (ClipStatus.REJECTED, ClipRejectReason.BUDGET)
    assert len(model.requests) == 1


async def test_a_stale_reading_lets_one_call_through(world):
    await world.transcribe_range(-120, 120)
    model = ScriptedJudgeModel([with_reading(clip_answer(), reading(world, 0.9)), clip_answer()])
    first = await world.candidate()
    svc = world.service(model, usage_five_hour_max=0.8)  # stale after 900 s
    await world.run_jobs(svc)
    second = await world.candidate(peak=60.0, window=(-30.0, 30.0))
    await world.run_jobs(svc)
    assert (await world.clip(second)).status == ClipStatus.JUDGING  # deferred
    world.clock.advance(901)
    await world.make_runnable()
    await world.run_jobs(svc)
    assert (await world.clip(second)).status == ClipStatus.PLANNED
    assert len(model.requests) == 2 and (await world.clip(first)).status == ClipStatus.PLANNED


async def test_a_refused_call_waits_for_the_reset_even_with_caps_unset(world):
    await world.transcribe_range(-120, 60)
    cand = await world.candidate()
    model = ScriptedJudgeModel(
        [UsageLimitError("five-hour limit reached", resets_at=world.at(640)), clip_answer()]
    )
    svc = world.service(model)
    assert await world.run_jobs(svc) == 1
    (job,) = await world.jobs(JobKind.JUDGE)
    assert job.status == JobStatus.QUEUED and job.attempts == 0
    assert job.run_after == world.at(640)
    (call,) = await world.calls()
    assert call.error == "usage_limit: five-hour limit reached" and call.input_tokens == 0
    assert (await world.clip(cand)).status == ClipStatus.JUDGING
    world.clock.now = world.at(640)
    await world.make_runnable()
    assert await world.run_jobs(svc) == 1
    assert (await world.clip(cand)).status == ClipStatus.PLANNED
    assert len(await world.calls()) == 2


# --------------------------------------------------------------------------- no video


async def test_audio_only_is_no_video_without_a_call(make_world):
    world = await make_world(recording_mode="audio_only", video=False)
    cand = await world.candidate()  # nothing transcribed yet: no need to wait for it
    model = FakeJudgeModel()
    svc = world.service(model)
    assert await world.run_jobs(svc) == 1
    clip = await world.clip(cand)
    assert (clip.status, clip.reject_reason) == (ClipStatus.REJECTED, ClipRejectReason.NO_VIDEO)
    assert not model.requests and await world.calls() == []


async def test_recorded_without_video_is_no_video_unless_judge_audio_only(make_world):
    world = await make_world(video=False)  # a full channel, but these chunks have no video
    await world.transcribe_range(-120, 60)
    first = await world.candidate()
    svc = world.service(FakeJudgeModel())
    await world.run_jobs(svc)
    assert (await world.clip(first)).reject_reason == ClipRejectReason.NO_VIDEO
    second = await world.candidate(peak=1.0)
    svc = world.service(FakeJudgeModel(), judge_audio_only=True)
    await world.run_jobs(svc)
    assert (await world.clip(second)).status == ClipStatus.PLANNED


async def test_decided_clips_and_odd_candidates(world, sessions):
    await world.transcribe_range(-120, 60)
    cand = await world.candidate()
    model = FakeJudgeModel()
    svc = world.service(model)
    await world.run_jobs(svc)
    assert (await world.clip(cand)).status == ClipStatus.PLANNED
    async with sessions.begin() as s:  # a second job for a decided clip: nothing happens
        await jobs.enqueue(
            s, JobKind.JUDGE, payload={"candidate_id": cand}, ref_id=cand, dedupe_key="judge:x"
        )
        await jobs.enqueue(
            s, JobKind.JUDGE, payload={"candidate_id": 999999}, ref_id=999999, dedupe_key="judge:y"
        )
    assert await world.run_jobs(svc) == 2
    assert len(model.requests) == 1 and len(await world.plans((await world.clip(cand)).id)) == 1
    missing = next(j for j in await world.jobs(JobKind.JUDGE) if j.ref_id == 999999)
    assert missing.status == JobStatus.FAILED and "does not exist" in missing.last_error


# --------------------------------------------------------------------------- revisions


async def planned_clip(world):
    await world.transcribe_range(-120, 60)
    cand = await world.candidate()
    svc = world.service(FakeJudgeModel())
    await world.run_jobs(svc)
    clip = await world.clip(cand)
    (plan,) = await world.plans(clip.id)
    return svc, clip, plan


async def revise(world, sessions, event: int, clip_id: int, base_plan_id: int) -> None:
    async with sessions.begin() as s:
        await jobs.enqueue(
            s,
            JobKind.REVISE_PLAN,
            payload=RevisePlanPayload(
                review_event_id=event, clip_id=clip_id, base_plan_id=base_plan_id
            ).model_dump(),
            priority=Priority.JUDGE_CONTEXT,
            ref_id=event,
            dedupe_key=dedupe_key(JobKind.REVISE_PLAN, event),
        )


async def test_a_patch_makes_v2_and_a_render_job(world, sessions):
    svc, clip, v1 = await planned_clip(world)
    world.revisions.events[501] = Revision(
        clip_id=clip.id,
        patch={"title": "He really jumped", "in_utc": world.at(-14.0).isoformat()},
    )
    await revise(world, sessions, 501, clip.id, v1.id)
    assert await world.run_jobs(svc) == 1
    clip = await world.clip(clip.candidate_id)
    plans = await world.plans(clip.id)
    assert [p.version for p in plans] == [1, 2] and clip.plan_version == 2
    v2 = plans[1]
    assert (v2.author, v2.parent_id, v2.review_event_id, v2.verdict) == (
        "operator",
        v1.id,
        501,
        "clip",
    )
    assert v2.model == "operator" and v2.prompt_version == v1.prompt_version
    assert v2.judge_call_id is None and v2.round == v1.round
    edit = EditPlan.model_validate(v2.plan)
    assert edit.title == "He really jumped" and edit.model == "operator"
    assert abs((edit.in_utc - world.at(-14.0)).total_seconds()) < 1.5
    assert edit.in_utc != EditPlan.model_validate(v1.plan).in_utc
    renders = await world.jobs(JobKind.RENDER)
    assert [r.ref_id for r in renders] == [v1.id, v2.id]
    assert renders[1].priority == Priority.JUDGE_CONTEXT
    assert clip.x == v2.x == renders[1].x  # one transaction
    # a retried job finds its plan and does nothing
    payload = RevisePlanPayload(review_event_id=501, clip_id=clip.id, base_plan_id=v1.id)
    assert await svc.revise.revise(payload) is None
    assert len(await world.plans(clip.id)) == 2


async def test_refused_revisions_notify_and_write_no_plan(world, sessions):
    svc, clip, v1 = await planned_clip(world)
    world.revisions.events[601] = Revision(clip_id=clip.id, patch={"title": "Second title"})
    await revise(world, sessions, 601, clip.id, v1.id)
    await world.run_jobs(svc)
    v2 = (await world.plans(clip.id))[1]
    world.revisions.events.update(
        {
            602: Revision(clip_id=clip.id, patch={"title": "Stale base"}),
            603: Revision(clip_id=clip.id, instruction="   "),
            604: Revision(clip_id=clip.id, patch={"title": "x", "sparkle": True}),
            605: Revision(clip_id=clip.id, patch={"title": "both"}, instruction="and this"),
        }
    )
    await revise(world, sessions, 602, clip.id, v1.id)  # v1 is not the latest any more
    for event in (603, 604, 605):
        await revise(world, sessions, event, clip.id, v2.id)
    assert await world.run_jobs(svc) == 4
    assert (
        len(await world.plans(clip.id)) == 2
        and (await world.clip(clip.candidate_id)).plan_version == 2
    )
    notices = {n.ref_id: n for n in await world.jobs(JobKind.NOTIFY)}
    assert sorted(notices) == [602, 603, 604, 605]
    for event, n in notices.items():
        assert n.dedupe_key == dedupe_key(JobKind.NOTIFY, event, NoticeKind.REVISION_REFUSED.value)
        assert n.payload["kind"] == "revision_refused" and n.payload["clip_id"] == clip.id
    assert "not the clip's latest plan" in notices[602].payload["text"]
    assert "the instruction is empty" in notices[603].payload["text"]
    assert "the patch is not valid" in notices[604].payload["text"]
    assert "either a patch or an instruction" in notices[605].payload["text"]
    assert len(await world.jobs(JobKind.RENDER)) == 2  # v1 and v2 only
    assert svc.revise.refused == 4


async def test_review_events_source_needs_the_table(sessions):
    async with sessions() as s:
        with pytest.raises(PermanentJobError, match="review_events doesn't exist yet"):
            await ReviewEventsSource().load(s, 1)


async def test_heartbeat(world):
    svc = world.service(FakeJudgeModel())
    await svc.beat()
    row = await world.one("SELECT * FROM worker_heartbeats WHERE worker_id = 'judge-test'")
    assert row.service == "judge" and row.info["model"] == "fake"
    assert row.info["prompt_version"].startswith("v1-") and row.info["waiting_clips"] == 0
    assert row.info["usage_guard"]["enabled"] is False


# --------------------------------------------------------------------------- review fixes


async def test_a_round_job_defers_itself_when_its_clip_is_not_ready(world, sessions):
    cand = await world.candidate()
    svc = world.service(FakeJudgeModel())
    await world.run_jobs(svc)  # the clip waits
    clip = await world.clip(cand)
    async with sessions.begin() as s:  # as if the waiter had seen it ready
        await writes.enqueue_round(s, reads.clip_row(clip))
    assert await world.run_jobs(svc) == 1
    job = await round_job(world, cand, 0)
    assert job.status == JobStatus.QUEUED and job.attempts == 0
    assert "media not complete any more" in job.last_error
    assert job.run_after == world.at(70)  # min(soft deadline T + 280, now + the 30 s sweep)
    assert (await world.clip(cand)).status == ClipStatus.WAITING_FOR_MEDIA


class RacingModel:
    """On its first call, a duplicate job (a stale-lock requeue) runs to its end first."""

    name = "racing"
    options: ClassVar[dict] = {}

    def __init__(self, world, cand: int) -> None:
        self.world, self.cand, self.calls, self.svc = world, cand, 0, None

    async def judge(self, request):
        self.calls += 1
        if self.calls == 1:
            (job,) = await self.world.jobs(JobKind.JUDGE)
            dup = jobs.ClaimedJob(job.id, job.kind, job.ref_id, job.priority, job.payload, 1, 5)
            await self.svc.judge.advance(self.cand, dup)
        return response_for(request, json.dumps(clip_answer()), prefix="r")


async def test_two_jobs_on_one_judging_clip_commit_once(world):
    await world.transcribe_range(-120, 60)
    cand = await world.candidate()
    model = RacingModel(world, cand)
    model.svc = svc = world.service(model)
    assert await world.run_jobs(svc) == 1
    clip = await world.clip(cand)
    assert clip.status == ClipStatus.PLANNED and clip.plan_version == 1
    (plan,) = await world.plans(clip.id)  # the first job's result was dropped
    calls = await world.calls()
    assert len(calls) == 2 and model.calls == 2
    assert plan.judge_call_id == calls[0].id  # the duplicate's call, committed first
    assert len(await world.jobs(JobKind.RENDER)) == 1


async def test_a_permanent_model_error_fails_clip_and_job(world):
    await world.transcribe_range(-120, 60)
    cand = await world.candidate()
    model = ScriptedJudgeModel([PermanentModelError("apiKeySource is not none")])
    svc = world.service(model)
    assert await world.run_jobs(svc) == 1
    clip = await world.clip(cand)
    assert clip.status == ClipStatus.FAILED and clip.error == "model: apiKeySource is not none"
    (job,) = await world.jobs(JobKind.JUDGE)
    assert job.status == JobStatus.FAILED and job.attempts == 1
    (call,) = await world.calls()
    assert call.error == "permanent: apiKeySource is not none"
    assert call.io_key.startswith(f"clips/{clip.id}/judge/{call.request_hash}.error-")


async def test_give_up_fails_a_waiting_clip_only_in_its_own_round(world, sessions, monkeypatch):
    cand = await world.candidate()
    svc = world.service(FakeJudgeModel())
    await world.run_jobs(svc)  # the clip waits (round 0)
    async with sessions.begin() as s:  # its round job, with a single attempt
        await jobs.enqueue(
            s,
            JobKind.JUDGE,
            payload={"candidate_id": cand},
            priority=Priority.TRIGGER,
            ref_id=cand,
            dedupe_key=judge_round_key(cand, 0),
            max_attempts=1,
        )

    async def broken(*args, **kwargs):
        raise RuntimeError("coverage query failed")

    monkeypatch.setattr(svc.judge, "_wake", broken)
    assert await world.run_jobs(svc) == 1  # the last (only) attempt fails on a waiting clip
    clip = await world.clip(cand)
    assert clip.status == ClipStatus.FAILED and "coverage query failed" in clip.error
    assert (await round_job(world, cand, 0)).status == JobStatus.FAILED

    other = await world.candidate(peak=60.0, window=(-30.0, 30.0), judge_job=False)
    async with world.sessions.begin() as s:  # a clip in round 1
        await s.execute(
            text(
                "INSERT INTO clips (candidate_id, stream_id, channel_id, round, anchor_utc,"
                " context_from, context_to, priority, waiting_since)"
                " VALUES (:c, :s, :ch, 1, :t, :a, :b, 80, :t)"
            ),
            {
                "c": other,
                "s": world.stream_id,
                "ch": world.channel_id,
                "t": world.at(60),
                "a": world.at(0),
                "b": world.at(90),
            },
        )
    await svc.judge._give_up(other, "a stale round-0 job", 0)
    assert (await world.clip(other)).status == ClipStatus.WAITING_FOR_MEDIA


async def test_failed_calls_keep_their_own_s3_records(world):
    await world.transcribe_range(-120, 60)
    await world.candidate()
    model = ScriptedJudgeModel([ModelError("overloaded"), clip_answer()])
    svc = world.service(model)
    await world.run_jobs(svc)
    await world.make_runnable()
    await world.run_jobs(svc)
    failed, ok = await world.calls()
    assert failed.request_hash == ok.request_hash and failed.io_key != ok.io_key
    assert ".error-" in failed.io_key and failed.io_key.endswith(".json")
    assert ok.io_key == keys.judge_io(ok.request_hash, ok.clip_id)
    assert json.loads(world.store.objects[failed.io_key])["error"] == "provider: overloaded"
    assert json.loads(world.store.objects[ok.io_key])["error"] is None


async def test_the_service_exits_when_a_runner_dies(world, monkeypatch):
    svc = world.service(FakeJudgeModel(), concurrency=2)

    async def db_down() -> bool:
        raise OSError("connection to the database lost")

    monkeypatch.setattr(svc.runners[1], "run_once", db_down)
    stop = asyncio.Event()
    assert await asyncio.wait_for(svc.run(stop), timeout=30) == 1
    assert stop.is_set()  # the other loops were stopped too


async def test_the_service_stops_cleanly_on_stop(world):
    svc = world.service(FakeJudgeModel())
    stop = asyncio.Event()
    task = asyncio.create_task(svc.run(stop))
    await asyncio.sleep(0.5)
    stop.set()
    assert await asyncio.wait_for(task, timeout=30) == 0
