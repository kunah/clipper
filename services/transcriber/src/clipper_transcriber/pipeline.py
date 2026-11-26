"""One window of audio -> words, sentence segments, provisional tail, per-second audio metrics.

A window is PCM whose sample 0 is `Window.start_utc` (the words file's `t0`, the transcript's
`owned_from`): the chunk's own audio, possibly with the previous chunk's provisional tail in
front (DESIGN.md §16). Everything in a `WindowResult` is in window offsets (seconds from
`start_utc`), except the metrics rows, which are whole UTC seconds. Offsets are on a whole
microsecond grid, the precision of Postgres timestamps, so `start_utc + offset` is exact and
comparisons with `owned_to` hold after the conversion.

Steps (`process_window`): voice activity over the whole window -> speech stretches (merged,
padded, <= ~30 s) -> speech engine, all stretches in one `transcribe_many` call -> words mapped
back to window offsets and clamped to the window -> sentences (the engine's, or split on
punctuation and pauses) -> junk filter -> provisional tail -> metrics. Everything but the engine
calls is a pure function here.
"""

from __future__ import annotations

import asyncio
import bisect
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Protocol

import numpy as np

from clipper_core.contracts import SoundGroup
from clipper_core.logs import get_logger
from clipper_core.timeline import Interval, ensure_utc
from clipper_transcriber.audio import SAMPLE_RATE, Pcm, samples
from clipper_transcriber.engines import (
    Engines,
    RecognizedWord,
    SecondScores,
    SpeechResult,
    SpeechSegment,
    VadResult,
    transcribe_all,
)

log = get_logger(__name__)

US = 1_000_000  # microseconds per second
ONE_US = 1 / US


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    gate_merge_gap_s: float = 1.0  # speech regions closer than this go to the engine together
    gate_pad_s: float = 0.2  # audio added around each stretch; also the tail's lead-in
    gate_max_s: float = 30.0  # longest stretch sent to the engine in one piece
    sentence_max_s: float = 30.0
    sentence_pause_s: float = 1.0  # a longer pause between words ends a sentence
    word_speech_tolerance_s: float = 0.2  # a word this far from all detected speech is junk
    phantom_max_speech: float = 0.8  # "thanks for watching" is junk below this speech share
    silence_phantom_max_speech: float = 0.5  # "thank you" alone is junk below this speech share
    loop_ngram: int = 3  # a repetition loop: this n-gram recurring ...
    loop_min_repeats: int = 4  # ... this many times in a row ...
    loop_max_period: int = 20  # ... every <= this many words; when the engine scores segments,
    loop_min_compression_ratio: float = 2.4  # ... also needs a ratio above this ...
    loop_max_avg_logprob: float = -1.0  # ... or an avg_logprob below this
    tail_margin_s: float = 0.5  # speech ending this close to the window end runs into the cut
    tagger_context_s: int = 2  # whole seconds of audio before the first metric second
    loudness_floor_db: float = -100.0  # digital silence is stored as this, never -inf

    def __post_init__(self) -> None:
        if not (math.isfinite(self.loudness_floor_db) and self.loudness_floor_db < 0):
            raise ValueError(f"loudness_floor_db must be finite and < 0: {self.loudness_floor_db}")
        if self.gate_max_s <= 2 * self.gate_pad_s:
            raise ValueError("gate_max_s must be longer than twice gate_pad_s")


def to_us(seconds: float) -> int:
    """Offset in seconds -> whole microseconds (the grid all offsets live on)."""
    return round(seconds * US)


def on_grid(seconds: float) -> float:
    return to_us(seconds) / US


def _finite(value: float | None) -> float | None:
    return float(value) if value is not None and math.isfinite(value) else None


def _unit(value: float | None) -> float | None:
    v = _finite(value)
    return None if v is None else min(max(v, 0.0), 1.0)


# --------------------------------------------------------------------------- input / output


