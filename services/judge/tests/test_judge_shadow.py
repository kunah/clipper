"""Shadow runs (DESIGN §19 "Shadow runs", built 2026-11-25): extra model/prompt pairs judge the
same inputs after a committed round; their calls and plans are `shadow`, never rendered, best
effort."""

from dataclasses import replace
from datetime import timedelta

import pytest

from clipper_core.contracts import ClipStatus, JobKind
from clipper_judge import reads
from clipper_judge.adapters import Shadow, make_shadows
from clipper_judge.claude_cli import ClaudeCliModel
from clipper_judge.fakes import FakeJudgeModel, ScriptedJudgeModel
from clipper_judge.model import ModelError, RateLimitReading
from clipper_judge.settings import JudgeSettings, ShadowSpec

pytestmark = pytest.mark.integration


async def shadow_rows(world, clip_id: int):
    plans = await world.fetch(
        "SELECT * FROM clip_plans WHERE clip_id = :c AND shadow ORDER BY version", c=clip_id
    )
    calls = await world.fetch(
        "SELECT * FROM judge_calls WHERE clip_id = :c AND shadow ORDER BY id", c=clip_id
    )
    return plans, calls


async def test_a_shadow_judges_the_same_inputs_and_changes_nothing_else(world):
    await world.transcribe_range(-480, 180)
    cand = await world.candidate()
    main = FakeJudgeModel()
    strict = FakeJudgeModel(name="fake-strict", min_score=100.0)  # rejects everything
    svc = world.service(main, shadows=[Shadow("fake-strict/v1", strict, "v1")])
    assert await world.run_jobs(svc) == 1
    clip = await world.clip(cand)
    assert clip.status == ClipStatus.PLANNED and clip.plan_version == 1  # the main plan
    (main_plan,) = [p for p in await world.plans(clip.id) if not p.shadow]
    (plan,), (call,) = await shadow_rows(world, clip.id)
    assert (plan.version, plan.round, plan.verdict, plan.model) == (2, 0, "reject", "fake-strict")
    assert plan.judge_call_id == call.id and call.model == "fake-strict" and call.shadow
    assert plan.prompt_version == main_plan.prompt_version
    # the same inputs: the same request text as the main call
    (main_req,), (shadow_req,) = list(main.requests), list(strict.requests)
    assert shadow_req.user == main_req.user and shadow_req.system == main_req.system
    # nothing rendered for it, not counted by the usage guard
    (render,) = await world.jobs(JobKind.RENDER)
    assert render.ref_id == main_plan.id
    async with world.sessions() as s:
        counts = await reads.usage_counts(s, clip.stream_id, not_counted=())
    assert counts.today == 1
    assert svc.stats.shadow == {"reject": 1}
    await svc.beat()
    row = await world.one("SELECT info FROM worker_heartbeats WHERE worker_id = 'judge-test'")
    assert row.info["shadows"] == ["fake-strict/v1"] and row.info["shadow"] == {"reject": 1}


async def test_a_requeued_round_job_adds_no_second_shadow_plan(world):
    await world.transcribe_range(-480, 180)
    cand = await world.candidate()
    strict = FakeJudgeModel(name="fake-strict", min_score=100.0)
    svc = world.service(FakeJudgeModel(), shadows=[Shadow("s", strict, "v1")])
    assert await world.run_jobs(svc) == 1
    clip = await world.clip(cand)
    # the same round's shadows again (a requeued job that reused the cached main answer)
    async with world.sessions() as s:
        row = await reads.clip_for_update(s, clip_id=clip.id)
        cand_info = await reads.candidate(s, cand)
        stream_info = await reads.stream(s, clip.stream_id)
        inputs = await reads.load_inputs(
            s, world.store, row, cand_info, stream_info, params=svc.params, now=world.clock()
        )
    await svc.judge._run_shadows(row, inputs)
    plans, calls = await shadow_rows(world, clip.id)
    assert len(plans) == 1 and len(calls) == 1 and len(strict.requests) == 1


