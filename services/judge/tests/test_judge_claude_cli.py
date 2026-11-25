"""`ClaudeCliModel` against a fake `claude` executable (DESIGN §19 build item 6d "Done").

The fake (`fake_claude.py`, the `fake_claude` fixture) replays stream-json lines per scenario and
records its argv, environment, cwd and stdin; the real CLI is never run. The last test runs one
judge round through the 6c service path (Postgres) with the adapter.
"""

import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clipper_core.contracts import ClipStatus, JobKind, JobStatus
from clipper_judge import claude_cli
from clipper_judge.adapters import check_model, make_model, settings_for_model
from clipper_judge.claude_cli import (
    ALLOWED_CLAUDE_CODE,
    MAX_TURNS,
    ClaudeAuthError,
    ClaudeCliModel,
    child_env,
    find_executable,
    limit_resets_at,
    parse_reading,
    parse_time,
)
from clipper_judge.model import (
    CallTimeoutError,
    ModelError,
    ModelUnavailableError,
    PermanentModelError,
    ReportsStatus,
    UsageLimitError,
)
from clipper_judge.prompt import build_request, request_hash
from clipper_judge.samples import sample_inputs
from clipper_judge.settings import CLAUDE_MODEL, JudgeSettings, check_config_dir

RESETS = datetime(2026, 11, 22, 18, 0, tzinfo=UTC)
WEEK_RESETS = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
CLIP = {
    "reasons": ["he jumps off the map"],
    "verdict": "clip",
    "confidence": 0.8,
    "need_context": None,
    "plan": {"in_s": -11.5, "out_s": 4.0, "title": "He jumped off the map", "hashtags": ["xqc"]},
}


def request(final: bool = False):
    return build_request(sample_inputs(2 if final else 0))


# --------------------------------------------------------------------------- success


async def test_success_maps_structured_output_usage_and_reading(fake_claude):
    model = fake_claude.model(clock=lambda: datetime(2026, 11, 22, 15, tzinfo=UTC))
    req = request()
    response = await model.judge(req)
    assert json.loads(response.text) == CLIP
    # cached = cache reads; input = the rest of the prompt (cache writes included)
    u = response.usage
    assert (u.input_tokens, u.cached_input_tokens, u.output_tokens) == (1750, 2900, 820)
    assert response.latency_s == pytest.approx(12.345)
    assert response.request_id == "5b1c0e9e-4c1f-4c55-9d7e-1f3f0f6a9a01"  # session_id
    assert response.cost_usd == pytest.approx(0.0612)  # list-price estimate
    r = response.rate_limit
    assert r is not None and r.status == "allowed" and r.resets_at == RESETS
    assert r.observed_at == datetime(2026, 11, 22, 15, tzinfo=UTC)
    assert (r.five_hour.utilization, r.five_hour.resets_at, r.five_hour.status) == (
        0.12,
        RESETS,
        "allowed",
    )
    assert (r.seven_day.utilization, r.seven_day.resets_at) == (0.31, WEEK_RESETS)
    cli = response.extras["claude_cli"]
    assert cli["version"] == "2.1.330" and cli["exit_code"] == 0
    assert cli["init"]["apiKeySource"] == "none" and cli["result"]["num_turns"] == 2
    assert cli["rate_limit_info"]["unifiedWindows"]["five_hour"]["utilization"] == 0.12
    assert cli["events"] == {  # the order seen on 2026-11-25 (one model response)
        "system/init": 1,
        "system/thinking_tokens": 1,
        "assistant": 2,
        "user": 1,
        "rate_limit_event": 1,
        "result/success": 1,
    }
    assert cli["order"] == [
        ["system/init", 1],
        ["system/thinking_tokens", 1],
        ["assistant", 2],
        ["user", 1],
        ["rate_limit_event", 1],
        ["result/success", 1],
    ]
    assert cli["first_event"] == "system/init" and r.overage is False
    assert "<request schema>" in cli["argv"]  # the schema isn't stored twice
    json.dumps(response.extras)  # JSON values only (the S3 record)
    assert model.cli_version == "2.1.330"
    assert model.credential_expires_at == datetime.fromtimestamp(1826000000, tz=UTC)
    # name and hashed options: the pinned model id, effort and max turns; not the CLI version
    assert model.name == CLAUDE_MODEL
    assert model.options == {"adapter": "claude-cli", "effort": None, "max_turns": MAX_TURNS}
    h = request_hash(model.name, req, model.options)
    assert h != request_hash(model.name, req, {**model.options, "effort": "high"})


async def test_argv_stdin_cwd_and_system_prompt_file(fake_claude):
    model = fake_claude.model(effort="high", max_budget_usd=0.5)
    req = request()
    await model.judge(req)
    (call,) = fake_claude.calls()
    argv = call["argv"]
    assert argv[:15] == [
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "claude-opus-5-5",
        "--tools",
        "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--max-turns",
        "3",
        "--safe-mode",  # no CLAUDE.md, skills, installed plugins, hooks, MCP servers
        "--disable-slash-commands",
        "--restricted",  # no user/project/local settings files
    ]
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--max-budget-usd") + 1] == "0.5"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == req.schema
    # the schema keeps its property order (reasons before the verdict)
    assert list(json.loads(argv[argv.index("--json-schema") + 1])["properties"]) == list(
        req.schema["properties"]
    )
    for flag in ("--bare", "--fallback-model", "--system-prompt", "--setting-sources"):
        assert flag not in argv
    assert call["system_prompt"] == req.system  # byte for byte, LF line endings
    assert call["stdin"] == req.user  # the user content on stdin (closed: the fake read EOF)
    assert call["cwd_entries"] == []  # an empty working dir
    assert not Path(call["cwd"]).parent.exists()  # removed after the call


