"""Decoding with ffmpeg and sample-exact slicing."""

from __future__ import annotations

import subprocess

import numpy as np
import pytest

from clipper_transcriber import audio
from clipper_transcriber.audio import (
    SAMPLE_RATE,
    AudioError,
    DecodeError,
    decode,
    decode_async,
    duration_s,
    encode_aac,
    samples,
    slice_seconds,
)


def lavfi(ffmpeg: str, path, *, seconds: float, video: bool = False, fmt: str = "adts") -> None:
    """A test file from ffmpeg's generators: 48 kHz sine as AAC (+ video in MPEG-TS)."""
    args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if video:
        args += ["-f", "lavfi", "-i", f"testsrc=size=160x120:rate=30:duration={seconds}"]
    args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}"]
    if video:
        args += ["-c:v", "mpeg2video"]
    args += ["-c:a", "aac", "-f", fmt, str(path)]
    subprocess.run(args, check=True)


def test_decode_aac_to_16k_mono_float32(ffmpeg, tmp_path):
    path = tmp_path / "a.aac"
    lavfi(ffmpeg, path, seconds=2.5)
    pcm = decode(path, ffmpeg=ffmpeg)
    assert pcm.dtype == np.float32 and pcm.ndim == 1 and pcm.flags.writeable
    # AAC adds encoder delay/padding of a few frames (~21 ms each at 48 kHz)
    assert abs(duration_s(pcm) - 2.5) < 0.08
    assert 0.05 < float(np.abs(pcm).max()) <= 1.0


def test_decode_ts_takes_the_audio_track(ffmpeg, tmp_path):
    path = tmp_path / "v.ts"
    lavfi(ffmpeg, path, seconds=1.0, video=True, fmt="mpegts")
    pcm = decode(path, ffmpeg=ffmpeg)
    assert abs(duration_s(pcm) - 1.0) < 0.08


async def test_decode_async(ffmpeg, tmp_path):
    path = tmp_path / "a.aac"
    lavfi(ffmpeg, path, seconds=1.0)
    pcm = await decode_async(path, ffmpeg=ffmpeg)
    assert np.array_equal(pcm, decode(path, ffmpeg=ffmpeg))


def test_empty_short_and_broken_files(ffmpeg, tmp_path):
    empty = tmp_path / "empty.aac"
    empty.write_bytes(b"")
    assert len(decode(empty, ffmpeg=ffmpeg)) == 0

    short = tmp_path / "short.aac"
    lavfi(ffmpeg, short, seconds=0.05)
    assert 0 < len(decode(short, ffmpeg=ffmpeg)) < SAMPLE_RATE // 5

    broken = tmp_path / "broken.aac"
    broken.write_bytes(b"this is not audio" * 100)
    with pytest.raises(DecodeError):  # give up on this chunk
        decode(broken, ffmpeg=ffmpeg)


def test_failures_that_are_worth_a_retry_are_audio_errors(ffmpeg, tmp_path):
    with pytest.raises(AudioError):  # e.g. the download went wrong
        decode(tmp_path / "missing.aac", ffmpeg=ffmpeg)
    path = tmp_path / "a.aac"
    lavfi(ffmpeg, path, seconds=0.5)
    with pytest.raises(AudioError, match="FileNotFoundError"):
        decode(path, ffmpeg=str(tmp_path / "no-ffmpeg-here"))
    with pytest.raises(AudioError, match="TimeoutExpired"):
        decode(path, ffmpeg=ffmpeg, timeout_s=1e-6)
    with pytest.raises(AudioError):
        encode_aac(np.zeros(100, np.float32), path, ffmpeg=str(tmp_path / "no-ffmpeg-here"))
    assert not issubclass(DecodeError, AudioError) and not issubclass(AudioError, DecodeError)


@pytest.mark.parametrize("code", [-9, -15, 0xC0000005])
def test_ffmpeg_killed_by_a_signal_is_an_audio_error(code, tmp_path, monkeypatch):
    path = tmp_path / "a.aac"
    path.write_bytes(b"x" * 100)

    def killed(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, code, b"", b"")

    monkeypatch.setattr(audio.subprocess, "run", killed)
    with pytest.raises(AudioError, match="killed"):
        decode(path)


def test_video_without_audio_is_a_decode_error(ffmpeg, tmp_path):
    path = tmp_path / "video_only.ts"
    subprocess.run(
        [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "testsrc=size=160x120:rate=30:duration=1", "-c:v", "mpeg2video", "-f", "mpegts",
         str(path)],
        check=True,
    )  # fmt: skip
    with pytest.raises(DecodeError):
        decode(path, ffmpeg=ffmpeg)


def test_encode_aac_roundtrip(ffmpeg, tmp_path):
    t = np.arange(SAMPLE_RATE) / SAMPLE_RATE
    pcm = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    encode_aac(pcm, tmp_path / "x.aac", ffmpeg=ffmpeg)
    back = decode(tmp_path / "x.aac", ffmpeg=ffmpeg)
    assert abs(len(back) - len(pcm)) < 0.08 * SAMPLE_RATE
    assert abs(float(np.sqrt(np.mean(back[4000:12000] ** 2))) - 0.5 / np.sqrt(2)) < 0.02


def test_slicing_is_sample_exact():
    pcm = np.arange(5 * SAMPLE_RATE, dtype=np.float32)
    assert samples(1.0) == 16000 and samples(0.00003) == 0 and samples(0.0000313) == 1
    part = slice_seconds(pcm, 1.0, 2.0)
    assert len(part) == SAMPLE_RATE and part[0] == 16000 and part[-1] == 31999
    # offsets that aren't representable exactly in binary still hit the right sample
    assert slice_seconds(pcm, 0.1, 0.3)[0] == 1600 and len(slice_seconds(pcm, 0.1, 0.3)) == 3200
    assert slice_seconds(pcm, 4.5)[0] == 72000 and len(slice_seconds(pcm, 4.5)) == 8000
    assert len(slice_seconds(pcm, 4.5, 99)) == 8000  # clamped to the audio
    assert len(slice_seconds(pcm, -1, 0.5)) == 8000
    assert len(slice_seconds(pcm, 3, 2)) == 0
    assert len(slice_seconds(pcm, 9, 10)) == 0
