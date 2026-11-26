"""`clipper-judge eval` (DESIGN §19 build item 6d): fixtures, labels, the summary, the CLI
adapter's usage numbers, and the read-only database mode (Postgres, the 6c world)."""

import csv
import io
import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from clipper_core.db import create_engine, session_factory
from clipper_judge import evaluate
from clipper_judge.evalset import (
    EvalSetError,
    case_to_json,
    inputs_from_json,
    inputs_to_json,
    load_fixtures,
    load_labels,
    write_fixture,
)
from clipper_judge.fakes import FakeJudgeModel, ScriptedJudgeModel
from clipper_judge.model import ModelError, UsageLimitError
from clipper_judge.prompt import build_request
from clipper_judge.samples import CandidateRow, sample_inputs
from clipper_judge.settings import JudgeSettings

CLIP = {
    "reasons": ["he jumps off the map"],
    "verdict": "clip",
    "confidence": 0.8,
    "need_context": None,
    "plan": {"in_s": -11.5, "out_s": 4.0, "title": "He jumped off the map", "hashtags": ["xqc"]},
}


def settings(**kw):
    return JudgeSettings(_env_file=None, **kw)


def fixtures(tmp_path, *ids: str, labels: dict | None = None):
    """One fixture file per id (the sample candidate, its id changed)."""
    d = tmp_path / "fixtures"
    for i, case_id in enumerate(ids):
        inputs = sample_inputs(candidate=CandidateRow(id=100 + i))
        write_fixture(d / f"{case_id}.json", case_to_json(case_id, inputs))
    if labels is not None:
        (tmp_path / "labels.json").write_text(json.dumps({"labels": labels}), encoding="utf-8")
    return d


async def run(args_list, *, model, out=None, **kw):
    out = out or io.StringIO()
    args = evaluate.parse_args(args_list)
    code = await evaluate.run_eval(
        args, settings=kw.pop("judge_settings", settings()), model=model, out=out, **kw
    )
    return code, out.getvalue()


# --------------------------------------------------------------------------- fixtures, labels


@pytest.mark.parametrize("round_", [0, 2])
def test_fixture_inputs_are_lossless_for_the_judge(round_):
    inputs = sample_inputs(round_, flags=("partial_context",))
    again = inputs_from_json(json.loads(json.dumps(inputs_to_json(inputs))))
    assert build_request(again) == build_request(inputs)  # the same bytes, round 2's schema too
    assert again.candidate.window_from == inputs.candidate.window_from
    assert again.bots.is_bot("nightbot") and not again.bots.is_bot("viewer01")


def test_fixture_files_and_errors(tmp_path):
    d = fixtures(tmp_path, "a", "b")
    cases = load_fixtures([d])
    assert [c.id for c in cases] == ["a", "b"] and cases[0].label is None
    assert (
        build_request(cases[0].inputs).user
        == build_request(sample_inputs(candidate=CandidateRow(id=100))).user
    )
    (d / "c.json").write_text(json.dumps([{"format": "other"}]), encoding="utf-8")
    with pytest.raises(EvalSetError, match="not a clipper-judge-eval-case"):
        load_fixtures([d])
    (tmp_path / "nothing").mkdir()
    with pytest.raises(EvalSetError, match="no fixture cases"):
        load_fixtures([tmp_path / "nothing"])


def test_labels_toml_and_json(tmp_path):
    toml = tmp_path / "labels.toml"
    toml.write_text(
        '[labels]\n"803" = "clip"\n"804" = { verdict = "reject", note = "spam" }\n'
        '"805" = { verdict = "clip", in_s = -12.0, out_s = 5.5 }\n',
        encoding="utf-8",
    )
    labels = load_labels(toml)
    assert labels["803"].verdict == "clip" and labels["804"].note == "spam"
    assert (labels["805"].in_s, labels["805"].out_s) == (-12.0, 5.5)
    js = tmp_path / "labels.json"
    js.write_text(json.dumps({"7": {"verdict": "REJECT"}}), encoding="utf-8")
    assert load_labels(js)["7"].verdict == "reject"
    for bad in ({"7": "maybe"}, {"7": {"verdict": "clip", "in_s": 3}}, ["clip"]):
        js.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(EvalSetError):
            load_labels(js)


