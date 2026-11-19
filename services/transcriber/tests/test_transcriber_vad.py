"""Voice-activity detection: the speech_regions state machine, energy VAD, Silero VAD."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest

from clipper_transcriber.audio import SAMPLE_RATE
from clipper_transcriber.fakes import silence, tone
from clipper_transcriber.vad import (
    FRAME_SAMPLES,
    SILERO_FILENAME,
    EnergyVad,
    ModelDownloadError,
    SileroVad,
    VadParams,
    ensure_silero_model,
    speech_regions,
)

F = FRAME_SAMPLES  # 512 samples = 32 ms
PAD = 480  # default speech_pad_ms = 30 ms
DATA = Path(__file__).parent / "data"


def probs(*runs: tuple[float, int]) -> list[float]:
    """(probability, frames) runs -> a frame probability sequence."""
    return [p for p, n in runs for _ in range(n)]


# --------------------------------------------------------------------------- state machine


def test_one_region_is_padded():
    seq = probs((0.0, 10), (0.9, 20), (0.0, 10))
    assert speech_regions(seq, len(seq) * F) == [(10 * F - PAD, 30 * F + PAD)]


def test_short_blips_are_dropped():
    seq = probs((0.0, 10), (0.9, 7), (0.0, 10))  # 7 frames = 224 ms < 250 ms
    assert speech_regions(seq, len(seq) * F) == []
    seq = probs((0.0, 10), (0.9, 9), (0.0, 10))  # 288 ms
    assert len(speech_regions(seq, len(seq) * F)) == 1


def test_hysteresis_and_min_silence():
    # a dip between neg_threshold (0.35) and threshold changes nothing
    seq = probs((0.9, 10), (0.4, 20), (0.9, 10), (0.0, 10))
    assert speech_regions(seq, len(seq) * F) == [(0, 40 * F + PAD)]
    # a dip below neg_threshold shorter than min_silence (100 ms) doesn't split
    seq = probs((0.9, 10), (0.1, 3), (0.9, 10), (0.0, 10))
    assert len(speech_regions(seq, len(seq) * F)) == 1
    # a long one does
    seq = probs((0.9, 10), (0.1, 10), (0.9, 10), (0.0, 10))
    assert speech_regions(seq, len(seq) * F) == [(0, 10 * F + PAD), (20 * F - PAD, 30 * F + PAD)]


def test_close_regions_split_the_gap_instead_of_padding_over_each_other():
    params = VadParams(speech_pad_ms=100, min_silence_ms=50)  # pad 1600 samples
    seq = probs((0.0, 5), (0.9, 10), (0.0, 4), (0.9, 10), (0.0, 5))  # gap 2048 < 2 * pad
    (a1, b1), (a2, b2) = speech_regions(seq, len(seq) * F, params)
    assert b1 == a2 == 15 * F + 1024  # middle of the gap
    assert a1 == 5 * F - 1600 and b2 == 29 * F + 1600


def test_speech_running_into_the_end_is_kept():
    seq = probs((0.0, 10), (0.9, 10))
    n = len(seq) * F - 100  # last frame zero-padded
    assert speech_regions(seq, n) == [(10 * F - PAD, n)]


def test_long_speech_is_split_at_its_longest_pause():
    # pauses of 128 ms and 256 ms, too short to end speech (min_silence 500 ms); 77 frames
    # of speech in total = 2.5 s > max_speech 2 s
    seq = probs((0.9, 20), (0.2, 4), (0.9, 15), (0.2, 8), (0.9, 30), (0.0, 20))
    params = VadParams(max_speech_s=2.0, min_silence_ms=500)
    regions = speech_regions(seq, len(seq) * F, params)
    # split at the longer pause (frames 39..46), not the shorter one
    assert regions == [(0, 39 * F + PAD), (47 * F - PAD, 77 * F + PAD)]
    assert all(b - a <= 2.0 * SAMPLE_RATE for a, b in regions)


def test_long_speech_without_pause_is_cut_at_max():
    seq = probs((0.9, 100))
    regions = speech_regions(seq, len(seq) * F, VadParams(max_speech_s=1.0))
    assert len(regions) >= 2
    assert all(b - a <= 1.0 * SAMPLE_RATE + 2 * PAD for a, b in regions)


def test_empty_input():
    assert speech_regions([], 0) == []


def test_default_max_speech_fits_the_gate():
    assert VadParams().max_speech_s == pytest.approx(30 - 2 * 0.2)


GOLDEN = json.loads((DATA / "silero_regions_golden.json").read_text())


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda c: c["name"])
def test_regions_match_the_reference_on_real_stream_probabilities(case):
    """Silero probabilities of 60 s of real stream audio; golden regions from the reference port
    verified against silero-vad's own get_speech_timestamps (v6.2.3)."""
    probs = np.load(DATA / "silero_probs_stream_60s.npy")
    params = dict(case["params"])
    params["max_speech_s"] = params["max_speech_s"] or math.inf
    regions = speech_regions(probs, GOLDEN["n_samples"], VadParams(**params))
    assert regions == [tuple(r) for r in case["regions"]]


