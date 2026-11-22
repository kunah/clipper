"""Judge contract (DESIGN §19, build item 6a): job kinds, enums vs. model CHECKs, payloads, dedupe
keys, the storage key, the EditPlan / PlanPatch validators, and a clip stored with its plan,
judge call, media request and render job."""

import re
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, select
from sqlalchemy.exc import IntegrityError

from clipper_core import jobs, media
from clipper_core.contracts import (
    CLIP_MAX,
    CLIP_MIN,
    EDIT_PLAN_VERSION,
    CandidateSource,
    CandidateStatus,
    CaptionStyle,
    ClipRejectReason,
    ClipStatus,
    EditPlan,
    Hook,
    HookMode,
    JobKind,
    LayoutKind,
    LifecycleState,
    MomentType,
    MusicMood,
    NoticeKind,
    NotifyPayload,
    PlanAuthor,
    PlanPatch,
    Priority,
    RenderPayload,
    ReviewPayload,
    RevisePlanPayload,
    Verdict,
    dedupe_key,
    judge_round_key,
)
from clipper_core.models import Candidate, Channel, Clip, ClipPlan, Job, JudgeCall, MediaRequest
from clipper_core.models import Stream as StreamRow
from clipper_core.storage import keys
from clipper_core.timeline import Interval, utc_now


def check_values(table, name: str) -> set[str]:
    full = f"ck_{table.name}_{name}"  # naming convention
    (ck,) = [c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == full]
    return set(re.findall(r"'([^']+)'", str(ck.sqltext)))


def test_job_kinds():
    assert {k.value for k in JobKind} >= {
        "transcribe",
        "judge",
        "detect_offline",
        "render",
        "review",
        "revise_plan",
        "notify",
    }
    assert (JobKind.TRANSCRIBE, JobKind.JUDGE) == ("transcribe", "judge")  # unchanged


def test_clip_enums_match_the_check_constraints():
    assert check_values(Clip.__table__, "status") == {s.value for s in ClipStatus}
    assert Clip.__table__.c.status.server_default.arg == ClipStatus.WAITING_FOR_MEDIA
    assert check_values(ClipPlan.__table__, "verdict") == {v.value for v in Verdict}
    assert check_values(ClipPlan.__table__, "author") == {a.value for a in PlanAuthor}
    assert ClipPlan.__table__.c.author.server_default.arg == PlanAuthor.JUDGE


def test_state_enums():
    """Plain-text values other services read (§19-§22); clip statuses are lifecycle states."""
    assert {r.value for r in ClipRejectReason} == {"model", "no_video", "no_media", "budget"}
    assert [s.value for s in LifecycleState] == [
        "candidate",
        "waiting_for_media",
        "judging",
        "rejected",
        "planned",
        "rendering",
        "in_review",
        "approved",
        "discarded",
        "publishing",
        "published",
        "failed",
        "unrenderable",
    ]
    assert {s.value for s in ClipStatus} <= {s.value for s in LifecycleState}
    assert {k.value for k in NoticeKind} == {"revision_refused", "render_failed"}
    assert {m.value for m in HookMode} == {"none", "text", "teaser"}
    assert LayoutKind.AUTO == "auto" and CaptionStyle.BOLD_POP == "bold_pop"
    assert len(MusicMood) == 7


def test_payloads_roundtrip_and_reject_unknown_fields():
    render = RenderPayload(plan_id=9)
    assert render.model_dump() == {"plan_id": 9, "profile": "short_1080x1920"}
    revise = RevisePlanPayload(review_event_id=3, clip_id=7, base_plan_id=9)
    assert RevisePlanPayload.model_validate(revise.model_dump()) == revise
    assert ReviewPayload(render_id=4).model_dump() == {"render_id": 4}
    notify = NotifyPayload.model_validate(
        {"kind": "revision_refused", "text": "stale", "clip_id": 7}
    )
    assert (notify.kind, notify.severity, notify.publication_id) == (
        NoticeKind.REVISION_REFUSED,
        "info",
        None,
    )
    assert NotifyPayload.model_validate(notify.model_dump(mode="json")) == notify
    for bad in (
        lambda: RenderPayload.model_validate({"plan_id": 9, "clip_id": 1}),  # extra="forbid"
        lambda: RevisePlanPayload.model_validate({"review_event_id": 3, "clip_id": 7}),
        lambda: NotifyPayload(kind="nope", text="x"),
        lambda: NotifyPayload(kind=NoticeKind.RENDER_FAILED, text="x", severity="fatal"),
    ):
        with pytest.raises(ValidationError):
            bad()


