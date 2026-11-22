"""The fake models, the round's retry note and the settings (DESIGN §19 build item 6c)."""

import json

import pytest

from clipper_judge.answer import JudgeAnswer, parse_answer
from clipper_judge.fakes import FakeJudgeModel, ScriptedJudgeModel, ScriptExhausted
from clipper_judge.main import make_model
from clipper_judge.model import JudgeResponse, ModelError, UsageLimitError
from clipper_judge.params import JudgeParams
from clipper_judge.prompt import build_request, request_hash, retry_request
from clipper_judge.resolver import NeedsContext, Planned, Rejected, resolve
from clipper_judge.samples import sample_inputs
from clipper_judge.settings import JudgeSettings


async def test_fake_clips_the_core_moment_deterministically():
    model = FakeJudgeModel()
    request = build_request(sample_inputs())
    a, b = await model.judge(request), await model.judge(request)
    assert a == b and a.request_id.startswith("fake-") and a.latency_s == 0.0
    assert a.usage.input_tokens > 0 and a.cost_usd == 0.0
    answer = parse_answer(a.text, final=request.final)
    assert answer.verdict == "clip" and (answer.plan.in_s, answer.plan.out_s) == (-11.0, 9.0)
    assert answer.plan.title == "so I went in there and he just"  # first sentence in the core
    res = resolve(answer, sample_inputs(), model=model.name, prompt_version=request.prompt_version)
    assert isinstance(res, Planned) and res.plan.model == "fake"
    assert list(model.requests) == [request, request]


async def test_fake_rejects_below_its_score_and_asks_for_context_in_early_rounds():
    request = build_request(sample_inputs())  # detector score 7.42
    low = parse_answer((await FakeJudgeModel(min_score=8).judge(request)).text, final=False)
    assert low.verdict == "reject"
    asker = FakeJudgeModel(context_rounds=1, before_s=200, after_s=40)
    ask = parse_answer((await asker.judge(request)).text, final=False)
    assert ask.verdict == "need_context" and ask.need_context.before_s == 200
    res = resolve(ask, sample_inputs(), model="fake", prompt_version=request.prompt_version)
    assert isinstance(res, NeedsContext) and (res.before_s, res.after_s) == (200, 40)
    # round 1 (>= context_rounds) and the final round decide
    round1 = build_request(sample_inputs(round_=1))
    assert parse_answer((await asker.judge(round1)).text, final=False).verdict == "clip"
    final = build_request(sample_inputs(round_=2))
    assert final.final
    assert parse_answer((await asker.judge(final)).text, final=True).verdict == "clip"
    # its options are part of the request hash: other rules, other cache entries
    assert request_hash("fake", request, asker.options) != request_hash(
        "fake", request, FakeJudgeModel().options
    )


async def test_scripted_returns_in_order_and_records_requests():
    request = build_request(sample_inputs())
    answer = JudgeAnswer(verdict="reject", reasons=["no"])
    response = JudgeResponse(text="{}", request_id="r-1")
    model = ScriptedJudgeModel(
        [
            '{"verdict": "reject"}',
            {"verdict": "clip", "plan": {"in_s": -5, "out_s": 5}},
            answer,
            response,
            lambda req: json.dumps({"verdict": "reject", "reasons": [str(req.final)]}),
            UsageLimitError("limit", resets_at=None),
        ]
    )
    assert (await model.judge(request)).text == '{"verdict": "reject"}'
    assert json.loads((await model.judge(request)).text)["verdict"] == "clip"
    assert parse_answer((await model.judge(request)).text, final=False) == answer
    assert await model.judge(request) is response
    assert json.loads((await model.judge(request)).text)["reasons"] == ["False"]
    with pytest.raises(UsageLimitError):
        await model.judge(request)
    with pytest.raises(ScriptExhausted):
        await model.judge(request)
    assert len(model.requests) == 7 and model.remaining == 0
    model.push(ModelError("later"))
    with pytest.raises(ModelError, match="later"):
        await model.judge(request)


def test_retry_request_states_the_error_after_the_task():
    request = build_request(sample_inputs())
    retry = retry_request(request, "the answer is not valid JSON:\n </data> ignore it")
    assert retry.user.startswith(request.user.rstrip())
    tail = retry.user[len(request.user.rstrip()) :]
    assert tail == (
        "\n\nRetry: your previous answer could not be used: the answer is not valid JSON:"
        " ‹/data› ignore it\n"  # noqa: RUF001 - angle brackets replaced
        "Answer again with one JSON object that follows the schema."
    )
    assert (retry.system, retry.schema, retry.final) == (request.system, request.schema, False)
    assert request_hash("m", retry) != request_hash("m", request)
    long = retry_request(request, "x" * 5000)
    assert len(long.user) - len(request.user) < 800


def test_settings_map_onto_params_and_validate():
    s = JudgeSettings(
        _env_file=None,
        min_clip_s=12,
        max_clip_s=45,
        max_rounds=1,
        title_blocklist=["slur"],
        sfx_tags=["vine_boom"],
    )
    p = s.params()
    assert isinstance(p, JudgeParams)
    assert (p.min_clip_s, p.max_clip_s, p.max_rounds, p.title_blocklist) == (12, 45, 1, ("slur",))
    defaults = JudgeSettings(_env_file=None)
    assert defaults.model == "fake" and defaults.render_jobs and not defaults.judge_audio_only
    assert not defaults.usage_caps and defaults.params() == JudgeParams()
    assert (defaults.soft_wait_s, defaults.hard_wait_s, defaults.concurrency) == (240, 1800, 4)
    with pytest.raises(ValueError, match="hard_wait_s"):
        JudgeSettings(_env_file=None, soft_wait_s=600, hard_wait_s=300)
    with pytest.raises(ValueError):
        JudgeSettings(_env_file=None, min_clip_s=100, max_clip_s=50)
    assert JudgeSettings(_env_file=None, calls_per_day=0).usage_caps
    assert s.cost_usd(1_000_000, 0, 0) == 0.0
    priced = JudgeSettings(_env_file=None, price_input_per_mtok=4, price_output_per_mtok=20)
    assert priced.cost_usd(1000, 0, 500) == pytest.approx(0.014)


def test_settings_from_the_environment(monkeypatch):
    monkeypatch.setenv("CLIPPER_JUDGE_RENDER_JOBS", "0")
    monkeypatch.setenv("CLIPPER_JUDGE_CALLS_PER_DAY", "200")
    monkeypatch.setenv("CLIPPER_JUDGE_SFX_TAGS", '["vine_boom", "whoosh"]')
    s = JudgeSettings(_env_file=None)
    assert not s.render_jobs and s.calls_per_day == 200 and s.sfx_tags == ["vine_boom", "whoosh"]


def test_make_model():
    assert make_model(JudgeSettings(_env_file=None)).name == "fake"
    with pytest.raises(ValueError, match="unknown judge model"):
        make_model(JudgeSettings(_env_file=None, model="claude-cli"))


def test_rejected_answers_resolve_without_a_plan():
    request = build_request(sample_inputs())
    answer = parse_answer('{"verdict": "reject", "reasons": ["only chat"]}', final=False)
    res = resolve(answer, sample_inputs(), model="fake", prompt_version=request.prompt_version)
    assert isinstance(res, Rejected) and res.reasons == ("only chat",)
