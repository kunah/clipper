"""`CLIPPER_JUDGE_*` settings of the judge service (DESIGN §19 "Outside core")."""

from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from clipper_judge.params import JudgeParams


class JudgeSettings(BaseSettings):
    """The service. The builder's and resolver's tunables map onto `JudgeParams` (`params()`)."""

    model_config = SettingsConfigDict(env_prefix="CLIPPER_JUDGE_", env_file=".env", extra="ignore")

    # The model adapter: `fake` (deterministic rules, `clipper_judge.fakes`) until build item 6d
    # adds the Claude Code CLI adapter.
    model: str = "fake"
    prompt: str = "v1"  # the prompt directory (`clipper_judge/prompts/<prompt>/`)
    concurrency: int = Field(default=4, gt=0)  # job runners per process (1 for the CLI adapter)
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

    @model_validator(mode="after")
    def _check(self) -> JudgeSettings:
        if self.hard_wait_s < self.soft_wait_s:
            raise ValueError("hard_wait_s must not be shorter than soft_wait_s")
        self.params()  # JudgeParams validates its own ranges
        return self

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
