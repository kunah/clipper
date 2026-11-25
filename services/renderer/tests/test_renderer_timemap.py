"""The time map (DESIGN §20 step 3): trims per chunk, jitter fill, gap cuts, overlaps, the teaser
first, mapping plan times to output times."""

from datetime import timedelta

import pytest

from clipper_core.timeline import Interval
from clipper_renderer import timemap
from clipper_renderer.samples import at, sample_chunks, sample_plan
from clipper_renderer.timemap import ChunkVideo, NoVideo, OutRange, build, plan_ranges


def chunk(cid: int, a: float, b: float, seq: int | None = None) -> ChunkVideo:
    return ChunkVideo(cid, cid if seq is None else seq, at(a), at(b), f"k{cid}")


def clip(a: float, b: float) -> list[OutRange]:
    return [OutRange(Interval(at(a), at(b)), teaser=False)]


def test_one_chunk_one_piece_trimmed_from_the_chunk_start():
    tm = build(clip(10, 25), [chunk(1, 0, 60)])
    (p,) = tm.pieces
    assert (p.trim_from_s, p.trim_to_s, p.out_at_s, p.pad_after_s) == (10.0, 25.0, 0.0, 0.0)
    assert (tm.duration_s, tm.teaser_s, tm.warnings) == (15.0, 0.0, ())
    assert tm.clip_out(at(12.5)) == 2.5
    assert tm.clip_out(at(25)) is None and tm.clip_out(at(9.99)) is None


def test_contiguous_chunks_join_without_a_cut():
    tm = build(clip(50, 70), [chunk(1, 0, 60), chunk(2, 60, 120)])
    a, b = tm.pieces
    assert (a.trim_from_s, a.trim_to_s, b.trim_from_s, b.trim_to_s) == (50.0, 60.0, 0.0, 10.0)
    assert (b.out_at_s, b.cut_before) == (10.0, False)
    assert len(tm.runs) == 1 and tm.clip_out(at(65)) == 15.0


def test_a_jitter_hole_is_filled_and_time_stays_linear():
    tm = build(clip(50, 70), [chunk(1, 0, 60), chunk(2, 60.3, 120)])
    a, b = tm.pieces
    assert (a.pad_after_s, a.out_to_s) == (0.3, 10.3)
    assert (b.trim_from_s, b.out_at_s) == (0.0, 10.3)
    assert tm.duration_s == 20.0 and tm.warnings == ()
    (run,) = tm.runs
    assert (run.utc_from, run.utc_to) == (at(50), at(70))
    for rel in (50, 59.9, 60.1, 60.3, 69.9):  # inside the hole too: the held frame
        assert tm.clip_out(at(rel)) == pytest.approx(rel - 50)


def test_a_hole_longer_than_the_jitter_is_a_hard_cut():
    tm = build(clip(50, 70), [chunk(1, 0, 60), chunk(2, 62, 120)])
    a, b = tm.pieces
    assert (a.pad_after_s, b.out_at_s, b.cut_before) == (0.0, 10.0, True)
    assert tm.duration_s == 18.0
    assert [w.split(":")[0] for w in tm.warnings] == ["gap_cut"]
    assert "skips 2.00 s" in tm.warnings[0]
    assert len(tm.runs) == 2
    assert tm.clip_out(at(61)) is None and tm.clip_out(at(62)) == 10.0
    assert tm.out_ranges(at(55), at(65)) == [(5.0, 10.0), (10.0, 13.0)]


def test_overlaps_are_cut_from_the_later_chunk_and_order_is_by_start_then_seq():
    # chunk 3 starts inside chunk 1 (a fill chunk beside a live one, say); chunk 2 is the same
    # range as 3 with a higher seq: it is fully covered and dropped
    chunks = [chunk(2, 58, 80, seq=9), chunk(1, 0, 60, seq=5), chunk(3, 58, 80, seq=7)]
    tm = build(clip(50, 70), chunks)
    assert [(p.chunk.chunk_id, p.trim_from_s, p.trim_to_s) for p in tm.pieces] == [
        (1, 50.0, 60.0),
        (3, 2.0, 12.0),
    ]
    assert tm.duration_s == 20.0


def test_ranges_start_or_end_without_video_are_shortened_with_a_warning():
    tm = build(clip(-5, 70), [chunk(1, 0, 60)])
    assert tm.pieces[0].trim_from_s == 0.0 and tm.duration_s == 60.0
    assert [w.split(":")[0] for w in tm.warnings] == ["no_video_start", "no_video_end"]
    quiet = build(clip(-0.2, 60.4), [chunk(1, 0, 60)])  # within the jitter: no warning
    assert quiet.warnings == () and quiet.duration_s == 60.0


def test_no_video_under_a_range_raises():
    with pytest.raises(NoVideo) as err:
        build([*clip(0, 10), *clip(200, 210)], [chunk(1, 0, 60)])
    assert err.value.range_index == 1
    with pytest.raises(NoVideo):
        build(clip(0, 10), [])


def test_the_sample_plan_teaser_first_then_two_segments():
    plan = sample_plan()
    ranges = plan_ranges(plan)
    assert [(r.teaser, r.interval.seconds) for r in ranges] == [
        (True, 2.5),
        (False, 14.0),
        (False, 11.0),
    ]
    tm = build(ranges, sample_chunks())
    got = [
        (p.chunk.seq, p.trim_from_s, p.trim_to_s, p.out_at_s, p.pad_after_s, p.teaser, p.cut_before)
        for p in tm.pieces
    ]
    assert got == [
        (10, 58.5, 60.0, 0.0, 0.3, True, True),  # teaser: -1.5 .. 0, the jitter hole held
        (11, 0.0, 0.7, 1.8, 0.0, True, False),  # .. 1.0
        (10, 52.0, 60.0, 2.5, 0.3, False, True),  # segment 1: -8 .. 0 (+ the hole)
        (11, 0.0, 5.7, 10.8, 0.0, False, False),  # .. 6
        (11, 54.7, 59.7, 16.5, 0.0, False, True),  # segment 2: 55 .. 60 (chunk 11 starts at +0.3)
        (12, 0.0, 2.3, 21.5, 0.0, False, True),  # the gap 60 .. 63.7 is cut; 63.7 .. 66
    ]
    assert tm.duration_s == 23.8 and tm.teaser_s == 2.5
    assert [w.split(":")[0] for w in tm.warnings] == ["gap_cut"]
    assert tm.out_times(at(0.5)) == [2.0, 11.0]  # in the teaser and in the clip
    assert tm.out_times(at(0.5), teaser=True) == [2.0]
    assert tm.clip_out(at(0.5)) == 11.0
    assert tm.clip_out(at(64)) == 21.8
    assert tm.out_ranges(at(58), at(61), teaser=False) == [(19.5, 21.5)]  # the zoom, cut at the gap
    assert tm.out_ranges(at(-1), at(1.5)) == [(0.5, 2.5), (9.5, 12.0)]
    assert len(tm.runs) == 4
    assert tm.run_of(at(59.8), at(60.1), slack_s=0.05) == []  # crosses the gap's cut
    assert len(tm.run_of(at(-0.25), at(0.05))) == 2  # over the jitter hole: teaser + clip


def test_pieces_are_frozen_values():
    p = build(clip(0, 1), [chunk(1, 0, 60)]).pieces[0]
    with pytest.raises(AttributeError):
        p.out_at_s = 3.0  # type: ignore[misc]
    assert timemap.MIN_PIECE_S < 0.01
    assert p.duration_s == 1.0 and p.out_to_s == 1.0
    assert at(1) - at(0) == timedelta(seconds=1)