def test_dedupe_keys_of_judge_rounds_and_later_jobs():
    assert dedupe_key(JobKind.JUDGE, 5) == "judge:5"  # the detector's first job
    assert judge_round_key(5, 0) == "judge:5:r0"  # the waiter's, per round
    assert judge_round_key(5, 2) == dedupe_key(JobKind.JUDGE, 5, "r2") == "judge:5:r2"
    assert len({dedupe_key(JobKind.JUDGE, 5), judge_round_key(5, 0), judge_round_key(5, 1)}) == 3
    with pytest.raises(ValueError):
        judge_round_key(5, -1)
    assert dedupe_key(JobKind.RENDER, 9) == "render:9"
    assert dedupe_key(JobKind.REVISE_PLAN, 3) == "revise_plan:3"
    assert dedupe_key(JobKind.NOTIFY, 3, NoticeKind.RENDER_FAILED) == "notify:3:render_failed"


def test_judge_io_storage_key_is_the_request_hash():
    digest = "ab" * 32
    assert keys.judge_io(digest, clip_id=7) == f"clips/7/judge/{digest}.json"
    assert keys.judge_io(digest) == f"judge/{digest}.json"  # outside a clip (evals)
    for bad in ("", "AB12", "../x", "ab/cd"):
        with pytest.raises(ValueError):
            keys.judge_io(bad, clip_id=7)
    # several records of one request (each failed call its own)
    assert (
        keys.judge_io(digest, 7, suffix="error-1a2b") == f"clips/7/judge/{digest}.error-1a2b.json"
    )
    for bad in ("", "-x", "A", "a/b", "a.b"):
        with pytest.raises(ValueError):
            keys.judge_io(digest, 7, suffix=bad)


# --------------------------------------------------------------------------- EditPlan

T = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T + timedelta(seconds=seconds)


def seg(a: float, b: float) -> dict:
    return {"start_utc": at(a), "end_utc": at(b)}


def plan_data(**kw) -> dict:
    return {
        "clip_id": 7,
        "stream_id": 3,
        "anchor_utc": T,
        "segments": [seg(-20, 10)],
        "title": "he really did that",
        "confidence": 0.8,
        "model": "fake",
        "prompt_version": "v1-00000000",
    } | kw


def make_plan(**kw) -> EditPlan:
    return EditPlan.model_validate(plan_data(**kw))


def full_plan() -> EditPlan:
    return make_plan(
        segments=[seg(-20, -5), seg(-2, 10)],  # a jump cut
        hook={"mode": "teaser", "start_utc": at(0), "end_utc": at(2.5)},
        hashtags=["xqc", "twitch"],
        caption_style="karaoke",
        emphasis=[{"start_utc": at(-1.2), "text": "WHAT", "kind": "big"}],
        word_fixes=[{"start_utc": at(-10), "text": "Pog"}],
        zooms=[
            {"start_utc": at(-19), "end_utc": at(-10), "scale": 1.2, "target": "face"},
            {"start_utc": at(0), "end_utc": at(4), "scale": 1.5},
        ],
        sfx=[{"at_utc": at(0.1), "tag": "vine_boom", "gain_db": -3}],
        music={"mood": "funny"},
        layout={"kind": "full_crop", "focus_x": 0.4},
        thumbnail_utc=at(1),
        moment_type="funny",
        reasons=["streamer screams at the jump scare", "chat KEKW x8"],
        flags=["partial_context"],
        repairs=["snap_in", "split_gap"],
    )


def test_edit_plan_defaults_and_properties():
    plan = make_plan()
    assert plan.version == EDIT_PLAN_VERSION == 1
    assert (plan.hook.mode, plan.layout.kind, plan.caption_style) == (
        HookMode.NONE,
        LayoutKind.AUTO,
        CaptionStyle.BOLD_POP,
    )
    assert (plan.music, plan.thumbnail_utc, plan.moment_type) == (None, None, MomentType.UNKNOWN)
    assert plan.hashtags == plan.emphasis == plan.word_fixes == plan.zooms == plan.sfx == []
    full = full_plan()
    assert (full.in_utc, full.out_utc, full.duration) == (at(-20), at(10), timedelta(seconds=27))
    assert full.music.gain_db == -20.0 and full.music.duck