# --------------------------------------------------------------------------- energy VAD


def test_energy_vad_finds_loud_stretches():
    pcm = np.concatenate([silence(1.0), tone(440, 1.0), silence(1.0)])
    result = EnergyVad().detect(pcm)
    assert result.n_samples == len(pcm) and len(result.probs) == -(-len(pcm) // F)
    ((a, b),) = result.regions_s()
    assert abs(a - 1.0) < 0.07 and abs(b - 2.0) < 0.07
    mask = result.speech_mask()
    assert len(mask) == len(pcm) and mask[16000:32000].mean() > 0.9 and not mask[:15000].any()


def test_energy_vad_threshold_follows_params():
    pcm = tone(440, 1.0, amplitude=0.01)  # about -43 dBFS
    assert EnergyVad(threshold_db=-40).detect(pcm).regions == ()
    assert len(EnergyVad(threshold_db=-50).detect(pcm).regions) == 1
    assert len(EnergyVad(VadParams(threshold=0.8), threshold_db=-50).detect(pcm).regions) == 1


# --------------------------------------------------------------------------- Silero


def test_silero_on_silence_and_noise(silero_model):
    vad = SileroVad(model_path=silero_model)
    quiet = silence(3.0)
    result = vad.detect(quiet)
    assert len(result.probs) == -(-len(quiet) // F)
    assert result.regions == () and float(result.probs.max()) < 0.2
    rng = np.random.default_rng(1)
    noise = (rng.standard_normal(5 * SAMPLE_RATE) * 0.1).astype(np.float32)
    probs_noise = vad.frame_probs(noise)
    assert float((probs_noise >= 0.5).mean()) < 0.1
    assert np.array_equal(probs_noise, vad.frame_probs(noise))  # no state between calls
    assert vad.frame_probs(silence(0)).shape == (0,)
    assert len(vad.frame_probs(silence(0.01))) == 1  # partial frame zero-padded


def speech_like(seconds: float, seed: int = 3) -> np.ndarray:
    """Voiced-sounding test signal: harmonics + noise, syllable-rate amplitude modulation."""
    rng = np.random.default_rng(seed)
    t = np.arange(round(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    voice = np.sin(2 * np.pi * 140 * t) + 0.5 * np.sin(2 * np.pi * 280 * t)
    x = (voice + 0.3 * rng.standard_normal(len(t))) * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t))
    return (0.2 * x).astype(np.float32)


def reference_probs(session, audio: np.ndarray) -> np.ndarray:
    """The spec's loop: prepend the previous input's last 64 samples, carry the state."""
    state = np.zeros((2, 1, 128), np.float32)
    ctx = np.zeros((1, 64), np.float32)
    n = -(-len(audio) // 512)
    buf = np.zeros(n * 512, np.float32)
    buf[: len(audio)] = audio
    out = np.empty(n, np.float32)
    sr = np.array(16000, dtype=np.int64)
    for i in range(n):
        inp = np.concatenate([ctx, buf[None, i * 512 : (i + 1) * 512]], axis=1)
        p, state = session.run(None, {"input": inp, "state": state, "sr": sr})
        ctx = inp[:, -64:]
        out[i] = p[0, 0]
    return out


def test_silero_matches_the_explicit_context_loop(silero_model):
    vad = SileroVad(model_path=silero_model)
    audio = np.concatenate([silence(1.0), speech_like(6.0), silence(0.7)])[:-123]
    probs = vad.frame_probs(audio)
    assert np.array_equal(probs, reference_probs(vad._get_session(), audio))  # bit-exact
    assert float(probs.max()) > 0.0 and len(probs) == -(-len(audio) // F)


def test_silero_downloads_lazily_into_models_dir(silero_model, tmp_path):
    # a models dir that already holds the file (as after the first download): no network
    (tmp_path / SILERO_FILENAME).write_bytes(silero_model.read_bytes())
    vad = SileroVad(tmp_path)
    assert vad._session is None
    vad.load()
    assert vad._session is not None and vad.detect(silence(0.5)).regions == ()


def test_ensure_model_checks_hash_and_writes_atomically(tmp_path):
    source = tmp_path / "src" / "model.onnx"
    source.parent.mkdir()
    source.write_bytes(b"pretend this is a model")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    models = tmp_path / "models"

    path = ensure_silero_model(models, url=source.as_uri(), sha256=digest, filename="m.onnx")
    assert path == models / "m.onnx" and path.read_bytes() == source.read_bytes()

    path.write_bytes(b"corrupt")  # a wrong file is replaced
    assert ensure_silero_model(models, url=source.as_uri(), sha256=digest, filename="m.onnx")
    assert path.read_bytes() == source.read_bytes()

    with pytest.raises(ModelDownloadError, match="sha256"):
        ensure_silero_model(models, url=source.as_uri(), sha256="0" * 64, filename="x.onnx")
    assert sorted(p.name for p in models.iterdir()) == ["m.onnx"]  # no partial files left
