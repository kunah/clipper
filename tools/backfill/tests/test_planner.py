"""Chunk planner: contiguous ~60 s chunks, gaps for muted/lost/skipped parts, exact UTC."""

import itertools
import re
from datetime import UTC, datetime, timedelta

import pytest

from clipper_backfill.planner import (
    PlanError,
    PlannedGap,
    at,
    check_aligned,
    fill_ranges,
    plan_fill,
    plan_units,
)
from clipper_core.contracts import GapReason
from clipper_core.timeline import Interval
from clipper_twitch.hls import parse_vod_playlist

T0 = datetime(2026, 11, 18, 6, 27, 49, 784000, tzinfo=UTC)
BASE = "https://cdn.test/abc123_streamer_777_1795000000/audio_only/index-dvr.m3u8"


def playlist(n=16, *, muted=(), disc=(), durations=None, maps=None):
    """A Twitch-style VOD playlist: `N.ts` / `N-muted.ts`, 10 s EXTINF unless `durations`."""
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:13", "#EXT-X-MEDIA-SEQUENCE:0"]
    for i in range(n):
        if maps and i in maps:
            lines.append(f'#EXT-X-MAP:URI="{maps[i]}"')
        if i in disc:
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append(f"#EXTINF:{(durations or {}).get(i, 10.0):.3f},")
        lines.append(f"{i}-muted.ts" if i in muted else f"{i}.ts")
    lines.append("#EXT-X-ENDLIST")
    return parse_vod_playlist("\n".join(lines) + "\n", BASE).segments


def shape(plan):
    """[(seq, [segment indexes], [(gap start, end, reason)], audio)] per unit."""
    return [
        (
            u.seq,
            [s.index for s in u.segments],
            [(g.start_s, g.end_s, str(g.reason)) for g in u.gaps],
            u.audio,
        )
        for u in plan.units
    ]


def test_plain_vod_becomes_60_s_chunks_and_a_short_last_one():
    plan = plan_units(playlist(16))
    assert shape(plan) == [
        (0, [0, 1, 2, 3, 4, 5], [], True),
        (1, [6, 7, 8, 9, 10, 11], [], True),
        (2, [12, 13, 14, 15], [], True),
    ]
    assert plan.complete and plan.end_s == 160.0 and plan.vod_end_s == 160.0
    first, second = plan.chunks[:2]
    assert first.interval(T0).start == T0
    assert first.interval(T0).end == second.interval(T0).start == T0 + timedelta(seconds=60)


def test_muted_run_is_one_gap_and_a_discontinuity_splits():
    plan = plan_units(playlist(16, muted={8, 9}, disc={12}))
    assert shape(plan) == [
        (0, [0, 1, 2, 3, 4, 5], [], True),
        (1, [6, 7], [], True),
        (2, [10, 11], [(80.0, 100.0, "muted")], True),
        (3, [12, 13, 14, 15], [], True),
    ]
    gap = plan.gaps[0].utc(T0)
    assert (gap.start, gap.end, gap.reason) == (at(T0, 80), at(T0, 100), GapReason.MUTED)
    # the chunk after the gap starts exactly at its end; no chunk contains a muted segment
    assert plan.chunks[2].interval(T0).start == gap.end
    assert not any(s.muted for u in plan.chunks for s in u.segments)


def test_muted_start_and_end():
    plan = plan_units(playlist(10, muted={0, 1, 8, 9}))
    assert shape(plan) == [
        (0, [2, 3, 4, 5, 6, 7], [(0.0, 20.0, "muted")], True),
        (None, [], [(80.0, 100.0, "muted")], True),  # no chunk after it: a unit of its own
    ]
    assert plan.units[-1].done_s == 100.0 and plan.complete


def test_all_muted():
    plan = plan_units(playlist(3, muted={0, 1, 2}))
    assert shape(plan) == [(None, [], [(0.0, 30.0, "muted")], True)]


def test_init_section_change_splits_fmp4_chunks():
    segs = playlist(6, maps={0: "init-0.mp4", 3: "init-1.mp4"})
    plan = plan_units(segs)
    assert [[s.index for s in u.segments] for u in plan.units] == [[0, 1, 2], [3, 4, 5]]
    assert plan.units[1].segments[0].init_uri.endswith("init-1.mp4")


def test_resume_from_done_gives_the_same_units_with_the_next_seq():
    segs = playlist(16, muted={8, 9}, disc={12})
    full = plan_units(segs)
    for k, unit in enumerate(full.units):  # a crash after committing unit k
        rest = plan_units(segs, done_s=unit.done_s, first_seq=unit.seq + 1)
        assert shape(rest) == shape(full)[k + 1 :]