async def test_shadow_failures_and_the_guard_never_touch_the_clip(world):
    await world.transcribe_range(-480, 180)
    first = await world.candidate()
    second = await world.candidate(peak=60.0)
    broken = ScriptedJudgeModel([ModelError("boom")], name="broken")
    svc = world.service(FakeJudgeModel(), shadows=[Shadow("broken/v1", broken, "v1")])
    assert await world.run_jobs(svc, limit=1) == 1
    clip = await world.clip(first)
    assert clip.status == ClipStatus.PLANNED
    plans, calls = await shadow_rows(world, clip.id)
    assert plans == [] and len(calls) == 1 and calls[0].error.startswith("provider: boom")
    assert svc.stats.shadow == {"failed": 1}
    (job,) = [j for j in await world.jobs(JobKind.JUDGE) if j.ref_id == first]
    assert job.status == "done"  # the job isn't failed or retried for its shadow

    # the main call's own reading reports extra usage: its answer is kept, the guard latches,
    # and the shadow doesn't start
    class OverageFake(FakeJudgeModel):
        async def judge(self, request):
            r = await super().judge(request)
            reading = RateLimitReading(
                observed_at=world.clock(), overage=True, resets_at=world.at(7200)
            )
            return replace(r, rate_limit=reading)

    svc2 = world.service(OverageFake(), shadows=[Shadow("broken/v1", broken, "v1")])
    assert await world.run_jobs(svc2) == 1
    assert (await world.clip(second)).status == ClipStatus.PLANNED
    assert len(broken.requests) == 1 and svc2.stats.shadow == {"skipped": 1}


def test_shadow_settings_and_adapters(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "CLIPPER_JUDGE_SHADOW",
        '[{"model": "claude-sonnet-5-5"}, {"prompt": "v1", "effort": "low"}]',
    )
    monkeypatch.setenv("CLIPPER_JUDGE_MODEL", "claude-cli")
    monkeypatch.setenv("CLIPPER_JUDGE_CLAUDE_OAUTH_TOKEN", "sk-ant-oat01-abc")
    monkeypatch.setenv("CLIPPER_JUDGE_CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    s = JudgeSettings(_env_file=None)
    assert s.shadow == [
        ShadowSpec(model="claude-sonnet-5-5"),
        ShadowSpec(prompt="v1", effort="low"),
    ]
    assert [sp.label(s) for sp in s.shadow] == ["claude-sonnet-5-5/v1", "claude-opus-5-5@low/v1"]
    shadows = make_shadows(s)
    assert all(isinstance(sh.model, ClaudeCliModel) for sh in shadows)
    assert [sh.model.name for sh in shadows] == ["claude-sonnet-5-5", "claude-opus-5-5"]
    assert (
        shadows[1].model.options["effort"] == "low" and shadows[0].model.options["effort"] is None
    )
    assert shadows[0].model.env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-abc"
    monkeypatch.setenv("CLIPPER_JUDGE_SHADOW", "")  # compose's ${VAR:-}
    assert JudgeSettings(_env_file=None).shadow == []
    with pytest.raises(ValueError, match="repeats the judge"):
        JudgeSettings(_env_file=None, shadow=[{"prompt": "v1"}])  # = the main judge
    with pytest.raises(ValueError, match="repeats the judge or another shadow"):
        JudgeSettings(_env_file=None, shadow=[{"model": "claude-haiku-5-5"}] * 2)
    with pytest.raises(ValueError, match="full model id"):
        JudgeSettings(_env_file=None, shadow=[{"model": "sonnet"}])
    with pytest.raises(ValueError):
        JudgeSettings(_env_file=None, shadow=[{"modle": "x"}])  # unknown field
    with pytest.raises(OSError):
        make_shadows(JudgeSettings(_env_file=None, shadow=[{"prompt": "v99"}]))
    fake = JudgeSettings(_env_file=None, model="fake", shadow=[{"model": "fake-b"}])
    (sh,) = make_shadows(fake)
    assert isinstance(sh.model, FakeJudgeModel) and sh.model.name == "fake-b"


def test_the_guard_blocks_shadows_only_when_a_real_call_would_wait():
    from datetime import UTC, datetime

    from clipper_judge.guard import UsageGuard
    from clipper_judge.model import UsageWindow

    now = datetime(2026, 11, 25, 20, tzinfo=UTC)
    off = UsageGuard(JudgeSettings(_env_file=None))
    assert off.shadow_block(now) is None  # caps unset, no reading: go
    off.overage_until = now + timedelta(minutes=5)
    assert off.shadow_block(now) == "extra usage (overage) reported"
    capped = UsageGuard(JudgeSettings(_env_file=None, usage_five_hour_max=0.5))
    assert capped.shadow_block(now) == "no fresh usage reading"  # never refreshes one itself
    capped.observe(
        RateLimitReading(observed_at=now, five_hour=UsageWindow(utilization=0.6, status="allowed"))
    )
    assert capped.shadow_block(now) == "usage five_hour over the cap"
    capped.observe(
        RateLimitReading(observed_at=now, five_hour=UsageWindow(utilization=0.2, status="allowed"))
    )
    assert capped.shadow_block(now) is None
