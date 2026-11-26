"""Audio as 16 kHz mono float32 PCM: decoding (ffmpeg) and sample-exact time slicing.

We decode ourselves and hand engines plain arrays, never files (DESIGN.md §16 step 3).
Offsets are seconds from the start of the array; offset t is sample `round(t * 16000)`.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import numpy as np
import numpy.typing as npt

SAMPLE_RATE = 16_000

Pcm = npt.NDArray[np.float32]


class AudioError(RuntimeError):
    """ffmpeg could not run or was killed: not installed, OS error, timeout, signal (OOM
    killer, shutdown), unreadable input path. Transient or a setup problem: retry the job."""


class DecodeError(RuntimeError):
    """ffmpeg ran but could not decode the file (broken data, no audio track). Retrying
    won't help: give up on the chunk."""


def _killed(returncode: int) -> bool:
    """ffmpeg didn't fail on the data but was stopped: a POSIX signal (negative code) or a
    Windows crash status (NTSTATUS 0xC0xxxxxx, e.g. 0xC0000005). ffmpeg's own error exits
    (negative AVERROR codes, seen as large unsigned values on Windows) lie outside that range."""
    return returncode < 0 or 0xC0000000 <= returncode <= 0xC0FFFFFF


def samples(seconds: float) -> int:
    """Offset in seconds -> sample index (sample-exact, the same rounding everywhere)."""
    return round(seconds * SAMPLE_RATE)


def duration_s(pcm: Pcm) -> float:
    return len(pcm) / SAMPLE_RATE


def empty() -> Pcm:
    return np.zeros(0, dtype=np.float32)


def slice_seconds(pcm: Pcm, start_s: float, end_s: float | None = None) -> Pcm:
    """`pcm[start, end)` by offsets in seconds, clamped to the array (a view, no copy)."""
    a = min(max(samples(start_s), 0), len(pcm))
    b = len(pcm) if end_s is None else min(max(samples(end_s), a), len(pcm))
    return pcm[a:b]


def decode(path: Path | str, *, ffmpeg: str = "ffmpeg", timeout_s: float = 120.0) -> Pcm:
    """First audio track of `path` (.aac, .ts, ...) -> 16 kHz mono float32 in [-1, 1].

    An empty file gives an empty array. Raises DecodeError for a file ffmpeg can't decode,
    AudioError when ffmpeg can't run (or the file can't be read).
    """
    path = Path(path)
    try:
        if path.stat().st_size == 0:
            return empty()
    except OSError as exc:
        raise AudioError(f"{type(exc).__name__}: {exc}") from exc
    cmd = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        "f32le",
        "pipe:1",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioError(f"{type(exc).__name__}: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()[-500:]
        if _killed(result.returncode):
            raise AudioError(f"ffmpeg was killed (exit status {result.returncode}) {stderr}")
        raise DecodeError(stderr or f"ffmpeg exited with {result.returncode}")
    data = result.stdout[: len(result.stdout) // 4 * 4]
    return np.frombuffer(data, dtype="<f4").astype(np.float32)  # copy: writable, native order


async def decode_async(
    path: Path | str, *, ffmpeg: str = "ffmpeg", timeout_s: float = 120.0
) -> Pcm:
    """`decode` in a worker thread (Windows' selector loop has no asyncio subprocesses)."""
    return await asyncio.to_thread(decode, path, ffmpeg=ffmpeg, timeout_s=timeout_s)


def encode_aac(
    pcm: Pcm, path: Path | str, *, ffmpeg: str = "ffmpeg", bitrate: str = "128k"
) -> None:
    """16 kHz mono PCM -> ADTS .aac at 48 kHz, like the recorder's audio files (tests, tools)."""
    cmd = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "f32le",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "1",
        "-i",
        "pipe:0",
        "-ar",
        "48000",
        "-c:a",
        "aac",
        "-b:a",
        bitrate,
        "-f",
        "adts",
        str(path),
    ]
    data = np.ascontiguousarray(pcm, dtype="<f4").tobytes()
    try:
        result = subprocess.run(cmd, input=data, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioError(f"{type(exc).__name__}: {exc}") from exc
    if result.returncode != 0:
        raise AudioError(result.stderr.decode(errors="replace").strip()[-500:])
