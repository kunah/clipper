from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DetectorSettings(BaseSettings):
    """CLIPPER_DETECTOR_* env vars (the service; scoring tunables live in `DetectorParams`)."""

    model_config = SettingsConfigDict(
        env_prefix="CLIPPER_DETECTOR_", env_file=".env", extra="ignore"
    )

    # live: chat spikes of the held channels; offline: `detect_offline` jobs (build item 5, not
    # built yet: logs that and idles); all: both.
    mode: Literal["live", "offline", "all"] = "live"
    # Enqueue a `judge` job with every emitted candidate. Off until the judge exists (§19): the
    # candidate and its media request are written either way.
    judge_jobs: bool = False
    params_path: Path | None = None  # TOML overrides of DetectorParams; default: code defaults
    lexicon_path: Path | None = None  # default: the packaged data/lexicon.toml

    max_channels: int = Field(default=200, gt=0)  # per process
    live_interval_s: float = Field(default=1.0, gt=0)  # chat_metrics_1s polling
    explicit_interval_s: float = Field(default=5.0, gt=0)  # raw chat: clip mentions and links
    sync_interval_s: float = Field(default=10.0, gt=0)  # channels, leases, streams
    clips_interval_s: float = Field(default=60.0, gt=0)  # Helix Get Clips per live channel
    clips_lookback_s: float = Field(default=600.0, gt=0)  # clips created in the last 10 min
    sweep_interval_s: float = Field(default=60.0, gt=0)  # media requests -> fulfilled
    heartbeat_interval_s: float = Field(default=15.0, gt=0)
    lease_ttl_s: float = Field(default=60.0, gt=0)
    lease_renew_s: float = Field(default=20.0, ge=0)
    # A detector media request counts as fulfilled once its window is transcribed, or this long
    # after the window's end at the latest.
    fulfil_after_s: float = Field(default=900.0, ge=0)
    # Each live tick re-reads this much before the cursor: rows of seconds already fed (as zero
    # or other values) are counted as `late_rows` (heartbeat) and logged.
    late_check_s: float = Field(default=30.0, ge=0)
    # The live loop doesn't go past the newest chat_metrics_1s row of the held channels + 1 s
    # (chat-ingest writes every channel's seconds in one flush) unless that row is more than
    # this far behind the final cutoff (all quiet, or chat-ingest down: zeros, as before).
    watermark_slack_s: float = Field(default=10.0, ge=0)
    # Explicit signals wait this long at most while the channel is between streams.
    signal_max_age_s: float = Field(default=900.0, gt=0)

    @model_validator(mode="after")
    def _lease_timing(self) -> DetectorSettings:
        if self.lease_renew_s >= self.lease_ttl_s:
            raise ValueError("lease_renew_s must be shorter than lease_ttl_s")
        if self.sync_interval_s + self.lease_renew_s >= self.lease_ttl_s:
            # renewals happen in the sync loop: up to sync_interval_s late
            raise ValueError("sync_interval_s + lease_renew_s must be shorter than lease_ttl_s")
        return self
