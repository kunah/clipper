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
    lease_ttl_s: float = Field(default=120.0, ge=10)  # renewed every third of it
    temp_dir: Path | None = None  # parent of the per-run temp dir (default: the system's)