def test_start_offset_skips_ahead_with_a_not_recorded_gap():
    plan = plan_units(playlist(16), start_s=65.0)
    assert shape(plan)[0] == (0, [6, 7, 8, 9, 10, 11], [(0.0, 60.0, "not_recorded")], True)
    # resuming later: the gap is not planned again
    again = plan_units(playlist(16), done_s=120.0, start_s=65.0, first_seq=1)
    assert shape(again) == [(1, [12, 13, 14, 15], [], True)]


def test_until_caps_the_run_at_segment_starts():
    plan = plan_units(playlist(16), until_s=100.0)
    assert shape(plan) == [(0, [0, 1, 2, 3, 4, 5], [], True), (1, [6, 7, 8, 9], [], True)]
    assert plan.end_s == 100.0 and not plan.complete
    nothing = plan_units(playlist(16), done_s=100.0, until_s=100.0)
    assert nothing.units == [] and nothing.end_s == 100.0


def test_lost_segments_become_not_recorded_gaps_next_to_muted_ones():
    plan = plan_units(playlist(12, muted={5}), lost=frozenset({3, 4, 6}))
    assert shape(plan) == [
        (0, [0, 1, 2], [], True),
        (
            1,
            [7, 8, 9, 10, 11],
            [(30.0, 50.0, "not_recorded"), (50.0, 60.0, "muted"), (60.0, 70.0, "not_recorded")],
            True,
        ),
    ]
    assert [u.lost for u in plan.units] == [0, 3]


def test_with_video_muted_runs_are_video_only_chunks_with_their_own_gap():
    plan = plan_units(playlist(10, muted={3, 4}), muted_video=True)
    assert shape(plan) == [
        (0, [0, 1, 2], [], True),
        (1, [3, 4], [(30.0, 50.0, "muted")], False),
        (2, [5, 6, 7, 8, 9], [], True),
    ]


def test_longer_segments_close_the_chunk_once_it_reaches_60_s():
    plan = plan_units(playlist(8, durations=dict.fromkeys(range(8), 13.0)))
    assert [len(u.segments) for u in plan.units] == [5, 3]
    assert plan.units[0].end_s == 65.0


def test_odd_durations_still_give_microsecond_exact_contiguous_intervals():
    durations = {i: 10.001 + (i % 3) * 0.333 for i in range(40)}
    plan = plan_units(playlist(40, durations=durations))
    intervals = [u.interval(T0) for u in plan.chunks]
    assert all(a.end == b.start for a, b in itertools.pairwise(intervals))
    total = sum(durations.values())
    assert intervals[-1].end == at(T0, total)


def test_empty_playlist_and_bad_chunk_length():
    assert plan_units([]).units == [] and plan_units([]).complete
    with pytest.raises(ValueError):
        plan_units(playlist(2), chunk_s=0)


def test_planned_gap_seconds():
    assert PlannedGap(10.0, 25.5, GapReason.MUTED).seconds == 15.5


def test_done_must_be_a_segment_boundary_of_the_playlist():
    """Committed by a 10 s rendition up to 30 s; a rendition cut every 12 s can't continue."""
    other = playlist(5, durations=dict.fromkeys(range(5), 12.0))
    with pytest.raises(PlanError, match=re.escape("spans [24.000, 36.000)")):
        plan_units(other, done_s=30.0, first_seq=3)
    for done in (0.0, 36.0, 60.0, 75.0):  # start, a boundary, the end, past the end
        check_aligned(other, done)
    assert plan_units(other, done_s=36.0, first_seq=3).units[0].start_s == 36.0
    # a start offset inside a segment is fine: it only moves forward from done_s
    assert plan_units(other, done_s=24.0, start_s=30.0).units[0].start_s == 24.0


# --------------------------------------------------------------------------- --fill-gaps