async def test_inline_system_prompt_and_no_optional_flags(fake_claude):
    model = fake_claude.model(system_prompt_file=False, max_budget_usd=None)
    req = request(final=True)
    await model.judge(req)
    (call,) = fake_claude.calls()
    argv = call["argv"]
    assert argv[argv.index("--system-prompt") + 1] == req.system
    for flag in ("--system-prompt-file", "--effort", "--max-budget-usd"):
        assert flag not in argv
    assert json.loads(argv[argv.index("--json-schema") + 1]) == req.schema  # the final schema


async def test_child_environment_is_built_from_scratch(fake_claude, monkeypatch):
    parent = {
        "ANTHROPIC_API_KEY": "sk-ant-api-PARENT",
        "ANTHROPIC_AUTH_TOKEN": "parent-token",
        "ANTHROPIC_BASE_URL": "https://proxy.example",
        "ANTHROPIC_MODEL": "claude-haiku-5-5",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_RETRY_WATCHDOG": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "CLAUDE_CODE_OAUTH_TOKEN": "parent-oauth",
        "CLAUDECODE": "1",
        "CLAUDE_CONFIG_DIR": "/home/user/.claude",
        "CLIPPER_DATABASE_URL": "postgresql://live",
        "CLIPPER_JUDGE_CLAUDE_OAUTH_TOKEN": "x",
        "SOME_SECRET": "s3cret",
    }
    for k, v in parent.items():
        monkeypatch.setenv(k, v)
    model = fake_claude.model(max_retries=2)
    await model.judge(request())
    (call,) = fake_claude.calls()
    env = {k: v for k, v in call["env"].items() if not k.startswith("__PYVENV")}  # py launcher
    assert not [k for k in env if k.upper().startswith("ANTHROPIC_")]
    assert {k for k in env if k.upper().startswith("CLAUDE_CODE_")} <= ALLOWED_CLAUDE_CODE
    for name in ("CLAUDECODE", "CLAUDE_CODE_RETRY_WATCHDOG", "CLAUDE_CODE_USE_BEDROCK"):
        assert name not in {k.upper() for k in env}
    assert not [k for k in env if k.upper().startswith("CLIPPER_")]
    assert "s3cret" not in json.dumps(env) and "SOME_SECRET" not in {k.upper() for k in env}
    home = str(fake_claude.config_dir.resolve())
    assert env["HOME"] == home and env["CLAUDE_CONFIG_DIR"] == os.path.join(home, ".claude")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-test-token"  # ours, not the parent's
    assert env["CLAUDE_CODE_MAX_RETRIES"] == "2"
    assert env["DISABLE_AUTOUPDATER"] == "1" and env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert env["PATH"] == os.environ["PATH"]
    if sys.platform == "win32":
        assert env["USERPROFILE"] == home and "SYSTEMROOT" in {k.upper() for k in env}


def test_child_env_without_token_and_allowlist(tmp_path):
    parent = {"PATH": "/usr/bin", "LANG": "C.UTF-8", "HTTPS_PROXY": "http://p:3128", "X": "y"}
    env = child_env(tmp_path / "cfg", oauth_token=None, max_retries=0, parent=parent)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and env["CLAUDE_CODE_MAX_RETRIES"] == "0"
    assert env["PATH"] == "/usr/bin" and env["LANG"] == "C.UTF-8" and "X" not in env
    assert env["HTTPS_PROXY"] == "http://p:3128"  # network config is passed on


# --------------------------------------------------------------------------- answers and errors


async def test_no_structured_output_returns_the_result_text(fake_claude):
    fake_claude.runs(
        [
            {"line": fake_claude.init()},
            {"line": fake_claude.result(None, text="I can't judge this clip.")},
        ]
    )
    response = await fake_claude.model().judge(request())
    assert response.text == "I can't judge this clip."  # an invalid answer: the one retry
    assert response.rate_limit is None and response.usage.output_tokens == 820


async def test_is_error_raises_model_error_with_the_stderr_tail(fake_claude):
    fake_claude.runs(
        {
            "steps": [
                {"line": fake_claude.init()},
                {"stderr": "API Error: 500 internal server error"},
                {
                    "line": fake_claude.result(
                        subtype="error_during_execution", is_error=True, text="API Error: 500"
                    )
                },
            ],
            "exit": 1,
        }
    )
    with pytest.raises(ModelError) as info:
        await fake_claude.model().judge(request())
    exc = info.value
    assert type(exc) is ModelError  # retried by the queue, not a usage limit
    assert "error_during_execution" in str(exc) and "internal server error" in str(exc)
    assert exc.request_id.startswith("5b1c0e9e") and exc.latency_s == pytest.approx(12.345)
    assert exc.extras["claude_cli"]["exit_code"] == 1
    assert exc.usage.input_tokens == 1750 and exc.cost_usd == pytest.approx(0.0612)  # paid


@pytest.mark.parametrize("subtype", ["error_max_turns", "error_max_structured_output_retries"])
async def test_runs_that_ended_without_an_answer_are_invalid_answers(fake_claude, subtype):
    fake_claude.runs(
        [
            {"line": fake_claude.init()},
            {"line": fake_claude.result(subtype=subtype, is_error=True, text="no answer")},
        ]
    )
    response = await fake_claude.model().judge(request())  # the resolver's one retry, no queue
    assert response.text == "no answer" and response.usage.output_tokens == 820
    assert response.cost_usd == pytest.approx(0.0612)  # paid


