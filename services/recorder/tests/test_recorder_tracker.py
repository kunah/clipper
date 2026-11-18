from datetime import UTC, datetime, timedelta

from clipper_core.contracts import GapReason
from clipper_recorder.tracker import SegmentTracker
from clipper_twitch.hls import Segment

T0 = datetime(2026, 11, 18, 20, 0, tzinfo=UTC)


def seg(i: int, *, ad=False, disc=False, dur=2.0, offset=0.0) -> Segment:
    """Segment number i of a 2 s grid starting at T0."""
    return Segment(
        sequence=i,
        uri=f"s{i}.ts",
        start=T0 + timedelta(seconds=2 * i + offset),
        duration=dur,
        title=None if ad else "live",
        ad=ad,
        discontinuity=disc,
    )


def test_fresh_stream_records_time_missed_since_go_live():
    t = SegmentTracker.fresh(T0 - timedelta(seconds=40))
    plan = t.live(seg(0))
    assert plan.split
    assert plan.gap.reason == GapReason.NOT_RECORDED
    assert plan.gap.seconds == 40
    assert t.live(seg(1)).gap is None


def test_go_live_after_first_segment_is_not_a_gap():
    t = SegmentTracker.fresh(T0 + timedelta(seconds=1))
    assert t.live(seg(0)).gap is None


def test_contiguous_segments_and_small_jitter_make_no_gap():
    t = SegmentTracker(media_end=None)
    assert t.live(seg(0)).gap is None
    assert t.live(seg(1, offset=0.3)).gap is None
    assert not t.live(seg(2)).split


def test_duplicates_from_overlapping_reloads_are_skipped():
    t = SegmentTracker(media_end=None)
    for i in range(3):
        assert t.is_new(seg(i))
        t.live(seg(i))
    assert not t.is_new(seg(1))
    assert not t.is_new(seg(2))
    assert t.is_new(seg(3))


def test_resume_skips_what_was_recorded_and_marks_downtime():
    t = SegmentTracker.resume(T0 + timedelta(seconds=6))  # recorded up to the end of seg 2
    assert not t.is_new(seg(2))
    assert t.is_new(seg(3))
    assert t.live(seg(3)).gap is None  # nothing missed
    t2 = SegmentTracker.resume(T0 + timedelta(seconds=6))
    plan = t2.live(seg(10))
    assert plan.gap.reason == GapReason.NOT_RECORDED
    assert plan.gap.start == T0 + timedelta(seconds=6)
    assert plan.gap.end == T0 + timedelta(seconds=20)


def test_ad_break_becomes_an_ad_gap():
    t = SegmentTracker(media_end=None)
    t.live(seg(0))
    assert t.ad(seg(1, ad=True)) is True  # break starts
    assert t.ad(seg(2, ad=True)) is False
    assert not t.is_new(seg(2, ad=True))  # ads are not looked at twice
    plan = t.live(seg(3, disc=True))
    assert plan.split
    assert plan.gap.reason == GapReason.AD
    assert (plan.gap.start, plan.gap.end) == (seg(1).start, seg(3).start)
    assert not t.in_ad


def test_lost_segment_becomes_a_reconnect_gap():
    t = SegmentTracker(media_end=None)
    t.live(seg(0))
    t.lost(seg(1))
    assert not t.is_new(seg(1))
    plan = t.live(seg(2))
    assert plan.gap.reason == GapReason.RECONNECT and plan.gap.seconds == 2


def test_unexplained_jump_is_unknown_and_reason_resets_after_live_media():
    t = SegmentTracker(media_end=None)
    t.live(seg(0))
    assert t.live(seg(5)).gap.reason == GapReason.UNKNOWN
    t.interrupted(GapReason.RECONNECT)
    t.interrupted(GapReason.AD)  # first known reason wins
    assert t.live(seg(9)).gap.reason == GapReason.RECONNECT
    assert t.live(seg(12)).gap.reason == GapReason.UNKNOWN


def test_discontinuity_without_gap_still_splits():
    t = SegmentTracker(media_end=None)
    t.live(seg(0))
    plan = t.live(seg(1, disc=True))
    assert plan.split and plan.gap is None
