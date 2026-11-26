"""Merge: delay, video-time peak/moment/window, cooldown fold-in, no hourly cap, the draft row."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from clipper_core.contracts import (
    CandidateSource,
    CandidateStatus,
    DelaySource,
    MomentType,
    SuppressReason,
)
from clipper_core.models import Candidate
from clipper_detector.merge import (
    CandidateDraft,
    FoldIn,
    PriorCandidate,
    candidate_times,
    plan_candidate,
    resolve_delay,
)
from clipper_detector.params import DetectorParams
from clipper_detector.stage2 import EventSummary, Shares, Stage2Result
from clipper_detector.synthetic import episode

P = DetectorParams()
T0 = datetime(2026, 11, 20, 12, tzinfo=UTC)
DEFAULT = resolve_delay(None, [], P)


def s(seconds: float) -> timedelta:
    return timedelta(seconds=seconds)


def stage2(score: float = 10.0, *, reason: SuppressReason | None = None) -> Stage2Result:
    return Stage2Result(
        status=CandidateStatus.EMITTED if reason is None else CandidateStatus.SUPPRESSED,
        reason=reason,
        rules=(reason,) if reason is not None else (),
        z_clean=score,
        clean_fraction=1.0,
        score=score,
        moment_type=MomentType.FUNNY,
        votes={"funny": 6.0},
        shares=Shares(reaction=0.7),
        events=EventSummary(known=True),
        messages=100,
        chatters=80,
        clean_chatters=78,
    )


def plan(ep, result=None, recent=(), delay=DEFAULT):
    return plan_candidate(
        ep,
        result or stage2(),
        stream_id=7,
        channel_id=3,
        delay=delay,
        recent=list(recent),
        params=P,
        detector_version="d1-p00000000-l1",
    )


def test_resolve_delay():
    assert resolve_delay(None, [], P) == DEFAULT
    assert (DEFAULT.seconds, DEFAULT.source) == (8.0, DelaySource.DEFAULT)
    learned = resolve_delay(None, [5.0, 6.0, 30.0, 7.0, 5.5, 99.0, 99.0], P)  # newest first
    assert (learned.seconds, learned.source) == (6.0, DelaySource.LEARNED)  # median of first 5
    override = resolve_delay(4.0, [6.0], P)
    assert (override.seconds, override.source) == (4.0, DelaySource.OVERRIDE)


def test_times_for_a_short_episode():
    peak = T0 + s(100)
    t = candidate_times(peak, peak - s(4), peak + s(6), DEFAULT, P)
    assert t.peak_utc == peak - s(8)  # video time = chat time - delay
    assert (t.moment_from, t.moment_to) == (peak - s(12), peak - s(2))
    assert (t.window_from, t.window_to) == (t.peak_utc - s(90), t.peak_utc + s(30))


def test_times_follow_a_long_episode_and_are_clamped_to_5_min():
    peak = T0 + s(1000)
    # long tail after the peak: window_to = close - d + 15 s
    t = candidate_times(peak, peak - s(5), peak + s(150), DEFAULT, P)
    assert t.window_to == peak + s(150) - s(8) + s(15)
    assert t.window_from == t.peak_utc - s(90)
    # very long: both sides beyond [T - 90, T + 30]; the larger is cut first, to 5 min total
    t = candidate_times(peak, peak - s(170), peak + s(170), DEFAULT, P)
    assert (t.window_to - t.window_from).total_seconds() == pytest.approx(300)
    assert t.window_from <= t.peak_utc - s(90) and t.window_to >= t.peak_utc + s(30)
    assert t.window_from <= t.moment_from <= t.peak_utc <= t.moment_to <= t.window_to
    left = (t.peak_utc - s(90) - t.window_from).total_seconds()
    right = (t.window_to - t.peak_utc - s(30)).total_seconds()
    assert left == pytest.approx(right) == pytest.approx(90)


def test_draft_row_matches_the_model_and_nests():
    ep = episode(T0 + s(100))
    draft = plan(ep)
    assert isinstance(draft, CandidateDraft) and draft.emitted
    row = draft.row()
    columns = {c.name for c in Candidate.__table__.columns}
    assert set(row) <= columns
    assert columns - set(row) == {"id", "media_request_id", "created_at", "updated_at"}
    assert row["source"] == CandidateSource.LIVE.value and row["trigger"] == "chat"
    assert row["delay_source"] == "default" and row["chat_delay_s"] == 8.0
    assert row["chat_peak_utc"] == ep.peak and row["peak_utc"] == ep.peak - s(8)
    assert row["moment_type"] == "funny" and row["suppress_reason"] is None
    assert draft.window_from <= draft.moment_from <= draft.peak_utc <= draft.moment_to
    assert draft.moment_to <= draft.window_to and draft.window.seconds == 120
    features = json.loads(json.dumps(row["features"]))
    assert features["z"] == pytest.approx(ep.strongest.z, abs=1e-3)
    assert features["delay"] == {"seconds": 8.0, "source": "default"}
    assert features["chat"]["peak"] == ep.peak.isoformat()
    assert features["shares"]["reaction"] == 0.7


def test_suppressed_stage2_keeps_its_reason():
    draft = plan(episode(T0 + s(100)), stage2(reason=SuppressReason.RAID))
    assert draft.status is CandidateStatus.SUPPRESSED
    assert draft.row()["suppress_reason"] == "raid"


def prior(chat_peak_s, *, score=10.0, status=CandidateStatus.EMITTED, cid=1, end_after_s=6):
    peak = T0 + s(chat_peak_s)
    return PriorCandidate(cid, status, peak, peak + s(end_after_s), score)


def test_cooldown_folds_a_weaker_followup():
    prev = prior(100)  # chat end at 106 s
    ep = episode(T0 + s(140))  # opens at 142 s: 36 s after
    folded = plan(ep, stage2(8.0), [prev])
    assert isinstance(folded, FoldIn) and folded.into is prev
    assert folded.followup["score"] == 8.0 and folded.followup["peak"] == ep.peak.isoformat()
    # the cooldown now runs from the follow-up's close; its span and peaks grow (replay dedupe)
    assert folded.extended.chat_end == ep.end and folded.extended.score == prev.score
    assert folded.extended.followup_peaks == (ep.peak,)
    assert folded.extended.covers(ep.peak) and not prev.covers(ep.peak)
    assert folded.extended.near_peak(ep.peak + s(30), 60) and not prev.near_peak(ep.peak, 30)
    nxt = episode(T0 + s(190))  # opens 192 s: 86 s after the first close, 42 s after the second
    assert isinstance(plan(nxt, stage2(8.0), [folded.extended]), FoldIn)
    assert isinstance(plan(nxt, stage2(8.0), [prev]), CandidateDraft)

    stronger = plan(ep, stage2(12.0), [prev])
    assert isinstance(stronger, CandidateDraft) and stronger.emitted
    assert stronger.features["prev_candidate_id"] == 1


def test_cooldown_ignores_suppressed_and_old_candidates():
    ep = episode(T0 + s(140))
    suppressed = prior(100, status=CandidateStatus.SUPPRESSED)
    assert isinstance(plan(ep, stage2(1.0), [suppressed]), CandidateDraft)
    assert isinstance(plan(ep, stage2(1.0), [prior(60)]), CandidateDraft)  # ended 76 s before
    # a suppressed new episode is stored as such, even inside the cooldown
    draft = plan(ep, stage2(1.0, reason=SuppressReason.VOTE), [prior(100)])
    assert isinstance(draft, CandidateDraft) and draft.suppress_reason is SuppressReason.VOTE


def test_no_hourly_cap():
    """However many were emitted in the hour before, a weaker moment outside the cooldown is
    emitted too (the per-stream-hour budget was removed 2026-11-26)."""
    recent = [prior(100 + 400 * i, score=10.0 + i, cid=i) for i in range(20)]  # 20 in < 2.3 h
    draft = plan(episode(T0 + s(8100)), stage2(1.0), recent)
    assert isinstance(draft, CandidateDraft) and draft.emitted
    assert draft.suppress_reason is None and "budget" not in draft.features


def test_as_prior_round_trip():
    ep = episode(T0 + s(100))
    draft = plan(ep)
    p = draft.as_prior(42)
    assert (p.candidate_id, p.chat_peak, p.chat_end, p.score) == (42, ep.peak, ep.end, 10.0)
