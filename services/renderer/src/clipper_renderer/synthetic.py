"""Synthetic chunks for tests and smoke runs (DESIGN §20 "Interfaces + fakes"), made by ffmpeg's
lavfi sources and encoded like Twitch's (H.264 with B-frames + AAC in MPEG-TS):

- video: every frame is one flat grey whose luma encodes the global frame index:
  Y = 16 + 4 * (n mod 50), n = frames since the synthetic stream's origin `t0`;
- audio: a quiet 220 Hz tone, replaced by a 1 kHz beep for the first 40 ms of every whole second
  of `t0` time (so a beep marks a known UTC second).

`make_chunk()` writes one chunk and returns its UTC range as a recorder would store it: the
chunk's start is its first audio sample (the transcriber's origin), which the AAC encoder's
priming puts ~21 ms before the content's start (measured from the file, not assumed).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from clipper_renderer.media import Tools

LUMA_STEP = 4
LUMA_PERIOD = 50
BEEP_S = 0.04
BEEP_HZ = 1000


@dataclass(frozen=True, slots=True)
class SynthChunk:
    path: Path
    start_utc: datetime  # the first audio sample
    end_utc: datetime
    content_start_s: float  # seconds since t0 of the first frame


def luma(n: int) -> int:
    return 16 + LUMA_STEP * (n % LUMA_PERIOD)


def frame_index_from_luma(y: float, expected: float) -> int:
    """The frame index nearest `expected` whose luma is `y` (the index is only known mod 50)."""
    r = round((y - 16) / LUMA_STEP) % LUMA_PERIOD
    base = int(expected) - int(expected) % LUMA_PERIOD
    candidates = [base + r + k * LUMA_PERIOD for k in (-1, 0, 1)]
    return min(candidates, key=lambda c: abs(c - expected))


def make_chunk(
    tools: Tools,
    path: Path,
    *,
    t0: datetime,
    start_s: float,
    duration_s: float,
    fps: int = 60,
    size: tuple[int, int] = (320, 180),
    audio: bool = True,
) -> SynthChunk:
    n0 = round(start_s * fps)
    w, h = size
    video = (
        f"color=c=black:s={w}x{h}:r={fps}:d={duration_s},format=yuv420p,"
        f"geq=lum='16+{LUMA_STEP}*mod(N+{n0},{LUMA_PERIOD})':cb=128:cr=128"
    )
    beep = f"lt(mod(t+{start_s},1),{BEEP_S})"
    tone = f"if({beep},0.12*sin(2*PI*{BEEP_HZ}*t),0.1*sin(2*PI*220*t))"
    args = ["-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", video]
    if audio:
        args += ["-f", "lavfi", "-i", f"aevalsrc=exprs='{tone}|{tone}':s=48000:d={duration_s}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-g", str(2 * fps), "-bf", "2"]
    args += ["-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-b:a", "128k"]
    args += ["-f", "mpegts", str(path)]
    path.parent.mkdir(parents=True, exist_ok=True)
    tools.run(args, timeout_s=120)
    info = tools.probe(path)
    priming = 0.0
    if audio and info.audio_start_s is not None and info.video_start_s is not None:
        priming = max(info.video_start_s - info.audio_start_s, 0.0)
    start = t0 + timedelta(seconds=start_s - priming)
    end = t0 + timedelta(seconds=start_s + duration_s)
    return SynthChunk(path, start, end, start_s)