def t(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def iv(a: float, b: float) -> Interval:
    return Interval(t(a), t(b))


LIVE_CHUNKS = [iv(23.3, 53.3), iv(53.3, 83.3), iv(121.7, 151.7)]
LIVE_GAPS = [(iv(-3, 23.3), "not_recorded"), (iv(83.3, 121.7), "not_recorded")]


def test_fill_ranges_are_not_recorded_gaps_head_and_tail_inside_the_vod():
    ranges = fill_ranges(vod=iv(0, 200), chunks=LIVE_CHUNKS, gaps=LIVE_GAPS)
    assert [(r.kind, r.interval, r.gap_row) for r in ranges] == [
        ("head", iv(0, 23.3), True),  # the gap row cut to the VOD's start
        ("gap", iv(83.3, 121.7), True),
        ("tail", iv(151.7, 200), False),  # no gap row: the recorder never wrote one
    ]
    # an ad is no hole to fill, a chunk without audio is live media, gaps beyond the VOD are cut,
    # a hole between live chunks without a gap row is not filled (the recorder leaves none)
    gaps = [*LIVE_GAPS, (iv(90, 95), "ad"), (iv(190, 230), "not_recorded")]
    chunks = [*LIVE_CHUNKS, iv(170, 175)]
    ranges = fill_ranges(vod=iv(0, 200), chunks=chunks, gaps=gaps)
    assert [(r.kind, r.interval) for r in ranges] == [
        ("head", iv(0, 23.3)),
        ("gap", iv(83.3, 90)),
        ("gap", iv(95, 121.7)),
        ("tail", iv(175, 200)),
    ]
    # an unrecorded head without a gap row; nothing recorded at all; a VOD inside live media
    ranges = fill_ranges(vod=iv(0, 200), chunks=LIVE_CHUNKS, gaps=[])
    assert [(r.kind, r.interval, r.gap_row) for r in ranges] == [
        ("head", iv(0, 23.3), False),
        ("tail", iv(151.7, 200), False),
    ]
    assert [(r.kind, r.interval) for r in fill_ranges(vod=iv(0, 50), chunks=[], gaps=[])] == [
        ("all", iv(0, 50))
    ]
    assert fill_ranges(vod=iv(30, 50), chunks=LIVE_CHUNKS, gaps=LIVE_GAPS) == []


def fill_shape(plan):
    return [(u.seq, [s.index for s in u.segments], u.gaps, u.mark_s) for u in plan.units]


def test_fill_plan_takes_only_segments_inside_and_never_plans_a_gap():
    segs = playlist(20, muted={18}, disc={10})
    ranges = fill_ranges(vod=iv(0, 200), chunks=LIVE_CHUNKS, gaps=LIVE_GAPS)
    plan = plan_fill(segs, ranges, T0, first_seq=1_000_000, chunk_s=20)
    assert fill_shape(plan) == [
        (1_000_000, [0, 1], (), None),  # [20, 30) crosses the live chunk at 23.3: unfilled
        (1_000_001, [9], (), None),  # the discontinuity before 10 splits
        (1_000_002, [10, 11], (), None),
        (1_000_003, [16, 17], (), None),  # 18 muted: skipped, no gap
        (1_000_004, [19], (), None),
    ]
    assert plan.complete and plan.vod_end_s == 200.0
    for unit in plan.chunks:  # never across a live chunk, always inside a range
        span = unit.interval(T0)
        assert not any(span.overlaps(c) for c in LIVE_CHUNKS)
        assert any(r.interval.start <= span.start and span.end <= r.interval.end for r in ranges)
    head, gap, tail = plan.parts
    assert (head.segments, head.first_s, head.last_s) == (2, 0, 20)
    assert head.unfilled_edges_s == pytest.approx(3.3)
    assert (gap.segments, gap.chunks, gap.unfilled_edges_s) == (3, 2, pytest.approx(8.4))
    assert (tail.segments, tail.muted_s, tail.chunks, tail.planned_s) == (4, 10.0, 2, 30.0)

    # resume from any committed point: the same units, the same seqs
    for unit in plan.units[:-1]:
        nxt = plan.units.index(unit) + 1
        again = plan_fill(segs, ranges, T0, done_s=unit.done_s, first_seq=unit.seq + 1, chunk_s=20)
        assert fill_shape(again) == fill_shape(plan)[nxt:]


def test_fill_plan_marks_lost_segments_and_the_end():
    segs = playlist(20)
    ranges = fill_ranges(vod=iv(0, 200), chunks=LIVE_CHUNKS[:2], gaps=LIVE_GAPS[:1])
    plan = plan_fill(segs, ranges, T0, first_seq=7, chunk_s=60, lost=frozenset({9, 19}))
    assert fill_shape(plan) == [
        (7, [0, 1], (), None),
        (8, [10, 11, 12, 13, 14, 15], (), None),  # lost 9 counted with the chunk after it
        (9, [16, 17, 18], (), None),
        (None, [], (), 200.0),  # lost 19 at the end: counted by the mark
    ]
    assert [u.lost for u in plan.units] == [0, 1, 0, 1]
    # nothing inside any range (the VOD's segments all cross live media): only the end mark
    plan = plan_fill(playlist(3), [], T0, first_seq=7)
    assert fill_shape(plan) == [(None, [], (), 30.0)] and plan.chunks == []
    assert plan_fill(playlist(3), [], T0, done_s=30.0, first_seq=7).units == []
