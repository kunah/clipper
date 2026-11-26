"""Backtest metrics on hand-built candidates and clips (DESIGN §18.8): hit, k, recall weighting,
moments, precision set, chance, truth selection, partial chat coverage, exclusions, false
suppressions, totals, grid, labels; and the command line of `clipper-detector`."""

from datetime import UTC, datetime, timedelta

import pytest

from clipper_core.timeline import Interval
from clipper_detector.backtest import (
    DEDUPE,
    ClipRow,
    GridError,
    Pick,
    StreamResult,
    at_k,
    chat_coverage,
    classify_clips,
    grid_params,
    grid_table,
    hours_of,
    parse_grid,
    percentile,
    ranked,
    run_label,
    score_stream,
    select_truth,
    stream_table,
    top_k,
    totals,
    unhashed,
)
from clipper_detector.main import parse_args
from clipper_detector.params import DetectorParams

T = datetime(2026, 11, 20, 12, tzinfo=UTC)
RATES = (2.0, 4.0, 8.0)
P = DetectorParams()


def at(s: float) -> datetime:
    return T + timedelta(seconds=s)


def clip(cid: str, start_s: float, views: int, *, dur: float = 30, slack: float | None = 5):
    return ClipRow(cid, at(start_s), at(start_s + dur), dur, views, slack)


def pick(peak_s: float, score: float = 5.0, *, emitted: bool = True, reason: str | None = None):
    return Pick(at(peak_s), score, emitted, reason)


def everywhere(clips):
    """Truth with chat over the whole day and nothing muted."""
    return classify_clips(clips, coverage=[Interval(at(-3600), at(86400))], muted=[], delay_s=8)


# --------------------------------------------------------------------------- hit, k


def test_hit_is_the_peak_inside_the_span_plus_minus_slack():
    c = clip("a", 1000, 10, slack=5)
    assert c.holds(at(995)) and c.holds(at(1035)) and c.holds(at(1010))
    assert not c.holds(at(994.9)) and not c.holds(at(1035.1))
    default = clip("b", 1000, 10, slack=None)  # §17: duration + 10 s
    assert default.slack == timedelta(seconds=40)
    assert default.holds(at(960)) and not default.holds(at(959))


@pytest.mark.parametrize(
    ("rate", "hours", "k"),
    [(4, 2.0, 8), (4, 4.95, 20), (2, 0.1, 1), (8, 6.0, 48), (4, 0.0, 0), (4, 1 / 3 * 3, 4)],
)
def test_k_is_ceil_rate_times_chat_hours(rate, hours, k):
    assert top_k(rate, hours) == k


def test_ranking_by_score_then_earlier_peak_and_only_emitted():
    picks = [
        pick(10, 3.0),
        pick(20, 9.0),
        pick(30, 3.0),
        pick(40, 99.0, emitted=False, reason="raid"),
    ]
    assert [p.peak for p in ranked(picks)] == [at(20), at(10), at(30)]


def test_k_cuts_the_candidates_the_judge_would_get():
    truth = everywhere([clip("a", 1000, 50), clip("b", 2000, 30), clip("c", 3000, 20)])
    picks = [pick(1010, 9), pick(2010, 8), pick(3010, 7), pick(5000, 6)]
    # half an hour of chat: k = 1 (2/h), 2 (4/h), 4 (8/h)
    by_rate = {r: at_k(ranked(picks), truth, rate=r, hours=0.5) for r in RATES}
    assert [by_rate[r].k for r in RATES] == [1, 2, 4]
    assert [by_rate[r].considered for r in RATES] == [1, 2, 4]
    assert [by_rate[r].hits for r in RATES] == [1, 2, 3]
    assert [by_rate[r].hit_rate for r in RATES] == [1.0, 1.0, 0.75]
    assert [by_rate[r].recall for r in RATES] == [0.5, 0.8, 1.0]
    # more k than candidates: all of them, the hit rate over what there is
    few = at_k(ranked(picks[:2]), truth, rate=8, hours=2)
    assert (few.k, few.considered, few.hit_rate) == (16, 2, 1.0)


