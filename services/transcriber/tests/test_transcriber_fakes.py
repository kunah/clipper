"""The fake engines: deterministic from the audio, robust to cuts and AAC encoding."""

from __future__ import annotations

import pytest

from clipper_core.contracts import SoundGroup
from clipper_transcriber.audio import SAMPLE_RATE, decode, samples
from clipper_transcriber.fakes import (
    FakeSoundTagger,
    FakeSpeechEngine,
    classify_burst,
    dominant_hz,
    silence,
    synth_speech,
    synth_to_aac,
    tone,
    word_hz,
)

SCRIPT = "w1 w2 w3 . _1.5 w4 w99 w0 . w50"


def summary(words, shift: float = 0.0):
    return [(w.text, round(w.start + shift, 2), round(w.end + shift, 2)) for w in words]


def assert_same_words(got, expected, tolerance: float):
    assert [w.text for w in got] == [w.text for w in expected]
    for g, e in zip(got, expected, strict=True):
        assert abs(g.start - e.start) <= tolerance and abs(g.end - e.end) <= tolerance, (g, e)


def test_synth_script_and_recognition():
    pcm, expected = synth_speech(SCRIPT)
    assert [w.text for w in expected] == ["w1", "w2", "w3.", "w4", "w99", "w0.", "w50"]
    assert expected[0].start == 0.5 and expected[3].start - expected[2].end > 1.5
    result = FakeSpeechEngine().transcribe(pcm)
    assert result.segments is None
    assert_same_words(result.words, expected, 0.011)
    assert all(w.confidence == 0.9 for w in result.words)


def test_engine_segments_split_at_full_stops_and_pauses():
    pcm, _ = synth_speech("w1 w2 . w3 _1.5 w4 w5")
    result = FakeSpeechEngine(segments=True).transcribe(pcm)
    assert [s.text for s in result.segments] == ["w1 w2.", "w3", "w4 w5"]
    assert all(s.avg_logprob == -0.1 for s in result.segments)


def test_bursts_and_frequencies():
    assert word_hz(0) == 300 and word_hz(10) == 500
    with pytest.raises(ValueError):
        word_hz(100)
    assert abs(dominant_hz(tone(523.0, 0.3)) - 523.0) < 1
    assert classify_burst(200) == "." and classify_burst(500) == "w10"
    assert classify_burst(507) == "w10" and classify_burst(250) is None
    assert classify_burst(5000) is None
    assert FakeSpeechEngine().transcribe(silence(2)).words == []
    assert FakeSpeechEngine().transcribe(silence(0)).words == []


@pytest.mark.parametrize("cut", [0.3, 0.55, 0.81, 1.0, 1.3, 1.8, 2.9, 3.6])
def test_same_words_wherever_the_audio_is_cut(cut):
    pcm, expected = synth_speech(SCRIPT)
    words = FakeSpeechEngine().transcribe(pcm[samples(cut) :]).words
    shifted = [type(w)(w.text, w.start + cut, w.end + cut, w.confidence) for w in words]
    # words that start after the cut come out the same; a cut word may come out shorter
    # (cuts are >= 30 ms away from word onsets, outside the 10 ms frame tolerance)
    full = [w for w in shifted if w.start > cut + 0.011]
    assert_same_words(full, [w for w in expected if w.start > cut + 0.011], 0.011)


def test_recognition_survives_aac(ffmpeg, tmp_path):
    path = tmp_path / "speech.aac"
    pcm = synth_to_aac(SCRIPT, path, ffmpeg=ffmpeg)
    _, expected = synth_speech(SCRIPT)
    decoded = decode(path, ffmpeg=ffmpeg)
    assert abs(len(decoded) - len(pcm)) < 0.08 * SAMPLE_RATE
    # ADTS keeps the encoder's priming samples: ~21 ms later than the source
    assert_same_words(FakeSpeechEngine().transcribe(decoded).words, expected, 0.035)
    segments = FakeSpeechEngine(segments=True).transcribe(decoded).segments
    assert [s.text for s in segments] == ["w1 w2 w3.", "w4 w99 w0.", "w50"]


def test_fake_tagger_scores_each_second_by_loudness():
    pcm = tone(440, 2.5, amplitude=0.5)
    pcm[SAMPLE_RATE : 2 * SAMPLE_RATE] = 0
    rows = FakeSoundTagger().classify(pcm)
    assert len(rows) == 3 and set(rows[0]) == set(SoundGroup)
    assert rows[0][SoundGroup.SPEECH] > 0.8 and rows[1][SoundGroup.SPEECH] == 0.0
    assert rows[2][SoundGroup.SHOUTING] > 0.4 and rows[0][SoundGroup.LAUGHTER] == 0.0
    assert FakeSoundTagger().classify(silence(0)) == []
