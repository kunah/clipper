from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BackfillSettings(BaseSettings):
    """CLIPPER_BACKFILL_* env vars (database, storage and logging come from core `Settings`)."""

    model_config = SettingsConfigDict(
        env_prefix="CLIPPER_BACKFILL_", env_file=".env", extra="ignore"
    )

    ffmpeg: str = "ffmpeg"
    downloads: int = Field(default=4, ge=1, le=4)  # concurrent segment downloads (politeness)
    segment_attempts: int = Field(default=6, ge=1)  # per segment, with backoff
    retry_backoff_s: float = Field(default=1.0, ge=0)  # doubles per attempt, max 30 s
    http_timeout_s: float = 30.0
    chunk_seconds: float = Field(default=60.0, gt=0, le=120)  # a chunk is built in memory
    t0_chat_pages: int = Field(default=6, ge=0)  # chat pages sampled for the T0 cross-check
    chat_batch_pages: int = Field(default=20, ge=1)  # chat replay pages per commit (~1000 msgs)
    # guard 2: skip a VOD's chat when the channel has at least this many seconds of other (live)
    # chat_metrics_1s rows in its range; fewer are left alone and flagged (chat_foreign_seconds)
    chat_guard_min_s: int = Field(default=60, ge=1)
    clips_top: int = Field(default=100, ge=1, le=1000)  # viewer clips per broadcast, by views
    lease_ttl_s: float = Field(default=120.0, ge=10)  # renewed every third of it
    temp_dir: Path | None = None  # parent of the per-run temp dir (default: the system's)
    # `vod --fill-gaps` T0: live chunks next to the fill ranges, searched +/- this in the VOD,
    # up to `fill_seam_tries` per seam (nearest first, the next one when a match fails). A fill
    # writes only with a trusted T0 (else `--accept-t0`): at least `fill_t0_min_windows` good
    # matches, at most `fill_t0_spread_s` apart, within `fill_t0_max_pdt_diff_s` of T0_pdt
    fill_search_s: float = Field(default=20.0, gt=0)
    fill_seam_tries: int = Field(default=3, ge=1)
    fill_t0_spread_s: float = Field(default=0.05, gt=0)
    fill_t0_min_windows: int = Field(default=2, ge=1)
    fill_t0_max_pdt_diff_s: float = Field(default=0.5, gt=0)