def test_edit_plan_json_round_trip():
    plan = full_plan()
    assert EditPlan.model_validate_json(plan.model_dump_json()) == plan
    stored = plan.model_dump(mode="json")  # what goes into clip_plans.plan (jsonb)
    assert stored["segments"][0]["start_utc"] == "2026-11-20T11:59:40Z"
    again = EditPlan.model_validate(stored)
    assert again == plan and again.anchor_utc.tzinfo == UTC
    # forward compatible: fields a newer writer adds are ignored, not rejected
    assert EditPlan.model_validate(stored | {"b_roll": [{"at": 1}]}) == plan


def test_edit_plan_converts_offsets_to_utc():
    plus_two = timezone(timedelta(hours=2))
    plan = make_plan(anchor_utc=T.astimezone(plus_two), segments=[seg(-5, 5)])
    assert plan.anchor_utc == T and plan.anchor_utc.utcoffset() == timedelta(0)
    assert make_plan(anchor_utc="2026-11-20T14:00:00+02:00").anchor_utc == T


NAIVE = T.replace(tzinfo=None)


@pytest.mark.parametrize(
    "bad",
    [
        {"anchor_utc": NAIVE},
        {"anchor_utc": "2026-11-20T12:00:00"},  # ISO text without an offset
        {"segments": [{"start_utc": NAIVE, "end_utc": at(10)}]},
        {"thumbnail_utc": NAIVE},
        {"emphasis": [{"start_utc": NAIVE, "text": "x"}]},
        {"word_fixes": [{"start_utc": NAIVE, "text": "x"}]},
        {"sfx": [{"at_utc": NAIVE, "tag": "boom"}]},
        {"zooms": [{"start_utc": NAIVE, "end_utc": at(5), "scale": 1.2}]},
        {"hook": {"mode": "teaser", "start_utc": NAIVE, "end_utc": at(1)}},
    ],
)
def test_edit_plan_rejects_naive_times(bad):
    with pytest.raises(ValidationError, match="naive datetime"):
        make_plan(**bad)


@pytest.mark.parametrize(
    ("segments", "error"),
    [
        ([], "at least 1"),
        ([seg(0, 10), seg(-20, -5)], "sorted"),  # unordered
        ([seg(-20, 0), seg(-5, 10)], "overlap"),  # overlapping
        ([seg(0, 2), seg(2, 4)], "touch"),  # touching: the resolver merges them
        ([seg(0, 0)], "empty or reversed"),
        ([seg(10, 0)], "empty or reversed"),
        ([seg(0, 2.9)], "clip length"),  # < CLIP_MIN
        ([seg(0, 180.5)], "clip length"),  # > CLIP_MAX
        ([seg(0, 1), seg(100, 101)], "clip length"),  # the total counts, not the span
        ([seg(0, 100), seg(110, 191)], "clip length"),
    ],
)
def test_edit_plan_rejects_bad_segments_and_durations(segments, error):
    with pytest.raises(ValidationError, match=error):
        make_plan(segments=segments)


def test_edit_plan_accepts_the_duration_bounds():
    assert make_plan(segments=[seg(0, 3)]).duration == CLIP_MIN
    assert make_plan(segments=[seg(-60, 120)]).duration == CLIP_MAX
    assert make_plan(segments=[seg(0, 2), seg(2.1, 4)]).duration == timedelta(seconds=3.9)
    assert make_plan(segments=[seg(0, 2), seg(100, 102)]).duration == timedelta(seconds=4)


@pytest.mark.parametrize("version", [0, 2, "v1", None])
def test_edit_plan_rejects_other_versions(version):
    with pytest.raises(ValidationError, match="version"):
        make_plan(version=version)


