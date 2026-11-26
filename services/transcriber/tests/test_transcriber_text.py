"""Sentences from words or engine segments, and the junk filter."""

from __future__ import annotations

import numpy as np
import pytest

from clipper_core.contracts import Word
from clipper_transcriber.engines import RecognizedWord, SpeechSegment, VadResult
from clipper_transcriber.pipeline import (
    PipelineConfig,
    Segment,
    build_segments,
    ends_sentence,
    filter_junk,
    has_no_words,
    is_phantom,
    is_silence_phantom,
    normalize,
    repetition_mask,
    segments_from_engine,
    segments_from_words,
    split_sentences,
)


def words(spec: str, *, start: float = 0.0, step: float = 0.4, dur: float = 0.3):
    """'a b. c' -> evenly spaced words; '|2' inserts a 2 s pause."""
    out, t = [], start
    for token in spec.split():
        if token.startswith("|"):
            t += float(token[1:])
            continue
        out.append(RecognizedWord(token, round(t, 3), round(t + dur, 3), 0.8))
        t += step
    return out


def texts(groups) -> list[str]:
    return [" ".join(w.text for w in g) for g in groups]


def vad_all_speech(seconds: float) -> VadResult:
    n = round(seconds * 16000)
    frames = -(-n // 512)
    return VadResult(n, 512, 0.5, np.ones(frames, np.float32), ((0, n),))


def vad_regions(seconds: float, *regions: tuple[float, float]) -> VadResult:
    n = round(seconds * 16000)
    probs = np.zeros(-(-n // 512), np.float32)
    for a, b in regions:
        probs[int(a * 16000) // 512 : int(b * 16000) // 512] = 1.0
    sample_regions = tuple((round(a * 16000), round(b * 16000)) for a, b in regions)
    return VadResult(n, 512, 0.5, probs, sample_regions)


# --------------------------------------------------------------------------- sentences


def test_sentence_end_detection():
    assert ends_sentence("now.") and ends_sentence("what?") and ends_sentence("go!")
    assert ends_sentence('it."') and ends_sentence("this.)") and ends_sentence("wait...")
    assert not ends_sentence("Mr.") and not ends_sentence("e.g.") and not ends_sentence("3.5")
    assert not ends_sentence("hello,") and not ends_sentence("")


def test_split_on_punctuation_pause_and_length():
    assert texts(split_sentences(words("hi chat. how are you? good"))) == [
        "hi chat.",
        "how are you?",
        "good",
    ]
    assert texts(split_sentences(words("so then |1.5 we go"))) == ["so then", "we go"]
    assert texts(split_sentences(words("so then |0.5 we go"))) == ["so then we go"]
    assert texts(split_sentences(words("I met Mr. Smith today."))) == ["I met Mr. Smith today."]
    long = split_sentences(words(" ".join(["la"] * 100)), max_s=10)  # 0.4 s per word
    assert [len(g) for g in long] == [25, 25, 25, 25]
    assert all(g[-1].end - g[0].start <= 10 for g in long)
    assert split_sentences([]) == []


def test_segments_from_words_also_take_words_file_words():
    file_words = [Word(text="hello", start=0, end=0.4), Word(text="chat.", start=0.5, end=0.9)]
    (seg,) = segments_from_words(file_words)
    assert seg.text == "hello chat." and (seg.start, seg.end) == (0, 0.9)
    assert isinstance(seg.words[0], RecognizedWord) and seg.confidence is None
    (seg,) = segments_from_words(words("a b c"))
    assert seg.confidence == 0.8 and seg.avg_logprob is None and not seg.provisional


def test_engine_segments_group_words_by_midpoint():
    ws = words("one two three four five")  # 0.0, 0.4, 0.8, 1.2, 1.6
    engine = [
        SpeechSegment(0.0, 0.7, "One two", avg_logprob=-0.2, no_speech_prob=0.05),
        SpeechSegment(0.75, 2.0, "three four five", avg_logprob=-0.5, confidence=0.7),
        SpeechSegment(5.0, 6.0, "nothing here"),
    ]
    segs = segments_from_engine(ws, engine)
    assert [s.text for s in segs] == ["one two", "three four five"]  # text from the words
    assert segs[0].avg_logprob == -0.2 and segs[0].no_speech_prob == 0.05
    assert segs[0].confidence == 0.8  # engine gave none: mean word confidence
    assert segs[1].confidence == 0.7
    # a word outside every segment goes to the nearest one
    stray = [*ws, RecognizedWord("six", 3.0, 3.2)]
    assert segments_from_engine(stray, engine)[-1].text == "three four five six"
    # no engine segments: split ourselves
    assert [s.text for s in build_segments(words("a. b"), None)] == ["a.", "b"]
    assert [s.text for s in build_segments(words("a. b"), [])] == ["a.", "b"]


def test_segment_needs_words():
    with pytest.raises(ValueError):
        Segment(())


# --------------------------------------------------------------------------- phantoms


def test_normalize():
    assert normalize("  Don't  STOP, Amara.org!") == "dont stop amara org"
    assert normalize("I’m here") == "im here"  # noqa: RUF001 - typographic apostrophe
    assert normalize("♪ ♪") == ""


@pytest.mark.parametrize(
    "text",
    [
        "Thanks for watching!",
        "thank you for watching.",
        "Thanks for watching and please subscribe.",
        "Please subscribe to my channel.",
        "Don't forget to like and subscribe!",
        "Subtitles by the Amara.org community",
        "Transcription by CastingWords",
    ],
)
def test_phantom_sentences(text):
    assert is_phantom(text) and not has_no_words(text)


def test_sentences_without_letters():
    assert has_no_words("♪") and has_no_words("...") and has_no_words(" - ")
    assert not has_no_words("ok") and not has_no_words("42")


@pytest.mark.parametrize(
    "text",
    [
        "Thanks for watching the stream, guys, love you.",
        "I said please subscribe and nobody did",
        "and",
        "thank you",
        "the subtitles by the way are wrong",
    ],
)
def test_real_sentences_are_not_phantoms(text):
    assert not is_phantom(text)


def test_silence_phantoms():
    assert (
        is_silence_phantom("Thank you.")
        and is_silence_phantom("you")
        and is_silence_phantom("Bye!")
    )
    assert not is_silence_phantom("thank you chat")


def test_silence_phantom_dropped_only_without_speech_under_it():
    segs = segments_from_words(words("Thank you. |2 Thank you. |2 Welcome back."))
    # VAD heard speech only under the last two sentences
    vad = vad_regions(10, (2.6, 3.6), (5.0, 7.0))
    cfg = PipelineConfig(word_speech_tolerance_s=5)  # isolate the phantom rule
    assert [s.text for s in filter_junk(segs, vad, cfg)] == ["Thank you.", "Welcome back."]
    assert filter_junk(segs, vad, cfg)[0].start == 2.8


# --------------------------------------------------------------------------- loops


def drops(text: str) -> str:
    tokens = text.split()
    mask = repetition_mask(tokens)
    return " ".join(t for t, d in zip(tokens, mask, strict=True) if not d)


def test_repetition_loops():
    assert drops("here " * 12) == "here here here"
    assert drops("a b " * 5 + "end") == "a b a b end"
    sentence = "i am going home now "
    assert drops("ok " + sentence * 4 + "bye") == "ok i am going home now bye"
    # the 4th copy only needs to contain the 3-gram
    assert drops(sentence * 3 + "i am going") == "i am going home now"


def test_short_or_natural_repetition_is_kept():
    for text in [
        "no no no no",
        "no no no no no",
        "lets go lets go lets go lets go",
        "oh my god oh my god oh my god",
        "you know what i mean you know it you know what i mean",
        "",
    ]:
        assert drops(text) == " ".join(text.split())


def test_filter_junk_removes_loops_across_sentences_and_words_outside_speech():
    segs = segments_from_words(
        words("Hello chat. I'm going to go. I'm going to go. I'm going to go. I'm going to go.")
    )
    out = filter_junk(segs, vad_all_speech(30))
    assert [s.text for s in out] == ["Hello chat.", "I'm going to go."]
    assert out[1].words[0].start == 0.8

    # words far outside detected speech are dropped; empty segments disappear
    segs = segments_from_words(words("real words. |3 made up stuff. |3 more real"))
    vad = vad_regions(12, (0.0, 1.0), (7.2, 9.0))
    out = filter_junk(segs, vad, PipelineConfig(word_speech_tolerance_s=0.3))
    assert [s.text for s in out] == ["real words.", "more real"]
    # within the tolerance they stay
    vad = vad_regions(12, (0.0, 0.5), (7.2, 9.0))
    out = filter_junk(segs, vad, PipelineConfig(word_speech_tolerance_s=0.3))
    assert [s.text for s in out] == ["real words.", "more real"]


def test_phantoms_are_dropped_unless_clearly_spoken():
    phantom = Segment(tuple(words("Thanks for watching.")), avg_logprob=-0.3)
    real = Segment(tuple(words("we are back.", start=5)), avg_logprob=-0.2, no_speech_prob=0.1)
    cfg = PipelineConfig(word_speech_tolerance_s=10)  # isolate the phantom rule
    # no speech under the phantom: dropped; the rest keeps its scores and order
    assert filter_junk([phantom, real], vad_regions(10, (5.0, 6.5)), cfg) == [real]
    # a streamer clearly saying it (speech under all of it) keeps it
    assert filter_junk([phantom, real], vad_all_speech(10), cfg) == [phantom, real]
    # sentences without letters go regardless of speech
    notes = Segment((RecognizedWord("♪", 1.0, 2.0),))
    assert filter_junk([notes], vad_all_speech(10)) == []


def test_a_stretched_word_end_keeps_the_word_and_its_sentence():
    # "you." ends 0.9 s into a pause (engines stretch word ends); the sentence was spoken
    segs = [
        Segment((RecognizedWord("Thank", 0.0, 0.3, 0.9), RecognizedWord("you.", 0.3, 1.5, 0.9))),
        Segment((RecognizedWord("Welcome", 1.7, 2.1, 0.9), RecognizedWord("back.", 2.1, 2.5, 0.9))),
    ]
    out = filter_junk(segs, vad_regions(5, (0.0, 0.6), (1.6, 2.6)))
    assert [s.text for s in out] == ["Thank you.", "Welcome back."]
    # a word whose whole span stays away from speech is still dropped
    stray = Segment((RecognizedWord("um", 3.5, 3.9),))
    assert [s.text for s in filter_junk([*segs, stray], vad_regions(5, (0.0, 0.6), (1.6, 2.6)))][
        -1
    ] == "Welcome back."


def looped(**scores) -> Segment:
    return Segment(tuple(words("here " * 12)), **scores)


def test_loops_need_corroboration_when_the_engine_scores_segments():
    vad = vad_all_speech(10)
    assert filter_junk([looped()], vad)[0].text == "here here here"  # no scores: 3-gram rule
    assert filter_junk([looped(compression_ratio=3.1, avg_logprob=-0.2)], vad)[0].text == (
        "here here here"
    )
    assert filter_junk([looped(avg_logprob=-1.4)], vad)[0].text == "here here here"
    # the engine is confident and the text isn't degenerate: a real chant, kept
    kept = filter_junk([looped(compression_ratio=1.8, avg_logprob=-0.3)], vad)[0]
    assert len(kept.words) == 12
