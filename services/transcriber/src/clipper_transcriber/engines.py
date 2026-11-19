"""Engine interfaces: speech recognition, sound tagging, voice-activity detection.

Engines are synchronous and blocking (GPU/CPU work); the pipeline runs them in a worker thread.
They get 16 kHz mono float32 PCM (`audio.Pcm`) and return times in seconds relative to the
start of the array they were given. Adapters for real models (faster-whisper, Parakeet,
Qwen3-ASR, AST, CED, ...) implement these protocols; `fakes` has deterministic test engines.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np
import numpy.typing as npt

from clipper_core.contracts import SoundGroup
from clipper_transcriber.audio import SAMPLE_RATE, Pcm

# --------------------------------------------------------------------------- speech


@dataclass(frozen=True, slots=True)
class RecognizedWord:
    """A word with times in seconds (relative to the engine's input, or to the window start in
    pipeline results). `text` carries its punctuation ("Hello," / "now."), no outer spaces."""

    text: str
    start: float
    end: float
    confidence: float | None = None  # word probability 0..1, if the engine gives one


@dataclass(frozen=True, slots=True)
class SpeechSegment:
    """A sentence-like segment as the engine produced it. The pipeline only uses its time span
    (to group words) and its quality scores; the text is rebuilt from the words."""

    start: float
    end: float
    text: str = ""
    avg_logprob: float | None = None  # whisper-style decoder score
    no_speech_prob: float | None = None  # whisper-style "this is not speech" probability
    confidence: float | None = None  # any other 0..1 score the engine has
    compression_ratio: float | None = None  # whisper: gzip ratio of the text (loops: > 2.4)


@dataclass(frozen=True, slots=True)
class SpeechResult:
    """Words in time order. `segments` None = the engine doesn't group words into sentences
    (the pipeline then splits on punctuation and pauses)."""

    words: Sequence[RecognizedWord]
    segments: Sequence[SpeechSegment] | None = None


@runtime_checkable
class SpeechEngine(Protocol):
    """Speech recognition, English. Must return word timestamps (captions, ownership rule).

    The pipeline calls `transcribe_many` once per window with all its speech stretches, so a
    batching adapter can run them together; `BaseSpeechEngine` gives a sequential default.
    """

    @property
    def name(self) -> str:
        """Model id, written to `chunk_transcripts.model` (e.g. "faster-whisper/large-v3")."""
        ...

    def transcribe(self, pcm: Pcm) -> SpeechResult:
        """One stretch of speech (<= ~30 s, cut out by the voice-activity gate)."""
        ...

    def transcribe_many(self, pcms: Sequence[Pcm]) -> list[SpeechResult]:
        """Several independent stretches; one result per input, in order."""
        ...


class BaseSpeechEngine(ABC):
    """Adapters implement `name` and `transcribe`; `transcribe_many` runs them one by one
    unless the adapter batches."""

    name: str

    @abstractmethod
    def transcribe(self, pcm: Pcm) -> SpeechResult: ...

    def transcribe_many(self, pcms: Sequence[Pcm]) -> list[SpeechResult]:
        return [self.transcribe(pcm) for pcm in pcms]


def transcribe_all(engine: SpeechEngine, pcms: Sequence[Pcm]) -> list[SpeechResult]:
    """`engine.transcribe_many(pcms)`, or one `transcribe` call per input for an engine
    without it."""
    many = getattr(engine, "transcribe_many", None)
    if many is None:
        return [engine.transcribe(pcm) for pcm in pcms]
    return list(many(pcms))


# --------------------------------------------------------------------------- sound tagging

# One second of sound-class scores: group -> 0..1. Groups a model can't score are left out.
SecondScores = Mapping[SoundGroup, float]

# AudioSet labels per group; a group's score is the max over its labels (as in
# tools/asr-bench/run_tagger.py). Taggers built on AudioSet classifiers use `group_scores`.
AUDIOSET_GROUPS: Mapping[SoundGroup, tuple[str, ...]] = {
    SoundGroup.LAUGHTER: (
        "Laughter",
        "Baby laughter",
        "Giggle",
        "Snicker",
        "Belly laugh",
        "Chuckle, chortle",
    ),
    SoundGroup.SCREAMING: ("Screaming",),
    SoundGroup.SHOUTING: ("Shout", "Yell", "Battle cry", "Children shouting"),
    SoundGroup.CHEERING: ("Cheering", "Applause", "Crowd"),
    SoundGroup.GASP: ("Gasp",),
    SoundGroup.CRYING: ("Crying, sobbing", "Whimper"),
    SoundGroup.MUSIC: ("Music",),
    SoundGroup.SINGING: ("Singing",),
    SoundGroup.SPEECH: ("Speech",),
    SoundGroup.GUNFIRE_EXPLOSION: ("Gunshot, gunfire", "Explosion", "Burst, pop"),
}


def group_scores(label_scores: Mapping[str, float]) -> dict[SoundGroup, float]:
    """AudioSet label -> probability, to group -> max over its labels (groups without any of
    their labels in `label_scores` are left out)."""
    out: dict[SoundGroup, float] = {}
    for group, labels in AUDIOSET_GROUPS.items():
        values = [label_scores[label] for label in labels if label in label_scores]
        if values:
            out[group] = float(max(values))
    return out


@runtime_checkable
class SoundTagger(Protocol):
    @property
    def name(self) -> str: ...

    def classify(self, pcm: Pcm) -> Sequence[SecondScores]:
        """One entry per second of `pcm`: entry i scores `pcm[i * 16000 : (i + 1) * 16000]`
        (the last second may be partial), so `ceil(len(pcm) / 16000)` entries. The model picks
        its own windows (AST 10 s, CED 2 s) but reports per second of its input. The pipeline
        starts `pcm` on a UTC second, with up to a few seconds of context before the seconds it
        needs (their entries are discarded)."""
        ...


# --------------------------------------------------------------------------- voice activity


@dataclass(frozen=True, slots=True)
class VadResult:
    """Voice activity over one array: a probability per frame and the speech regions.

    Frame i covers samples `[i * frame_samples, (i + 1) * frame_samples)` (the last one is
    zero-padded). `regions` are sample ranges `[start, end)`, sorted and disjoint.
    """

    n_samples: int
    frame_samples: int
    threshold: float  # a frame with prob >= threshold is speech
    probs: npt.NDArray[np.float32]
    regions: tuple[tuple[int, int], ...] = field(default=())

    def speech_mask(self) -> npt.NDArray[np.bool_]:
        """Per sample: inside a frame whose prob >= threshold."""
        flags = np.asarray(self.probs) >= self.threshold
        return np.repeat(flags, self.frame_samples)[: self.n_samples]

    def regions_s(self) -> list[tuple[float, float]]:
        return [(a / SAMPLE_RATE, b / SAMPLE_RATE) for a, b in self.regions]


@runtime_checkable
class VoiceActivityDetector(Protocol):
    @property
    def name(self) -> str: ...

    def detect(self, pcm: Pcm) -> VadResult:
        """Independent audio: state starts fresh on every call."""
        ...


# --------------------------------------------------------------------------- bundle


@dataclass(frozen=True, slots=True)
class Engines:
    """What the pipeline runs. One instance per worker process (models load once)."""

    speech: SpeechEngine
    vad: VoiceActivityDetector
    tagger: SoundTagger | None = None
