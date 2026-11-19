"""Deterministic engines for tests: "speech" made of sine bursts, recognised from the audio.

Each word is a tone burst whose frequency encodes the word id (`300 + 20 * id` Hz, text
`w{id}`); a short burst at `FULL_STOP_HZ` ends a sentence (the word before it gets a "."). The
fake engine finds bursts by energy and their frequency by FFT, so the same audio gives the same
words wherever a window is cut, and survives AAC encoding. Times are accurate to ~10 ms.

    pcm, expected = synth_speech("w1 w2 w3 . _1.5 w4 w5 .")
    engines = Engines(speech=FakeSpeechEngine(), vad=EnergyVad(), tagger=FakeSoundTagger())
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from clipper_core.contracts import SoundGroup
from clipper_transcriber.audio import SAMPLE_RATE, Pcm, encode_aac
from clipper_transcriber.engines import (
    BaseSpeechEngine,
    RecognizedWord,
    SecondScores,
    SpeechResult,
    SpeechSegment,
)
from clipper_transcriber.pipeline import ends_sentence, loudness_db
from clipper_transcriber.vad import frame_rms_db

WORD_BASE_HZ = 300.0
WORD_STEP_HZ = 20.0
MAX_WORD_ID = 99
FULL_STOP_HZ = 200.0
FRAME = 160  # 10 ms analysis frames
N_FFT = 16384  # ~1 Hz bins


def word_hz(word_id: int) -> float:
    if not 0 <= word_id <= MAX_WORD_ID:
        raise ValueError(f"word id must be 0..{MAX_WORD_ID}, got {word_id}")
    return WORD_BASE_HZ + WORD_STEP_HZ * word_id


def tone(freq: float, seconds: float, *, amplitude: float = 0.3, fade_s: float = 0.005) -> Pcm:
    """A sine burst with short fades (no clicks)."""
    n = round(seconds * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    x = amplitude * np.sin(2 * np.pi * freq * t)
    fade = min(round(fade_s * SAMPLE_RATE), n // 2)
    if fade:
        ramp = np.linspace(0.0, 1.0, fade)
        x[:fade] *= ramp
        x[n - fade :] *= ramp[::-1]
    return x.astype(np.float32)


def silence(seconds: float) -> Pcm:
    return np.zeros(round(seconds * SAMPLE_RATE), dtype=np.float32)


def synth_speech(
    script: str,
    *,
    lead_s: float = 0.5,
    word_s: float = 0.3,
    stop_s: float = 0.12,
    gap_s: float = 0.12,
    trail_s: float = 0.5,
    total_s: float | None = None,
    amplitude: float = 0.3,
) -> tuple[Pcm, list[RecognizedWord]]:
    """Audio for a script and the words the fake engine should find in it.

    Tokens: `w{id}` a word, `.` a full stop (ends the sentence), `_{seconds}` extra silence.
    Bursts are separated by `gap_s`; `total_s` pads or cuts the result.
    """
    parts: list[Pcm] = [silence(lead_s)]
    pos = lead_s
    words: list[RecognizedWord] = []
    for token in script.split():
        if token.startswith("_"):
            seconds = float(token[1:])
            parts.append(silence(seconds))
            pos += seconds
            continue
        if token == ".":
            freq, dur = FULL_STOP_HZ, stop_s
            if words and not words[-1].text.endswith("."):
                last = words[-1]
                words[-1] = RecognizedWord(last.text + ".", last.start, last.end)
        elif token.startswith("w"):
            freq, dur = word_hz(int(token[1:])), word_s
            words.append(RecognizedWord(token, round(pos, 4), round(pos + dur, 4)))
        else:
            raise ValueError(f"unknown script token {token!r}")
        parts += [tone(freq, dur, amplitude=amplitude), silence(gap_s)]
        pos += dur + gap_s
    parts.append(silence(trail_s))
    pcm = np.concatenate(parts)
    if total_s is not None:
        n = round(total_s * SAMPLE_RATE)
        pcm = np.pad(pcm, (0, max(0, n - len(pcm))))[:n]
        words = [w for w in words if w.start < total_s]
    return pcm, words


def synth_to_aac(script: str, path: Path | str, *, ffmpeg: str = "ffmpeg", **kwargs) -> Pcm:
    """`synth_speech` written as an ADTS .aac file (like the recorder's); returns the PCM."""
    pcm, _ = synth_speech(script, **kwargs)
    encode_aac(pcm, path, ffmpeg=ffmpeg)
    return pcm


def find_bursts(
    pcm: Pcm, *, threshold_db: float = -35.0, merge_gap_s: float = 0.03, min_s: float = 0.06
) -> list[tuple[int, int]]:
    """Sample ranges of loud stretches (10 ms frames above `threshold_db`)."""
    active = frame_rms_db(pcm, FRAME) > threshold_db
    runs: list[list[int]] = []
    for i in np.flatnonzero(active).tolist():
        if runs and i - runs[-1][1] <= round(merge_gap_s * SAMPLE_RATE / FRAME):
            runs[-1][1] = i + 1
        else:
            runs.append([i, i + 1])
    min_frames = round(min_s * SAMPLE_RATE / FRAME)
    return [(a * FRAME, min(b * FRAME, len(pcm))) for a, b in runs if b - a >= min_frames]


def dominant_hz(x: Pcm) -> float:
    """Frequency of the strongest spectral peak (Hann window, interpolated)."""
    trim = len(x) // 5 if len(x) > SAMPLE_RATE // 10 else 0  # skip the edges of long bursts
    seg = np.asarray(x[trim : len(x) - trim], dtype=np.float64)
    spectrum = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), n=N_FFT))
    k = int(np.argmax(spectrum[1:-1])) + 1
    a, b, c = spectrum[k - 1], spectrum[k], spectrum[k + 1]
    denom = a - 2 * b + c
    shift = 0.5 * (a - c) / denom if denom else 0.0
    return (k + shift) * SAMPLE_RATE / N_FFT


