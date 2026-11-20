"""The job handler's pure parts: window planning (handover rules), window audio, lock keys,
stats, and the engine registry. No database, no object storage."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from clipper_transcriber.audio import SAMPLE_RATE
from clipper_transcriber.engines import RecognizedWord
from clipper_transcriber.fakes import FakeSoundTagger, FakeSpeechEngine
from clipper_transcriber.handler import (
    ChunkState,
    JobStats,
    TranscriptState,
    WindowPlan,
    contiguous,
    plan_window,
    stream_lock_key,
    transcript_rows,
    window_pcm,
)
from clipper_transcriber.pipeline import Segment, Timings, WindowResult
from clipper_transcriber.service import build_engines, engine_device
from clipper_transcriber.settings import TranscriberSettings
from clipper_transcriber.vad import EnergyVad

T0 = datetime(2026, 11, 19, 12, 0, 0, 400000, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def transcript(
    chunk_id: int, owned: tuple[float, float], tail: float | None = None
) -> TranscriptState:
    return TranscriptState(
        chunk_id=chunk_id,
        owned_from=at(owned[0]),
        owned_to=at(owned[1]),
        tail_from=None if tail is None else at(tail),
        words_key=f"words/{chunk_id}",
        model="fake",
        language="en",
    )


def chunk(
    seq: int,
    a: float,
    b: float,
    *,
    owned: tuple[float, float] | None = None,
    tail: float | None = None,
    audio: bool = True,
) -> ChunkState:
    cid = 100 + seq
    return ChunkState(
        id=cid,
        stream_id=1,
        seq=seq,
        start_utc=at(a),
        end_utc=at(b),
        audio_key=f"audio/{seq}" if audio else None,
        transcript=None if owned is None else transcript(cid, owned, tail),
    )


def plan(n: ChunkState, p: ChunkState | None = None, s: ChunkState | None = None, **kw):
    return plan_window(n, p, s, **kw)


N = chunk(1, 60, 120)


def test_alone_the_window_is_the_chunk():
    assert plan(N) == WindowPlan(window_start=at(60), owned_to=at(120), keep_tail=True)


def test_previous_without_tail_or_untranscribed_is_left_alone():
    assert plan(N, chunk(0, 0, 60, owned=(0, 60))).window_start == at(60)
    assert plan(N, chunk(0, 0, 60)).window_start == at(60)


def test_takeover_starts_at_the_previous_tail():
    p = plan(N, chunk(0, 0, 60, owned=(0, 60), tail=55.5))
    assert p == WindowPlan(
        window_start=at(55.5),
        owned_to=at(120),
        keep_tail=True,
        takeover=True,
        prev_new_owned_to=at(55.5),
    )


def test_takeover_is_capped():
    p = plan(N, chunk(0, 0, 60, owned=(0, 60), tail=20))
    assert p.takeover and p.window_start == at(30) == p.prev_new_owned_to
    assert p.prev_capped_from == at(20)  # P keeps [20, 30) non-provisionally
    p = plan(N, chunk(0, 0, 60, owned=(0, 60), tail=50), cap=timedelta(seconds=5))
    assert p.window_start == at(55) and p.prev_capped_from == at(50)


def test_takeover_never_starts_before_the_previous_chunk():
    # P took its predecessor's tail over and its own tail starts before P's start
    short = chunk(0, 50, 60, owned=(45, 60), tail=46)
    p = plan(N, short)
    assert p.takeover and p.window_start == at(50) and p.prev_capped_from == at(46)


@pytest.mark.parametrize(
    ("p_end", "takes_over"),
    # default tolerance: contracts.CHUNK_JITTER (0.5 s)
    [(60, True), (59.6, True), (60.45, True), (59.4, False), (60.6, False)],
)
def test_takeover_only_from_a_contiguous_chunk(p_end, takes_over):
    p = plan(N, chunk(0, 0, p_end, owned=(0, p_end), tail=55))
    assert p.takeover is takes_over
    assert p.window_start == (at(55) if takes_over else at(60))


def test_takeover_window_never_starts_after_the_chunk():
    # P overlaps N by 400 ms (jitter) and its tail starts inside the overlap
    p = plan(N, chunk(0, 0, 60.4, owned=(0, 60.4), tail=60.2))
    assert p.takeover and p.window_start == at(60) == p.prev_new_owned_to
    assert p.prev_capped_from is None


def test_no_takeover_from_a_chunk_without_audio_or_a_broken_row():
    assert not plan(N, chunk(0, 0, 60, owned=(0, 60), tail=55, audio=False)).takeover
    # P's owned range starts after N's start (can't shorten P to before its owned_from)
    assert not plan(N, chunk(0, 0, 60.04, owned=(60.02, 60.04), tail=60.03)).takeover


def test_retake_after_an_earlier_takeover():
    p_done = chunk(0, 0, 60, owned=(0, 55.5))  # tail already handed over
    n_done = chunk(1, 60, 120, owned=(55.5, 120))
    p = plan(n_done, p_done)
    assert p == WindowPlan(window_start=at(55.5), owned_to=at(120), keep_tail=True, retake=True)
    # P doesn't end where N's owned range starts: no re-take
    assert plan(n_done, chunk(0, 0, 60, owned=(0, 60))).window_start == at(60)
    # N's earlier attempt didn't take anything over
    assert not plan(chunk(1, 60, 120, owned=(60, 120)), p_done).retake


def test_next_chunk_already_transcribed_bounds_ownership():
    s_took_over = chunk(2, 120, 180, owned=(117.2, 180))
    p = plan(N, None, s_took_over)
    assert (p.owned_to, p.keep_tail) == (at(117.2), False)
    s_own = chunk(2, 120.03, 180, owned=(120.03, 180))  # jitter hole: N keeps its end
    assert (plan(N, None, s_own).owned_to, plan(N, None, s_own).keep_tail) == (at(120), False)
    assert plan(N, None, chunk(2, 120, 180)).keep_tail  # recorded, not transcribed
    far = chunk(2, 121, 180, owned=(121, 180))  # not contiguous
    assert (plan(N, None, far).owned_to, plan(N, None, far).keep_tail) == (at(120), True)


def test_rerun_between_two_transcribed_neighbours():
    p = plan(
        chunk(1, 60, 120, owned=(55.5, 117.2)),
        chunk(0, 0, 60, owned=(0, 55.5)),
        chunk(2, 120, 180, owned=(117.2, 180)),
    )
    assert p == WindowPlan(window_start=at(55.5), owned_to=at(117.2), keep_tail=False, retake=True)


def test_contiguous():
    tol = timedelta(milliseconds=50)
    assert contiguous(at(60), at(60.05), tol) and contiguous(at(60.05), at(60), tol)
    assert not contiguous(at(60), at(60.051), tol)
    # the default is the chunk jitter, also in the settings
    assert plan(N, chunk(0, 0, 59.5, owned=(0, 59.5), tail=55)).takeover
    assert not plan(N, chunk(0, 0, 59.49, owned=(0, 59.49), tail=55)).takeover
    assert not plan(
        N, chunk(0, 0, 59.6, owned=(0, 59.6), tail=55), tolerance=timedelta(milliseconds=50)
    ).takeover
    assert TranscriberSettings(engine="fake", _env_file=None).contiguity_tolerance_s == 0.5


def segment(words: list[tuple[str, float, float]], *, provisional: bool = False) -> Segment:
    return Segment(
        tuple(RecognizedWord(t, a, b, 0.9) for t, a, b in words), provisional=provisional
    )


def window_result(segments: list[Segment], tail_from: float | None) -> WindowResult:
    words = tuple(w for s in segments for w in s.words)
    return WindowResult(
        model="fake",
        start_utc=at(55),
        words=words,
        segments=tuple(segments),
        tail_from=tail_from,
        metrics=(),
        speech_regions=(),
        timings=Timings(1, 1, 0, 0, 0, 0.1),
    )


def test_transcript_rows_keep_tail_and_provisional_flags_consistent():
    n = chunk(1, 60, 70)
    plan_ = WindowPlan(window_start=at(55), owned_to=at(70), keep_tail=True)
    segs = [segment([("a.", 1, 2)]), segment([("b", 13, 14.9)], provisional=True)]
    tail, rows = transcript_rows(n, plan_, window_result(segs, 12.8))
    assert tail == at(67.8) and [r["provisional"] for r in rows] == [False, True]
    assert rows[0]["start_utc"] == at(56) and rows[1]["end_utc"] == at(69.9)
    # a tail that would break the CHECK (at or after owned_to) is dropped: nothing provisional
    tail, rows = transcript_rows(n, plan_, window_result(segs, 15.0))
    assert tail is None and [r["provisional"] for r in rows] == [False, False]
    # segments starting outside the owned range are not written
    short = WindowPlan(window_start=at(55), owned_to=at(67), keep_tail=False)
    tail, rows = transcript_rows(n, short, window_result(segs, None))
    assert tail is None and [r["text"] for r in rows] == ["a."]


# --------------------------------------------------------------------------- window audio


def test_window_pcm_prepends_the_previous_audio_sample_exactly():
    prev = np.arange(10 * SAMPLE_RATE, dtype=np.float32)  # P: [0, 10)
    own = np.full(SAMPLE_RATE, -1, np.float32)  # N: [10, 11)
    pcm = window_pcm(own, at(10), at(7.5), prev, at(0))
    assert len(pcm) == int(3.5 * SAMPLE_RATE)
    assert pcm[0] == 7.5 * SAMPLE_RATE and pcm[int(2.5 * SAMPLE_RATE) - 1] == 10 * SAMPLE_RATE - 1
    assert (pcm[int(2.5 * SAMPLE_RATE) :] == -1).all()
    assert window_pcm(own, at(10), at(10)) is own


def test_window_pcm_cuts_overlap_and_pads_holes():
    own = np.full(100, -1, np.float32)
    prev = np.ones(SAMPLE_RATE, np.float32)  # P: [0, 1)
    # P runs 30 ms past N's start: cut, N's audio starts at exactly 0.5 s
    pcm = window_pcm(own, at(0.97), at(0.47), prev, at(0))
    assert len(pcm) == SAMPLE_RATE // 2 + 100 and (pcm[: SAMPLE_RATE // 2] == 1).all()
    # P ends 20 ms before N's start (jitter hole): zeros
    pcm = window_pcm(own, at(1.02), at(0.52), prev, at(0))
    hole = round(0.02 * SAMPLE_RATE)
    assert len(pcm) == SAMPLE_RATE // 2 + 100
    assert (pcm[: SAMPLE_RATE // 2 - hole] == 1).all()
    assert (pcm[SAMPLE_RATE // 2 - hole : SAMPLE_RATE // 2] == 0).all()
    with pytest.raises(ValueError, match="previous chunk"):
        window_pcm(own, at(1), at(0.5))


# --------------------------------------------------------------------------- misc


def test_stream_lock_key_is_stable_and_per_stream():
    assert stream_lock_key(7) == stream_lock_key(7) != stream_lock_key(8)
    assert all(-(2**63) <= stream_lock_key(i) < 2**63 for i in range(100))


def test_realtime_factor_is_an_ewma():
    stats = JobStats()
    stats.record_realtime(40.0)
    assert stats.realtime_factor == 40.0
    stats.record_realtime(60.0)
    assert stats.realtime_factor == pytest.approx(44.0)
    stats.record_realtime(float("inf"))  # no audio processed: ignored
    assert stats.realtime_factor == pytest.approx(44.0)


def test_engine_registry():
    settings = TranscriberSettings(engine="fake", vad="energy", tagger="fake", _env_file=None)
    engines = build_engines(settings)
    assert isinstance(engines.speech, FakeSpeechEngine) and engines.speech.segments
    assert isinstance(engines.vad, EnergyVad) and isinstance(engines.tagger, FakeSoundTagger)
    assert engines.vad.params == settings.vad_params()
    assert engine_device(engines) == "cpu"
    no_tagger = build_engines(settings.model_copy(update={"tagger": "none"}))
    assert no_tagger.tagger is None
    with pytest.raises(ValueError, match=r"unknown speech engine 'nope' \(known: fake, whisper\)"):
        build_engines(settings.model_copy(update={"engine": "nope"}))
    with pytest.raises(ValueError, match="unknown sound tagger"):
        build_engines(settings.model_copy(update={"tagger": "nope"}))


def test_silero_is_loaded_at_build_time(silero_model):
    settings = TranscriberSettings(
        engine="fake", tagger="none", vad="silero", vad_model_path=silero_model, _env_file=None
    )
    engines = build_engines(settings)
    assert engines.vad.name.startswith("silero-vad") and engines.vad._session is not None


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"CLIPPER_TRANSCRIBER_ENGINE": ""}, "invalid transcriber settings"),
        ({"CLIPPER_TRANSCRIBER_ENGINE": "nope"}, "unknown engine"),
        ({"CLIPPER_TRANSCRIBER_ENGINE": "fake", "CLIPPER_TRANSCRIBER_TAGGER": "x"}, "unknown"),
        ({"CLIPPER_TRANSCRIBER_ENGINE": "fake", "CLIPPER_TRANSCRIBER_FFMPEG": "nope"}, "ffmpeg"),
        ({}, "engine libraries not installed"),  # defaults: whisper + ced, extra missing
    ],
)
async def test_entry_point_refuses_to_start_without_a_usable_config(
    env, message, monkeypatch, tmp_path, caplog
):
    from clipper_transcriber import main

    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # as on the dev env: no extra
    for name in ("ENGINE", "TAGGER", "FFMPEG"):
        monkeypatch.delenv(f"CLIPPER_TRANSCRIBER_{name}", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(main, "setup_logging", lambda settings: None)  # keep pytest's logging
    assert await main.amain() == 1
    assert any(m.startswith(message) for m in caplog.messages), caplog.messages
