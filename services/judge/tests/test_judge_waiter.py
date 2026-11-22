"""The waiter's state transitions (DESIGN §19 step 3), pure: deadlines and `readiness`.

Times in seconds relative to T; the clip waits since T + 40 (after its context end T + 30), so
the soft deadline is T + 280 and the hard one T + 1840.
"""

from datetime import UTC, datetime, timedelta

import pytest

from clipper_core.timeline import Interval
from clipper_core.transcripts import Coverage
from clipper_judge.params import JudgeParams
from clipper_judge.waiter import (
    Deadlines,
    NoMedia,
    Ready,
    Wait,
    core_interval,
    deadlines,
    readiness,
)

T = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)


def at(s: float) -> datetime:
    return T + timedelta(seconds=s)


def iv(a: float, b: float) -> Interval:
    return Interval(at(a), at(b))


def cov(a: float, b: float, *, transcribed=(), gap=(), untranscribed=(), unrecorded=()):
    return Coverage(
        interval=iv(a, b),
        transcribed=tuple(iv(*p) for p in transcribed),
        provisional=(),
        gap=tuple(iv(*p) for p in gap),
        untranscribed=tuple(iv(*p) for p in untranscribed),
        unrecorded=tuple(iv(*p) for p in unrecorded),
    )


D = deadlines(at(40), at(30), soft_wait_s=240, hard_wait_s=1800)
COMPLETE = cov(-90, 30, transcribed=[(-90, -40), (-25, 30)], gap=[(-40, -25)])
TAIL_PENDING = cov(-90, 30, transcribed=[(-90, 15)], untranscribed=[(15, 30)])
CORE_DONE = cov(-11, 9, transcribed=[(-11, 9)])
CORE_HALF = cov(-11, 9, transcribed=[(-11, 0)], untranscribed=[(0, 9)])
CORE_NONE = cov(-11, 9, unrecorded=[(-11, 9)])
CORE_GAP = cov(-11, 9, gap=[(-11, 9)])  # an ad over the whole core: nothing to wait for


def test_deadlines_count_from_the_later_of_waiting_since_and_context_end():
    assert (D.soft, D.hard) == (at(280), at(1840))
    later_context = deadlines(at(40), at(120), soft_wait_s=240, hard_wait_s=1800)
    assert later_context == Deadlines(soft=at(360), hard=at(1920))  # context after T+40: its end


@pytest.mark.parametrize(
    ("context", "core", "now", "expected"),
    [
        # complete coverage: ready at once, no flag, whatever the time
        (COMPLETE, CORE_DONE, 41, Ready()),
        (COMPLETE, CORE_NONE, 5000, Ready()),
        # before the soft deadline: wait for it (even with the core done)
        (TAIL_PENDING, CORE_DONE, 41, Wait(at(280))),
        (TAIL_PENDING, CORE_DONE, 279.9, Wait(at(280))),
        # soft deadline: the core done (or all gap) -> judge with what there is
        (TAIL_PENDING, CORE_DONE, 280, Ready(partial=True)),
        (TAIL_PENDING, CORE_GAP, 280, Ready(partial=True)),
        # soft deadline, the core still pending -> wait for the hard deadline
        (TAIL_PENDING, CORE_HALF, 280, Wait(at(1840))),
        (TAIL_PENDING, CORE_NONE, 1839, Wait(at(1840))),
        # hard deadline: any of the core transcribed -> judge, else no_media
        (TAIL_PENDING, CORE_HALF, 1840, Ready(partial=True)),
        (TAIL_PENDING, CORE_NONE, 1840, NoMedia()),
        (TAIL_PENDING, CORE_GAP, 1840, Ready(partial=True)),  # the soft rule still holds
    ],
)
def test_readiness(context, core, now, expected):
    assert readiness(context, core, at(now), D) == expected


def test_a_waiting_clip_only_moves_forward_in_time():
    """Walking the clock forward over one clip's coverage never goes back to waiting."""
    seen = []
    for now in range(41, 2000, 7):
        state = readiness(TAIL_PENDING, CORE_HALF, at(now), D)
        seen.append(type(state).__name__)
    first_ready = seen.index("Ready")
    assert set(seen[:first_ready]) == {"Wait"} and set(seen[first_ready:]) == {"Ready"}


def test_core_interval_is_clamped_into_the_context():
    p = JudgeParams()
    assert core_interval(at(-6), at(4), iv(-90, 30), p) == iv(-11, 9)
    assert core_interval(at(-6), at(4), iv(-8, 2), p) == iv(-8, 2)
    # a moment outside the context (shouldn't happen) degenerates to a point inside it
    point = core_interval(at(100), at(110), iv(-90, 30), p)
    assert point.start == point.end == at(30)