def test_recall_is_view_weighted_and_counts_a_clip_once():
    truth = everywhere([clip("big", 1000, 100), clip("mid", 2000, 10), clip("small", 3000, 1)])
    big_only = at_k(ranked([pick(1005, 9), pick(1015, 8)]), truth, rate=4, hours=1)
    assert big_only.truth_hit == 1 and big_only.hit_views == 100
    assert big_only.recall == pytest.approx(100 / 111)
    assert big_only.hits == 2 and big_only.hit_rate == 1.0  # two candidates on one moment
    small_ones = at_k(ranked([pick(2005, 9), pick(3005, 8), pick(9000, 7)]), truth, rate=4, hours=1)
    assert small_ones.recall == pytest.approx(11 / 111)
    assert small_ones.hit_rate == pytest.approx(2 / 3)


def test_delay_check_is_peak_minus_clip_start_of_the_best_ranked_hit():
    truth = everywhere([clip("a", 1000, 50, slack=10), clip("b", 2000, 40, slack=10)])
    picks = [pick(1020, 9), pick(1002, 3), pick(2025, 8)]  # a: best-ranked is at +20
    a = at_k(ranked(picks), truth, rate=8, hours=1)
    assert sorted(a.offsets) == [20.0, 25.0] and a.delay_median_s == 22.5
    early = at_k(ranked([pick(992, 9)]), truth, rate=8, hours=1)  # before the clip, in the slack
    assert early.offsets == (-8.0,)
    assert at_k([], truth, rate=4, hours=1).delay_median_s is None


def test_hits_any_counts_clips_outside_the_truth():
    clips = [clip(f"c{i}", 1000 + 100 * i, 100 - i) for i in range(12)]
    clips.append(clip("tiny", 5000, 1))  # not truth (12 x views 89..100 + 1: below p75)
    truth = everywhere(clips)
    assert "tiny" not in {c.clip_id for c in truth.clips}
    assert "tiny" in {c.clip_id for c in truth.any_clips}
    a = at_k(ranked([pick(5010, 9)]), truth, rate=4, hours=1)
    assert (a.hits, a.hits_any, a.hit_rate, a.hit_rate_any) == (0, 1, 0.0, 1.0)


# --------------------------------------------------------------------------- truth selection


def test_percentile_interpolates_linearly():
    assert percentile([1, 2, 3, 4, 5], 0.75) == 4
    assert percentile(list(range(1, 101)), 0.75) == pytest.approx(75.25)
    assert percentile([7], 0.75) == 7 and percentile([], 0.75) is None


def test_truth_is_top_10_plus_everything_at_or_above_p75():
    hundred = [clip(f"c{v:03d}", 10 * v, v) for v in range(1, 101)]
    truth = select_truth(hundred)
    assert [c.views for c in truth] == list(range(100, 75, -1))  # >= 75.25: 25 clips
    five = [clip(f"f{v}", 10 * v, v) for v in range(1, 6)]
    assert [c.views for c in select_truth(five)] == [5, 4, 3, 2, 1]  # all of the top 10
    twelve = [clip(f"t{v:02d}", 10 * v, v) for v in range(1, 13)]
    assert [c.views for c in select_truth(twelve)] == list(range(12, 2, -1))  # p75 = 9.25
    ties = [clip("b", 0, 5), clip("a", 100, 5), clip("c", 200, 9)]
    assert [c.clip_id for c in select_truth(ties)] == ["c", "a", "b"]  # views, then id
    unspanned = ClipRow("nospan", None, None, 30, 10**6)  # ranked too (then excluded)
    assert select_truth([unspanned, *five])[0].clip_id == "nospan"