async def test_a_structured_output_is_used_whatever_the_subtype(fake_claude):
    fake_claude.runs(
        [
            {"line": fake_claude.init()},
            {
                "line": fake_claude.result(
                    CLIP, subtype="error_max_turns", is_error=True, text="ran out of turns"
                )
            },
        ]
    )
    response = await fake_claude.model().judge(request())
    assert json.loads(response.text) == CLIP


async def test_a_budget_stop_is_permanent_and_records_the_paid_usage(fake_claude):
    fake_claude.runs(
        [
            {"line": fake_claude.init()},
            {"line": fake_claude.result(subtype="error_max_budget_usd", is_error=True)},
        ]
    )
    with pytest.raises(PermanentModelError, match="budget cap") as info:
        await fake_claude.model().judge(request())
    exc = info.value
    assert exc.usage is not None and exc.usage.output_tokens == 820
    assert exc.cost_usd == pytest.approx(0.0612)


async def test_exit_without_a_result_is_a_model_error(fake_claude):
    fake_claude.runs(
        {
            "steps": [{"raw": "Error: something broke"}, {"stderr": "fatal: config unreadable"}],
            "exit": 2,
        }
    )
    with pytest.raises(ModelError, match=r"without a result \(exit 2\).*config unreadable") as info:
        await fake_claude.model().judge(request())
    assert info.value.extras["claude_cli"]["non_json_stdout"] == ["Error: something broke"]


async def test_success_with_a_nonzero_exit_keeps_the_answer(fake_claude):
    fake_claude.runs({"steps": fake_claude.success(), "exit": 1})
    response = await fake_claude.model().judge(request())
    assert json.loads(response.text) == CLIP
    assert response.extras["claude_cli"]["exit_code"] == 1


async def test_429_is_a_usage_limit_with_the_reset_time(fake_claude):
    fake_claude.runs(
        [
            {"line": fake_claude.init()},
            {"line": fake_claude.rate(1.0, 0.6, status="rejected")},
            {
                "line": fake_claude.result(
                    subtype="error_during_execution",
                    is_error=True,
                    text="Claude AI usage limit reached",
                    api_error_status=429,
                )
            },
        ]
    )
    with pytest.raises(UsageLimitError) as info:
        await fake_claude.model().judge(request())
    exc = info.value
    assert exc.resets_at == RESETS  # the refusing window's reset
    assert exc.rate_limit is not None and exc.rate_limit.status == "rejected"


async def test_429_while_the_windows_allow_waits_the_default(fake_claude):
    fake_claude.runs(
        [
            {"line": fake_claude.init()},
            {"line": fake_claude.rate(0.4)},
            {"line": fake_claude.result(is_error=True, subtype="error", api_error_status=429)},
        ]
    )
    with pytest.raises(UsageLimitError) as info:
        await fake_claude.model().judge(request())
    assert info.value.resets_at is None  # the guard's limit_retry_s


async def test_rate_limit_retries_or_a_limit_message_are_usage_limits(fake_claude):
    retry = {
        "type": "system",
        "subtype": "api_retry",
        "attempt": 1,
        "max_retries": 2,
        "retry_delay_ms": 500,
        "error_status": 429,
        "error": "rate_limit",
    }
    fake_claude.runs(
        [
            {"line": fake_claude.init()},
            {"line": retry},
            {"line": fake_claude.result(is_error=True, subtype="error_during_execution")},
        ],
        [
            {"line": fake_claude.init()},
            {
                "line": fake_claude.result(
                    is_error=True, subtype="error", text="5-hour limit reached"
                )
            },
        ],
    )
    model = fake_claude.model()
    with pytest.raises(UsageLimitError) as info:
        await model.judge(request())
    assert info.value.extras["claude_cli"]["api_retries"][0]["error"] == "rate_limit"
    with pytest.raises(UsageLimitError):
        await model.judge(request())


async def test_timeout_kills_the_whole_process_tree(fake_claude):
    fake_claude.runs({"steps": [{"line": fake_claude.init()}, {"sleep": 60}], "heartbeat": True})
    # 6 s: the fake prints init only once it and its child run (a cold start can take ~3 s)
    model = fake_claude.model(timeout_s=6.0)
    started = time.monotonic()
    with pytest.raises(CallTimeoutError, match="timed out after 6 s") as info:
        await model.judge(request())
    assert time.monotonic() - started < 20
    assert info.value.extras["claude_cli"]["init"]["apiKeySource"] == "none"
    await assert_tree_dead(fake_claude, 0)


async def assert_tree_dead(fake_claude, n: int) -> None:
    beats = [fake_claude.record_dir / f"fake-{n}.beat", fake_claude.record_dir / f"child-{n}.beat"]
    for _ in range(100):  # the child may still be starting
        if all(b.exists() for b in beats):
            break
        await asyncio.sleep(0.05)
    assert all(b.exists() for b in beats)  # both were running
    await asyncio.sleep(0.3)
    before = [b.read_text() for b in beats]
    await asyncio.sleep(0.6)  # 12 beats if anything still ran
    assert [b.read_text() for b in beats] == before


async def test_a_process_that_stays_after_its_result_is_killed(fake_claude, monkeypatch):
    monkeypatch.setattr(claude_cli, "EXIT_GRACE_S", 1.0)
    fake_claude.runs({"steps": [*fake_claude.success(), {"sleep": 60}], "heartbeat": True})
    response = await fake_claude.model().judge(request())
    assert json.loads(response.text) == CLIP  # the answer is kept
    await assert_tree_dead(fake_claude, 0)


