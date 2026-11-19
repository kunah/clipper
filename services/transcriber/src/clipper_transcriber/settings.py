from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from clipper_core.contracts import CHUNK_JITTER
from clipper_transcriber.pipeline import PipelineConfig
from clipper_transcriber.vad import VadParams
from clipper_transcriber.whisper import WhisperOptions


class TranscriberSettings(BaseSettings):
    """CLIPPER_TRANSCRIBER_* env vars."""

    model_config = SettingsConfigDict(
        env_prefix="CLIPPER_TRANSCRIBER_", env_file=".env", extra="ignore"
    )

    # Engines (registries in service.py). The defaults need the package extras (the CUDA image
    # has them); natively set ENGINE=fake and TAGGER=none (or fake), the test engines.
    engine: str = Field(default="whisper", min_length=1)  # speech engine adapter
    tagger: str = "ced"  # sound tagger adapter; "none" = no `classes` in audio_metrics_1s
    models_dir: Path = Path("data/models")  # VAD model dir (HF weights: HF_HOME)

    # Speech engine "whisper" (faster-whisper; DESIGN.md §16).
    whisper_model: str = "large-v3-turbo"
    # Hub commit; empty = the pinned one of a known model (whisper.WHISPER_REVISIONS).
    whisper_revision: str | None = None
    whisper_device: Literal["cuda", "cpu", "auto"] = "cuda"
    whisper_compute_type: str = "float16"
    whisper_beam_size: int = Field(default=5, ge=1)
    # faster-whisper's own Silero VAD inside each stretch our gate sends: same words on the
    # benchmark audio, 3 % slower (4b-3 e2e), so off.
    whisper_vad_filter: bool = False

    # Sound tagger "ced" (CED-base). CPU by default: the image has the CPU build of torch.
    tagger_device: Literal["cpu", "cuda"] = "cpu"
    tagger_threads: int = Field(default=2, ge=0)  # torch CPU threads; 0 = torch's default
    tagger_batch_size: int = Field(default=16, ge=1)  # 2 s windows per forward pass

    # A GPU out-of-memory error (a game holding the VRAM) is retried in-process for this long
    # (waits 30, 60, 120, 240 s, ...) before the job attempt fails.
    gpu_oom_wait_s: float = Field(default=600.0, ge=0, allow_inf_nan=False)

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
    # Neighbouring chunks this close are contiguous (handover): the chunk jitter, as in coverage()
    # and the recorder's gap tolerance (the window audio zero-pads such a hole).
    contiguity_tolerance_s: float = Field(
        default=CHUNK_JITTER.total_seconds(), ge=0, allow_inf_nan=False
    )
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

    def whisper_options(self) -> WhisperOptions:
        return WhisperOptions(
            model=self.whisper_model,
            revision=self.whisper_revision or None,
            device=self.whisper_device,
            compute_type=self.whisper_compute_type,
            beam_size=self.whisper_beam_size,
            vad_filter=self.whisper_vad_filter,
            oom_wait_s=self.gpu_oom_wait_s,
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