def test_partial_coverage_scores_only_clips_inside_the_chat_and_counts_the_rest():
    """Chat only for [0, 3600) of a longer broadcast (like stream 803), with a missing-chat run:
    clips must lie inside one coverage interval in chat time (video + delay)."""
    clips = [
        clip("inside", 1000, 90),
        clip("edge", 3570, 80),  # chat time 3578..3608: runs past the chat's end
        clip("after", 5000, 70),  # chat time after the recording
        clip("hole", 2100, 60),  # inside the missing run
        clip("before_hole", 1950, 55),  # chat 1958..1988: ends before the run at 2000
        ClipRow("nospan", None, None, 30, 1000),
    ]
    coverage = chat_coverage(Interval(at(0), at(3600)), [Interval(at(2000), at(2600))])
    assert coverage == [Interval(at(0), at(2000)), Interval(at(2600), at(3600))]
    assert hours_of(coverage) == pytest.approx(3000 / 3600)
    truth = classify_clips(clips, coverage=coverage, muted=[], delay_s=8)
    assert [c.clip_id for c in truth.clips] == ["inside", "before_hole"]
    assert truth.excluded == {"no_span": 1, "outside_chat": 3}
    assert truth.excluded_ids == {
        "nospan": "no_span",
        **dict.fromkeys(("edge", "after", "hole"), "outside_chat"),
    }
    assert (truth.total, truth.no_span, truth.selected) == (6, 1, 6)
    assert len(truth.with_span) == 5  # the precision set keeps the outside ones
    # the delay shifts the test: with d = 0 "edge" (3570..3600) fits
    no_delay = classify_clips(clips, coverage=coverage, muted=[], delay_s=0)
    assert "edge" in {c.clip_id for c in no_delay.clips}
    assert chat_coverage(None, []) == [] and hours_of([]) == 0
    # a zero run lasting to the end of the data (fed past the chat's end) is the end, no hole
    trailing = chat_coverage(Interval(at(0), at(3600)), [Interval(at(3500), at(3801))])
    assert trailing == [Interval(at(0), at(3600))]


def test_truth_is_picked_before_the_exclusions():
    """The top clips by views stay the truth even where we have no chat (they are counted as
    outside); lower clips are not promoted in their place."""
    twelve = [clip(f"t{v:02d}", 100 * v, v) for v in range(1, 13)]  # truth: views 3..12
    coverage = [Interval(at(0), at(1050))]  # t11 (1100) and t12 (1200) are outside
    truth = classify_clips(twelve, coverage=coverage, muted=[], delay_s=8)
    assert [c.views for c in truth.clips] == list(range(10, 2, -1))
    assert truth.excluded == {"outside_chat": 2} and truth.selected == 10


def test_muted_clips_are_excluded():
    clips = [clip("loud", 1000, 50), clip("music", 1580, 40), clip("next", 1700, 30)]
    muted = [Interval(at(1500), at(1600))]
    truth = classify_clips(clips, coverage=[Interval(at(0), at(7200))], muted=muted, delay_s=8)
    assert [c.clip_id for c in truth.clips] == ["loud", "next"]
    assert truth.excluded == {"muted": 1}
    # a candidate on the muted moment is still a hit (precision), just not scored for recall
    assert "music" in {c.clip_id for c in truth.with_span}
    assert "music" in {c.clip_id for c in truth.any_clips}
    a = at_k(ranked([pick(1590, 9)]), truth, rate=4, hours=2)
    assert (a.hits, a.truth_hit, a.recall) == (1, 0, 0.0)


def test_precision_counts_truth_clips_outside_the_chat_but_recall_does_not():
    clips = [clip("in", 1000, 50), clip("out", 3000, 100)]
    truth = classify_clips(clips, coverage=[Interval(at(0), at(2000))], muted=[], delay_s=8)
    assert [c.clip_id for c in truth.clips] == ["in"] and truth.excluded == {"outside_chat": 1}
    a = at_k(ranked([pick(3010, 9), pick(7000, 8)]), truth, rate=4, hours=0.5)
    assert (a.considered, a.hits, a.hits_any, a.truth_hit, a.recall) == (2, 1, 1, 0, 0.0)


def test_chance_is_the_covered_share_inside_clip_spans_plus_slack():
    """11 clips, 40 s each (30 s + 2 x 5 s slack), 100 s apart: the top 10 are the truth."""
    clips = [clip(f"c{i:02d}", 100 + 100 * i, 100 - i) for i in range(11)]
    truth = classify_clips(clips, coverage=[Interval(at(0), at(3600))], muted=[], delay_s=8)
    assert len(truth.with_span) == 10 and len(truth.any_clips) == 11
    assert truth.chance == pytest.approx(400 / 3600)
    assert truth.chance_any == pytest.approx(440 / 3600)
    # overlapping windows count once; video time = chat time - delay (coverage ends at 3592)
    two = [clip("a", 1000, 10), clip("b", 1020, 9), clip("end", 3580, 8)]
    t2 = classify_clips(two, coverage=[Interval(at(0), at(3600))], muted=[], delay_s=8)
    assert t2.span_s == pytest.approx(60 + (3592 - 3575)) and t2.coverage_s == 3600
    s = score_stream([], t2, hours=1.0, rates=RATES)
    assert s.chance == t2.chance
    assert totals([s, s], RATES).chance == pytest.approx(t2.chance)