@pytest.mark.parametrize(
    ("bad", "error"),
    [
        ({"emphasis": [{"start_utc": at(30), "text": "x"}]}, "emphasis word"),
        ({"word_fixes": [{"start_utc": at(-30), "text": "x"}]}, "word fix"),
        ({"sfx": [{"at_utc": at(10), "tag": "boom"}]}, "sfx cue"),  # the out point: half-open
        ({"thumbnail_utc": at(11)}, "thumbnail"),
        (
            {
                "segments": [seg(-20, -5), seg(0, 10)],
                "emphasis": [{"start_utc": at(-3), "text": "x"}],  # in the cut-out part
            },
            "emphasis word",
        ),
        (
            {
                "word_fixes": [
                    {"start_utc": at(1), "text": "Pog"},
                    {"start_utc": at(1), "text": "PogU"},
                ]
            },
            "same word",
        ),
        (
            {
                "segments": [seg(-20, -5), seg(0, 10)],
                "zooms": [{"start_utc": at(-8), "end_utc": at(2), "scale": 1.2}],  # over the cut
            },
            "not inside one segment",
        ),
        (
            {
                "zooms": [
                    {"start_utc": at(2), "end_utc": at(6), "scale": 1.2},
                    {"start_utc": at(-5), "end_utc": at(3), "scale": 1.3},  # unsorted, overlaps
                ]
            },
            "zooms must not overlap",
        ),
        ({"zooms": [{"start_utc": at(2), "end_utc": at(2), "scale": 1.2}]}, "empty or reversed"),
        ({"zooms": [{"start_utc": at(0), "end_utc": at(2), "scale": 1.7}]}, "less than or equal"),
        ({"hook": {"mode": "teaser", "start_utc": at(9), "end_utc": at(11)}}, "teaser hook"),
        ({"hook": {"mode": "teaser", "start_utc": at(0), "end_utc": at(3.5)}}, "at most"),
        ({"hook": {"mode": "teaser", "start_utc": at(0)}}, "needs start_utc and end_utc"),
        ({"hook": {"mode": "text"}}, "needs text"),
        ({"hook": {"mode": "text", "text": "x" * 41}}, "at most 40"),
        ({"hashtags": ["#xqc"]}, "bare word"),
        ({"hashtags": ["two words"]}, "bare word"),
        ({"hashtags": [""]}, "bare word"),
        ({"hashtags": ["a", "b", "c", "d", "e", "f"]}, "at most 5"),
        ({"title": ""}, "at least 1"),
        ({"title": "\t \n"}, "only whitespace"),
        ({"title": "x" * 101}, "at most 100"),
        ({"hook": {"mode": "text", "text": "   "}}, "needs text"),
        ({"confidence": 1.5}, "less than or equal"),
        ({"sfx": [{"at_utc": at(0), "tag": "boom", "gain_db": 7}]}, "less than or equal"),
        ({"music": {"mood": "angry"}}, "mood"),
        ({"layout": {"kind": "full_crop", "focus_x": 1.2}}, "less than or equal"),
        ({"caption_style": "comic_sans"}, "caption_style"),
        ({"moment_type": "greeting"}, "moment_type"),
        ({"model": None}, "model"),
    ],
)
def test_edit_plan_rejects_inconsistent_details(bad, error):
    with pytest.raises(ValidationError, match=error):
        make_plan(**bad)


def test_edit_plan_text_hook_and_teaser_inside_a_segment():
    text_hook = make_plan(hook={"mode": "text", "text": "WAIT FOR IT"})
    assert text_hook.hook == Hook(mode=HookMode.TEXT, text="WAIT FOR IT")
    teaser = make_plan(hook={"mode": "teaser", "start_utc": at(7), "end_utc": at(10)})
    assert teaser.hook.end_utc - teaser.hook.start_utc == timedelta(seconds=3)


def test_plan_patch_keeps_by_default_and_validates_like_the_plan():
    keep = PlanPatch()
    assert keep.model_dump(exclude_defaults=True) == {}
    assert (keep.in_utc, keep.hook, keep.word_fixes, keep.no_music) == (None, None, None, False)
    patch = PlanPatch(
        in_utc=at(-18),
        out_utc="2026-11-20T12:00:12Z",
        title="new title",
        word_fixes=[{"start_utc": at(1), "text": "KEKW"}],
        music={"mood": "hype", "gain_db": -18},
    )
    assert patch.out_utc == at(12)
    assert PlanPatch.model_validate_json(patch.model_dump_json()) == patch
    assert PlanPatch(no_music=True).music is None


@pytest.mark.parametrize(
    ("bad", "error"),
    [
        ({"in_utc": NAIVE}, "naive datetime"),
        ({"word_fixes": [{"start_utc": NAIVE, "text": "x"}]}, "naive datetime"),
        ({"title": ""}, "at least 1"),
        ({"title": "   "}, "only whitespace"),
        ({"hook": {"mode": "text"}}, "needs text"),
        ({"hook": {"mode": "text", "text": " "}}, "needs text"),
        ({"music": {"mood": "hype"}, "no_music": True}, "not both"),
        # operator input is strict: typos are rejected, not dropped (also nested)
        ({"titel": "x"}, "Extra inputs"),
        ({"hook": {"mode": "text", "txt": "x"}}, r"unknown fields in hook: \['txt'\]"),
        ({"layout": {"kind": "auto", "focus": 0.5}}, "unknown fields in layout"),
        ({"music": {"mood": "hype", "volume": 3}}, "unknown fields in music"),
        (
            {"word_fixes": [{"start_utc": at(1), "text": "a"}, {"start": at(2), "text": "b"}]},
            r"unknown fields in word_fixes\[1\]",
        ),
    ],
)
def test_plan_patch_rejects_bad_operator_input(bad, error):
    with pytest.raises(ValidationError, match=error):
        PlanPatch.model_validate(bad)


