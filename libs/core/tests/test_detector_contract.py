"""Detector contract (DESIGN §18): job kinds, enums vs. model CHECKs, payloads, dedupe keys,
leases, and a candidate stored with its media request and judge job."""

import re
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from clipper_core import jobs, media
from clipper_core.contracts import (
    CandidateSource,
    CandidateStatus,
    DetectOfflinePayload,
    JobKind,
    JudgePayload,
    MomentType,
    Priority,
    backfill_lease,
    dedupe_key,
    detect_lease,
    recording_lease,
)
from clipper_core.models import Candidate, Channel, ChatDelayEstimate, Job, Stream
from clipper_core.timeline import Interval, utc_now


def check_values(table, name: str) -> set[str]:
    full = f"ck_{table.name}_{name}"  # naming convention
    (ck,) = [c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == full]
    return set(re.findall(r"'([^']+)'", str(ck.sqltext)))


def test_job_kinds():
    assert {k.value for k in JobKind} >= {"transcribe", "judge", "detect_offline"}
    assert JobKind.TRANSCRIBE == "transcribe"  # unchanged: queued rows keep working


def test_candidate_enums_match_the_check_constraints():
    table = Candidate.__table__
    assert check_values(table, "source") == {s.value for s in CandidateSource}
    assert check_values(table, "status") == {s.value for s in CandidateStatus}
    assert check_values(table, "moment_type") == {m.value for m in MomentType}
    assert {m.value for m in MomentType} == {
        "funny",
        "hype",
        "shock",
        "tense",
        "sad",
        "cringe",
        "wholesome",
        "mixed",
        "unknown",
    }
    assert table.c.moment_type.server_default.arg == MomentType.UNKNOWN


def test_dedupe_key_stays_compatible_and_takes_extra_parts():
    assert dedupe_key(JobKind.TRANSCRIBE, 42) == "transcribe:42"  # existing rows' keys
    assert dedupe_key(JobKind.JUDGE, 5) == "judge:5"  # the detector's job for a candidate
    assert dedupe_key(JobKind.JUDGE, 5, "r1") == "judge:5:r1"  # the judge's waiter, round 1
    assert dedupe_key(JobKind.JUDGE, 5, f"r{2}") != dedupe_key(JobKind.JUDGE, 5, "r1")
    assert dedupe_key(JobKind.DETECT_OFFLINE, 12, "0.1.0+p1a2b+l3") == (
        "detect_offline:12:0.1.0+p1a2b+l3"
    )
    # enum and int parts render as their values (e.g. a later `dedupe_key(NOTIFY, ref, kind)`)
    assert dedupe_key(JobKind.JUDGE, 5, MomentType.FUNNY, 7) == "judge:5:funny:7"


def test_detect_lease_is_its_own_resource():
    assert detect_lease(3) == "detect:channel:3"
    assert len({detect_lease(3), recording_lease(3), backfill_lease("3")}) == 3


def test_payloads_roundtrip_and_reject_unknown_fields():
    judge = JudgePayload(candidate_id=5)
    assert judge.model_dump() == {"candidate_id": 5}
    assert JudgePayload.model_validate({"candidate_id": 5}) == judge
    offline = DetectOfflinePayload(stream_id=12, detector_version="v1")
    assert DetectOfflinePayload.model_validate(offline.model_dump()) == offline
    with pytest.raises(ValidationError):
        JudgePayload.model_validate({"candidate_id": 5, "round": 1})  # extra="forbid"
    with pytest.raises(ValidationError):
        DetectOfflinePayload.model_validate({"stream_id": 12})  # version is required
    with pytest.raises(ValidationError):
        judge.candidate_id = 6  # frozen


# --------------------------------------------------------------------------- database

T0 = utc_now().replace(microsecond=0)


def at(seconds: float):
    return T0 + timedelta(seconds=seconds)


def candidate_values(stream: Stream, peak_s: float, **kw) -> dict:
    return {
        "stream_id": stream.id,
        "channel_id": stream.channel_id,
        "source": CandidateSource.LIVE,
        "status": CandidateStatus.EMITTED,
        "trigger": "chat",
        "peak_utc": at(peak_s),
        "chat_peak_utc": at(peak_s + 8),
        "chat_delay_s": 8.0,
        "delay_source": "default",
        "moment_from": at(peak_s - 10),
        "moment_to": at(peak_s + 5),
        "window_from": at(peak_s - 90),
        "window_to": at(peak_s + 30),
        "score": 6.5,
        "detector_version": "test",
    } | kw


def candidate(stream: Stream, peak_s: float, **kw) -> Candidate:
    return Candidate(**candidate_values(stream, peak_s, **kw))


async def make_stream(sessions) -> Stream:
    async with sessions.begin() as s:
        channel = Channel(login="detector-test")
        s.add(channel)
        await s.flush()
        stream = Stream(channel_id=channel.id, platform_stream_id="s1", started_at=T0)
        s.add(stream)
    return stream