def test_moment_recall_counts_overlapping_clips_once():
    clips = [clip("a", 1000, 100), clip("a2", 1020, 50), clip("b", 2000, 10)]
    truth = everywhere(clips)
    assert [[c.clip_id for c in g] for g in truth.moments] == [["a", "a2"], ["b"]]
    m = at_k(ranked([pick(1045, 9)]), truth, rate=4, hours=1)  # in a2's window only
    assert m.recall == pytest.approx(50 / 160)
    assert (m.moments, m.moments_hit, m.moment_views) == (2, 1, 110)
    assert m.moment_recall == pytest.approx(100 / 110)


# --------------------------------------------------------------------------- suppressions, totals


def test_false_suppressions_are_missed_truth_clips_by_the_best_suppressed_inside():
    """Per truth clip none of the k hits: the reason of the best-ranked suppressed candidate
    inside it. `suppressed_in_truth` keeps the raw count."""
    truth = everywhere([clip("a", 1000, 50), clip("b", 2000, 40), clip("c", 3000, 30)])
    picks = [
        pick(5000, 20),  # emitted, the best, on no clip
        pick(1015, 9),  # emitted, hits a
        pick(1010, 4, emitted=False, reason="raid"),
        pick(1020, 2, emitted=False, reason="weak"),
        pick(2010, 6, emitted=False, reason=DEDUPE),  # b: the best suppressed one inside
        pick(2020, 1, emitted=False, reason="weak"),
        pick(3010, 3, emitted=False, reason="weak"),
        pick(3020, 5, emitted=False, reason="raid"),  # c: the best suppressed one inside
        pick(9000, 1, emitted=False, reason="copypasta"),  # no clip there
    ]
    s = score_stream(picks, truth, hours=0.5, rates=RATES)
    assert s.at[2.0].k == 1  # only 5000: a is missed too, its best suppressed is the raid
    assert s.at[2.0].false_suppressions == {"dedupe": 1, "raid": 2}
    assert s.at[4.0].k == 2  # a is hit
    assert s.at[4.0].false_suppressions == {"dedupe": 1, "raid": 1}
    assert s.suppressed_in_truth == {"dedupe": 1, "raid": 2, "weak": 3}
    assert s.emitted == 2 and s.deduped == 1
    assert s.suppressed == {"copypasta": 1, "raid": 2, "weak": 3}
    assert s.candidates_per_hour == 4.0
    assert totals([s, s], RATES).at[4.0].false_suppressions == {"dedupe": 2, "raid": 2}


def test_totals_pool_the_streams():
    t1 = everywhere([clip("a", 1000, 100), clip("b", 2000, 100)])
    t2 = everywhere([clip("c", 1000, 10)])
    s1 = score_stream([pick(1010, 9), pick(5000, 8)], t1, hours=1.0, rates=RATES)
    s2 = score_stream([pick(1010, 9), pick(5000, 8), pick(6000, 7)], t2, hours=0.5, rates=RATES)
    tot = totals([s1, s2], RATES)
    m = tot.at[4.0]
    assert (s1.at[4.0].k, s2.at[4.0].k, m.k) == (4, 2, 6)
    assert (m.considered, m.hits) == (4, 2) and m.hit_rate == 0.5
    assert m.recall == pytest.approx((100 + 10) / 210)  # views pooled
    assert tot.hours == 1.5 and tot.emitted == 5
    assert tot.candidates_per_hour == pytest.approx(5 / 1.5)
    assert tot.streams == 2 and tot.scored == 3 and tot.selected == 3


