"""`CLIPPER_RENDERER_*` settings of the renderer (DESIGN §20 "Outside core")."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class RendererSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLIPPER_RENDERER_", env_file=".env", extra="ignore"
    )

    # auto: h264_nvenc when a test encode works at start (and libx264 again for a render whose
    # NVENC encode fails); nvenc: NVENC only (a failure is an error); x264: libx264 only
    encoder: Literal["auto", "nvenc", "x264"] = "auto"
    # layouts.toml (read for every render: edits need no restart); unset or missing: every
    # channel gets the file default (blurred_fit). Compose mounts services/renderer/config.
    layouts_path: Path | None = None
    # the user's licensed library: <dir>/manifest.toml (clipper_core.assets); unset: no SFX or
    # music (cues are skipped with a warning)
    assets_dir: Path | None = None
    # platforms every used asset's licence must list (comma-separated); empty: no requirement
    asset_platforms: Annotated[list[str], NoDecode] = Field(default_factory=list)
    fonts_dir: Path | None = None  # unset: the bundled fonts (package data)
    temp_dir: Path | None = None  # job work directories (chunks ~60 MB each); unset: system temp
    keep_work: bool = False  # keep each job's work directory (debugging)
    pin_margin_s: float = Field(default=30.0, ge=0)  # chunk video pinned around the plan (§20)
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    timeout_s: float = Field(default=1200.0, gt=0)  # per ffmpeg run
    poll_interval_s: float = Field(default=10.0, gt=0)  # besides LISTEN clipper_jobs
    heartbeat_interval_s: float = Field(default=30.0, gt=0)

    @field_validator("layouts_path", "assets_dir", "fonts_dir", "temp_dir", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("asset_platforms", mode="before")
    @classmethod
    def _comma_list(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [p.strip().lower() for p in value.split(",") if p.strip()]
        return value