async def test_a_cancelled_call_kills_the_process_tree(fake_claude):
    fake_claude.runs({"steps": [{"line": fake_claude.init()}, {"sleep": 60}], "heartbeat": True})
    task = asyncio.create_task(fake_claude.model().judge(request()))
    for _ in range(200):
        if (fake_claude.record_dir / "child-0.beat").exists():
            break
        await asyncio.sleep(0.05)
    task.cancel()  # the service stopping
    with pytest.raises(asyncio.CancelledError):
        await task
    await assert_tree_dead(fake_claude, 0)


async def test_wrong_api_key_source_switches_the_adapter_off(fake_claude):
    # the whole run is printed at once: the check doesn't depend on the fake pausing after init
    fake_claude.runs(
        {
            "steps": [
                {"line": fake_claude.init(api_key_source="ANTHROPIC_API_KEY")},
                *fake_claude.success()[1:],
                {"sleep": 30},  # only to show the tree is killed
            ],
            "heartbeat": True,
        }
    )
    model = fake_claude.model()
    with pytest.raises(ModelUnavailableError, match="apiKeySource is 'ANTHROPIC_API_KEY'") as info:
        await model.judge(request())
    assert info.value.called and info.value.extras["claude_cli"]["result"] is None  # no answer
    await assert_tree_dead(fake_claude, 0)
    # switched off: the next call starts nothing
    with pytest.raises(ModelUnavailableError, match="switched off") as again:
        await model.judge(request())
    assert not again.value.called and len(fake_claude.calls()) == 1
    assert "apiKeySource" in model.status()["warnings"]["model_off"]
    assert model.status()["off"] == model.off


@pytest.mark.parametrize(
    "first",
    [
        lambda f: f.result(CLIP),
        lambda f: {"type": "system", "subtype": "api_retry", "attempt": 1, "error": "unknown"},
        lambda f: f.rate(),
        lambda f: f.assistant(CLIP),
        lambda f: {"type": "system", "subtype": "hook_started", "hook_name": "x"},
    ],
    ids=["result", "api_retry", "rate_limit_event", "assistant", "hook"],
)
async def test_a_first_event_other_than_init_switches_off(fake_claude, first):
    fake_claude.runs([{"line": first(fake_claude)}, *fake_claude.success()])
    model = fake_claude.model()
    with pytest.raises(ModelUnavailableError, match="first event was") as info:
        await model.judge(request())
    assert info.value.called and model.off is not None


async def test_a_missing_executable_is_permanent(fake_claude, tmp_path):
    model = fake_claude.model(command=[str(tmp_path / "no-such-claude")])
    with pytest.raises(PermanentModelError, match="not found"):
        await model.judge(request())
    with pytest.raises(ClaudeAuthError, match="not found"):
        await model.check_auth()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows: .cmd shims")
async def test_a_cmd_shim_is_refused(fake_claude, tmp_path):
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off\n")
    with pytest.raises(PermanentModelError, match=r"native claude\.exe"):
        await fake_claude.model(command=[str(shim)]).judge(request())


# --------------------------------------------------------------------------- auth and status