# --------------------------------------------------------------------------- database

T0 = utc_now().replace(microsecond=0)


def db_at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


async def make_candidate(sessions) -> Candidate:
    async with sessions.begin() as s:
        channel = Channel(login="judge-test")
        s.add(channel)
        await s.flush()
        stream = StreamRow(channel_id=channel.id, platform_stream_id="s1", started_at=T0)
        s.add(stream)
        await s.flush()
        cand = Candidate(
            stream_id=stream.id,
            channel_id=channel.id,
            source=CandidateSource.LIVE,
            status=CandidateStatus.EMITTED,
            trigger="chat",
            peak_utc=db_at(600),
            chat_delay_s=8.0,
            delay_source="default",
            moment_from=db_at(590),
            moment_to=db_at(605),
            window_from=db_at(510),
            window_to=db_at(630),
            score=6.5,
            detector_version="test",
        )
        s.add(cand)
    return cand


def clip_values(cand: Candidate, **kw) -> dict:
    return {
        "candidate_id": cand.id,
        "stream_id": cand.stream_id,
        "channel_id": cand.channel_id,
        "anchor_utc": cand.peak_utc,
        "context_from": cand.window_from,
        "context_to": cand.window_to,
        "priority": int(Priority.TRIGGER),
        "waiting_since": utc_now(),
    } | kw


def plan_row(clip: Clip, **kw) -> ClipPlan:
    values = {
        "clip_id": clip.id,
        "version": 1,
        "round": 0,
        "verdict": Verdict.REJECT,
        "model": "fake",
        "prompt_version": "v1-00000000",
    } | kw
    return ClipPlan(**values)


@pytest.mark.integration
async def test_clip_plan_media_request_and_render_job_in_one_transaction(sessions):
    cand = await make_candidate(sessions)
    async with sessions.begin() as s:
        clip = Clip(**clip_values(cand))
        s.add(clip)
    async with sessions.begin() as s:  # a call is committed alone, right after it
        call = JudgeCall(
            clip_id=clip.id,
            stream_id=clip.stream_id,
            model="fake",
            prompt_version="v1-00000000",
            request_hash="ab" * 32,
            input_tokens=4000,
            cached_input_tokens=3000,
            output_tokens=800,
            cost_usd=0.02,
            latency_s=1.5,
            answer={"verdict": "clip"},
            io_key=keys.judge_io("ab" * 32, clip_id=clip.id),
        )
        s.add(call)
    plan = EditPlan.model_validate(
        plan_data(
            clip_id=clip.id,
            stream_id=clip.stream_id,
            anchor_utc=clip.anchor_utc,
            segments=[{"start_utc": db_at(585), "end_utc": db_at(610)}],
        )
    )
    async with sessions.begin() as s:  # plan, clip status, media request and render job together
        row = plan_row(
            clip,
            verdict=Verdict.CLIP,
            plan=plan.model_dump(mode="json"),
            confidence=plan.confidence,
            reasons=plan.reasons,
            repairs=plan.repairs,
            judge_call_id=call.id,
        )
        s.add(row)
        stored_clip = await s.get(Clip, clip.id)
        stored_clip.status, stored_clip.plan_version = ClipStatus.PLANNED, 1
        request_id = await media.request_media(
            s,
            stream_id=clip.stream_id,
            window=Interval(clip.context_from, clip.context_to),
            priority=Priority.JUDGE_CONTEXT,
            requested_by="judge",
            clip_id=clip.id,
        )
        await s.flush()
        job_id = await jobs.enqueue(
            s,
            JobKind.RENDER,
            payload=RenderPayload(plan_id=row.id).model_dump(),
            ref_id=row.id,
            dedupe_key=dedupe_key(JobKind.RENDER, row.id),
            priority=clip.priority,
        )
    async with sessions() as s:
        stored = (await s.execute(select(ClipPlan))).scalar_one()
        job = await s.get(Job, job_id)
        request = await s.get(MediaRequest, request_id)
        stored_clip = await s.get(Clip, clip.id)
    assert EditPlan.model_validate(stored.plan) == plan
    assert (stored.author, stored.shadow, stored.context_request) == ("judge", False, None)
    assert (stored_clip.status, stored_clip.plan_version, stored_clip.round) == ("planned", 1, 0)
    assert request.clip_id == clip.id
    assert (job.kind, job.ref_id, job.dedupe_key) == ("render", stored.id, f"render:{stored.id}")
    assert RenderPayload.model_validate(job.payload).plan_id == stored.id


