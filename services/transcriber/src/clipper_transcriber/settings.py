from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from clipper_transcriber.pipeline import PipelineConfig
from clipper_transcriber.vad import VadParams


class TranscriberSettings(BaseSettings):
    """CLIPPER_TRANSCRIBER_* env vars."""

    model_config = SettingsConfigDict(
        env_prefix="CLIPPER_TRANSCRIBER_", env_file=".env", extra="ignore"
    )

    # Engines. Required until the real adapter exists (then it becomes the default); "fake"
    # (the test engine) only when set explicitly.
    engine: str = Field(min_length=1)  # speech engine adapter
    tagger: str = "none"  # sound tagger adapter; "none" = no `classes` in audio_metrics_1s
    models_dir: Path = Path("data/models")  # model cache (a volume in Docker), incl. the VAD

    ffmpeg: str = "ffmpeg"
    decode_timeout_s: float = Field(default=120.0, gt=0, allow_inf_nan=False)

    # Voice-activity detection (Silero VAD defaults; see vad.VadParams).
    vad: Literal["silero", "energy"] = "silero"
    vad_model_path: Path | None = None  # local silero_vad.onnx; default: download to models_dir
    vad_threshold: float = Field(default=0.5, gt=0, lt=1)
    vad_min_speech_ms: float = Field(default=250.0, ge=0, allow_inf_nan=False)
    vad_min_silence_ms: float = Field(default=100.0, ge=0, allow_inf_nan=False)
    vad_speech_pad_ms: float = Field(default=30.0, ge=0, allow_inf_nan=False)

    # Gating: what is sent to the speech engine.
    gate_merge_gap_s: float = Field(default=1.0, ge=0, allow_inf_nan=False)
    gate_pad_s: float = Field(default=0.2, ge=0, allow_inf_nan=False)
    gate_max_s: float = Field(default=30.0, gt=0, allow_inf_nan=False)

    # Sentences, junk filter, chunk boundaries, metrics.
    sentence_max_s: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    sentence_pause_s: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    word_speech_tolerance_s: float = Field(default=0.2, ge=0, allow_inf_nan=False)
    tail_margin_s: float = Field(default=0.5, ge=0, allow_inf_nan=False)
    takeover_cap_s: float = Field(default=30.0, gt=0, allow_inf_nan=False)  # max prepended
    contiguity_tolerance_s: float = Field(default=0.05, ge=0, allow_inf_nan=False)
    tagger_context_s: int = Field(default=2, ge=0)
    loudness_floor_db: float = Field(default=-100.0, lt=0, allow_inf_nan=False)

    heartbeat_interval_s: float = Field(default=30.0, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _gate_fits_padding(self) -> TranscriberSettings:
        if self.gate_max_s <= 2 * self.gate_pad_s:
            raise ValueError("gate_max_s must be longer than twice gate_pad_s")
        return self

    def vad_params(self) -> VadParams:
        # Silero splits longer speech at its longest pause, so a padded region fits the gate.
        return VadParams(
            threshold=self.vad_threshold,
            min_speech_ms=self.vad_min_speech_ms,
            min_silence_ms=self.vad_min_silence_ms,
            speech_pad_ms=self.vad_speech_pad_ms,
            max_speech_s=self.gate_max_s - 2 * self.gate_pad_s,
        )

    def pipeline_config(self) -> PipelineConfig:
        return PipelineConfig(
            gate_merge_gap_s=self.gate_merge_gap_s,
            gate_pad_s=self.gate_pad_s,
            gate_max_s=self.gate_max_s,
            sentence_max_s=self.sentence_max_s,
            sentence_pause_s=self.sentence_pause_s,
            word_speech_tolerance_s=self.word_speech_tolerance_s,
            tail_margin_s=self.tail_margin_s,
            tagger_context_s=self.tagger_context_s,
            loudness_floor_db=self.loudness_floor_db,
        )
