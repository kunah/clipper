"""`CLIPPER_JUDGE_*` settings of the judge service (DESIGN §19 "Outside core")."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from clipper_judge.params import JudgeParams

FAKE = "fake"
CLAUDE_CLI = "claude-cli"
MODELS = (FAKE, CLAUDE_CLI)
# The pinned model of the CLI adapter (decided 2026-11-21: Opus, a full id, never an alias such
# as `opus`, so a new release can't change verdicts unnoticed).
CLAUDE_MODEL = "claude-opus-5-5"
Effort = Literal["low", "medium", "high", "xhigh", "max"]
DEFAULT_CLAUDE_CONFIG_DIR = Path("~/.clipper/judge-claude")


def check_config_dir(path: Path | str | None, *, home: Path | None = None) -> Path:
    """The CLI adapter's own directory (its HOME; its config in `.claude/`), resolved; blank ->
    the default. `ValueError` for a directory that could reach the user's own Claude config or a
    project's: the user's home; the user's real config (`~/.claude`, and `$CLAUDE_CONFIG_DIR` of
    this process when set), a directory above it or inside it, or one whose `.claude` is it; a
    directory inside a git work tree (its `.claude/` settings and hooks would apply)."""
    if path is None or not str(path).strip():
        path = DEFAULT_CLAUDE_CONFIG_DIR
    resolved = Path(os.path.expanduser(str(path))).resolve()
    home = (home if home is not None else Path.home()).resolve()
    if resolved == home:
        raise ValueError(f"claude_config_dir must not be the home directory ({resolved})")
    real = [home / ".claude"]
    if os.environ.get("CLAUDE_CONFIG_DIR", "").strip():
        real.append(Path(os.path.expanduser(os.environ["CLAUDE_CONFIG_DIR"])).resolve())
    for user in real:
        if (
            user == resolved
            or user.is_relative_to(resolved)
            or resolved.is_relative_to(user)
            or (resolved / ".claude") == user
        ):
            raise ValueError(
                f"claude_config_dir {resolved} would reach the user's own Claude config ({user})"
            )
    for d in (resolved, *resolved.parents):
        if (d / ".git").exists():
            raise ValueError(
                f"claude_config_dir {resolved} is inside a git work tree ({d}): its .claude/ "
                "settings would apply"
            )
    return resolved


class JudgeSettings(BaseSettings):
    """The service. The builder's and resolver's tunables map onto `JudgeParams` (`params()`)."""

    model_config = SettingsConfigDict(env_prefix="CLIPPER_JUDGE_", env_file=".env", extra="ignore")

    # The model adapter: `fake` (deterministic rules, `clipper_judge.fakes`) or `claude-cli` (the
    # Claude Code CLI on the user's subscription, `clipper_judge.claude_cli`, build item 6d).
    model: Literal["fake", "claude-cli"] = FAKE
    prompt: str = "v1"  # the prompt directory (`clipper_judge/prompts/<prompt>/`)
    # Job runners per process. The CLI adapter runs one call at a time (the usage reading stays
    # current): `main` uses 1 for it and logs a warning when this is set higher.
    concurrency: int = Field(default=4, gt=0)

    # Claude Code CLI adapter (6d; DESIGN §19 "Claude Code CLI adapter"). The CLI's environment
    # is built from scratch: PATH, a HOME and CLAUDE_CONFIG_DIR inside `claude_config_dir`, the
    # token below, the retry count; nothing ANTHROPIC_* or CLAUDE_CODE_* of this process.
    # The native executable: preferably an absolute path; a bare name is looked up on PATH's
    # absolute entries only (never the working directory). Windows: claude.exe, never a .cmd.
    claude_path: str = "claude"
    claude_model: str = CLAUDE_MODEL
    claude_effort: Effort | None = None  # `--effort`; None: the CLI's default for the model
    # `claude setup-token` (one year, "can only make model requests"); env
    # CLIPPER_JUDGE_CLAUDE_OAUTH_TOKEN. None: a login stored in `claude_config_dir` (natively,
    # `CLAUDE_CONFIG_DIR=<dir>/.claude claude` once and /login).
    claude_oauth_token: SecretStr | None = None
    # A writable directory of the judge's own (the CLI's HOME; its config in `.claude/`), never
    # the user's ~/.claude, home or a git checkout (`check_config_dir`; blank: the default).
    claude_config_dir: Path = DEFAULT_CLAUDE_CONFIG_DIR
    claude_timeout_s: float = Field(default=180.0, gt=0)  # per call; then the tree is killed
    claude_max_retries: int = Field(default=2, ge=0)  # CLAUDE_CODE_MAX_RETRIES (the queue waits)
    # `--max-budget-usd`: caps a runaway run (a list-price estimate, not a charge). None: no cap.
    claude_max_budget_usd: float | None = Field(default=0.50, gt=0)
    # Pass the system prompt as a file (`--system-prompt-file`), else inline (`--system-prompt`).
    claude_system_prompt_file: bool = True
    # Extra usage beyond the plan (`isUsingOverage`) is billed money. False: a reading showing it
    # kills the call (UsageLimitError until the window resets) and the usage guard holds every
    # call until then, caps or not, with a heartbeat warning.
    claude_allow_overage: bool = False
    # Enqueue the `render` job with every `clip` plan. Nothing runs them until the renderer
    # exists (§20): they just queue.
    render_jobs: bool = True
    # Judge audio_only clips too (cut on audio ranges). Off: they are rejected as `no_video`
    # without a call, since the renderer can't render them (decided 2026-11-21).
    judge_audio_only: bool = False

    # Waiter (§19 step 3): soft deadline = max(waiting_since, context_to) + soft_wait_s (judge
    # with what exists if the core moment is covered), hard deadline the same + hard_wait_s.
    soft_wait_s: float = Field(default=240.0, ge=0)
    hard_wait_s: float = Field(default=1800.0, ge=0)
    sweep_interval_s: float = Field(default=30.0, gt=0)  # besides LISTEN clipper_transcripts
    heartbeat_interval_s: float = Field(default=15.0, gt=0)
    poll_interval_s: float = Field(default=10.0, gt=0)  # job runners, besides LISTEN clipper_jobs

    # JudgeParams (prompt + resolver; provisional, tuned on real candidates)
    max_rounds: int = Field(default=2, ge=0)
    max_before_s: float = Field(default=600.0, ge=0)
    max_after_s: float = Field(default=120.0, ge=0)
    min_clip_s: float = 10.0
    max_clip_s: float = 60.0
    max_output_tokens: int = Field(default=4000, gt=0)
    # SFX tags the model may use; empty until the renderer's asset manifest exists (§20).
    sfx_tags: list[str] = Field(default_factory=list)
    title_blocklist: list[str] = Field(default_factory=list)  # whole words -> flag `sensitive`

    # Usage guard (decided 2026-11-21: off by default). Every reading is logged and in the
    # heartbeat; nothing blocks while these caps are unset (None).
    usage_five_hour_max: float | None = Field(default=None, ge=0, le=1)
    usage_seven_day_max: float | None = Field(default=None, ge=0, le=1)
    calls_per_stream_hour: int | None = Field(default=None, ge=0)
    calls_per_day: int | None = Field(default=None, ge=0)
    usage_stale_s: float = Field(default=900.0, gt=0)  # older: one call through to refresh it
    # A call refused for a rate/usage limit (with or without caps) waits for the provider's
    # resetsAt, else this long; never past the clip's hard deadline.
    limit_retry_s: float = Field(default=900.0, gt=0)

    # Price table, USD per 1M tokens, for `judge_calls.cost_usd` when the model gives no estimate
    # of its own (the CLI's list-price estimate is used as it is, §19).
    price_input_per_mtok: float = Field(default=0.0, ge=0)
    price_cached_input_per_mtok: float = Field(default=0.0, ge=0)
    price_output_per_mtok: float = Field(default=0.0, ge=0)

    @field_validator("claude_effort", "claude_oauth_token", "claude_max_budget_usd", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        # compose passes `${VAR:-}`: an empty value means "not set"
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, SecretStr) and not value.get_secret_value().strip():
            return None
        return value

    @field_validator("claude_config_dir", mode="before")
    @classmethod
    def _blank_dir_is_default(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return DEFAULT_CLAUDE_CONFIG_DIR
        return value

    @field_validator("claude_model")
    @classmethod
    def _full_model_id(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("claude-"):
            raise ValueError(
                f"claude_model must be a full model id such as {CLAUDE_MODEL!r}, not an alias "
                f"({value!r})"
            )
        return value

    @model_validator(mode="after")
    def _check(self) -> JudgeSettings:
        if self.hard_wait_s < self.soft_wait_s:
            raise ValueError("hard_wait_s must not be shorter than soft_wait_s")
        self.params()  # JudgeParams validates its own ranges
        if self.model == CLAUDE_CLI:
            check_config_dir(self.claude_config_dir)
        return self

    @property
    def oauth_token(self) -> str | None:
        token = self.claude_oauth_token
        return None if token is None else token.get_secret_value()

    @property
    def usage_caps(self) -> bool:
        """Any cap of the usage guard set (the guard is on)."""
        return any(
            v is not None
            for v in (
                self.usage_five_hour_max,
                self.usage_seven_day_max,
                self.calls_per_stream_hour,
                self.calls_per_day,
            )
        )

    def params(self) -> JudgeParams:
        return JudgeParams(
            min_clip_s=self.min_clip_s,
            max_clip_s=self.max_clip_s,
            max_rounds=self.max_rounds,
            max_before_s=self.max_before_s,
            max_after_s=self.max_after_s,
            max_output_tokens=self.max_output_tokens,
            title_blocklist=tuple(self.title_blocklist),
        )

    def cost_usd(self, input_tokens: int, cached_input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.price_input_per_mtok
            + cached_input_tokens * self.price_cached_input_per_mtok
            + output_tokens * self.price_output_per_mtok
        ) / 1_000_000