@pytest.mark.integration
async def test_one_clip_per_candidate_and_one_plan_per_version(sessions):
    cand = await make_candidate(sessions)
    async with sessions.begin() as s:
        clip = Clip(**clip_values(cand))
        s.add(clip)
    with pytest.raises(IntegrityError, match="uq_clips_candidate_id"):
        async with sessions.begin() as s:
            s.add(Clip(**clip_values(cand)))
    async with sessions.begin() as s:
        first = plan_row(clip)
        s.add(first)
        await s.flush()
        s.add(  # an operator revision of it
            plan_row(
                clip,
                version=2,
                verdict=Verdict.CLIP,
                plan={"version": 1},
                author=PlanAuthor.OPERATOR,
                parent_id=first.id,
                instruction=None,
            )
        )
    with pytest.raises(IntegrityError, match="uq_clip_plans_clip_id_version"):
        async with sessions.begin() as s:
            s.add(plan_row(clip, version=2))


@pytest.mark.integration
@pytest.mark.parametrize(
    ("bad", "check"),
    [
        ({"status": "rendering"}, "status"),  # a lifecycle state, not a clip status
        ({"status": ClipStatus.REJECTED}, "reject_reason"),  # without a reason
        ({"reject_reason": ClipRejectReason.MODEL}, "reject_reason"),  # not rejected
        ({"round": -1}, "round"),
        ({"context_from": db_at(600), "context_to": db_at(600)}, "context_range"),  # empty
        ({"anchor_utc": db_at(640)}, "anchor_in_context"),
    ],
)
async def test_clip_checks_reject_bad_rows(sessions, bad, check):
    cand = await make_candidate(sessions)
    with pytest.raises(IntegrityError, match=f"ck_clips_{check}"):
        async with sessions.begin() as s:
            s.add(Clip(**clip_values(cand, **bad)))


@pytest.mark.integration
@pytest.mark.parametrize(
    ("bad", "check"),
    [
        ({"verdict": "maybe"}, "verdict"),
        ({"verdict": Verdict.CLIP}, "plan"),  # a clip verdict without a plan
        ({"plan": {"version": 1}}, "plan"),  # a reject with a plan
        ({"verdict": Verdict.NEED_CONTEXT}, "context_request"),  # without the request
        ({"context_request": {"before_s": 60, "after_s": 0, "why": "x"}}, "context_request"),
        ({"author": "bot"}, "author"),
    ],
)
async def test_clip_plan_checks_reject_bad_rows(sessions, bad, check):
    cand = await make_candidate(sessions)
    async with sessions.begin() as s:
        clip = Clip(**clip_values(cand))
        s.add(clip)
    with pytest.raises(IntegrityError, match=f"ck_clip_plans_{check}"):
        async with sessions.begin() as s:
            s.add(plan_row(clip, **bad))


@pytest.mark.integration
async def test_a_rejected_clip_and_a_context_round(sessions):
    cand = await make_candidate(sessions)
    async with sessions.begin() as s:
        clip = Clip(**clip_values(cand))
        s.add(clip)
        await s.flush()
        s.add(
            plan_row(
                clip,
                verdict=Verdict.NEED_CONTEXT,
                context_request={"before_s": 120, "after_s": 0, "why": "setup earlier"},
            )
        )
        clip.round, clip.context_from = 1, db_at(480)
    async with sessions.begin() as s:
        s.add(plan_row(clip, version=2, round=1, reasons=["just loud"]))
        stored = await s.get(Clip, clip.id)
        stored.status, stored.reject_reason = ClipStatus.REJECTED, ClipRejectReason.MODEL
    async with sessions() as s:
        rows = (await s.execute(select(ClipPlan).order_by(ClipPlan.version))).scalars().all()
        stored = await s.get(Clip, clip.id)
    assert [(r.round, r.verdict, r.plan) for r in rows] == [
        (0, "need_context", None),
        (1, "reject", None),
    ]
    assert (stored.status, stored.reject_reason, stored.round) == ("rejected", "model", 1)