def classify_burst(freq: float) -> str | None:
    """ "." for a full stop, `w{id}` for a word, None for anything else."""
    if abs(freq - FULL_STOP_HZ) < WORD_STEP_HZ / 2:
        return "."
    word_id = round((freq - WORD_BASE_HZ) / WORD_STEP_HZ)
    if 0 <= word_id <= MAX_WORD_ID and abs(freq - word_hz(word_id)) < WORD_STEP_HZ / 2:
        return f"w{word_id}"
    return None


class FakeSpeechEngine(BaseSpeechEngine):
    """Recognises `synth_speech` audio. `segments=True` also returns engine segments (split at
    full stops and pauses), like a whisper-style engine; otherwise words only."""

    name = "fake-tones"

    def __init__(self, *, segments: bool = False, pause_s: float = 1.0) -> None:
        self.segments = segments
        self.pause_s = pause_s
        self.calls: list[int] = []  # length in samples of each input, for tests
        self.batches: list[int] = []  # inputs per transcribe_many call

    def transcribe_many(self, pcms: Sequence[Pcm]) -> list[SpeechResult]:
        self.batches.append(len(pcms))
        return super().transcribe_many(pcms)

    def transcribe(self, pcm: Pcm) -> SpeechResult:
        self.calls.append(len(pcm))
        words: list[RecognizedWord] = []
        for a, b in find_bursts(pcm):
            label = classify_burst(dominant_hz(pcm[a:b]))
            if label == ".":
                if words and not words[-1].text.endswith("."):
                    last = words[-1]
                    words[-1] = RecognizedWord(last.text + ".", last.start, last.end, 0.9)
            elif label is not None:
                words.append(RecognizedWord(label, a / SAMPLE_RATE, b / SAMPLE_RATE, 0.9))
        if not self.segments:
            return SpeechResult(words=words)
        groups: list[list[RecognizedWord]] = []
        for w in words:
            if (
                not groups
                or ends_sentence(groups[-1][-1].text)
                or (w.start - groups[-1][-1].end > self.pause_s)
            ):
                groups.append([])
            groups[-1].append(w)
        segments = [
            SpeechSegment(
                start=g[0].start,
                end=g[-1].end,
                text=" ".join(w.text for w in g),
                avg_logprob=-0.1,
                no_speech_prob=0.01,
            )
            for g in groups
        ]
        return SpeechResult(words=words, segments=segments)


class FakeSoundTagger:
    """Scores from loudness: `speech` rises from -60 dBFS (0) to 0 dBFS (1), `shouting` from
    -20 dBFS; every other group 0. One entry per (partial) second of the input."""

    name = "fake-loudness"

    def __init__(self) -> None:
        self.calls: list[int] = []

    def classify(self, pcm: Pcm) -> Sequence[SecondScores]:
        self.calls.append(len(pcm))
        out: list[SecondScores] = []
        for i in range(math.ceil(len(pcm) / SAMPLE_RATE)):
            db = loudness_db(pcm[i * SAMPLE_RATE : (i + 1) * SAMPLE_RATE])
            scores = dict.fromkeys(SoundGroup, 0.0)
            scores[SoundGroup.SPEECH] = min(max((db + 60) / 60, 0.0), 1.0)
            scores[SoundGroup.SHOUTING] = min(max((db + 20) / 20, 0.0), 1.0)
            out.append(scores)
        return out
