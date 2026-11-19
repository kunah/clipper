"""Chunk planner: contiguous ~60 s chunks, gaps for muted/lost/skipped parts, exact UTC."""

import itertools
import re
from datetime import UTC, datetime, timedelta

import pytest

from clipper_backfill.planner import PlanError, PlannedGap, at, check_aligned, plan_units
from clipper_core.contracts import GapReason
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
