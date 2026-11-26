"""Speech engine: Whisper through faster-whisper (CTranslate2), large-v3-turbo on CUDA (§16).

    engine = WhisperEngine.load(WhisperOptions())     # downloads/loads the model, warms up CUDA
    result = engine.transcribe(pcm)                   # one gated stretch of speech, <= ~30 s

faster-whisper is the optional `whisper` extra and imported only in `load`. The model is given
16 kHz mono float32 arrays, never file paths (its own PyAV file reading broke with current
PyAV). Weights come from the Hugging Face cache (`HF_HOME`, a volume in Docker) at a pinned
revision: "large-v3-turbo" is mobiuslabsgmbh/faster-whisper-large-v3-turbo at the benchmarked
commit.

Stretches are transcribed one by one (`BaseSpeechEngine.transcribe_many`). faster-whisper's
`BatchedInferencePipeline` (stretches as `clip_timestamps`, timestamps on) was only 1.2x faster
on the benchmark audio (79 stretches, 20 min), changed 1-2 % of the words and drops the
temperature fallback, so it isn't used (4b-3 e2e, DESIGN.md §16).

A call that runs out of GPU memory (a game holding the VRAM) is retried in-process for up to
`oom_wait_s` (`gpu.retry_on_oom`) before the job attempt fails.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from clipper_core.logs import get_logger
from clipper_transcriber.audio import SAMPLE_RATE, Pcm
from clipper_transcriber.engines import (
    BaseSpeechEngine,
    RecognizedWord,
    SpeechResult,
    SpeechSegment,
    require,
    route_hf_logging,
)
from clipper_transcriber.gpu import OOM_WAIT_S, oom_delays, retry_on_oom

log = get_logger(__name__)

# faster-whisper's default temperatures (the fallback, off by default); 0.0 alone without it.
FALLBACK_TEMPERATURES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)

# Hub revisions of the models we benchmarked: weights never change under a stored model id.
WHISPER_REVISIONS = {
    # mobiuslabsgmbh/faster-whisper-large-v3-turbo, as in tools/asr-bench (2026-11-19)
    "large-v3-turbo": "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
}


@dataclass(frozen=True, slots=True)
class WhisperOptions:
    """Model and decoding options (DESIGN.md §15/§16: turbo, float16, beam 5, word timestamps,
    no conditioning on the previous text)."""

    model: str = "large-v3-turbo"  # faster-whisper size name, Hub repo id or local directory
    # Hub commit; None = the pinned one of a known model (WHISPER_REVISIONS), else the latest.
    revision: str | None = None
    device: str = "cuda"
    compute_type: str = "float16"
    language: str = "en"
    beam_size: int = 5
    # faster-whisper's own Silero VAD inside each stretch, on top of our gate in front: no
    # difference on the benchmark audio, slightly slower (DESIGN.md §16, 4b-3).
    vad_filter: bool = False
    # faster-whisper's temperature fallback: a stretch whose decode looks bad (compression ratio
    # > 2.4, avg_logprob < -1) is decoded again with sampling at rising temperatures. Off (beam
    # search at temperature 0 only): the same text on every run (retries, re-plans), and on the
    # benchmark audio the same words, 3-5 % faster; the fallback's sampled decodes only made
    # some runs differ (DESIGN.md §16, 2026-11-20). Without it, a low-scoring segment that
    # isn't a repetition loop is kept as decoded; the loop filter only drops repetition runs.
    temperature_fallback: bool = False
    cpu_threads: int = 0  # CTranslate2 CPU threads (0 = its default); matters on device "cpu"
    oom_wait_s: float = OOM_WAIT_S  # how long a call waits for GPU memory before failing

    @property
    def pinned_revision(self) -> str | None:
        return self.revision or WHISPER_REVISIONS.get(self.model)

    @property
    def model_id(self) -> str:
        """Stored in chunk_transcripts.model / words files: model and short revision."""
        rev = self.pinned_revision
        return f"faster-whisper/{self.model}" + (f"@{rev[:7]}" if rev else "")


def result_from_segments(segments: Iterable[Any]) -> SpeechResult:
    """faster-whisper `Segment`s (with `words`) -> words with their probability, and engine
    segments with avg_logprob, no_speech_prob, compression_ratio. Times stay relative to the
    input; `pipeline.map_result` clamps and cleans them."""
    words: list[RecognizedWord] = []
    out: list[SpeechSegment] = []
    for s in segments:
        for w in s.words or ():
            text = w.word.strip()
            if text:
                words.append(RecognizedWord(text, float(w.start), float(w.end), w.probability))
        out.append(
            SpeechSegment(
                start=float(s.start),
                end=float(s.end),
                text=s.text.strip(),
                avg_logprob=s.avg_logprob,
                no_speech_prob=s.no_speech_prob,
                compression_ratio=s.compression_ratio,
            )
        )
    return SpeechResult(words=words, segments=out)


class WhisperEngine(BaseSpeechEngine):
    """faster-whisper `WhisperModel`, loaded once per process. Calls are serialised (one
    CTranslate2 model, one GPU). `stopping` (set on shutdown) ends a wait for GPU memory."""

    def __init__(self, model: Any, options: WhisperOptions) -> None:
        self._model = model
        self.options = options
        self.name = options.model_id
        self.device = options.device
        self.stopping = threading.Event()
        self._lock = threading.Lock()

    @classmethod
    def load(cls, options: WhisperOptions | None = None, *, warm_up: bool = True) -> WhisperEngine:
        """Load the model (downloads it into the Hugging Face cache on first use) and run one
        warm-up decode, so a broken CUDA setup fails at start, not on the first job."""
        o = options or WhisperOptions()
        faster_whisper = require("faster_whisper", "whisper")
        route_hf_logging()
        log.info("loading speech model", **asdict(o), pinned_revision=o.pinned_revision)
        model = faster_whisper.WhisperModel(
            o.model,
            device=o.device,
            compute_type=o.compute_type,
            cpu_threads=o.cpu_threads,
            revision=o.pinned_revision,
        )
        engine = cls(model, o)
        if warm_up:
            noise = np.random.default_rng(0).normal(0.0, 0.01, SAMPLE_RATE).astype(np.float32)
            engine._run(noise, vad_filter=False)
        return engine

    def transcribe(self, pcm: Pcm) -> SpeechResult:
        if len(pcm) == 0:
            return SpeechResult(words=[], segments=[])
        return retry_on_oom(
            lambda: self._run(pcm, vad_filter=self.options.vad_filter),
            what=self.name,
            stopping=self.stopping,
            delays=oom_delays(self.options.oom_wait_s),
        )

    def _run(self, pcm: Pcm, *, vad_filter: bool) -> SpeechResult:
        audio = np.ascontiguousarray(pcm, dtype=np.float32)
        o = self.options
        with self._lock:
            segments, _info = self._model.transcribe(
                audio,
                language=o.language,
                beam_size=o.beam_size,
                word_timestamps=True,
                vad_filter=vad_filter,
                condition_on_previous_text=False,
                temperature=list(FALLBACK_TEMPERATURES) if o.temperature_fallback else 0.0,
            )
            # A generator: decoding happens while it is consumed, so consume it under the lock.
            return result_from_segments(list(segments))
