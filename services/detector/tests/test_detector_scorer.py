"""Stage 1 scorer on synthetic per-second stats (seeded, fast)."""

import random
import statistics
from datetime import UTC, datetime, timedelta

import pytest

from clipper_detector.params import DetectorParams
from clipper_detector.pipeline import run_seconds
from clipper_detector.scorer import (
    ChannelScorer,
    Episode,
    MissingChat,
    PriorBaseline,
    Second,
    _mad,
)
from clipper_detector.synthetic import bump, poisson_seconds

P = DetectorParams()
T0 = datetime(2026, 11, 20, 12, tzinfo=UTC)
LONG_AGO = T0 - timedelta(hours=1)  # stream start: no warm-up


def at(offset_s: float) -> datetime:
    return T0 + timedelta(seconds=offset_s)


def off(ts: datetime) -> int:
    return int((ts - T0).total_seconds())


def burst_rate(centers, base=5.0, width=15, factor=4.0):
    def rate(i):
        m = 1.0
        for c in centers:
            m *= bump(i, c, width, factor)
        return base * m

    return rate


@pytest.mark.parametrize("rate", [5, 20])
def test_noise_gives_nothing(rate):
    for seed in range(3):
        secs = poisson_seconds(random.Random(seed), T0, 7200, rate)
        episodes, _, missing = run_seconds(secs, P, started_at=LONG_AGO)
        assert episodes == [] and missing == []


@pytest.mark.parametrize("seed", range(8))
def test_burst_gives_one_episode_with_the_peak_within_2s(seed):
    center = 1800
    secs = poisson_seconds(random.Random(seed), T0, 2400, burst_rate([center]))
    episodes, suspects, _ = run_seconds(secs, P, started_at=LONG_AGO)
    assert len(episodes) == 1
    ep = episodes[0]
    assert abs(off(ep.peak) - center) <= 2
    assert ep.onset <= ep.peak <= ep.end and ep.onset <= ep.opened_at < ep.decided_at
    assert ep.end + timedelta(seconds=P.close_after_s) == ep.decided_at
    assert not (ep.forced or ep.truncated or ep.warmup or ep.prior_used)
    assert ep.strongest.z >= P.z_open and ep.strongest.rho >= P.rho_open
    assert ep.strongest.viewer_ratio is None  # no viewer data: no scaling
    # a suspect announced the rise no later than the open
    assert any(s.ts <= ep.opened_at and off(s.ts) > center - 30 for s in suspects)


def test_bursts_closer_than_close_after_merge():
    near = poisson_seconds(random.Random(1), T0, 2400, burst_rate([1800, 1808], width=8))
    far = poisson_seconds(random.Random(1), T0, 2400, burst_rate([1800, 1840], width=8))
    assert len(run_seconds(near, P, started_at=LONG_AGO)[0]) == 1
    assert len(run_seconds(far, P, started_at=LONG_AGO)[0]) == 2


def test_long_spike_is_force_closed():
    secs = poisson_seconds(random.Random(2), T0, 2600, lambda i: 20 if 1800 <= i < 2200 else 5)
    episodes, _, _ = run_seconds(secs, P, started_at=LONG_AGO)
    first = episodes[0]
    assert first.forced and first.duration_s == P.force_close_s
    assert first.decided_at == first.end


def ramp_rate(i: int, *, pre: int = 1260, ramp_s: int = 300, low: float = 0.3, full: float = 5):
    return low if i < pre else low + (full - low) * min(1.0, (i - pre) / ramp_s)


def ramp_viewers(i: int, *, pre: int = 1260, ramp_s: int = 300, full: int = 3000):
    return None if i < pre else max(1, int(full * min(1.0, (i - pre + 1) / ramp_s)))


@pytest.mark.parametrize(
    ("prior", "with_viewers"),
    [
        (PriorBaseline(50, 3000), True),
        (PriorBaseline(50, 3000), False),
        (None, True),
        (None, False),  # a first backfilled stream: no prior, no viewers
    ],
)
@pytest.mark.parametrize("ramp_s", [30, 300])
def test_start_ramp_gives_nothing(prior, with_viewers, ramp_s):
    """21 min of offline chat, then the stream starts and chat/viewers ramp up."""
    started_at = at(1260)
    for seed in range(4):
        secs = poisson_seconds(
            random.Random(seed),
            T0,
            1260 + 3600,
            lambda i: ramp_rate(i, ramp_s=ramp_s),
            viewers=(lambda i: ramp_viewers(i, ramp_s=ramp_s)) if with_viewers else None,
        )
        episodes, _, _ = run_seconds(secs, P, started_at=started_at, prior=prior)
        assert episodes == [], (seed, [(off(e.opened_at), e.strongest.z) for e in episodes])


