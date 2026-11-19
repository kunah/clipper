"""ffmpeg stream copies (no re-encoding): the audio track out of a chunk, fMP4 -> MPEG-TS.

Used by the recorder (live chunks) and the backfill tool (VOD chunks), so both store the same
formats: `.ts` video and ADTS `.aac` audio (`storage.keys`).

The functions are blocking (`subprocess.run`) and need the `ffmpeg` executable (PATH or a full
path). From async code run them in a worker thread:

    error = await asyncio.to_thread(ffmpeg.extract_audio, "ffmpeg", media_path, audio_path)

(Windows: the selector event loop that psycopg needs has no asyncio subprocess support.)
They return an error message, or None on success, instead of raising: a chunk with a broken or
missing track is normal (e.g. no audio), and the caller decides what to keep.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

TIMEOUT_S = 300


def copy(ffmpeg: str, src: Path, out: Path, args: list[str]) -> str | None:
    """Run ffmpeg on `src` with output options `args` and `-c copy` into `out`.

    Returns an error message, or None on success (`out` exists and is not empty).
    """
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    try:
        result = subprocess.run(
            [*cmd, *args, "-c", "copy", str(out)], capture_output=True, text=True, timeout=TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        return result.stderr.strip()[-500:] or f"ffmpeg exited with {result.returncode}"
    if not out.exists() or out.stat().st_size == 0:
        return "ffmpeg wrote nothing"
    return None


def extract_audio(ffmpeg: str, src: Path, out: Path) -> str | None:
    """First audio track -> ADTS .aac file."""
    return copy(ffmpeg, src, out, ["-map", "0:a:0", "-f", "adts"])


def remux_to_ts(ffmpeg: str, src: Path, out: Path) -> str | None:
    """Fragmented MP4 (init + fragments) -> MPEG-TS, so storage only ever holds .ts video."""
    return copy(ffmpeg, src, out, ["-map", "0:v:0", "-map", "0:a:0?", "-f", "mpegts"])
