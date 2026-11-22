"""The usage guard's decisions (DESIGN §19 "Usage guard"), pure: off by default, caps when set."""

from datetime import UTC, datetime, timedelta

import pytest

from clipper_judge.guard import Allow, Defer, Reject, UsageGuard
from clipper_judge.model import RateLimitReading, UsageWindow
from clipper_judge.reads import UsageCounts
from clipper_judge.settings import JudgeSettings

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
HARD = NOW + timedelta(minutes=30)
FEW = UsageCounts(stream_last_hour=0, today=0)
MANY = UsageCounts(stream_last_hour=50, today=500)


def guard(**kw) -> UsageGuard:
    return UsageGuard(JudgeSettings(_env_file=None, **kw))


def reading(five_hour=0.5, seven_day=0.2, *, status="allowed", age_s=0.0, resets_in_s=3600):
    resets = NOW + timedelta(seconds=resets_in_s)
    return RateLimitReading(
        observed_at=NOW - timedelta(seconds=age_s),
        status=status,
        resets_at=resets,
        five_hour=UsageWindow(five_hour, resets),
        seven_day=UsageWindow(seven_day, NOW + timedelta(days=3)),
    )


def check(g: UsageGuard, *, counts=FEW, score=5.0, median=None, now=NOW):
    return g.check(now=now, hard_deadline=HARD, counts=counts, score=score, hour_median=median)


def test_off_by_default_nothing_blocks():
    g = guard()
    assert not g.enabled
    g.observe(reading(0.99, 0.99, status="rejected"))
    assert check(g, counts=MANY, score=0.0, median=9.0) == Allow()
    assert g.blocked == 0 and g.info()["reading"]["five_hour"]["utilization"] == 0.99


def test_calls_per_day_rejects_and_warns():
    g = guard(calls_per_day=10)
    assert check(g, counts=UsageCounts(0, 9)) == Allow()
    decision = check(g, counts=UsageCounts(0, 10))
    assert isinstance(decision, Reject) and "calls_per_day" in decision.reason
    assert "calls_per_day" in g.info()["warnings"]


def test_calls_per_stream_hour_keeps_candidates_above_the_median():
    g = guard(calls_per_stream_hour=4)
    over = UsageCounts(stream_last_hour=4, today=4)
    assert check(g, counts=over, score=7.0, median=6.0) == Allow()
    assert isinstance(check(g, counts=over, score=6.0, median=6.0), Reject)
    assert check(g, counts=over, score=1.0, median=None) == Allow()  # no median to compare
    assert check(g, counts=UsageCounts(3, 3), score=1.0, median=6.0) == Allow()


def test_usage_caps_defer_until_the_window_resets_but_not_past_the_hard_deadline():
    g = guard(usage_five_hour_max=0.8)
    g.observe(reading(0.79))
    assert check(g) == Allow()
    g.observe(reading(0.85, resets_in_s=600))
    decision = check(g)
    assert decision == Defer(NOW + timedelta(seconds=600), "usage five_hour over the cap")
    g.observe(reading(0.85, resets_in_s=7200))
    assert check(g) == Defer(HARD, "usage five_hour over the cap")
    g.observe(reading(0.85, age_s=-1800, resets_in_s=7200))  # read at the hard deadline
    assert isinstance(check(g, now=HARD), Reject)  # still over the cap there: budget


def test_seven_day_cap_and_a_status_other_than_allowed():
    g = guard(usage_seven_day_max=0.9)
    g.observe(reading(0.99, 0.95))  # the five-hour window has no cap here
    assert check(g) == Defer(HARD, "usage seven_day over the cap")  # resets in 3 days
    g.observe(reading(0.1, 0.1, status="rejected", resets_in_s=120))
    assert check(g) == Defer(NOW + timedelta(seconds=120), "usage status rejected")


def test_missing_or_stale_reading_lets_one_call_through():
    g = guard(usage_five_hour_max=0.8)
    assert check(g) == Allow(refresh=True)  # no reading yet
    assert isinstance(check(g), Defer)  # another call waits for that refresh
    g.observe(reading(0.95))
    assert isinstance(check(g), Defer)  # fresh and over the cap
    g.observe(reading(0.95, age_s=901))
    assert check(g) == Allow(refresh=True)  # stale: one call to refresh it
    g.call_done()  # it ended without a reading
    assert check(g) == Allow(refresh=True)


@pytest.mark.parametrize("cap", ["usage_five_hour_max", "usage_seven_day_max"])
def test_caps_are_validated(cap):
    with pytest.raises(ValueError):
        JudgeSettings(_env_file=None, **{cap: 1.5})


def test_a_refresh_in_flight_at_the_hard_deadline_rejects():
    g = guard(usage_five_hour_max=0.8)
    assert check(g) == Allow(refresh=True)
    decision = check(g, now=HARD)  # another call while the refresh runs, at the hard deadline
    assert isinstance(decision, Reject) and "hard deadline" in decision.reason
    late = check(g, now=HARD + timedelta(minutes=5))
    assert isinstance(late, Reject)  # never a Defer dated in the past
