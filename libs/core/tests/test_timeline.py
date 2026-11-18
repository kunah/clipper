from datetime import UTC, datetime, timedelta, timezone

import pytest

from clipper_core.timeline import (
    Interval,
    chat_to_video_time,
    ensure_utc,
    from_unix_ms,
    merge,
    uncovered,
)

T0 = datetime(2026, 11, 18, 20, 0, tzinfo=UTC)


def s(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def iv(a: float, b: float) -> Interval:
    return Interval(s(a), s(b))


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError):
        ensure_utc(datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        Interval(datetime(2026, 1, 1), T0)


def test_other_timezones_are_converted_to_utc():
    cet = timezone(timedelta(hours=1))
    assert ensure_utc(datetime(2026, 11, 18, 21, 0, tzinfo=cet)) == T0
    assert Interval(datetime(2026, 11, 18, 21, 0, tzinfo=cet), s(10)).start.tzinfo == UTC


def test_interval_must_not_end_before_start():
    with pytest.raises(ValueError):
        iv(10, 5)


def test_twitch_timestamp_and_delay():
    assert from_unix_ms("1795550400000") == datetime(2026, 11, 24, 20, 0, tzinfo=UTC)
    assert chat_to_video_time(s(30), timedelta(seconds=12)) == s(18)


def test_half_open_overlap_and_contains():
    a = iv(0, 60)
    assert a.contains(s(0)) and not a.contains(s(60))
    assert a.overlaps(iv(59, 120)) and not a.overlaps(iv(60, 120))
    assert a.intersection(iv(30, 90)) == iv(30, 60)
    assert a.intersection(iv(60, 90)) is None


def test_around_and_expand():
    w = Interval.around(s(100), before=timedelta(seconds=90), after=timedelta(seconds=30))
    assert w == iv(10, 130)
    assert w.expand(before=timedelta(seconds=10)) == iv(0, 130)
    assert w.seconds == 120


def test_merge_joins_overlapping_and_touching():
    assert merge([iv(60, 120), iv(0, 60), iv(200, 210), iv(100, 130)]) == [iv(0, 130), iv(200, 210)]
    assert merge([]) == []


@pytest.mark.parametrize(
    ("covered", "expected"),
    [
        ([], [(0, 300)]),
        ([(0, 300)], []),
        ([(-60, 400)], []),
        ([(0, 60), (60, 120), (180, 300)], [(120, 180)]),
        ([(60, 120)], [(0, 60), (120, 300)]),
        ([(250, 400), (-10, 10)], [(10, 250)]),
    ],
)
def test_uncovered(covered, expected):
    target = iv(0, 300)
    assert uncovered(target, [iv(a, b) for a, b in covered]) == [iv(a, b) for a, b in expected]
