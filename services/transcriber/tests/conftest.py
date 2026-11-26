"""Fixtures for the transcriber tests (no database, no object storage)."""

from __future__ import annotations

import shutil
import urllib.error
from pathlib import Path

import pytest

from clipper_transcriber.vad import ModelDownloadError, ensure_silero_model


@pytest.fixture
def ffmpeg() -> str:
    """ffmpeg executable (tests using it are skipped when it isn't installed)."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    return "ffmpeg"


@pytest.fixture(scope="session")
def silero_model(request) -> Path:
    """The pinned Silero VAD model, downloaded once into pytest's cache (skip when offline)."""
    models_dir = request.config.cache.mkdir("silero_vad")
    try:
        return ensure_silero_model(models_dir, timeout_s=30)
    except (OSError, urllib.error.URLError, ModelDownloadError) as exc:
        pytest.skip(f"Silero VAD model not available (offline?): {exc}")