@pytest.mark.integration
async def test_candidate_with_media_request_and_judge_job_in_one_transaction(sessions):
    stream = await make_stream(sessions)
    async with sessions.begin() as s:
        cand = candidate(stream, 600)
        s.add(cand)
        await s.flush()
        cand.media_request_id = await media.request_media(
            s,
            stream_id=stream.id,
            window=Interval(cand.window_from, cand.window_to),
            priority=Priority.TRIGGER,
            requested_by="detector",
        )
        job_id = await jobs.enqueue(
            s,
            JobKind.JUDGE,
            payload=JudgePayload(candidate_id=cand.id).model_dump(),
            ref_id=cand.id,
            dedupe_key=dedupe_key(JobKind.JUDGE, cand.id),
            priority=Priority.TRIGGER,
        )
    async with sessions() as s:
        stored = (await s.execute(select(Candidate))).scalar_one()
        job = await s.get(Job, job_id)
    assert (stored.moment_type, stored.features, stored.suppress_reason) == ("unknown", {}, None)
    assert stored.media_request_id is not None and stored.created_at is not None
    assert (job.kind, job.ref_id, job.dedupe_key) == ("judge", stored.id, f"judge:{stored.id}")
    assert JudgePayload.model_validate(job.payload).candidate_id == stored.id


@pytest.mark.integration
async def test_one_candidate_per_stream_peak_and_source(sessions):
    stream = await make_stream(sessions)
    async with sessions.begin() as s:
        s.add(candidate(stream, 600))
        # the offline pass may find the same peak again; a suppressed row is a candidate too
        s.add(candidate(stream, 600, source=CandidateSource.OFFLINE, trigger="audio"))
        suppressed = candidate(
            stream, 900, status=CandidateStatus.SUPPRESSED, suppress_reason="raid"
        )
        s.add(suppressed)
    with pytest.raises(IntegrityError):
        async with sessions.begin() as s:
            s.add(candidate(stream, 600, score=9.0))
    async with sessions.begin() as s:  # how the detector inserts: a conflict returns no row
        inserted = await s.execute(
            insert(Candidate)
            .values(candidate_values(stream, 600, score=9.0))
            .on_conflict_do_nothing()
            .returning(Candidate.id)
        )
        assert inserted.scalar_one_or_none() is None
        # re-emitting a suppressed candidate updates it in place and keeps the reason
        await s.execute(
            text("UPDATE candidates SET status = 'emitted' WHERE id = :i"), {"i": suppressed.id}
        )
    async with sessions() as s:
        rows = (await s.execute(text("SELECT status, suppress_reason FROM candidates"))).all()
    assert len(rows) == 3 and {tuple(r) for r in rows} == {("emitted", None), ("emitted", "raid")}


POINT = {"window_from": at(600), "window_to": at(600), "moment_from": at(600), "moment_to": at(600)}


@pytest.mark.integration
@pytest.mark.parametrize(
    ("bad", "check"),
    [
        ({"source": "backfill"}, "source"),
        ({"status": "judged"}, "status"),
        ({"status": "suppressed"}, "suppress_reason"),  # without a reason
        ({"moment_type": "greeting"}, "moment_type"),  # a lexicon category, not a moment type
        ({"moment_to": T0}, "moment_range"),  # before moment_from (peak - 10 s)
        (POINT, "window_range"),  # an empty window (everything else nested)
        ({"moment_from": at(500)}, "nesting"),  # before window_from (peak - 90 s)
        ({"moment_to": at(640)}, "nesting"),  # after window_to (peak + 30 s)
        ({"peak_utc": at(650)}, "nesting"),
    ],
)
async def test_candidate_checks_reject_bad_rows(sessions, bad, check):
    stream = await make_stream(sessions)
    with pytest.raises(IntegrityError, match=f"ck_candidates_{check}"):
        async with sessions.begin() as s:
            s.add(candidate(stream, 600, **bad))


def estimate(stream: Stream, **kw) -> dict:
    return {
        "channel_id": stream.channel_id,
        "stream_id": stream.id,
        "method": "audio_events",
        "delay_s": 6.0,
        "events": 20,
        "strength": 2.1,
        "valid": True,
        "detector_version": "test",
    } | kw


@pytest.mark.integration
async def test_chat_delay_estimates_upsert_per_stream_and_method(sessions):
    stream = await make_stream(sessions)
    async with sessions.begin() as s:
        s.add(ChatDelayEstimate(**estimate(stream)))
    with pytest.raises(IntegrityError):
        async with sessions.begin() as s:
            s.add(ChatDelayEstimate(**estimate(stream, delay_s=7.0)))
    async with sessions() as s:
        first = (await s.execute(select(ChatDelayEstimate))).scalar_one()
    async with sessions.begin() as s:  # how the detector writes: a re-run replaces the estimate
        stmt = insert(ChatDelayEstimate).values(estimate(stream, delay_s=7.0))
        await s.execute(
            stmt.on_conflict_do_update(
                index_elements=["stream_id", "method"],
                set_={"delay_s": stmt.excluded.delay_s, "computed_at": text("now()")},
            )
        )
    async with sessions() as s:
        stored = (await s.execute(select(ChatDelayEstimate))).scalar_one()
    assert (stored.id, stored.delay_s) == (first.id, 7.0)
    assert stored.computed_at >= first.computed_at


@pytest.mark.integration
async def test_chat_delay_estimate_checks(sessions):
    stream = await make_stream(sessions)
    no_events = estimate(stream, delay_s=None, strength=None, events=0, valid=False)
    async with sessions.begin() as s:  # an invalid estimate may have no delay at all
        s.add(ChatDelayEstimate(**no_events))
    for bad, check in (({"strength": None}, "valid_has_delay"), ({"events": -1}, "events")):
        with pytest.raises(IntegrityError, match=f"ck_chat_delay_estimates_{check}"):
            async with sessions.begin() as s:
                s.add(ChatDelayEstimate(**estimate(stream, method="other", **bad)))