@dataclass(frozen=True, slots=True)
class Window:
    """What to transcribe.

    - `pcm`: 16 kHz mono float32; sample 0 is at `start_utc`.
    - `own`: the chunk's own range `[start_utc, end_utc)` of the chunk row; it owns the metrics
      rows of the UTC seconds whose middle (ts + 0.5 s) lies in it.
    - `owned_to`: words are kept and clamped up to here (default: the end of `pcm`); earlier on a
      re-run while the next contiguous chunk is already transcribed. Audio after it still goes
      to the engine as context and into the metrics.
    - `keep_tail`: False when the next chunk already took over (no provisional tail).
    """

    pcm: Pcm
    start_utc: datetime
    own: Interval
    owned_to: datetime | None = None
    keep_tail: bool = True

    def __post_init__(self) -> None:
        pcm = np.asarray(self.pcm, dtype=np.float32)
        if pcm.ndim != 1:
            raise ValueError(f"pcm must be 1-D (mono), got shape {pcm.shape}")
        object.__setattr__(self, "pcm", pcm)
        object.__setattr__(self, "start_utc", ensure_utc(self.start_utc))
        if self.owned_to is not None:
            owned_to = ensure_utc(self.owned_to)
            if owned_to < self.start_utc:
                raise ValueError(f"owned_to {owned_to} is before the window start")
            object.__setattr__(self, "owned_to", owned_to)

    @property
    def duration_s(self) -> float:
        return len(self.pcm) / SAMPLE_RATE

    @property
    def end_us(self) -> int:
        """Window end in whole microseconds: `owned_to`, but never past the audio."""
        audio_us = len(self.pcm) * US // SAMPLE_RATE  # floor: inside the audio
        if self.owned_to is None:
            return audio_us
        return min((self.owned_to - self.start_utc) // timedelta(microseconds=1), audio_us)

    @property
    def end_s(self) -> float:
        return self.end_us / US

    def offset(self, ts: datetime) -> float:
        return (ensure_utc(ts) - self.start_utc) / timedelta(microseconds=1) / US

    def utc(self, offset_s: float) -> datetime:
        return self.start_utc + timedelta(microseconds=to_us(offset_s))


@dataclass(frozen=True, slots=True)
class Segment:
    """A sentence: its words (window offsets) and the engine's quality scores, if any."""

    words: tuple[RecognizedWord, ...]
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    confidence: float | None = None  # engine's segment score, else mean word confidence
    compression_ratio: float | None = None  # engine's (whisper), for the loop filter; not stored
    provisional: bool = False  # the tail sentence: the next contiguous chunk will redo it

    def __post_init__(self) -> None:
        if not self.words:
            raise ValueError("a segment needs words")

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return max(w.end for w in self.words)

    @property
    def text(self) -> str:
        return join_words(self.words)


@dataclass(frozen=True, slots=True)
class MetricSecond:
    """One `audio_metrics_1s` row: `[ts, ts + 1 s)`."""

    ts: datetime
    loudness_db: float  # RMS dBFS, floored (finite) and <= 0
    speech_ratio: float  # share of the second in voice-activity frames, 0..1
    classes: Mapping[str, float] = field(default_factory=dict)  # SoundGroup value -> 0..1


@dataclass(frozen=True, slots=True)
class Timings:
    audio_s: float  # length of the window
    speech_input_s: float  # audio sent to the speech engine (after gating)
    vad_s: float
    speech_s: float
    tagger_s: float
    total_s: float

    @property
    def realtime_factor(self) -> float:
        """Audio seconds per processing second."""
        return self.audio_s / self.total_s if self.total_s > 0 else math.inf


@dataclass(frozen=True, slots=True)
class WindowResult:
    model: str
    start_utc: datetime  # = Window.start_utc: t0 of the words file, owned_from
    words: tuple[RecognizedWord, ...]  # window offsets, sorted; every start < owned_to
    segments: tuple[Segment, ...]  # every word is in exactly one segment
    tail_from: float | None  # window offset < owned_to; words at or after it are provisional
    metrics: tuple[MetricSecond, ...]
    speech_regions: tuple[tuple[float, float], ...]  # what went to the engine (offsets)
    timings: Timings

    def utc(self, offset_s: float) -> datetime:
        return self.start_utc + timedelta(microseconds=to_us(offset_s))

    @property
    def tail_from_utc(self) -> datetime | None:
        return None if self.tail_from is None else self.utc(self.tail_from)


# --------------------------------------------------------------------------- gating


def gate_regions(
    regions: Sequence[tuple[float, float]],
    total_s: float,
    *,
    merge_gap_s: float = 1.0,
    pad_s: float = 0.2,
    max_s: float = 30.0,
) -> list[tuple[float, float]]:
    """Voice-activity regions (seconds) -> the stretches to send to the speech engine.

    Regions closer than `merge_gap_s` are merged (sentence context), each stretch is padded by
    `pad_s` (half the gap when neighbours are closer than 2 * pad, so no audio is sent twice)
    and is at most `max_s` long (longer regions are split into equal parts).
    """
    inner_max = max(max_s - 2 * pad_s, max_s / 2)
    parts: list[tuple[float, float]] = []
    for a, b in sorted(regions):
        a, b = max(a, 0.0), min(b, total_s)
        if b <= a:
            continue
        n = math.ceil((b - a) / inner_max - 1e-9)
        step = (b - a) / n
        parts.extend((a + k * step, b if k == n - 1 else a + (k + 1) * step) for k in range(n))

    groups: list[list[float]] = []
    for a, b in parts:
        if groups and a - groups[-1][1] < merge_gap_s and b - groups[-1][0] <= inner_max:
            groups[-1][1] = max(groups[-1][1], b)
        else:
            groups.append([a, b])

    out: list[tuple[float, float]] = []
    for i, (a, b) in enumerate(groups):
        before = pad_s if i == 0 else min(pad_s, (a - groups[i - 1][1]) / 2)
        after = pad_s if i == len(groups) - 1 else min(pad_s, (groups[i + 1][0] - b) / 2)
        out.append((max(0.0, a - before), min(total_s, b + after)))
    return out


def map_result(
    result: SpeechResult, offset_s: float, length_s: float
) -> tuple[list[RecognizedWord], list[SpeechSegment] | None]:
    """Engine output for a stretch starting at `offset_s` -> window offsets (microsecond grid).

    Times are clamped to the stretch (engines sometimes overshoot the input); words without
    text or with non-finite times are dropped; scores are cleaned (finite or None, 0..1 where
    they are probabilities).
    """

    def clamp(t: float) -> float:
        return on_grid(min(max(float(t), 0.0), length_s) + offset_s)

    def finite(*values: float) -> bool:
        return all(math.isfinite(v) for v in values)

    words: list[RecognizedWord] = []
    for w in result.words:
        text = w.text.strip()
        if not text or not finite(w.start, w.end):
            continue
        start, end = clamp(w.start), clamp(w.end)
        words.append(RecognizedWord(text, start, max(start, end), _unit(w.confidence)))
    words.sort(key=lambda w: w.start)
    if result.segments is None:
        return words, None
    segments = []
    for s in result.segments:
        if not finite(s.start, s.end):
            continue
        start, end = clamp(s.start), clamp(s.end)
        ratio = _finite(s.compression_ratio)
        segments.append(
            replace(
                s,
                start=start,
                end=max(start, end),
                avg_logprob=_finite(s.avg_logprob),
                no_speech_prob=_unit(s.no_speech_prob),
                confidence=_unit(s.confidence),
                compression_ratio=ratio if ratio is not None and ratio >= 0 else None,
            )
        )
    segments.sort(key=lambda s: s.start)
    return words, segments


def clamp_words(words: Sequence[RecognizedWord], end_s: float) -> list[RecognizedWord]:
    """Words inside the window: starting before its end (in whole microseconds, as the owned
    range will be compared), ending at the latest there."""
    end_us = to_us(end_s)
    out = []
    for w in words:
        if to_us(w.start) >= end_us:
            continue
        start = on_grid(max(w.start, 0.0))
        out.append(replace(w, start=start, end=min(max(on_grid(w.end), start), end_us / US)))
    return out


# --------------------------------------------------------------------------- sentences


class WordLike(Protocol):
    """RecognizedWord, or `contracts.Word` from a words file."""

    @property
    def text(self) -> str: ...
    @property
    def start(self) -> float: ...
    @property
    def end(self) -> float: ...
    @property
    def confidence(self) -> float | None: ...


_CLOSERS = "\"')]}»”’"  # noqa: RUF001 - closing quotes/brackets after a full stop
_ABBREVIATIONS = frozenset(
    {"mr.", "mrs.", "ms.", "dr.", "st.", "vs.", "jr.", "sr.", "e.g.", "i.e."}
)


def ends_sentence(text: str) -> bool:
    """The word closes a sentence: ends with . ? ! (before closing quotes), not "Mr."."""
    t = text.strip().rstrip(_CLOSERS)
    return bool(t) and t[-1] in ".?!" and t.lower() not in _ABBREVIATIONS


def split_sentences[W: WordLike](
    words: Sequence[W], *, max_s: float = 30.0, pause_s: float = 1.0
) -> list[list[W]]:
    """Group words (in time order) into sentences: a sentence ends after a word ending with
    . ? !, before a pause longer than `pause_s`, and before a word that would make it longer
    than `max_s`."""
    sentences: list[list[W]] = []
    cur: list[W] = []
    for w in words:
        if cur and (
            ends_sentence(cur[-1].text)
            or w.start - cur[-1].end > pause_s
            or w.end - cur[0].start > max_s
        ):
            sentences.append(cur)
            cur = []
        cur.append(w)
    if cur:
        sentences.append(cur)
    return sentences


def join_words(words: Sequence[WordLike]) -> str:
    return " ".join(t for t in (w.text.strip() for w in words) if t)


def mean_confidence(words: Sequence[WordLike]) -> float | None:
    values = [w.confidence for w in words if w.confidence is not None]
    return round(sum(values) / len(values), 4) if values else None


def _as_recognized(w: WordLike) -> RecognizedWord:
    if isinstance(w, RecognizedWord):
        return w
    return RecognizedWord(w.text, w.start, w.end, w.confidence)


def segments_from_words(
    words: Sequence[WordLike], *, max_s: float = 30.0, pause_s: float = 1.0
) -> list[Segment]:
    """Sentences split from words alone (no engine scores). Also for rebuilding the sentences of
    a previous chunk from its words file."""
    return [
        Segment(tuple(_as_recognized(w) for w in group), confidence=mean_confidence(group))
        for group in split_sentences(words, max_s=max_s, pause_s=pause_s)
    ]


def segments_from_engine(
    words: Sequence[RecognizedWord], engine_segments: Sequence[SpeechSegment]
) -> list[Segment]:
    """Words grouped by the engine's segments: a word goes to the segment containing its
    midpoint, else to the nearest one. Segments that get no words are dropped."""
    if not engine_segments:
        return []
    groups: list[list[RecognizedWord]] = [[] for _ in engine_segments]
    for w in words:
        mid = (w.start + w.end) / 2
        distances = [max(s.start - mid, mid - s.end, 0.0) for s in engine_segments]
        groups[distances.index(min(distances))].append(w)
    out = []
    for seg, group in zip(engine_segments, groups, strict=True):
        if group:
            confidence = _unit(seg.confidence)
            out.append(
                Segment(
                    tuple(group),
                    avg_logprob=_finite(seg.avg_logprob),
                    no_speech_prob=_unit(seg.no_speech_prob),
                    confidence=confidence if confidence is not None else mean_confidence(group),
                    compression_ratio=_finite(seg.compression_ratio),
                )
            )
    return out


def build_segments(
    words: Sequence[RecognizedWord],
    engine_segments: Sequence[SpeechSegment] | None,
    *,
    max_s: float = 30.0,
    pause_s: float = 1.0,
) -> list[Segment]:
    if engine_segments:
        return segments_from_engine(words, engine_segments)
    return segments_from_words(words, max_s=max_s, pause_s=pause_s)


# --------------------------------------------------------------------------- junk filter


def normalize(text: str) -> str:
    """For matching: lower case, no apostrophes, other punctuation -> spaces, single spaces."""
    t = re.sub("['`’ʼ]", "", text.lower())  # noqa: RUF001 - apostrophes
    return " ".join(re.sub(r"[^\w\s]|_", " ", t).split())


def _phrases(*items: str) -> tuple[tuple[str, ...], ...]:
    return tuple(sorted({tuple(normalize(i).split()) for i in items}, key=len, reverse=True))


# Phrases speech models make up on silence/music (learned from YouTube subtitles). A sentence
# made only of these (joined by "and"/"please") is dropped unless clearly spoken.
PHANTOM_PHRASES = _phrases(
    "thanks for watching",
    "thank you for watching",
    "thank you so much for watching",
    "thank you very much for watching",
    "please subscribe",
    "please like and subscribe",
    "like and subscribe",
    "like comment and subscribe",
    "subscribe to my channel",
    "please subscribe to my channel",
    "don't forget to subscribe",
    "don't forget to like and subscribe",
    "don't forget to like comment and subscribe",
    "see you in the next video",
)
_PHANTOM_JOINERS = frozenset({"and", "please"})
# The same for a sentence starting with one of these ("Subtitles by the Amara.org community").
PHANTOM_PREFIXES = _phrases(
    "subtitles by",
    "subtitle by",
    "subtitled by",
    "subtitles made by",
    "captions by",
    "captioned by",
    "captioning by",
    "closed captioning by",
    "transcription by",
    "transcribed by",
    "translated by",
    "translation by",
    "amara.org",
)
# Real speech too ("thank you" for a sub), so dropped only when the voice-activity detector
# heard little speech under it.
SILENCE_PHANTOMS = frozenset(
    normalize(t)
    for t in (
        "you",
        "thank you",
        "thanks",
        "thank you very much",
        "thank you so much",
        "bye",
        "bye bye",
        "okay",
        "so",
        "oh",
        "hmm",
    )
)


def has_no_words(text: str) -> bool:
    """No letters or digits at all ("♪", "..."): always junk."""
    return not normalize(text)


def is_phantom(text: str) -> bool:
    """Known made-up text: only phantom phrases, or a phantom prefix."""
    tokens = normalize(text).split()
    if not tokens:
        return False
    if any(tuple(tokens[: len(p)]) == p for p in PHANTOM_PREFIXES):
        return True
    i, matched = 0, False
    while i < len(tokens):
        phrase = next((p for p in PHANTOM_PHRASES if tuple(tokens[i : i + len(p)]) == p), None)
        if phrase is not None:
            i += len(phrase)
            matched = True
        elif matched and tokens[i] in _PHANTOM_JOINERS:
            i += 1
        else:
            return False
    return matched


def is_silence_phantom(text: str) -> bool:
    return normalize(text) in SILENCE_PHANTOMS


def repetition_mask(
    tokens: Sequence[str], *, ngram: int = 3, min_repeats: int = 4, max_period: int = 20
) -> list[bool]:
    """Repetition loops (a decoder stuck on one phrase): True = drop this token.

    A loop is a run where the n-gram starting it recurs every `p` tokens (p <= max_period) at
    least `min_repeats` times in a row: "here" x6, "lets go" x5, a sentence x4. The first
    occurrence is kept (whole periods covering the n-gram), the rest of the run is dropped.
    """
    n = len(tokens)
    drop = [False] * n
    i = 0
    while i < n:
        loop_end = None
        for p in range(1, min(max_period, n - i - 1) + 1):
            j = i + p
            while j < n and tokens[j] == tokens[j - p]:
                j += 1
            if j - i >= ngram and (j - i - ngram) // p + 1 >= min_repeats:
                keep = p * math.ceil(ngram / p)
                for k in range(i + keep, j):
                    drop[k] = True
                loop_end = j
                break
        i = loop_end if loop_end is not None else i + 1
    return drop


def loop_suspect(segment: Segment, config: PipelineConfig) -> bool:
    """May the loop filter cut this segment? Without engine scores: yes. With them: only when
    they agree it's degenerate (high compression ratio or low avg_logprob)."""
    ratio, logprob = segment.compression_ratio, segment.avg_logprob
    if ratio is None and logprob is None:
        return True
    return (ratio is not None and ratio > config.loop_min_compression_ratio) or (
        logprob is not None and logprob < config.loop_max_avg_logprob
    )


class _SpeechIndex:
    """Voice-activity regions in seconds, for "is this time in speech" questions."""

    def __init__(self, vad: VadResult) -> None:
        self.regions = vad.regions_s()
        self.starts = [a for a, _ in self.regions]
        self.mask = vad.speech_mask()

    def near_speech(self, start: float, end: float, tolerance_s: float) -> bool:
        """Does `[start, end]` come within `tolerance_s` of a speech region?"""
        i = bisect.bisect_right(self.starts, end + tolerance_s) - 1
        return i >= 0 and start <= self.regions[i][1] + tolerance_s

    def speech_share(self, start: float, end: float) -> float:
        a = min(max(samples(start), 0), len(self.mask))
        b = min(max(samples(end), a + 1), len(self.mask))
        return float(self.mask[a:b].mean()) if b > a else 0.0

    def words_share(self, words: Sequence[RecognizedWord]) -> float:
        """Mean speech share of the words' own spans (one word whose end the engine stretched
        into a pause doesn't make a spoken sentence look silent)."""
        return sum(self.speech_share(w.start, w.end) for w in words) / len(words)


def filter_junk(
    segments: Sequence[Segment], vad: VadResult, config: PipelineConfig | None = None
) -> list[Segment]:
    """Drop made-up text; segments left without words are removed. In order:
    1. words that don't come within `word_speech_tolerance_s` of detected speech,
    2. sentences without letters/digits; phantom sentences (`is_phantom`) whose words' mean
       speech share is below `phantom_max_speech`; `is_silence_phantom` ones below
       `silence_phantom_max_speech`,
    3. repetition loops over the remaining words (across sentences, as loops span segments),
       cut only in segments `loop_suspect` allows.
    """
    cfg = config or PipelineConfig()
    speech = _SpeechIndex(vad)
    kept: list[tuple[Segment, list[RecognizedWord]]] = []
    for seg in segments:
        words = [
            w for w in seg.words if speech.near_speech(w.start, w.end, cfg.word_speech_tolerance_s)
        ]
        if not words:
            continue
        text = join_words(words)
        if has_no_words(text):
            continue
        share = speech.words_share(words)
        if is_phantom(text) and share < cfg.phantom_max_speech:
            continue
        if is_silence_phantom(text) and share < cfg.silence_phantom_max_speech:
            continue
        kept.append((seg, words))

    flat = [w for _, words in kept for w in words]
    tokens = [normalize(w.text) or w.text.strip().lower() for w in flat]
    drop = repetition_mask(
        tokens,
        ngram=cfg.loop_ngram,
        min_repeats=cfg.loop_min_repeats,
        max_period=cfg.loop_max_period,
    )
    out: list[Segment] = []
    k = 0
    for seg, words in kept:
        marks = drop[k : k + len(words)] if loop_suspect(seg, cfg) else [False] * len(words)
        k += len(words)
        survivors = [w for w, d in zip(words, marks, strict=True) if not d]
        if survivors:
            out.append(replace(seg, words=tuple(survivors)))
    return out


# --------------------------------------------------------------------------- tail


def find_tail(
    segments: Sequence[Segment],
    speech_regions_s: Sequence[tuple[float, float]],
    end_s: float,
    *,
    margin_s: float = 0.5,
    lead_s: float = 0.2,
) -> float | None:
    """Where the provisional tail starts (window offset on the microsecond grid), or None.

    Speech runs into the cut when the last sentence ends within `margin_s` of the window end
    (the tail sentence is the last one), or when detected speech does and the engine gave no
    words for its end (the cut word is often dropped): then it's the last sentence if that
    reaches into the speech region, else the region itself. The tail starts `lead_s` before
    that (engines often put word starts late, the next chunk gets some lead-in), but not
    before the end of the previous words, so no word changes owner. It must start at least
    1 microsecond before the window end.
    """
    last = segments[-1] if segments else None
    hi: float | None = None
    if last is not None and last.end >= end_s - margin_s:
        hi = last.start
    elif speech_regions_s and speech_regions_s[-1][1] >= end_s - margin_s:
        region_start = speech_regions_s[-1][0]
        hi = last.start if last is not None and last.end > region_start else region_start
    if hi is None:
        return None
    lo = max(
        (max(w.end, w.start + ONE_US) for s in segments for w in s.words if w.start < hi),
        default=0.0,
    )
    tail = on_grid(min(hi, max(lo, hi - lead_s, 0.0)))
    return tail if to_us(tail) <= to_us(end_s) - 1 else None


def mark_provisional(segments: Sequence[Segment], tail_from: float | None) -> list[Segment]:
    if tail_from is None:
        return list(segments)
    return [replace(s, provisional=s.start >= tail_from) for s in segments]


# --------------------------------------------------------------------------- metrics

_HALF_SECOND = timedelta(seconds=0.5)


def metric_seconds(
    start_utc: datetime, own: Interval, n_samples: int
) -> list[tuple[datetime, int, int]]:
    """The UTC seconds a chunk writes metrics for, with their samples in the window.

    A second belongs to the chunk whose own range contains its middle (`ts + 0.5 s`), so
    neighbouring chunks write disjoint seconds even with jitter, and a chunk's own audio
    covers at least half of each of its seconds. Returns `(ts, a, b)`: `a` = the sample of
    `ts` (negative when the second starts before the window audio), `b` = `min(a + 16000,
    n_samples)`; seconds without any window audio are left out.
    """
    start_utc = ensure_utc(start_utc)
    first = own.start - _HALF_SECOND
    ts = first.replace(microsecond=0)
    if ts < first:
        ts += timedelta(seconds=1)
    out = []
    while ts + _HALF_SECOND < own.end:
        a = samples((ts - start_utc) / timedelta(microseconds=1) / US)
        if a >= n_samples:
            break
        b = min(a + SAMPLE_RATE, n_samples)
        if b > max(a, 0):
            out.append((ts, a, b))
        ts += timedelta(seconds=1)
    return out


def loudness_db(x: Pcm, floor_db: float = -100.0) -> float:
    """RMS level in dBFS, within [floor_db, 0] (clipped audio can exceed full scale)."""
    if len(x) == 0:
        return floor_db
    ms = float(np.mean(np.square(x, dtype=np.float64)))
    if not math.isfinite(ms) or ms <= 0:
        return floor_db
    return min(max(10 * math.log10(ms), floor_db), 0.0)


_SOUND_GROUPS = frozenset(g.value for g in SoundGroup)


def audio_metrics(
    pcm: Pcm,
    seconds: Sequence[tuple[datetime, int, int]],
    vad: VadResult,
    scores: Sequence[SecondScores] | None = None,
    *,
    floor_db: float = -100.0,
) -> list[MetricSecond]:
    """Rows for `seconds` (from `metric_seconds`), from the window audio each one has.
    `scores[i]` (a tagger's output) belongs to `seconds[i]`."""
    mask = vad.speech_mask()
    rows = []
    for i, (ts, a, b) in enumerate(seconds):
        a = max(a, 0)
        part = mask[a:b]
        ratio = float(part.mean()) if len(part) else 0.0
        classes: dict[str, float] = {}
        if scores is not None and i < len(scores):
            for group, value in scores[i].items():
                v = _unit(value)
                if v is not None and group in _SOUND_GROUPS:
                    classes[str(group)] = round(v, 3)
        rows.append(
            MetricSecond(
                ts=ts,
                loudness_db=round(loudness_db(pcm[a:b], floor_db), 2),
                speech_ratio=round(min(max(ratio, 0.0), 1.0), 4),
                classes=classes,
            )
        )
    return rows


def tagger_input(
    pcm: Pcm, seconds: Sequence[tuple[datetime, int, int]], context_s: int = 2
) -> tuple[Pcm, int]:
    """Audio for the sound tagger, aligned to whole seconds: up to `context_s` whole seconds
    of window audio before the first metric second (so models with multi-second windows aren't
    blind at its start), then the metric seconds; a first second that starts before the window
    audio is zero-padded. Returns the audio and how many leading context seconds to skip."""
    a0, b1 = seconds[0][1], seconds[-1][2]
    context = min(max(context_s, 0), a0 // SAMPLE_RATE) if a0 >= 0 else 0
    start = a0 - context * SAMPLE_RATE
    audio = pcm[max(start, 0) : b1]
    if start < 0:
        audio = np.concatenate([np.zeros(-start, dtype=np.float32), audio])
    return audio, context


# --------------------------------------------------------------------------- orchestration


def _timed[T](fn: Callable[[], T]) -> tuple[T, float]:
    t = time.perf_counter()
    value = fn()
    return value, time.perf_counter() - t


def process_window_sync(
    window: Window, engines: Engines, config: PipelineConfig | None = None
) -> WindowResult:
    """The whole pipeline for one window, blocking (engines run in this thread)."""
    cfg = config or PipelineConfig()
    started = time.perf_counter()
    pcm, end_s = window.pcm, window.end_s

    vad, vad_s = _timed(lambda: engines.vad.detect(pcm))
    stretches: list[tuple[int, int]] = []
    for a, b in gate_regions(
        vad.regions_s(),
        window.duration_s,
        merge_gap_s=cfg.gate_merge_gap_s,
        pad_s=cfg.gate_pad_s,
        max_s=cfg.gate_max_s,
    ):
        ia, ib = samples(a), samples(b)
        if a < end_s and ib > ia:
            stretches.append((ia, ib))

    results: list[SpeechResult] = []
    speech_s = 0.0
    if stretches:
        pcms = [pcm[ia:ib] for ia, ib in stretches]
        results, speech_s = _timed(lambda: transcribe_all(engines.speech, pcms))
        if len(results) != len(pcms):
            raise ValueError(
                f"{engines.speech.name}: {len(results)} results for {len(pcms)} inputs"
            )
    segments: list[Segment] = []
    for (ia, ib), result in zip(stretches, results, strict=True):
        words, engine_segments = map_result(result, ia / SAMPLE_RATE, (ib - ia) / SAMPLE_RATE)
        segments += build_segments(
            clamp_words(words, end_s),
            engine_segments,
            max_s=cfg.sentence_max_s,
            pause_s=cfg.sentence_pause_s,
        )
    segments.sort(key=lambda s: s.start)
    segments = filter_junk(segments, vad, cfg)

    tail_from = None
    if window.keep_tail:
        in_window = [(a, b) for a, b in vad.regions_s() if a < end_s]
        tail_from = find_tail(
            segments, in_window, end_s, margin_s=cfg.tail_margin_s, lead_s=cfg.gate_pad_s
        )
    segments = mark_provisional(segments, tail_from)
    words = tuple(sorted((w for s in segments for w in s.words), key=lambda w: w.start))

    seconds = metric_seconds(window.start_utc, window.own, len(pcm))
    scores: Sequence[SecondScores] | None = None
    tagger_s = 0.0
    if engines.tagger is not None and seconds:
        tagger = engines.tagger
        audio, skip = tagger_input(pcm, seconds, cfg.tagger_context_s)
        rows, tagger_s = _timed(lambda: list(tagger.classify(audio)))
        if len(rows) != math.ceil(len(audio) / SAMPLE_RATE):
            log.warning(
                "sound tagger returned the wrong number of seconds",
                tagger=tagger.name,
                expected=math.ceil(len(audio) / SAMPLE_RATE),
                got=len(rows),
            )
        scores = rows[skip:]
    metrics = audio_metrics(pcm, seconds, vad, scores, floor_db=cfg.loudness_floor_db)

    gated = tuple((ia / SAMPLE_RATE, ib / SAMPLE_RATE) for ia, ib in stretches)
    timings = Timings(
        audio_s=round(window.duration_s, 3),
        speech_input_s=round(sum(ib - ia for ia, ib in stretches) / SAMPLE_RATE, 3),
        vad_s=round(vad_s, 4),
        speech_s=round(speech_s, 4),
        tagger_s=round(tagger_s, 4),
        total_s=round(time.perf_counter() - started, 4),
    )
    log.debug(
        "window processed",
        model=engines.speech.name,
        words=len(words),
        segments=len(segments),
        regions=len(gated),
        tail_from=tail_from,
        metrics=len(metrics),
        realtime_factor=round(timings.realtime_factor, 1),
    )
    return WindowResult(
        model=engines.speech.name,
        start_utc=window.start_utc,
        words=words,
        segments=tuple(segments),
        tail_from=tail_from,
        metrics=tuple(metrics),
        speech_regions=gated,
        timings=timings,
    )


async def process_window(
    window: Window, engines: Engines, config: PipelineConfig | None = None
) -> WindowResult:
    """`process_window_sync` in a worker thread, so the event loop (heartbeat, job lease)
    keeps running while the engines work."""
    return await asyncio.to_thread(process_window_sync, window, engines, config)
