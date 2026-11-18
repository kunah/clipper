from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class RecorderSettings(BaseSettings):
    """CLIPPER_RECORDER_* env vars."""

    model_config = SettingsConfigDict(
        env_prefix="CLIPPER_RECORDER_", env_file=".env", extra="ignore"
    )

    spool_dir: Path = Path("data/spool")  # one per process; /spool (a volume) in Docker
    chunk_seconds: float = 60.0
    # streamlink stream names tried in order for `full` mode ("best" = source quality)
    qualities: str = "best"
    max_channels: int = 20  # per recorder process
    sync_interval_s: float = 10.0
    finish_workers: int = 2
    drain_timeout_s: float = 25.0  # on shutdown; whatever is left is finished on next start
    ffmpeg: str = "ffmpeg"

    @property
    def quality_list(self) -> list[str]:
        return [q.strip() for q in self.qualities.split(",") if q.strip()]