def test_arguments():
    with pytest.raises(SystemExit) as e:
        evaluate.parse_args(["--fixtures", "x", "--model", "claude-cli"])
    assert e.value.code == 2  # the user's plan: only with --yes
    assert evaluate.parse_args(["--fixtures", "x", "--model", "claude-cli", "--yes"]).yes
    for bad in (
        [],  # no cases chosen
        ["--fixtures", "x", "--effort", "high"],  # claude-cli only
        ["--stream", "1", "--since", "2026-11-01"],
        ["--fixtures", "x", "--save-fixtures", "d"],
        ["--fixtures", "x", "--limit", "0"],
    ):
        with pytest.raises(SystemExit):
            evaluate.parse_args(bad)
    args = evaluate.parse_args(
        ["--labels", "l.toml", "--model", "claude-cli", "--yes", "--effort", "max",
         "--claude-model", "claude-sonnet-5-5", "--prompt", "v1"]
    )  # fmt: skip
    js = evaluate.judge_settings(args, settings(concurrency=3))
    assert (js.model, js.claude_effort, js.claude_model, js.concurrency) == (
        "claude-cli",
        "max",
        "claude-sonnet-5-5",
        3,
    )


# --------------------------------------------------------------------------- runs on fixtures


async def test_eval_on_fixtures_with_the_fake_model(tmp_path):
    d = fixtures(
        tmp_path,
        "a",
        "b",
        "c",
        labels={"a": "clip", "b": "reject", "c": {"verdict": "clip", "in_s": -11, "out_s": 9}},
    )
    report, table = tmp_path / "out" / "eval.json", tmp_path / "out" / "eval.csv"
    code, out = await run(
        ["--fixtures", str(d), "--labels", str(tmp_path / "labels.json"),
         "--report", str(report), "--csv", str(table)],
        model=FakeJudgeModel(),
    )  # fmt: skip
    assert code == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    s = data["summary"]
    assert s["outcomes"] == {"clip": 3}
    a = s["agreement"]
    assert (a["labelled"], a["decided"], a["agree"], a["rate"]) == (3, 3, 2, 0.667)
    assert a["confusion"] == {"clip": {"clip": 2}, "reject": {"clip": 1}}
    assert (a["clip_precision"], a["clip_recall"]) == (0.667, 1.0)
    assert a["cut_iou_cases"] == 1 and 0 < a["cut_iou_mean"] <= 1
    assert s["repairs"]["planned"] == 3 and s["repairs"]["per_clip"] is not None
    assert s["latency_s"]["call_p50"] == 0.0 and s["usage"]["calls"] == 3
    assert s["usage"]["utilization"] == {"five_hour": None, "seven_day": None}
    assert data["meta"]["model"] == "fake" and data["meta"]["source"] == "fixtures"
    case = data["cases"][0]
    assert case["outcome"] == "clip" and case["plan"]["title"] and case["calls"][0]["answer"]
    rows = list(csv.DictReader(table.open(encoding="utf-8")))
    assert [r["id"] for r in rows] == ["a", "b", "c"] and rows[1]["agree"] == "False"
    assert out.startswith("judge eval: 3 case(s), model fake")
    assert "agreement: 2/3 decided of 3 labelled" in out


async def test_retries_failures_and_a_usage_limit_stop(tmp_path):
    d = fixtures(tmp_path, "1", "2", "3", "4", "5")
    model = ScriptedJudgeModel(
        [
            "not json",  # case 1: invalid, then the retry clips
            CLIP,
            "{}",  # case 2: invalid twice -> failed
            "[1, 2]",
            ModelError("provider down", latency_s=1.5),  # case 3: error, the run goes on
            UsageLimitError("limit", resets_at=None),  # case 4: stops the run
        ]
    )
    report = tmp_path / "eval.json"
    code, out = await run(["--fixtures", str(d), "--report", str(report)], model=model)
    assert code == 1 and "STOPPED EARLY: UsageLimitError: limit" in out
    assert "outcomes: clip 1, error 2, failed 1, not_run 1" in out
    cases = json.loads(report.read_text(encoding="utf-8"))["cases"]
    assert [len(c["calls"]) for c in cases] == [2, 2, 1, 1, 0]
    assert cases[0]["calls"][0]["error"].startswith("answer:")  # the retry stated it
    assert cases[1]["error"].startswith("no usable answer after one retry")
    assert cases[2]["calls"][0]["latency_s"] == 1.5 and "provider down" in cases[2]["error"]
    assert json.loads(report.read_text(encoding="utf-8"))["meta"]["stopped"]


