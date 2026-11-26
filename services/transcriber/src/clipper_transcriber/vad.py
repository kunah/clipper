"""Voice-activity detection: Silero VAD v6.2 (ONNX, CPU) and a simple energy fallback.

Silero runs through onnxruntime without torch: 512-sample frames (32 ms at 16 kHz), each fed
with the previous frame's last 64 samples as context and the LSTM state carried over. Without
the context the model still runs but its probabilities collapse to ~0, so never drop it.
`speech_regions` is a numpy port of silero-vad's `get_speech_timestamps` (v6.2.3), verified
against the original on real stream audio.

The model file (MIT license, ~2.3 MB) is pinned by commit and sha256 and downloaded into the
models dir on first use.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from clipper_core.logs import get_logger
from clipper_transcriber.audio import SAMPLE_RATE, Pcm
from clipper_transcriber.engines import VadResult

log = get_logger(__name__)

FRAME_SAMPLES = 512
CONTEXT_SAMPLES = 64

SILERO_VERSION = "6.2"
SILERO_URL = (
    "https://raw.githubusercontent.com/snakers4/silero-vad/"
    "5cd7945676eb32225748052e2e6a0580e4686a08/src/silero_vad/data/silero_vad.onnx"
)
SILERO_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
SILERO_FILENAME = f"silero_vad_v{SILERO_VERSION}_{SILERO_SHA256[:8]}.onnx"


class ModelDownloadError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VadParams:
    """`get_speech_timestamps` parameters (silero-vad's defaults)."""

    threshold: float = 0.5
    neg_threshold: float | None = None  # default: max(threshold - 0.15, 0.01)
    min_speech_ms: float = 250.0
    # Longer speech is split at its longest pause. Upstream's default is inf; 29.6 s = the
    # 30 s gate minus 2 x 0.2 s padding, so each region fits one engine call.
    max_speech_s: float = 29.6
    min_silence_ms: float = 100.0
    speech_pad_ms: float = 30.0
    min_silence_at_max_speech_ms: float = 98.0
    use_max_poss_sil_at_max_speech: bool = True


# --------------------------------------------------------------------------- regions


def speech_regions(
    probs: Sequence[float] | npt.NDArray[np.floating],
    n_samples: int,
    params: VadParams | None = None,
    *,
    frame_samples: int = FRAME_SAMPLES,
    sample_rate: int = SAMPLE_RATE,
) -> list[tuple[int, int]]:
    """Frame probabilities -> speech regions as sample ranges `[start, end)`.

    Port of silero-vad's `get_speech_timestamps_from_probs` (v6.2.3): hysteresis between
    `threshold` and `neg_threshold`, a pause ends speech only after `min_silence_ms`, regions
    shorter than `min_speech_ms` are dropped, longer than `max_speech_s` are split (at the
    longest pause when there was one), and every region is padded by `speech_pad_ms`.
    """
    p = params or VadParams()
    threshold = p.threshold
    neg_threshold = p.neg_threshold
    if neg_threshold is None:
        neg_threshold = max(threshold - 0.15, 0.01)
    min_speech = sample_rate * p.min_speech_ms / 1000
    pad = sample_rate * p.speech_pad_ms / 1000
    max_speech = sample_rate * p.max_speech_s - frame_samples - 2 * pad
    min_sil = sample_rate * p.min_silence_ms / 1000
    min_sil_max = sample_rate * p.min_silence_at_max_speech_ms / 1000
    use_max_poss = p.use_max_poss_sil_at_max_speech

    triggered = False
    speeches: list[list[float]] = []
    cur: float | None = None  # start of the open region
    temp_end = prev_end = next_start = 0.0
    possible_ends: list[tuple[float, float]] = []
    for i, prob in enumerate(np.asarray(probs, dtype=np.float64).tolist()):
        s = float(frame_samples * i)
        if prob >= threshold and temp_end:  # speech resumed after a dip
            if s - temp_end > min_sil_max:
                possible_ends.append((temp_end, s - temp_end))
            temp_end = 0.0
            if next_start < prev_end:
                next_start = s
        if prob >= threshold and not triggered:  # speech starts
            triggered, cur = True, s
            continue
        if triggered and cur is not None and s - cur > max_speech:  # forced split
            if use_max_poss and possible_ends:
                prev_end, dur = max(possible_ends, key=lambda t: t[1])
                speeches.append([cur, prev_end])
                cur = None
                next_start = prev_end + dur
                if next_start < prev_end + s:  # upstream quirk: effectively always true
                    cur = next_start
                else:
                    triggered = False
                prev_end = next_start = temp_end = 0.0
                possible_ends = []
            elif prev_end:
                speeches.append([cur, prev_end])
                cur = None
                if next_start < prev_end:
                    triggered = False
                else:
                    cur = next_start
                prev_end = next_start = temp_end = 0.0
                possible_ends = []
            else:
                speeches.append([cur, s])
                cur = None
                prev_end = next_start = temp_end = 0.0
                triggered = False
                possible_ends = []
                continue
        if prob < neg_threshold and triggered:  # silence while in speech
            if not temp_end:
                temp_end = s
            if not use_max_poss and s - temp_end > min_sil_max:
                prev_end = temp_end
            if s - temp_end < min_sil:
                continue
            if cur is not None and temp_end - cur > min_speech:
                speeches.append([cur, temp_end])
            cur = None
            prev_end = next_start = temp_end = 0.0
            triggered = False
            possible_ends = []
            continue
    if cur is not None and n_samples - cur > min_speech:
        speeches.append([cur, float(n_samples)])

    for i, sp in enumerate(speeches):  # padding pass
        if i == 0:
            sp[0] = int(max(0, sp[0] - pad))
        if i != len(speeches) - 1:
            gap = speeches[i + 1][0] - sp[1]
            if gap < 2 * pad:
                sp[1] += int(gap // 2)
                speeches[i + 1][0] = int(max(0, speeches[i + 1][0] - gap // 2))
            else:
                sp[1] = int(min(n_samples, sp[1] + pad))
                speeches[i + 1][0] = int(max(0, speeches[i + 1][0] - pad))
        else:
            sp[1] = int(min(n_samples, sp[1] + pad))
    return [(int(a), int(b)) for a, b in speeches if b > a]


def _frame_count(n_samples: int, frame_samples: int = FRAME_SAMPLES) -> int:
    return -(-n_samples // frame_samples)


# --------------------------------------------------------------------------- silero


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_silero_model(
    models_dir: Path | str,
    *,
    url: str = SILERO_URL,
    sha256: str = SILERO_SHA256,
    filename: str = SILERO_FILENAME,
    timeout_s: float = 60.0,
) -> Path:
    """Path of the pinned Silero model in `models_dir`, downloaded (and checked) if missing.

    The file is written to a temp file next to it and renamed, so concurrent workers and
    crashes never leave a partial model behind. A file with the wrong hash is replaced.
    """
    models_dir = Path(models_dir)
    path = models_dir / filename
    if path.is_file():
        if _sha256(path) == sha256:
            return path
        log.warning("vad model has the wrong sha256, downloading it again", path=str(path))
    models_dir.mkdir(parents=True, exist_ok=True)
    log.info("downloading vad model", url=url, path=str(path))
    with urllib.request.urlopen(url, timeout=timeout_s) as response:  # pinned URL + sha256
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != sha256:
        raise ModelDownloadError(f"vad model sha256 mismatch: got {digest}, want {sha256}")
    fd, tmp = tempfile.mkstemp(dir=models_dir, prefix=f".{filename}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


class SileroVad:
    """Silero VAD v6.2 on onnxruntime (CPU, one thread: ~0.3 s per audio-minute).

    Give a local `model_path`, or a `models_dir` to download the pinned model into on first
    use (or call `load()` at startup). Thread-safe: no state is kept between calls.
    """

    name = f"silero-vad-v{SILERO_VERSION}"

    def __init__(
        self,
        models_dir: Path | str | None = None,
        *,
        model_path: Path | str | None = None,
        params: VadParams | None = None,
    ) -> None:
        if models_dir is None and model_path is None:
            raise ValueError("give models_dir or model_path")
        self.models_dir = Path(models_dir) if models_dir is not None else None
        self.model_path = Path(model_path) if model_path is not None else None
        self.params = params or VadParams()
        self._session: Any = None
        self._lock = threading.Lock()

    def load(self) -> None:
        """Download (if needed) and load the model now instead of on the first call."""
        self._get_session()

    def _get_session(self) -> Any:
        with self._lock:
            if self._session is None:
                import onnxruntime as ort

                path = self.model_path
                if path is None:
                    assert self.models_dir is not None
                    path = ensure_silero_model(self.models_dir)
                options = ort.SessionOptions()
                options.intra_op_num_threads = 1
                options.inter_op_num_threads = 1
                options.log_severity_level = 3
                self._session = ort.InferenceSession(
                    str(path), sess_options=options, providers=["CPUExecutionProvider"]
                )
            return self._session

    def frame_probs(self, pcm: Pcm) -> npt.NDArray[np.float32]:
        """Speech probability per 512-sample frame (last frame zero-padded); fresh state."""
        session = self._get_session()
        n_frames = _frame_count(len(pcm))
        # buf = 64 zeros (context of frame 0) + audio + zero padding: the model input for
        # frame i is buf[512 i : 512 i + 576] (previous frame's last 64 samples + frame i).
        buf = np.zeros(CONTEXT_SAMPLES + n_frames * FRAME_SAMPLES, dtype=np.float32)
        buf[CONTEXT_SAMPLES : CONTEXT_SAMPLES + len(pcm)] = pcm
        state = np.zeros((2, 1, 128), dtype=np.float32)
        sr = np.array(SAMPLE_RATE, dtype=np.int64)
        out = np.empty(n_frames, dtype=np.float32)
        width = CONTEXT_SAMPLES + FRAME_SAMPLES
        for i in range(n_frames):
            start = i * FRAME_SAMPLES
            inp = buf[start : start + width].reshape(1, width)
            prob, state = session.run(None, {"input": inp, "state": state, "sr": sr})
            out[i] = prob[0, 0]
        return out

    def detect(self, pcm: Pcm) -> VadResult:
        probs = self.frame_probs(pcm)
        return VadResult(
            n_samples=len(pcm),
            frame_samples=FRAME_SAMPLES,
            threshold=self.params.threshold,
            probs=probs,
            regions=tuple(speech_regions(probs, len(pcm), self.params)),
        )


# --------------------------------------------------------------------------- energy


def frame_rms_db(
    pcm: Pcm, frame_samples: int = FRAME_SAMPLES, floor_db: float = -120.0
) -> npt.NDArray[np.float64]:
    """RMS level (dBFS) per frame; the last, partial frame over its real samples only."""
    n_frames = _frame_count(len(pcm), frame_samples)
    out = np.full(n_frames, floor_db, dtype=np.float64)
    if n_frames == 0:
        return out
    x = np.asarray(pcm, dtype=np.float64)
    full = len(x) // frame_samples
    if full:
        sq = np.mean(x[: full * frame_samples].reshape(full, frame_samples) ** 2, axis=1)
        out[:full] = 10 * np.log10(np.maximum(sq, 10 ** (floor_db / 10)))
    if full < n_frames:
        sq_last = float(np.mean(x[full * frame_samples :] ** 2))
        out[full] = 10 * np.log10(max(sq_last, 10 ** (floor_db / 10)))
    return out


class EnergyVad:
    """Loudness threshold as a voice-activity detector: frames louder than `threshold_db`
    are "speech". No model needed; used by tests and as a fallback (it can't tell speech from
    music or game sound)."""

    name = "energy-vad"

    def __init__(
        self,
        params: VadParams | None = None,
        *,
        threshold_db: float = -40.0,
        softness_db: float = 2.0,
    ) -> None:
        self.params = params or VadParams()
        self.threshold_db = threshold_db
        self.softness_db = softness_db

    def frame_probs(self, pcm: Pcm) -> npt.NDArray[np.float32]:
        """A probability-like score per frame: 0.5 at `threshold_db`, a logistic around it."""
        db = frame_rms_db(pcm)
        z = np.clip((db - self.threshold_db) / self.softness_db, -50.0, 50.0)
        probs = 1.0 / (1.0 + np.exp(-z))
        # Map the logistic's 0.5 onto the configured threshold, so `threshold` keeps meaning
        # "speech from here on" whatever value it has.
        t = self.params.threshold
        mapped = np.where(probs >= 0.5, t + (probs - 0.5) * 2 * (1 - t), probs * 2 * t)
        return mapped.astype(np.float32)

    def detect(self, pcm: Pcm) -> VadResult:
        probs = self.frame_probs(pcm)
        return VadResult(
            n_samples=len(pcm),
            frame_samples=FRAME_SAMPLES,
            threshold=self.params.threshold,
            probs=probs,
            regions=tuple(speech_regions(probs, len(pcm), self.params)),
        )