def test_warmup_needs_a_stronger_spike():
    """A x2.5 plateau (rho ~2.5): enough after the warm-up, not inside it."""

    def rate(i):
        return 12.5 if (300 <= i < 320 or 1800 <= i < 1820 or 2700 <= i < 2720) else 5

    secs = poisson_seconds(random.Random(3), T0, 3000, rate, toggles=[2640])
    episodes, _, _ = run_seconds(secs, P, started_at=T0, prior=PriorBaseline(50))
    # 300 s: stream warm-up (10 min); 2700 s: 60 s after a room-mode toggle (5 min)
    assert [off(e.opened_at) // 100 for e in episodes] == [18]
    assert not episodes[0].warmup


def test_prior_is_a_floor_for_the_first_20_min():
    def rate(i):
        return 12 if 600 <= i < 620 or 1900 <= i < 1920 else 5

    secs = poisson_seconds(random.Random(4), T0, 2400, rate)
    # a prior above the spike: nothing in the first 20 min, the spike after them is found
    episodes = run_seconds(secs, P, started_at=T0 - timedelta(hours=1), prior=PriorBaseline(150))[0]
    assert [off(e.opened_at) // 100 for e in episodes] == [19]
    assert not episodes[0].prior_used and episodes[0].strongest.prior_baseline is None
    # without it both are found
    episodes = run_seconds(secs, P, started_at=T0 - timedelta(hours=1))[0]
    assert [off(e.opened_at) // 100 for e in episodes] == [6, 19]
    # a prior far below the chat never stands in for the own baseline: no false start
    assert run_seconds(secs[:500], P, started_at=T0, prior=PriorBaseline(2))[0] == []


def test_viewer_scaling_on_and_off():
    """Viewers and chat double at once (a raid): normalised away with viewer data only."""

    def rate(i):
        return 5 if i < 1800 else 10

    def viewers(i):
        return 2000 if i < 1800 else 4000

    for seed in range(3):
        with_v = poisson_seconds(random.Random(seed), T0, 2700, rate, viewers=viewers)
        without = poisson_seconds(random.Random(seed), T0, 2700, rate)
        assert run_seconds(with_v, P, started_at=LONG_AGO)[0] == []
        episodes = run_seconds(without, P, started_at=LONG_AGO)[0]
        assert episodes and abs(off(episodes[0].opened_at) - 1800) <= 20
        assert episodes[0].strongest.viewer_ratio is None
    # a burst on top of the raised level is still found, with the ratio recorded
    with_v = poisson_seconds(
        random.Random(9), T0, 2700, lambda i: rate(i) * bump(i, 1900, 15, 4), viewers=viewers
    )
    (ep,) = run_seconds(with_v, P, started_at=LONG_AGO)[0]
    assert ep.strongest.viewer_ratio == pytest.approx(2.0)
    assert ep.strongest.viewers == 4000


def test_viewer_floor():
    """Floor A >= 0.005 * V: a spike too small for the audience doesn't open."""
    rate = burst_rate([1800], base=2.0, factor=4)
    flat = P.model_copy(update={"viewer_ratio_min": 1.0, "viewer_ratio_max": 1.0})
    small = poisson_seconds(random.Random(5), T0, 2400, rate, viewers=lambda i: 1000)
    big = poisson_seconds(random.Random(5), T0, 2400, rate, viewers=lambda i: 20000)
    assert any(abs(off(e.peak) - 1800) <= 2 for e in run_seconds(small, flat)[0])
    assert run_seconds(big, flat)[0] == []  # floor 100 > A ~ 80


def without_rows(secs):
    return [s for s in secs if s.unique_chatters > 0]  # no row = 0


@pytest.mark.parametrize("gap_s", [120, 900, 1500])
def test_missing_chat_run(gap_s):
    def rate(i):
        return 0 if 1800 <= i < 1800 + gap_s else 5

    for seed in range(3):
        secs = without_rows(poisson_seconds(random.Random(seed), T0, 1800 + gap_s + 1200, rate))
        episodes, _, missing = run_seconds(secs, P, started_at=LONG_AGO)
        assert episodes == []
        assert len(missing) == 1
        assert abs(off(missing[0].start) - 1800) <= 2
        assert abs(off(missing[0].end) - 1800 - gap_s) <= 2


def test_missing_chat_keeps_the_baseline():
    """Without the missing-data rule, a 15 min outage drags the median down: false episodes."""
    secs = without_rows(
        poisson_seconds(random.Random(1), T0, 3900, lambda i: 0 if 1800 <= i < 2700 else 5)
    )
    no_rule = P.model_copy(update={"missing_chat_s": 10**6})
    assert run_seconds(secs, no_rule, started_at=LONG_AGO)[0] != []
    assert run_seconds(secs, P, started_at=LONG_AGO)[0] == []


def test_quiet_channel_zero_runs_are_not_missing():
    secs = without_rows(poisson_seconds(random.Random(2), T0, 3000, 0.1))  # B ~ 1
    assert run_seconds(secs, P, started_at=LONG_AGO)[2] == []


def test_replay_from_a_later_start_gives_the_same_episodes():
    centers = [700, 2000, 2600, 3300, 4100, 4145]
    for seed in range(3):
        secs = poisson_seconds(
            random.Random(seed),
            T0,
            5000,
            burst_rate(centers),
            viewers=lambda i: 2000 + i // 10,
            toggles=[3000],
        )
        full = run_seconds(secs, P, started_at=T0)[0]
        start = 1000
        late = run_seconds(secs[start:], P, started_at=T0)[0]
        cut = at(start + P.baseline_window_s + P.activity_window_s + P.onset_lookback_s)
        assert len(full) == len(centers)
        same_full = [e for e in full if e.onset >= cut]
        same_late = [e for e in late if e.onset >= cut]
        assert same_full and same_full == same_late  # equal down to every snapshot float


def test_skipped_seconds_equal_explicit_zeros():
    secs = poisson_seconds(random.Random(6), T0, 2400, burst_rate([1800], base=1.0))
    assert run_seconds(without_rows(secs), P, started_at=LONG_AGO) == run_seconds(
        secs, P, started_at=LONG_AGO
    )


def test_advance_and_flush_close_open_episodes():
    secs = poisson_seconds(random.Random(7), T0, 1810, burst_rate([1800]))
    scorer = ChannelScorer(P, started_at=LONG_AGO)
    events = [e for s in secs for e in scorer.step(s)]
    assert scorer.in_episode and not any(isinstance(e, Episode) for e in events)
    advanced = scorer.advance(at(1900))  # time passes without rows (chat stopped)
    (ep,) = [e for e in advanced if isinstance(e, Episode)]
    assert not ep.truncated and scorer.last_ts == at(1899)

    scorer = ChannelScorer(P, started_at=LONG_AGO)
    for s in secs:
        scorer.step(s)
    (ep,) = scorer.flush()
    assert ep.truncated and ep.decided_at == at(1809)
    assert scorer.flush() == []
    assert ChannelScorer(P).advance(at(10)) == [] and ChannelScorer(P).flush() == []


def test_flush_ends_a_missing_run():
    def rate(i):
        return 0 if i >= 1800 else 5

    secs = without_rows(poisson_seconds(random.Random(8), T0, 1900, rate))
    scorer = ChannelScorer(P, started_at=LONG_AGO)
    for s in secs:
        scorer.step(s)
    scorer.advance(at(1900))
    assert scorer.missing
    (miss,) = scorer.flush()
    assert isinstance(miss, MissingChat) and miss.end == at(1900)


# --------------------------------------------------------------------------- exact series
# Hand-built seconds, no noise: a flat 5 uniques/s gives A = 50 = B, MAD = 0, S = sqrt(50) ~ 7.07,
# so z >= 2 needs A >= 65 (one second of 20 in the 10 s window). Each test pins one parameter:
# changing close_after_s, z_stay, floor_activity, ratio_min_baseline or baseline_exclude_s makes it
# fail (checked by mutating the defaults).


def series(values, start_s: int = 0):
    return [Second(at(start_s + i), v) for i, v in enumerate(values)]


FLAT = [5] * 1300  # > 20 min: the full baseline window


@pytest.mark.parametrize(("gap_s", "episodes"), [(15, 1), (21, 2)])
def test_close_after_10_s_below_z_stay(gap_s, episodes):
    """Two 10 s bursts; z stays below 2 for gap - 9 s between them: 6 s merge, 12 s don't."""
    values = FLAT + [20] * 10 + [5] * gap_s + [20] * 10 + [5] * 100
    found = run_seconds(series(values), P, started_at=LONG_AGO)[0]
    assert len(found) == episodes
    if episodes == 2:
        assert found[0].end == at(1318)  # the last second with a burst second in A's window
        assert found[0].decided_at == at(1318 + P.close_after_s)


def test_episode_stays_open_while_z_is_at_least_z_stay():
    """A tail at 7/s (A = 70, z = 2.83) keeps the episode open; it ends 2 s after the tail,
    when A falls below 64.1 (z < 2). It opened when rho reached 2 (A = 110, 4 s in)."""
    values = FLAT + [20] * 10 + [7] * 30 + [5] * 100
    (ep,) = run_seconds(series(values), P, started_at=LONG_AGO)[0]
    assert ep.opened_at == at(1303) and ep.end == at(1341)


def test_floor_activity():
    """Silent channel (B = 0, S = 2): one second of 8 chatters gives z = 4 and rho = 2.7; only the
    floor A >= max(8, 0.005 V) decides. A floor below 8 changes nothing: z >= 4 with S >= 2
    already needs A >= B + 8."""
    values = [0] * 1300 + [8] + [0] * 60
    (ep,) = run_seconds(series(values), P, started_at=LONG_AGO)[0]
    assert ep.at_open.activity == 8 and ep.at_open.z == pytest.approx(4.0)
    assert ep.at_open.rho == pytest.approx(8 / 3)
    seven = [0] * 1300 + [7] + [0] * 60  # z = 3.5: not even a suspect
    assert run_seconds(series(seven), P, started_at=LONG_AGO) == ([], [], [])
    big_audience = [Second(x.ts, x.unique_chatters, viewers=2000) for x in series(values)]
    assert run_seconds(big_audience, P, started_at=LONG_AGO)[0] == []  # floor 10


def test_rho_divides_by_at_least_3():
    """B = 1 (one chatter every 10 s): rho = A / max(B, 3), not A / B."""
    values = [1 if i % 10 == 0 else 0 for i in range(1300)]
    values += [10] + [1 if i % 10 == 0 else 0 for i in range(1, 60)]  # 10 in place of a 1
    (ep,) = run_seconds(series(values), P, started_at=LONG_AGO)[0]
    assert ep.at_open.baseline == 1.0 and ep.at_open.activity == 10
    assert ep.at_open.rho == pytest.approx(10 / 3)
    assert ep.at_open.z == pytest.approx((10 - 1) / 2)


def test_the_last_minute_is_left_out_of_the_baseline():
    """An 80 s moment with a short (2 min) baseline window: excluding the last 60 s keeps the
    moment out of its own bar until more than half of [t - 120 s, t - 60 s] is the moment (t =
    490), so the episode lasts the whole moment and its tail (end 488; with 45 s excluded: 483,
    30 s: 476, nothing excluded: 461)."""
    short = P.model_copy(update={"baseline_window_s": 120, "min_baseline_values": 60})
    values = [5] * 400 + [20] * 80 + [5] * 100
    (ep,) = run_seconds(series(values), short, started_at=LONG_AGO)[0]
    assert ep.opened_at == at(403) and ep.end == at(488) and not ep.forced
    assert ep.strongest.baseline == 50.0


def test_quiet_offline_chat_is_not_missing_data():
    """A silent stretch before the stream starts is no missing chat (prior 50 would say B >= 5)."""
    rng = random.Random(0)
    values = [0 if 200 <= i < 380 else (1 if rng.random() < 0.5 else 0) for i in range(600)]
    values += [5] * 1800
    secs = without_rows(series(values))
    _, _, missing = run_seconds(secs, P, started_at=at(600), prior=PriorBaseline(50))
    assert missing == []


def test_rows_must_be_in_order_and_utc():
    scorer = ChannelScorer(P)
    scorer.step(Second(at(10), 3))
    with pytest.raises(ValueError, match="in order"):
        scorer.step(Second(at(10), 3))
    with pytest.raises(ValueError, match="naive"):
        scorer.step(Second(datetime(2026, 11, 20, 13), 3))


def test_mad_matches_brute_force():
    rng = random.Random(0)
    for n in (1, 2, 3, 10, 101, 1141):
        values = sorted(rng.randrange(0, 200) for _ in range(n))
        med = statistics.median(values)
        assert _mad(values, med) == statistics.median(abs(v - med) for v in values)