def test_no_truth_gives_no_recall_and_the_table_shows_dashes():
    s = score_stream([pick(1000, 9)], everywhere([]), hours=2.0, rates=RATES)
    assert s.at[4.0].recall is None and s.at[4.0].hit_rate == 0.0
    lines = stream_table(
        [
            StreamResult(7, "quiet", "live", s, warnings=("odd",)),
            StreamResult(8, "old", "backfill", note="no viewer clips"),
        ],
        RATES,
        4.0,
    )
    head, row, total, warning, note = lines
    assert head.split()[:3] == ["stream", "channel", "hours"]
    assert head.split()[-7:-2] == [
        "recall@2/h",
        "recall@4/h",
        "recall@8/h",
        "moments@4/h",
        "delay_s",
    ]
    assert row.split()[:4] == ["7", "quiet", "2.00", "0"] and row.split()[-4:-1] == ["-", "-", "-"]
    assert total.startswith("total")
    assert warning == "stream 7 (quiet): warning: odd"
    assert note == "stream 8 (old): not scored, no viewer clips"


# --------------------------------------------------------------------------- grid


def test_grid_is_the_cartesian_product_in_order():
    grid = parse_grid(["z_open=4,5", "rho_open = 2, 2.5,3"])
    assert grid == [("z_open", ["4", "5"]), ("rho_open", ["2", "2.5", "3"])]
    combos = grid_params(P, grid)
    assert [v for v, _ in combos] == [
        {"z_open": a, "rho_open": b} for a in (4.0, 5.0) for b in (2.0, 2.5, 3.0)
    ]
    for values, params in combos:
        assert params.z_open == values["z_open"] and params.z_suspect == P.z_suspect
    assert grid_params(P, []) == [({}, P)]


@pytest.mark.parametrize(
    ("specs", "message"),
    [
        (["z_on=3,4"], "not a detector param"),
        (["z_open"], "key=v1,v2"),
        (["z_open="], "key=v1,v2"),
        (["z_open=4", "z_open=5"], "given twice"),
        (["z_open=2.5"], "z_stay <= z_suspect <= z_open"),  # below z_suspect = 3
        (["mention_min_users=lots"], "mention_min_users"),
        (["mention_min_users=2.5"], "mention_min_users"),
        (["restart_emit_s=60,120"], "no effect here"),
        (["restart_replay_s=600"], "no effect here"),
    ],
)
def test_grid_errors(specs, message):
    with pytest.raises(GridError, match=message):
        grid_params(P, parse_grid(specs))


def test_grid_table_sorts_by_recall_at_the_main_rate():
    truth = everywhere([clip("a", 1000, 100), clip("b", 2000, 10)])
    good = totals([score_stream([pick(1010)], truth, hours=1.0, rates=RATES)], RATES)
    bad = totals([score_stream([pick(2010)], truth, hours=1.0, rates=RATES)], RATES)
    none = totals([score_stream([], truth, hours=1.0, rates=RATES)], RATES)
    lines = grid_table(
        [
            ({"z_open": 4.0}, "v-bad", bad),
            ({"z_open": 5.0}, "v-good", good),
            ({"z_open": 6.0}, "v0", none),
        ],
        ["z_open"],
        RATES,
        4.0,
    )
    assert lines[0].split()[0] == "z_open"
    assert [line.split()[0] for line in lines[1:]] == ["5", "4", "6"]
    assert lines[1].split()[-1] == "v-good"


def test_unhashed_params_label_the_run():
    """`dedupe_s`/`final_lag_s` aren't part of `detector_version`: a run with them off their
    defaults says so."""
    assert unhashed(P) == {} and run_label("d1-pabc-l2.x", P) == "d1-pabc-l2.x"
    odd = DetectorParams(dedupe_s=30, final_lag_s=12)
    assert unhashed(odd) == {"dedupe_s": 30, "final_lag_s": 12}
    assert run_label("d1-pabc-l2.x", odd) == "d1-pabc-l2.x+dedupe_s=30+final_lag_s=12"


def test_command_line_without_a_command_is_the_live_service():
    assert parse_args([]).cmd is None
    args = parse_args(["backtest", "--stream", "5", "7", "--grid", "z_open=4,5", "--json"])
    assert (args.cmd, args.stream, args.json) == ("backtest", [5, 7], True)
    assert args.grid == ["z_open=4,5"]
    since = parse_args(["backtest", "--channel", "x", "--since", "2026-11-01"]).since
    assert since == datetime(2026, 11, 1, tzinfo=UTC)
    for bad in (["backtest"], ["backtest", "--stream", "5", "--since", "2026-11-01"], ["nope"]):
        with pytest.raises(SystemExit):
            parse_args(bad)
