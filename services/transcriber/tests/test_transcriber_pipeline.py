"""The window pipeline: gating, tail, metrics alignment, and chunk handover end to end."""

from __future__ import annotations

import itertools
import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from pydantic import ValidationError

from clipper_core.contracts import SoundGroup
from clipper_core.timeline import Interval
from clipper_transcriber.audio import SAMPLE_RATE, decode, encode_aac, samples
from clipper_transcriber.engines import (
    BaseSpeechEngine,
    Engines,
    RecognizedWord,
    SpeechResult,
    SpeechSegment,
    VadResult,
)
from clipper_transcriber.fakes import FakeSoundTagger, FakeSpeechEngine, silence, synth_speech, tone
from clipper_transcriber.pipeline import (
    PipelineConfig,
    Segment,
    Window,
    clamp_words,
    find_tail,
    gate_regions,
    loudness_db,
    map_result,
    metric_seconds,
    process_window,
    process_window_sync,
    tagger_input,
    to_us,
)
from clipper_transcriber.settings import TranscriberSettings
from clipper_transcriber.vad import EnergyVad, VadParams

T0 = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def window(pcm, start: float = 0.0, own: tuple[float, float] | None = None, **kwargs) -> Window:
    own = own or (start, start + len(pcm) / SAMPLE_RATE)
    return Window(pcm, at(start), Interval(at(own[0]), at(own[1])), **kwargs)


def engines(*, segments: bool = False, tagger: bool = False) -> Engines:
    return Engines(
        speech=FakeSpeechEngine(segments=segments),
        vad=EnergyVad(),
        tagger=FakeSoundTagger() if tagger else None,
    )


def texts(result) -> list[str]:
    return [w.text for w in result.words]