async def test_eval_with_the_cli_adapter_reports_usage_and_utilization(tmp_path, fake_claude):
    d = fixtures(tmp_path, "a", "b", "c")
    fake_claude.runs(
        fake_claude.success(CLIP, five_hour=0.10),
        fake_claude.success(CLIP, five_hour=0.13),
        fake_claude.success(CLIP, five_hour=0.17),
    )
    report = tmp_path / "eval.json"
    code, out = await run(
        ["--fixtures", str(d), "--model", "claude-cli", "--yes", "--report", str(report)],
        model=fake_claude.model(),
    )
    assert code == 0, out
    assert len(fake_claude.auth_calls()) == 1  # the auth check ran first
    s = json.loads(report.read_text(encoding="utf-8"))["summary"]
    u = s["usage"]
    assert (u["calls"], u["calls_per_case"], u["input_tokens_per_case"]) == (3, 1.0, 1750)
    assert u["cached_input_tokens_per_case"] == 2900 and u["output_tokens_per_case"] == 820
    assert u["cost_usd_total"] == pytest.approx(0.1836)
    five = u["utilization"]["five_hour"]
    assert (five["first"], five["last"], five["readings"]) == (0.10, 0.17, 3)
    assert five["delta"] == pytest.approx(0.07) and five["per_call"] == pytest.approx(0.035)
    assert u["utilization"]["seven_day"]["delta"] == 0.0
    assert s["latency_s"]["call_p50"] == pytest.approx(12.345)
    assert "utilization five_hour: 0.1 -> 0.17" in out


async def test_eval_with_the_cli_adapter_stops_when_auth_fails(tmp_path, fake_claude):
    d = fixtures(tmp_path, "a")
    fake_claude.scenario({"runs": [{"steps": []}], "auth": fake_claude.auth("api_key")})
    code, _ = await run(
        ["--fixtures", str(d), "--model", "claude-cli", "--yes"], model=fake_claude.model()
    )
    assert code == 1 and fake_claude.calls() == []


# --------------------------------------------------------------------------- the database


@pytest.fixture
async def read_only(settings):
    """Sessions on the test database where every transaction is READ ONLY."""
    engines = []

    def make():
        engine = create_engine(settings, connect_args=evaluate.READ_ONLY)
        engines.append(engine)
        return session_factory(engine)

    yield make
    for engine in engines:
        await engine.dispose()


async def table_counts(world) -> dict[str, int]:
    out = {}
    for t in ("clips", "clip_plans", "judge_calls", "jobs", "media_requests", "candidates"):
        out[t] = (await world.one(f"SELECT count(*) AS n FROM {t}")).n
    return out


@pytest.mark.integration
async def test_eval_on_the_database_is_read_only_and_grows_the_context(world, read_only, tmp_path):
    await world.transcribe_range(-480, 180)
    first = await world.candidate()
    second = await world.candidate(peak=60.0, judge_job=False)
    (tmp_path / "labels.toml").write_text(
        f'[labels]\n"{first}" = "clip"\n"{second}" = "reject"\n', encoding="utf-8"
    )
    before = await table_counts(world)
    jobs_before = await world.fetch("SELECT id, status, run_after FROM jobs ORDER BY id")
    report = tmp_path / "eval.json"
    saved = tmp_path / "saved"
    code, out = await run(
        ["--labels", str(tmp_path / "labels.toml"), "--report", str(report),
         "--save-fixtures", str(saved)],
        model=FakeJudgeModel(context_rounds=1),
        sessions=read_only(),
        store=evaluate._ReadOnlyStore(world.store),
        now=world.clock.now,
    )  # fmt: skip
    assert code == 0, out
    data = json.loads(report.read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in data["cases"]}
    one = by_id[str(first)]
    assert one["outcome"] == "clip" and one["rounds"] == 2  # round 0 asked for more context
    assert [c["round"] for c in one["calls"]] == [0, 1]
    assert data["summary"]["agreement"]["agree"] == 1
    # nothing written: no clips, plans, calls, jobs or media requests
    assert await table_counts(world) == before
    assert await world.fetch("SELECT id, status, run_after FROM jobs ORDER BY id") == jobs_before
    # the saved fixtures replay round 0 with the same request
    cases = load_fixtures([saved])
    assert sorted(c.id for c in cases) == sorted([str(first), str(second)])
    assert {c.id: c.label.verdict for c in cases} == {str(first): "clip", str(second): "reject"}
    code, out2 = await run(["--fixtures", str(saved)], model=FakeJudgeModel(context_rounds=1))
    assert "need_context 2" in out2  # fixtures have no more media