async def test_auth_status_accepts_oauth_token_and_claude_ai(fake_claude):
    now = datetime(2026, 11, 22, 15, tzinfo=UTC)
    model = fake_claude.model(clock=lambda: now)
    status = await model.check_auth()
    assert status.auth_method == "oauth_token" and status.logged_in is True
    (call,) = fake_claude.auth_calls()
    assert call["argv"] == ["auth", "status"]
    assert call["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-test-token"
    assert not [k for k in call["env"] if k.upper().startswith("ANTHROPIC_")]
    assert model.warnings() == {}
    # natively: a login stored in the judge's config dir, expiring in 3 days -> a warning
    soon = now + timedelta(days=3)
    fake_claude.scenario(
        {
            "runs": [{"steps": fake_claude.success()}],
            "auth": fake_claude.auth("claude.ai", login_expires_at=soon.isoformat()),
        }
    )
    model = fake_claude.model(clock=lambda: now, oauth_token=None)
    status = await model.check_auth()
    assert status.auth_method == "claude.ai" and status.login_expires_at == soon
    assert model.warnings() == {"credential_expiring": soon.isoformat()}
    info = model.status()
    assert info["auth_method"] == "claude.ai" and info["warnings"] and info["calls"] == 0
    assert isinstance(model, ReportsStatus)
    assert await check_model(model)


@pytest.mark.parametrize("method", ["api_key", "api_key_helper", "third_party", "none"])
async def test_auth_status_refuses_api_keys_and_no_login(fake_claude, method):
    fake_claude.scenario(
        {
            "runs": [{"steps": fake_claude.success()}],
            "auth": fake_claude.auth(method),
            "auth_exit": 1 if method == "none" else 0,
        }
    )
    model = fake_claude.model()
    with pytest.raises(ClaudeAuthError, match=f"authMethod '{method}'"):
        await model.check_auth()
    assert not await check_model(model)  # the service exits without judging
    assert fake_claude.calls() == []  # no model request


async def test_auth_status_without_json_is_refused(fake_claude):
    fake_claude.scenario({"runs": [{"steps": []}], "auth": "not json"})
    with pytest.raises(ClaudeAuthError, match="no JSON"):
        await fake_claude.model().check_auth()


async def test_credential_expiry_from_results_warns(fake_claude):
    now = datetime(2026, 11, 22, 15, tzinfo=UTC)
    expires = now + timedelta(days=10)
    steps = fake_claude.success()
    steps[-1]["line"]["credential_expires_at"] = int(expires.timestamp() * 1000)
    fake_claude.runs(steps)
    model = fake_claude.model(clock=lambda: now)
    await model.judge(request())
    assert model.expires_at() == expires
    assert model.status()["warnings"] == {"credential_expiring": expires.isoformat()}


async def test_a_setup_token_expiry_comes_from_the_settings(fake_claude, monkeypatch, tmp_path):
    # seen 2026-11-25: with a setup token the CLI reports no expiry (null in the result and in
    # `claude auth status`), so the configured date is used; a value the CLI reports wins
    now = datetime(2027, 11, 15, 12, tzinfo=UTC)
    steps = fake_claude.success()
    steps[-1]["line"]["credential_expires_at"] = None
    fake_claude.runs(steps)
    model = fake_claude.model(
        clock=lambda: now, token_expires_at=datetime(2027, 11, 25, tzinfo=UTC)
    )
    await model.judge(request())
    assert model.expires_at() == datetime(2027, 11, 25, tzinfo=UTC)
    assert model.status()["warnings"] == {"credential_expiring": "2027-11-25T00:00:00+00:00"}
    assert model.status()["token_expires_at"] == "2027-11-25T00:00:00+00:00"
    far = fake_claude.model(clock=lambda: now, token_expires_at=datetime(2028, 1, 1, tzinfo=UTC))
    assert far.status()["warnings"] == {}
    # without a token the date means nothing (a stored login has its own expiry)
    assert fake_claude.model(oauth_token=None, token_expires_at=now).expires_at() is None
    # settings: a UTC date; blank (compose's ${VAR:-}) is unset
    monkeypatch.setenv("CLIPPER_JUDGE_CLAUDE_OAUTH_TOKEN", "sk-ant-oat01-abc")
    monkeypatch.setenv("CLIPPER_JUDGE_CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLIPPER_JUDGE_CLAUDE_TOKEN_EXPIRES", "2027-11-25")
    s = JudgeSettings(_env_file=None, model="claude-cli")
    built = ClaudeCliModel.from_settings(s)
    assert built.token_expires_at == datetime(2027, 11, 25, tzinfo=UTC)
    monkeypatch.setenv("CLIPPER_JUDGE_CLAUDE_TOKEN_EXPIRES", "")
    assert JudgeSettings(_env_file=None).claude_token_expires is None


# --------------------------------------------------------------------------- pure helpers


def test_parse_time_reads_epoch_seconds_milliseconds_and_iso():
    t = datetime(2026, 11, 22, 18, tzinfo=UTC)
    assert parse_time(int(t.timestamp())) == t
    assert parse_time(int(t.timestamp() * 1000)) == t
    assert parse_time(str(int(t.timestamp()))) == t
    assert parse_time("2026-11-22T18:00:00Z") == t
    assert parse_time("2026-11-22T19:00:00+01:00") == t
    assert parse_time("2026-11-22T18:00:00") == t  # no offset: UTC
    for bad in (None, True, "", "soon", [], {}):
        assert parse_time(bad) is None


def test_parse_reading_and_reset_times():
    now = datetime(2026, 11, 22, 15, tzinfo=UTC)
    info = {
        "status": "allowed_warning",
        "resetsAt": int(RESETS.timestamp()),
        "unifiedWindows": {
            "five_hour": {"utilization": 0.91, "resetsAt": int(RESETS.timestamp())},
            "seven_day": {"utilization": 1.0, "resetsAt": int(WEEK_RESETS.timestamp())},
        },
    }
    r = parse_reading(info, now)
    assert r.status == "allowed_warning" and r.five_hour.utilization == 0.91
    assert limit_resets_at(r) == WEEK_RESETS  # the full window decides
    old = parse_reading({"status": "rejected", "rateLimitType": "five_hour", "utilization": 1.0,
                         "resetsAt": int(RESETS.timestamp())}, now)  # fmt: skip
    assert old.five_hour is not None and old.five_hour.utilization == 1.0
    assert limit_resets_at(old) == RESETS
    assert parse_reading({}, now).status == "unknown" and limit_resets_at(None) is None


# --------------------------------------------------------------------------- settings


def test_settings_of_the_cli_adapter(monkeypatch, tmp_path, caplog):
    s = JudgeSettings(_env_file=None)
    assert (s.model, s.claude_path, s.claude_model, s.claude_effort) == (
        "fake",
        "claude",
        "claude-opus-5-5",
        None,
    )
    assert (s.claude_timeout_s, s.claude_max_retries, s.claude_max_budget_usd) == (180, 2, 0.5)
    assert s.claude_oauth_token is None and s.oauth_token is None
    monkeypatch.setenv("CLIPPER_JUDGE_MODEL", "claude-cli")
    monkeypatch.setenv("CLIPPER_JUDGE_CLAUDE_OAUTH_TOKEN", "sk-ant-oat01-abc")
    monkeypatch.setenv("CLIPPER_JUDGE_CLAUDE_EFFORT", "")  # compose's ${VAR:-}
    monkeypatch.setenv("CLIPPER_JUDGE_CLAUDE_MAX_BUDGET_USD", "")
    monkeypatch.setenv("CLIPPER_JUDGE_CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLIPPER_JUDGE_CONCURRENCY", "4")
    s = JudgeSettings(_env_file=None)
    assert s.oauth_token == "sk-ant-oat01-abc" and "sk-ant" not in repr(s)  # SecretStr
    assert s.claude_effort is None and s.claude_max_budget_usd is None
    one = settings_for_model(s)
    assert one.concurrency == 1 and s.concurrency == 4
    assert "runs one call at a time" in caplog.text  # asked for 4: a warning
    caplog.clear()
    monkeypatch.delenv("CLIPPER_JUDGE_CONCURRENCY")
    assert settings_for_model(JudgeSettings(_env_file=None)).concurrency == 1
    assert "runs one call at a time" not in caplog.text  # the default 4: silently 1
    model = make_model(one)
    assert isinstance(model, ClaudeCliModel) and model.name == "claude-opus-5-5"
    assert model.env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-abc"
    assert settings_for_model(JudgeSettings(_env_file=None, model="fake")).concurrency == 4
    with pytest.raises(ValueError, match="full model id"):
        JudgeSettings(_env_file=None, claude_model="opus")
    with pytest.raises(ValueError):
        JudgeSettings(_env_file=None, claude_effort="ultracode")
    with pytest.raises(ValueError):
        JudgeSettings(_env_file=None, model="openai")


# --------------------------------------------------------------------------- the service path


@pytest.mark.integration
async def test_one_round_through_the_service_with_the_cli_adapter(world, fake_claude):
    await world.transcribe_range(-480, 180)
    cand = await world.candidate()
    model = fake_claude.model(clock=world.clock)
    svc = world.service(model)
    assert await world.run_jobs(svc) == 1
    clip = await world.clip(cand)
    assert clip.status == ClipStatus.PLANNED
    (plan,) = await world.plans(clip.id)
    assert plan.model == "claude-opus-5-5" and plan.plan["title"] == "He jumped off the map"
    (call,) = await world.calls()
    assert (call.input_tokens, call.cached_input_tokens, call.output_tokens) == (1750, 2900, 820)
    assert call.cost_usd == pytest.approx(0.0612) and call.latency_s == pytest.approx(12.345)
    assert call.provider_request_id.startswith("5b1c0e9e") and call.error is None
    record = json.loads(world.store.objects[call.io_key])
    assert record["options"] == {"adapter": "claude-cli", "effort": None, "max_turns": 3}
    assert record["extras"]["claude_cli"]["version"] == "2.1.330"
    assert record["response"]["rate_limit"]["five_hour"]["utilization"] == 0.12
    assert svc.guard.reading is not None and svc.guard.reading.five_hour.utilization == 0.12
    (render,) = await world.jobs(JobKind.RENDER)
    assert render.ref_id == plan.id
    (sent,) = fake_claude.calls()
    assert "he just jumped off the map" in sent["stdin"]  # the user content went on stdin
    await svc.beat()
    row = await world.one("SELECT info FROM worker_heartbeats WHERE worker_id = 'judge-test'")
    status = row.info["model_status"]
    assert status["cli_version"] == "2.1.330" and status["calls"] == 1
    assert status["model"] == "claude-opus-5-5" and row.info["model"] == "claude-opus-5-5"


# --------------------------------------------------------------------------- review fixes


async def test_overage_kills_the_call_until_the_window_resets(fake_claude):
    fake_claude.runs(
        {
            "steps": [
                {"line": fake_claude.init()},
                {"line": fake_claude.rate(1.0, using_overage=True, overage_status="allowed")},
                {"sleep": 30},  # the second turn must not run
                *fake_claude.success()[2:],
            ],
            "heartbeat": True,
        }
    )
    with pytest.raises(UsageLimitError, match="overage") as info:
        await fake_claude.model().judge(request())
    exc = info.value
    assert exc.resets_at == RESETS and exc.rate_limit.overage is True  # the full window's reset
    await assert_tree_dead(fake_claude, 0)
    # the plan refusing while extra usage is allowed: billed as overage too
    fake_claude.runs(
        [
            {"line": fake_claude.init()},
            {"line": fake_claude.rate(1.0, status="rejected", overage_status="allowed")},
            *fake_claude.success()[2:],
        ]
    )
    with pytest.raises(UsageLimitError, match="overage"):
        await fake_claude.model().judge(request())


async def test_overage_after_the_answer_keeps_the_paid_answer(fake_claude):
    # the real order (2026-11-25): the reading follows the model's only response, so the answer
    # is paid already; it is kept and the reading (overage) latches the guard in the service
    fake_claude.runs(
        fake_claude.success(five_hour=1.0, using_overage=True, overage_status="allowed")
    )
    response = await fake_claude.model().judge(request())
    assert json.loads(response.text) == CLIP
    assert response.rate_limit.overage is True


async def test_overage_after_a_refused_tool_call_kills_the_call(fake_claude):
    # the CLI refused the StructuredOutput call (schema): another turn would be sent and paid
    steps = fake_claude.success()
    fake_claude.runs(
        {
            "steps": [
                *steps[:4],
                {"line": fake_claude.tool_result(is_error=True)},
                {"line": fake_claude.rate(1.0, using_overage=True, overage_status="allowed")},
                {"sleep": 30},  # the next turn must not run
                *steps[3:],
            ],
            "heartbeat": True,
        }
    )
    with pytest.raises(UsageLimitError, match="overage"):
        await fake_claude.model().judge(request())
    await assert_tree_dead(fake_claude, 0)


async def test_overage_allowed_passes_and_the_users_reading_is_no_overage(fake_claude):
    fake_claude.runs(fake_claude.success(using_overage=True, overage_status="allowed"))
    response = await fake_claude.model(allow_overage=True).judge(request())
    assert response.rate_limit.overage and json.loads(response.text) == CLIP
    # what the user's account reports: overageStatus rejected, isUsingOverage false
    reading = parse_reading(fake_claude.rate()["rate_limit_info"], datetime.now(UTC))
    assert not reading.overage and reading.overage_status == "rejected"


async def test_rejected_credentials_switch_the_adapter_off(fake_claude):
    fake_claude.runs(
        [
            {"line": fake_claude.init()},
            {
                "line": fake_claude.result(
                    is_error=True,
                    subtype="error_during_execution",
                    text='API Error: 401 {"type":"error","error":{"type":"authentication_error"}}',
                    api_error_status=401,
                )
            },
        ]
    )
    model = fake_claude.model()
    with pytest.raises(ModelUnavailableError, match="credentials were rejected") as info:
        await model.judge(request())
    assert info.value.called and info.value.usage is not None  # recorded as a real call
    with pytest.raises(ModelUnavailableError):
        await model.judge(request())
    assert len(fake_claude.calls()) == 1 and "model_off" in model.warnings()


async def test_an_authentication_text_switches_off_too(fake_claude):
    fake_claude.runs(
        [
            {"line": fake_claude.init()},
            {
                "line": fake_claude.result(
                    is_error=True, subtype="error", text="OAuth token has expired. Run /login"
                )
            },
        ]
    )
    with pytest.raises(ModelUnavailableError):
        await fake_claude.model().judge(request())


async def test_a_usage_limit_text_in_a_successful_result(fake_claude):
    reset = datetime(2026, 11, 22, 20, tzinfo=UTC)
    fake_claude.runs(
        [
            {"line": fake_claude.init()},
            {
                "line": fake_claude.result(
                    text=f"Claude AI usage limit reached|{int(reset.timestamp())}"
                )
            },
        ]
    )
    with pytest.raises(UsageLimitError) as info:
        await fake_claude.model().judge(request())
    assert info.value.resets_at == reset
    # the same text in a failed run: its epoch wins over the reading
    fake_claude.runs(
        [
            {"line": fake_claude.init()},
            {"line": fake_claude.rate(0.5)},
            {
                "line": fake_claude.result(
                    is_error=True, subtype="error",
                    text=f"Claude AI usage limit reached|{int(reset.timestamp())}",
                )
            },
        ]
    )  # fmt: skip
    with pytest.raises(UsageLimitError) as info:
        await fake_claude.model().judge(request())
    assert info.value.resets_at == reset


async def test_the_token_never_reaches_errors_or_records(fake_claude):
    token = "sk-ant-oat01-test-token"
    fake_claude.runs(
        {
            "steps": [
                {"line": fake_claude.init()},
                {"stderr": f"debug: using token {token} " + "x" * 2000},
                {"raw": f"not json {token}"},
                {
                    "line": fake_claude.result(
                        is_error=True, subtype="error_during_execution", text=f"boom {token}"
                    )
                },
            ],
            "exit": 1,
        }
    )
    with pytest.raises(ModelError) as info:
        await fake_claude.model(oauth_token=token).judge(request())
    exc = info.value
    dumped = json.dumps(exc.extras) + str(exc)
    assert token not in dumped and "[redacted]" in dumped
    tail = exc.extras["claude_cli"]["stderr_tail"]
    assert max(len(line) for line in tail.splitlines()) <= claude_cli.STDERR_LINE_CHARS + 1


def test_config_dir_rules(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    assert check_config_dir("", home=home) == check_config_dir(None, home=home)
    assert check_config_dir(None, home=home).name == "judge-claude"  # blank: the default
    for bad in (home, home / ".claude", home / ".claude" / "judge", tmp_path):
        with pytest.raises(ValueError, match=r"home directory|own Claude config"):
            check_config_dir(bad, home=home)
    assert check_config_dir(home / "judge", home=home) == (home / "judge").resolve()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(ValueError, match="git work tree"):
        check_config_dir(repo / "data" / "judge-claude", home=home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom-claude"))
    with pytest.raises(ValueError, match="own Claude config"):
        check_config_dir(tmp_path / "custom-claude" / "x", home=home)
    # `~` is the real home: refused; the settings refuse it for the CLI model
    with pytest.raises(ValueError):
        check_config_dir("~")
    with pytest.raises(ValueError):
        JudgeSettings(_env_file=None, model="claude-cli", claude_config_dir="~")
    assert JudgeSettings(_env_file=None, claude_config_dir="  ").claude_config_dir.name == (
        "judge-claude"
    )
    with pytest.raises(ValueError):
        ClaudeCliModel(config_dir=Path("~"))


async def test_auth_status_refuses_another_api_provider(fake_claude):
    fake_claude.scenario(
        {"runs": [{"steps": []}], "auth": {**fake_claude.auth(), "apiProvider": "bedrock"}}
    )
    with pytest.raises(ClaudeAuthError, match="apiProvider 'bedrock'"):
        await fake_claude.model().check_auth()


def test_the_executable_is_found_on_absolute_path_entries_only(tmp_path, monkeypatch):
    name = "claude-fake"
    exe = name + (".exe" if sys.platform == "win32" else "")
    here = tmp_path / "cwd"
    here.mkdir()
    (here / exe).write_bytes(b"")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.chdir(here)
    # the working dir (and relative or empty PATH entries) are never searched
    assert find_executable(name, os.pathsep.join(["", ".", str(bindir)])) is None
    (bindir / exe).write_bytes(b"")
    if sys.platform != "win32":
        (bindir / exe).chmod(0o755)
    assert find_executable(name, os.pathsep.join([".", str(bindir)])) == str(bindir / exe)
    with pytest.raises(PermanentModelError, match="absolute path or a bare name"):
        claude_cli.resolve_command(["bin/claude"], {"PATH": str(bindir)})


# --------------------------------------------------------------------------- service path


@pytest.mark.integration
async def test_a_switched_off_adapter_holds_the_job_and_keeps_the_clip(world, fake_claude):
    await world.transcribe_range(-480, 180)
    cand = await world.candidate()
    fake_claude.runs([{"line": fake_claude.init(api_key_source="ANTHROPIC_API_KEY")}])
    model = fake_claude.model(clock=world.clock)
    svc = world.service(model)
    assert await world.run_jobs(svc) == 1
    (job,) = await world.jobs(JobKind.JUDGE)
    assert job.status == JobStatus.QUEUED and job.attempts == 0  # held, no attempt used
    assert job.run_after == world.at(40 + claude_cli.OFF_RETRY_S)
    assert (await world.clip(cand)).status == ClipStatus.JUDGING  # not failed
    (call,) = await world.calls()
    assert call.error.startswith("unavailable: ")
    # later: refused without starting anything, no new row; still held
    await world.make_runnable()
    assert await world.run_jobs(svc) == 1
    assert len(fake_claude.calls()) == 1 and len(await world.calls()) == 1
    assert (await world.clip(cand)).status == ClipStatus.JUDGING
    await svc.beat()
    row = await world.one("SELECT info FROM worker_heartbeats WHERE worker_id = 'judge-test'")
    assert "model_off" in row.info["model_status"]["warnings"]


@pytest.mark.integration
async def test_notices_for_an_expiring_token_and_a_switched_off_model(world, fake_claude):
    expires = world.at(10 * 86400)
    model = fake_claude.model(clock=world.clock, token_expires_at=expires)
    svc = world.service(model)
    await svc.beat()
    await svc.beat()  # every heartbeat: still one notice
    (job,) = await world.jobs(JobKind.NOTIFY)
    assert job.dedupe_key == f"notify:{int(expires.timestamp())}:credential_expiring"
    assert job.payload["kind"] == "credential_expiring" and job.payload["severity"] == "warning"
    assert "claude setup-token" in job.payload["text"] and "in 10 days" in job.payload["text"]
    await world.service(model).beat()  # a restarted service: the queue dedupes it
    assert len(await world.jobs(JobKind.NOTIFY)) == 1
    # the adapter switches itself off on a call: a model_off notice, one per UTC day
    await world.transcribe_range(-480, 180)
    await world.candidate()
    fake_claude.runs([{"line": fake_claude.init(api_key_source="ANTHROPIC_API_KEY")}])
    assert await world.run_jobs(svc) == 1
    await svc.beat()
    await svc.beat()
    jobs = await world.jobs(JobKind.NOTIFY)
    assert [j.payload["kind"] for j in jobs] == ["credential_expiring", "model_off"]
    off = jobs[1]
    assert off.payload["severity"] == "error" and "apiKeySource" in off.payload["text"]
    day = int(model.off_since.timestamp() // 86400)
    assert off.dedupe_key == f"notify:{day}:model_off"


@pytest.mark.integration
async def test_overage_latches_the_guard_for_every_clip(world, fake_claude):
    await world.transcribe_range(-480, 180)
    first = await world.candidate()
    second = await world.candidate(peak=60.0)
    resets = world.at(3600)
    fake_claude.runs(
        [
            {"line": fake_claude.init()},
            {"line": fake_claude.rate(1.0, using_overage=True, resets=resets)},
        ]
    )
    model = fake_claude.model(clock=world.clock)
    svc = world.service(model)  # usage caps unset
    assert await world.run_jobs(svc) == 2
    jobs = await world.jobs(JobKind.JUDGE)
    # min(the reset, each clip's hard deadline: the second's context ends 60 s later)
    assert [j.run_after for j in jobs] == [world.at(1840), world.at(1890)]
    assert len(fake_claude.calls()) == 1  # the second clip was held without a call
    assert svc.guard.overage_until == resets and "overage" in svc.guard.warnings
    (call,) = await world.calls()
    assert call.error.startswith("usage_limit: extra usage")
    for cand in (first, second):
        assert (await world.clip(cand)).status == ClipStatus.JUDGING


@pytest.mark.integration
async def test_paid_failures_record_usage_and_timeouts_fail_after_two(world, fake_claude):
    await world.transcribe_range(-480, 180)
    cand = await world.candidate()
    fake_claude.runs(
        [
            {"line": fake_claude.init()},
            {"line": fake_claude.result(is_error=True, subtype="error_during_execution")},
        ],
        {"steps": [{"line": fake_claude.init()}, {"sleep": 30}]},
    )
    model = fake_claude.model(clock=world.clock, timeout_s=2.0)
    svc = world.service(model)
    assert await world.run_jobs(svc) == 1  # a paid failure: retried by the queue
    (paid,) = await world.calls()
    assert paid.error.startswith("provider: ") and paid.output_tokens == 820
    assert paid.cost_usd == pytest.approx(0.0612)
    for _ in range(2):  # two timeouts of the same request
        await world.make_runnable()
        await world.run_jobs(svc)
    calls = await world.calls()
    assert [c.error.split(":")[0] for c in calls] == ["provider", "timeout", "timeout"]
    clip = await world.clip(cand)
    assert clip.status == ClipStatus.FAILED and "timed out 2 times" in clip.error
    (job,) = await world.jobs(JobKind.JUDGE)
    assert job.status == JobStatus.FAILED
