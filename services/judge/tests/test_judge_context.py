"""Context requests (DESIGN §19): clamped to 10 min before / 2 min after T and the stream, the
context only grows, at most 2 rounds (0-based: rounds 0 and 1 may ask, round 2 must decide)."""

import pytest

from clipper_core.contracts import Verdict
from clipper_judge.answer import ContextAsk, JudgeAnswer
from clipper_judge.inputs import PriorRound
from clipper_judge.params import JudgeParams
from clipper_judge.prompt import build_request
from clipper_judge.resolver import NeedsContext, Repair, Unrepairable, extend_context, resolve
from clipper_judge.samples import T, at, sample_inputs, span

P = JudgeParams()


def ask(before_s: float, after_s: float, why: str = "need the setup", **kw):
    inputs = sample_inputs(**kw)
    a = JudgeAnswer(
        verdict="need_context",
        reasons=["can't tell yet"],
        confidence=0.4,
        need_context=ContextAsk(before_s=before_s, after_s=after_s, why=why),
    )
    return resolve(a, inputs, P, model="fake", prompt_version="v1-test")


def edges(res: NeedsContext) -> tuple[float, float]:
    return (res.context.start - T).total_seconds(), (res.context.end - T).total_seconds()


def test_within_the_caps():
    res = ask(300, 60)
    assert isinstance(res, NeedsContext) and res.verdict is Verdict.NEED_CONTEXT
    assert edges(res) == (-300.0, 60.0)
    assert res.context_request() == {"before_s": 300.0, "after_s": 60.0, "why": "need the setup"}
    assert res.repairs == () and res.reasons == ("can't tell yet",) and res.confidence == 0.4


def test_clamped_to_ten_minutes_before_and_two_after():
    res = ask(900, 500)
    assert edges(res) == (-600.0, 120.0)
    assert Repair.CONTEXT_CLAMP in res.repairs


def test_negative_asks_count_as_zero_and_the_context_never_shrinks():
    res = ask(-50, 60)
    assert edges(res) == (-90.0, 60.0)  # the window's -90 stays
    assert Repair.CONTEXT_CLAMP in res.repairs


def test_clamped_to_the_stream():
    res = ask(600, 120, stream_started_at=at(-200.0), stream_ended_at=at(50.0))
    assert edges(res) == (-200.0, 50.0)
    assert Repair.CONTEXT_CLAMP in res.repairs


def test_nothing_new_to_see_is_unrepairable():
    res = ask(30, 10)  # inside the current context [-90, +30]
    assert isinstance(res, Unrepairable) and "nothing more" in res.error
    res = ask(600, 120, stream_started_at=at(-90.0), stream_ended_at=at(30.0))
    assert isinstance(res, Unrepairable)


def test_a_context_already_past_the_caps_is_kept():
    inputs = sample_inputs(context=span(-700.0, 150.0))
    res = extend_context(ContextAsk(before_s=600, after_s=120), inputs, P)
    assert isinstance(res, Unrepairable)  # nothing to add, and nothing is cut away


@pytest.mark.parametrize("round_, ok", [(0, True), (1, True), (2, False), (3, False)])
def test_at_most_two_rounds(round_, ok):
    res = ask(300, 60, round=round_)
    assert isinstance(res, NeedsContext) is ok
    if not ok:
        assert "last one" in res.error


def test_round_cap_follows_the_params():
    inputs = sample_inputs(round=1)
    res = extend_context(ContextAsk(before_s=300, after_s=60), inputs, JudgeParams(max_rounds=1))
    assert isinstance(res, Unrepairable)


def test_missing_ask_takes_the_caps():
    a = JudgeAnswer(verdict="need_context")
    res = resolve(a, sample_inputs(), P, model="fake", prompt_version="v1-test")
    assert isinstance(res, NeedsContext)
    assert edges(res) == (-600.0, 120.0)
    assert Repair.CONTEXT_DEFAULT in res.repairs


def test_why_is_cleaned():
    res = ask(300, 60, why="see <data> before\nthat " + "x" * 500)
    assert "\n" not in res.why and len(res.why) <= 300
    assert res.why.startswith("see <data> before that")  # stored as said ...
    prior = PriorRound(0, res.before_s, res.after_s, res.why)
    user = build_request(sample_inputs(round_=1, prior_rounds=(prior,))).user
    assert "see ‹data› before that" in user and user.count("<data ") == 3  # noqa: RUF001
    # ... and fenced when a later round shows it to the model


def test_nothing_new_asks_for_the_final_schema():
    res = ask(30, 10)
    assert isinstance(res, Unrepairable) and res.final
    retry = build_request(sample_inputs(), final=True)
    assert retry.final and "need_context" not in str(retry.schema)
    assert "the last one: decide clip or reject" in retry.user
    assert isinstance(ask(300, 60, round=2), Unrepairable) and ask(300, 60, round=2).final