class FixedVad:
    """Voice activity from given frame flags / regions (exact expectations)."""

    name = "fixed"

    def __init__(self, speech_frames: range = range(0), regions=None) -> None:
        self.speech_frames = speech_frames
        self.regions = regions  # callable n_samples -> sample regions

    def detect(self, pcm) -> VadResult:
        probs = np.zeros(-(-len(pcm) // 512), np.float32)
        probs[self.speech_frames.start : self.speech_frames.stop] = 1.0
        regions = tuple(self.regions(len(pcm))) if self.regions else ()
        return VadResult(len(pcm), 512, 0.5, probs, regions)


# --------------------------------------------------------------------------- pure parts


def test_gate_regions_merge_pad_and_split():
    assert gate_regions([], 10) == []
    # closer than 1 s: merged; then padded by 0.2 s, clamped to the audio
    assert gate_regions([(0.1, 1.0), (1.5, 2.0), (5.0, 9.9)], 10) == [(0.0, 2.2), (4.8, 10.0)]
    # neighbours closer than 2 * pad share the gap (no audio sent twice)
    regions = gate_regions([(0.0, 20.0), (20.2, 40.0)], 50)
    assert regions == [(0.0, 20.1), (20.1, 40.2)]
    # one region longer than max: equal parts, each with padding <= max
    regions = gate_regions([(0.0, 70.0)], 70, max_s=30)
    assert len(regions) == 3 and all(b - a <= 30 for a, b in regions)
    assert regions[0][0] == 0.0 and regions[-1][1] == 70.0
    assert all(b1 <= a2 for (_, b1), (a2, _) in itertools.pairwise(regions))


def test_map_result_cleans_engine_output():
    result = SpeechResult(
        words=[
            RecognizedWord(" b ", 1.0, 1.4, 1.2),
            RecognizedWord("a", -0.2, 0.3, None),
            RecognizedWord("  ", 0.5, 0.6),
            RecognizedWord("c", 1.9, 2.5, math.nan),
            RecognizedWord("nan", math.nan, 1.0),
            RecognizedWord("inf", 0.5, math.inf),
        ],
        segments=[
            SpeechSegment(
                -1.0, 3.0, "a b c", avg_logprob=-0.3, no_speech_prob=2.0, compression_ratio=1.7
            ),
            SpeechSegment(0.0, math.inf, "broken"),
            SpeechSegment(0.0, 1.0, "x", avg_logprob=math.nan, compression_ratio=math.inf),
        ],
    )
    words, segments = map_result(result, offset_s=10.0, length_s=2.0)
    assert [(w.text, w.start, w.end, w.confidence) for w in words] == [
        ("a", 10.0, 10.3, None),
        ("b", 11.0, 11.4, 1.0),
        ("c", 11.9, 12.0, None),
    ]
    assert segments == [  # sorted by start (stable)
        SpeechSegment(
            10.0, 12.0, "a b c", avg_logprob=-0.3, no_speech_prob=1.0, compression_ratio=1.7
        ),
        SpeechSegment(10.0, 11.0, "x"),
    ]
    assert map_result(SpeechResult(words=[]), 0, 1) == ([], None)


def test_offsets_live_on_the_microsecond_grid():
    words, _ = map_result(SpeechResult([RecognizedWord("a", 0.1234567, 0.2)]), 1.0000001, 5)
    assert words[0].start == 1.123457 and to_us(words[0].start) == 1123457
    ws = [
        RecognizedWord("a", 0.0, 1.0),
        RecognizedWord("b", 1.8, 2.3),
        RecognizedWord("c", 2.0, 2.4),
        RecognizedWord("d", 1.9999996, 2.0),  # starts at 2.000000 on the grid: dropped
    ]
    assert clamp_words(ws, 2.0) == [ws[0], RecognizedWord("b", 1.8, 2.0)]
    w = Window(silence(1.0)[: 16001 - 2], T0, Interval(T0, at(1)))  # 15999 samples
    assert w.end_us == 999937 and w.end_s == 0.999937  # floor: never past the audio
    assert Window(silence(1.0), T0, Interval(T0, at(1)), owned_to=at(0.5)).end_us == 500000


def seg(start: float, end: float) -> Segment:
    return Segment((RecognizedWord("x", start, end),))


def test_find_tail_rules():
    segs = [seg(1.0, 3.0), seg(5.0, 9.7)]
    # last sentence into the cut; 0.2 s lead-in before it (engines start words late)
    assert find_tail(segs, [(4.9, 9.7)], 10.0) == 4.8
    assert find_tail(segs, [(4.9, 9.4)], 10.0, lead_s=0) == 5.0
    assert find_tail([seg(1.0, 3.0), seg(5.0, 9.0)], [(4.9, 9.1)], 10.0) is None
    # speech runs into the cut but the engine gave no words there (cut word dropped)
    assert find_tail([seg(1.0, 3.0), seg(5.0, 9.0)], [(4.9, 10.0)], 10.0) == 4.8
    # ... or nothing at all after the last sentence: the speech region's start
    assert find_tail([seg(1.0, 3.0)], [(1.0, 3.0), (8.2, 10.0)], 10.0) == 8.0
    assert find_tail([], [(8.2, 10.0)], 10.0) == 8.0
    assert find_tail([], [], 10.0) is None
    # the lead-in never reaches back over earlier words, nor before the window
    assert find_tail([seg(1.0, 4.9), seg(5.0, 9.8)], [], 10.0) == 4.9
    assert find_tail([seg(0.1, 9.8)], [], 10.0) == 0.0
    # it must start at least 1 microsecond before the window end
    assert find_tail([seg(10.0, 10.0)], [], 10.0, lead_s=0) is None
    assert find_tail([seg(9.999999, 10.0)], [], 10.0, lead_s=0) == 9.999999
    assert find_tail([seg(9.9999996, 10.0)], [], 10.0, lead_s=0) is None


def test_metric_seconds_belong_to_the_chunk_holding_their_middle():
    start = at(0.3)  # window starts at a fractional second
    rows = metric_seconds(start, Interval(at(1.5), at(5.3)), samples(5.0))
    # middles 1.5 .. 4.5 lie in [1.5, 5.3); 5.5 doesn't
    assert [ts for ts, _, _ in rows] == [at(1), at(2), at(3), at(4)]
    assert [(a, b) for _, a, b in rows] == [
        (11200, 27200),
        (27200, 43200),
        (43200, 59200),
        (59200, 75200),
    ]
    # the first second may start before the window audio (a < 0: partial)
    rows = metric_seconds(start, Interval(at(0.3), at(2.0)), samples(1.7))
    assert [(ts, a, b) for ts, a, b in rows] == [(at(0), -4800, 11200), (at(1), 11200, 27200)]
    # seconds beyond the audio are left out
    assert len(metric_seconds(at(0), Interval(at(0), at(10)), samples(3.5))) == 4
    # neighbouring chunks write disjoint seconds that tile, also with jitter
    for gap in (0.0, -0.03, 0.04):
        first = metric_seconds(at(0.3), Interval(at(0.3), at(7.45)), samples(7.15))
        second = metric_seconds(
            at(7.45 + gap), Interval(at(7.45 + gap), at(12.0)), samples(4.55 - gap)
        )
        assert [ts for ts, _, _ in first + second] == [at(s) for s in range(12)]


def test_loudness_is_finite_and_at_most_zero():
    assert loudness_db(np.zeros(100, np.float32)) == -100.0
    assert loudness_db(np.zeros(0, np.float32), floor_db=-90) == -90
    assert loudness_db(np.full(10, 2.0, np.float32)) == 0.0  # clipped audio
    assert loudness_db(np.full(10, np.nan, np.float32)) == -100.0
    assert abs(loudness_db(np.full(10, 0.1, np.float32)) + 20) < 1e-6


def test_window_and_config_validation():
    with pytest.raises(ValueError, match="naive"):
        Window(silence(1), datetime(2026, 11, 19), Interval(T0, at(1)))
    with pytest.raises(ValueError, match="1-D"):
        Window(np.zeros((2, 10), np.float32), T0, Interval(T0, at(1)))
    with pytest.raises(ValueError, match="before the window"):
        Window(silence(1), T0, Interval(T0, at(1)), owned_to=at(-1))
    w = Window(np.zeros(16000, np.float64), T0, Interval(T0, at(1)), owned_to=at(5))
    assert w.pcm.dtype == np.float32 and w.end_s == 1.0 and w.utc(0.5) == at(0.5)
    for bad in (0.0, 5.0, -math.inf, math.nan):
        with pytest.raises(ValueError):
            PipelineConfig(loudness_floor_db=bad)


def test_settings_are_validated_and_build_the_configs():
    defaults = TranscriberSettings(_env_file=None)
    assert (defaults.engine, defaults.tagger) == ("whisper", "ced")
    with pytest.raises(ValidationError, match="engine"):
        TranscriberSettings(_env_file=None, engine="")
    settings = TranscriberSettings(_env_file=None, engine="fake", gate_max_s=20, tail_margin_s=0.7)
    assert settings.pipeline_config() == PipelineConfig(gate_max_s=20, tail_margin_s=0.7)
    assert settings.vad_params().max_speech_s == pytest.approx(19.6)
    assert settings.vad_params().threshold == 0.5
    for bad in ({"loudness_floor_db": 0}, {"loudness_floor_db": "nan"}, {"vad_threshold": 1.5}):
        with pytest.raises(ValidationError):
            TranscriberSettings(_env_file=None, engine="fake", **bad)
    with pytest.raises(ValidationError, match="gate_max_s"):
        TranscriberSettings(_env_file=None, engine="fake", gate_max_s=0.3)


# --------------------------------------------------------------------------- gating


def test_engine_only_hears_speech_and_times_map_back():
    pcm, expected = synth_speech("w1 w2 _6 w3 . _3", lead_s=3.0)
    eng = engines()
    result = process_window_sync(window(pcm), eng)
    assert texts(result) == ["w1", "w2", "w3."]
    for got, want in zip(result.words, expected, strict=True):
        assert abs(got.start - want.start) < 0.011 and abs(got.end - want.end) < 0.011
    # two stretches (6 s apart), each the words + small padding, not the 13 s window, sent in
    # one transcribe_many call
    assert eng.speech.batches == [2] and len(eng.speech.calls) == 2
    assert sum(eng.speech.calls) / SAMPLE_RATE == pytest.approx(result.timings.speech_input_s)
    assert result.timings.speech_input_s < 2.5 < result.timings.audio_s
    (a1, b1), (a2, b2) = result.speech_regions
    assert a1 < 3.0 < 3.72 < b1 < a2 < expected[2].start < expected[2].end < b2


def test_no_speech_means_no_engine_call():
    eng = engines(tagger=True)
    result = process_window_sync(window(silence(4.0)), eng)
    assert eng.speech.batches == [] and result.words == () and result.segments == ()
    assert result.tail_from is None and result.speech_regions == ()
    assert [m.loudness_db for m in result.metrics] == [-100.0] * 4
    assert all(m.speech_ratio == 0.0 for m in result.metrics)


def test_long_speech_goes_to_the_engine_in_stretches_of_at_most_30_s():
    script = " ".join(f"w{i % 90}" for i in range(100))  # 50 s of words, 0.2 s apart
    pcm, expected = synth_speech(script, gap_s=0.2)
    eng = Engines(speech=FakeSpeechEngine(), vad=EnergyVad(VadParams(max_speech_s=29.6)))
    result = process_window_sync(window(pcm), eng)
    assert eng.speech.batches == [2]
    assert all(n <= 30 * SAMPLE_RATE for n in eng.speech.calls)
    assert texts(result) == [w.text for w in expected]
    assert all(s.end - s.start <= 30 for s in result.segments)


def test_merged_neighbours_go_in_one_stretch():
    pcm, _ = synth_speech("w1 w2 _0.5 w3")
    eng = engines()
    process_window_sync(window(pcm), eng)
    assert eng.speech.batches == [1]


class TranscribeOnly:
    """An engine with only `transcribe` (no transcribe_many): still usable."""

    name = "plain"

    def __init__(self) -> None:
        self.inner = FakeSpeechEngine()

    def transcribe(self, pcm):
        return self.inner.transcribe(pcm)


def test_engine_without_transcribe_many():
    pcm, _ = synth_speech("w1 _3 w2")
    result = process_window_sync(window(pcm), Engines(speech=TranscribeOnly(), vad=EnergyVad()))
    assert texts(result) == ["w1", "w2"] and result.model == "plain"


class WrongCount(BaseSpeechEngine):
    name = "wrong"

    def transcribe(self, pcm):
        return SpeechResult([])

    def transcribe_many(self, pcms):
        return []


def test_engine_must_answer_every_stretch():
    pcm, _ = synth_speech("w1")
    with pytest.raises(ValueError, match="0 results for 1"):
        process_window_sync(window(pcm), Engines(speech=WrongCount(), vad=EnergyVad()))


# --------------------------------------------------------------------------- tail


def test_tail_when_speech_runs_into_the_cut():
    pcm, _ = synth_speech("w1 w2 . w3 w4 w5", total_s=2.65)  # w5 (2.42..2.72) is cut
    result = process_window_sync(window(pcm), engines())
    assert texts(result) == ["w1", "w2.", "w3", "w4", "w5"]
    # the last sentence starts at 1.58; 0.2 s lead-in (w2 ends at 1.22)
    assert result.tail_from == pytest.approx(1.38, abs=0.011)
    assert [s.provisional for s in result.segments] == [False, True]
    assert result.tail_from_utc == at(result.tail_from)


def test_no_tail_when_the_window_ends_in_silence():
    pcm, _ = synth_speech("w1 w2 . w3 w4 w5", total_s=4.0)
    result = process_window_sync(window(pcm), engines())
    assert result.tail_from is None and not any(s.provisional for s in result.segments)


def test_tail_from_voice_activity_when_the_engine_drops_the_cut_word():
    words, _ = synth_speech("w1 w2 . w3 w4 w5", trail_s=0)
    noise = tone(3500, 1.0)  # loud, but not a word: the engine gives nothing for it

    # right after the last sentence (same speech region): the sentence becomes the tail
    pcm = np.concatenate([words, noise])
    result = process_window_sync(window(pcm), engines())
    assert texts(result)[-1] == "w5" and result.tail_from == pytest.approx(1.38, abs=0.011)

    # after a pause: the tail starts where that speech starts (with lead-in)
    pcm = np.concatenate([words, silence(0.5), noise])
    result = process_window_sync(window(pcm), engines())
    noise_start = len(words) / SAMPLE_RATE + 0.5
    assert result.tail_from == pytest.approx(noise_start - 0.2, abs=0.07)
    assert not any(s.provisional for s in result.segments)


def test_no_tail_when_the_next_chunk_already_took_over():
    pcm, _ = synth_speech("w1 w2 . w3 w4 w5", total_s=2.65)
    result = process_window_sync(window(pcm, keep_tail=False), engines())
    assert result.tail_from is None and not any(s.provisional for s in result.segments)


class EndWords(BaseSpeechEngine):
    """Whisper-like: a last word at or past the end of its input."""

    name = "end-words"

    def transcribe(self, pcm):
        length = len(pcm) / SAMPLE_RATE
        return SpeechResult(
            [
                RecognizedWord("hello", 0.1, 0.5, 0.9),
                RecognizedWord("wor", length - 1e-9, length + 0.02, 0.5),
                RecognizedWord("ld", length + 0.02, length + 0.02, 0.5),
            ]
        )


def test_words_and_tail_stay_before_owned_to():
    """Float sums like `(ib - ia) / sr + ia / sr` land 1 ulp below `ib / sr`; after the UTC
    conversion a word or tail could then equal owned_to (CHECK violation on every retry)."""
    n = 30 * SAMPLE_RATE + 123
    ulp_cases = [
        ia for ia in range(0, n - SAMPLE_RATE, 7) if (n - ia) / 16000 + ia / 16000 < n / 16000
    ]
    assert ulp_cases  # the test covers the float trap
    start = T0 + timedelta(microseconds=400_000)
    pcm = np.full(n, 0.1, np.float32)
    for ia in ulp_cases[:: max(1, len(ulp_cases) // 40)] + list(range(0, n - SAMPLE_RATE, 9001)):
        vad = FixedVad(range(0, n), regions=lambda n, ia=ia: [(ia + 3200, n)])
        for owned_to in (None, start + timedelta(seconds=n / 16000)):
            w = Window(pcm, start, Interval(start, start + timedelta(seconds=30)), owned_to)
            result = process_window_sync(w, Engines(speech=EndWords(), vad=vad))
            end = start + timedelta(microseconds=w.end_us)
            assert end <= (owned_to or end)
            assert all(result.utc(x.start) < end for x in result.words)
            assert result.tail_from_utc is None or start <= result.tail_from_utc < end
            assert result.tail_from is not None  # "hello" ... runs into the cut


# --------------------------------------------------------------------------- metrics


def test_metrics_are_aligned_to_utc_seconds():
    start = 0.3  # the window starts 300 ms into a UTC second
    n = samples(5.0)  # covers 00.3 .. 05.3
    # constant level per UTC second k: 0.05 * (k + 1)
    second_of_sample = (samples(start) + np.arange(n)) // SAMPLE_RATE
    pcm = (0.05 * (second_of_sample + 1)).astype(np.float32)
    # voice activity: frames 85..115 = samples 43520..59392 (UTC 03.02 .. 04.012)
    vad = FixedVad(range(85, 116))
    tagger = FakeSoundTagger()
    eng = Engines(speech=FakeSpeechEngine(), vad=vad, tagger=tagger)
    result = process_window_sync(window(pcm, start, own=(2.5, 5.3)), eng)

    # middles 2.5, 3.5, 4.5 lie in the own range [2.5, 5.3)
    assert [m.ts for m in result.metrics] == [at(2), at(3), at(4)]
    for m, k in zip(result.metrics, range(2, 5), strict=True):
        assert m.loudness_db == round(20 * math.log10(0.05 * (k + 1)), 2)
        speech = round(min(max((m.loudness_db + 60) / 60, 0), 1), 3)
        assert m.classes[SoundGroup.SPEECH] == pytest.approx(speech, abs=0.002)
        assert set(m.classes) == {g.value for g in SoundGroup}
    assert [m.speech_ratio for m in result.metrics] == [0.0, 0.98, 0.012]
    # the tagger heard UTC 01.0 .. 05.0: one second of context, then the three seconds
    assert tagger.calls == [samples(4.7) - samples(0.7)]


def test_tagger_input_context_and_padding():
    pcm = np.arange(5 * SAMPLE_RATE, dtype=np.float32)
    seconds = [(at(3), 3 * SAMPLE_RATE, 4 * SAMPLE_RATE), (at(4), 4 * SAMPLE_RATE, 5 * SAMPLE_RATE)]
    audio, skip = tagger_input(pcm, seconds, context_s=2)
    assert skip == 2 and audio[0] == SAMPLE_RATE and len(audio) == 4 * SAMPLE_RATE
    audio, skip = tagger_input(pcm, seconds, context_s=0)
    assert skip == 0 and audio[0] == 3 * SAMPLE_RATE
    # a first second that starts before the window audio: zeros up to the second boundary
    audio, skip = tagger_input(pcm, [(at(0), -4800, 11200)], context_s=2)
    assert skip == 0 and len(audio) == SAMPLE_RATE
    assert not audio[:4800].any() and audio[4800] == 0 and audio[4801] == 1


def test_first_second_may_be_partial():
    pcm = np.full(samples(2.7), 0.1, np.float32)  # UTC 00.3 .. 03.0
    tagger = FakeSoundTagger()
    eng = Engines(speech=FakeSpeechEngine(), vad=EnergyVad(), tagger=tagger)
    result = process_window_sync(window(pcm, 0.3), eng)
    assert [m.ts for m in result.metrics] == [at(0), at(1), at(2)]
    assert all(m.loudness_db == -20.0 for m in result.metrics)  # from the audio available
    assert tagger.calls == [3 * SAMPLE_RATE]  # zero-padded to 00.0


def test_metrics_without_tagger_have_no_classes():
    pcm, _ = synth_speech("w1 w2")
    result = process_window_sync(window(pcm), engines())
    assert result.metrics and all(m.classes == {} for m in result.metrics)
    assert all(-100 <= m.loudness_db <= 0 and 0 <= m.speech_ratio <= 1 for m in result.metrics)


class BadTagger:
    name = "bad"

    def classify(self, pcm):
        return [{SoundGroup.MUSIC: 1.7, "not_a_group": 0.5, SoundGroup.GASP: math.nan}]


def test_tagger_output_is_sanitised():
    eng = Engines(speech=FakeSpeechEngine(), vad=EnergyVad(), tagger=BadTagger())
    result = process_window_sync(window(silence(3.0)), eng)
    assert result.metrics[0].classes == {"music": 1.0}
    assert result.metrics[1].classes == {} and len(result.metrics) == 3


def test_owned_to_clamps_words_but_not_metrics():
    pcm, _ = synth_speech("w1 w2 . w3 w4 w5")  # w2 0.92..1.22, w3 1.58..1.88; 3.22 s
    result = process_window_sync(
        window(pcm, owned_to=at(1.1), keep_tail=False), engines(tagger=True)
    )
    # the engine still hears the audio after owned_to (the full stop after w2)
    assert texts(result) == ["w1", "w2."]
    assert result.words[-1].end == pytest.approx(1.1)
    assert [m.ts for m in result.metrics] == [at(0), at(1), at(2)]


# --------------------------------------------------------------------------- end to end

SCRIPT = "w1 w2 w3 . w4 w5 . _1.2 w6 w7 w8 w9 . w10 w11 w12 . w13 w14 . w15 w16 w17 . w18"


def utc_words(result, *, before=None):
    return [
        (w.text, result.utc(w.start))
        for w in result.words
        if before is None or result.utc(w.start) < before
    ]


def assert_same_words(got, reference, tolerance=0.021):
    assert [t for t, _ in got] == [t for t, _ in reference]
    for (_, a), (_, b) in zip(got, reference, strict=True):
        assert abs((a - b).total_seconds()) < tolerance


def cut_inside(expected, text: str) -> float:
    word = next(w for w in expected if w.text == text)
    return round((word.start + word.end) / 2, 3)


def chunk_window(pcm, start: float, window_from: float, own: tuple[float, float], **kw):
    """The window a handler builds: audio from `window_from` (offset in `pcm`) on."""
    offset = samples(window_from)
    return Window(
        pcm[offset : samples(own[1] - start)],
        at(start + offset / SAMPLE_RATE),
        Interval(at(own[0]), at(own[1])),
        **kw,
    )


@pytest.mark.parametrize("segments", [False, True])
def test_chunk_handover_gives_the_same_words_as_one_window(segments):
    start = 0.4  # chunks start at fractional UTC seconds
    pcm, expected = synth_speech(SCRIPT)
    total = len(pcm) / SAMPLE_RATE
    cut = cut_inside(expected, "w12.")  # its sentence runs into the cut
    whole = process_window_sync(window(pcm, start), engines(segments=segments, tagger=True))

    first = process_window_sync(
        window(pcm[: samples(cut)], start), engines(segments=segments, tagger=True)
    )
    assert first.tail_from is not None
    assert first.segments[-1].provisional and first.segments[-1].text == "w10 w11 w12"

    # chunk 2's window starts at chunk 1's tail (the handler prepends that audio)
    second = process_window_sync(
        chunk_window(pcm, start, first.tail_from, (start + cut, start + total)),
        engines(segments=segments, tagger=True),
    )

    # ownership: chunk 1 owns [start, tail), chunk 2 owns [tail, end)
    merged = utc_words(first, before=first.tail_from_utc) + utc_words(second)
    assert_same_words(merged, utc_words(whole))
    sentences = [s.text for s in first.segments if not s.provisional]
    sentences += [s.text for s in second.segments]
    assert sentences == [s.text for s in whole.segments]
    assert sentences[:5] == ["w1 w2 w3.", "w4 w5.", "w6 w7 w8 w9.", "w10 w11 w12.", "w13 w14."]
    if segments:
        assert all(s.avg_logprob == -0.1 for s in second.segments)

    # metrics: disjoint whole UTC seconds that tile the stream; equal where both had full audio
    assert [m.ts for m in first.metrics] + [m.ts for m in second.metrics] == [
        m.ts for m in whole.metrics
    ]
    whole_by_ts = {m.ts: m for m in whole.metrics}
    for m in first.metrics[:-1] + second.metrics[:-1]:  # the last ones are partial seconds
        ref = whole_by_ts[m.ts]
        assert (m.loudness_db, m.classes) == (ref.loudness_db, ref.classes)
        # voice-activity frames (32 ms) lie on each window's own grid: edges shift a little
        assert abs(m.speech_ratio - ref.speech_ratio) <= 0.1


def test_rerun_while_the_next_chunk_is_done_keeps_owned_ranges_tiling():
    """Chunks A, B, C; B is transcribed again (a retry) after C took over B's tail: B's window
    still starts at A's tail, ends at C's owned_from, and B keeps no tail of its own."""
    start = 0.7
    pcm, expected = synth_speech(SCRIPT)
    total = len(pcm) / SAMPLE_RATE
    cut1, cut2 = cut_inside(expected, "w5."), cut_inside(expected, "w12.")
    whole = process_window_sync(window(pcm, start), engines())

    a = process_window_sync(window(pcm[: samples(cut1)], start), engines())
    b = process_window_sync(
        chunk_window(pcm, start, a.tail_from, (start + cut1, start + cut2)), engines()
    )
    c_from = (b.tail_from_utc - at(start)).total_seconds()  # C's window starts at B's tail
    c = process_window_sync(
        chunk_window(pcm, start, c_from, (start + cut2, start + total)), engines()
    )
    assert a.tail_from is not None and b.tail_from is not None

    rerun = process_window_sync(
        chunk_window(
            pcm,
            start,
            a.tail_from,
            (start + cut1, start + cut2),
            owned_to=c.start_utc,
            keep_tail=False,
        ),
        engines(),
    )
    assert rerun.start_utc == b.start_utc and rerun.tail_from is None
    assert all(rerun.utc(w.start) < c.start_utc for w in rerun.words)
    assert not any(s.provisional for s in rerun.segments)
    # B's words before C's window are the same as the first time
    assert_same_words(utc_words(rerun), utc_words(b, before=c.start_utc), tolerance=1e-6)

    merged = utc_words(a, before=a.tail_from_utc) + utc_words(rerun) + utc_words(c)
    assert_same_words(merged, utc_words(whole))


def test_whole_pipeline_on_aac_audio(ffmpeg, tmp_path):
    pcm, expected = synth_speech(SCRIPT)
    encode_aac(pcm, tmp_path / "chunk.aac", ffmpeg=ffmpeg)
    decoded = decode(tmp_path / "chunk.aac", ffmpeg=ffmpeg)
    result = process_window_sync(window(decoded, 0.25), engines(tagger=True))
    assert texts(result) == [w.text for w in expected]
    assert [s.text for s in result.segments][:2] == ["w1 w2 w3.", "w4 w5."]
    assert result.metrics[0].ts == at(0)  # middle 00.5 lies in the chunk (from 00.25)


async def test_async_wrapper_and_timings():
    pcm, _ = synth_speech("w1 w2 . w3")
    result = await process_window(window(pcm), engines(tagger=True))
    assert texts(result) == ["w1", "w2.", "w3"] and result.model == "fake-tones"
    t = result.timings
    assert t.audio_s == round(len(pcm) / SAMPLE_RATE, 3) and t.total_s > 0
    assert t.realtime_factor > 1 and t.vad_s >= 0 and t.tagger_s >= 0
    assert 0 < t.speech_input_s < t.audio_s