@pytest.mark.integration
async def test_eval_waits_for_nothing_not_ready_and_partial(world, read_only):
    await world.transcribe_range(-120, 0)  # [0, 60) of the context is not transcribed
    cand = await world.candidate()
    code, out = await run(
        ["--candidate", str(cand)],
        model=FakeJudgeModel(),
        sessions=read_only(),
        store=world.store,
        now=world.clock.now,
    )
    assert code == 0 and "outcomes: not_ready 1" in out
    code, out = await run(
        ["--candidate", str(cand), "--partial"],
        model=FakeJudgeModel(),
        sessions=read_only(),
        store=world.store,
        now=world.clock.now,
    )
    assert code == 0 and "outcomes: clip 1" in out
    with pytest.raises(evaluate.UsageError, match="unknown candidate"):
        await run(["--candidate", "99999"], model=FakeJudgeModel(), sessions=read_only(),
                  store=world.store)  # fmt: skip


@pytest.mark.integration
async def test_audio_only_is_rejected_without_a_call(make_world, read_only):
    world = await make_world(recording_mode="audio_only", video=False)
    await world.transcribe_range(-480, 180)
    cand = await world.candidate()
    model = FakeJudgeModel()
    code, out = await run(
        ["--candidate", str(cand)], model=model, sessions=read_only(), store=world.store,
        now=world.clock.now,
    )  # fmt: skip
    assert code == 0 and "outcomes: rejected:no_video 1" in out and not model.requests


@pytest.mark.integration
async def test_amain_uses_a_read_only_engine(world, settings, monkeypatch, capsys):
    await world.transcribe_range(-480, 180)
    cand = await world.candidate()
    engines = []
    real = evaluate.create_engine

    def engine(s, **kw):
        engines.append(kw)
        return real(s, **kw)

    monkeypatch.setattr(evaluate, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(evaluate, "Settings", lambda **kwargs: settings)  # the test database
    monkeypatch.setattr(evaluate, "base_settings", lambda: JudgeSettings(_env_file=None))
    monkeypatch.setattr(evaluate, "ObjectStore", lambda s: world.store)
    monkeypatch.setattr(evaluate, "create_engine", engine)
    assert await evaluate.amain(evaluate.parse_args(["--candidate", str(cand)])) == 0
    assert engines == [{"connect_args": evaluate.READ_ONLY}]
    assert "outcomes: clip 1" in capsys.readouterr().out
    assert await evaluate.amain(evaluate.parse_args(["--candidate", "424242"])) == 2
    assert "unknown candidate id(s): 424242" in capsys.readouterr().err
    # the read-only connection refuses writes
    ro = real(settings, connect_args=evaluate.READ_ONLY)
    try:
        async with session_factory(ro)() as s:
            with pytest.raises(DBAPIError, match="read-only"):
                await s.execute(text("DELETE FROM clips"))
    finally:
        await ro.dispose()


def test_percentile_and_utilization_helpers():
    assert evaluate.percentile([], 0.5) is None
    assert evaluate.percentile([3.0, 1.0, 2.0], 0.5) == 2.0
    assert evaluate.percentile([1.0, 2.0], 0.95) == pytest.approx(1.95)
