"""Running ffmpeg/ffprobe (blocking; call from a worker thread: the Windows selector loop that
psycopg needs has no asyncio subprocesses). Parsing what they print.

    tools = Tools("ffmpeg", "ffprobe")
    info = tools.probe(path)                  # size, fps, stream starts
    result = tools.run(args, cwd=work)        # FfmpegError on failure
    loud = parse_ebur128(result.stderr)       # ebur128's summary (I, threshold, LRA, true peak)
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from clipper_renderer.graph import Loudness

TIMEOUT_S = 1200.0
# what ffmpeg prints when h264_nvenc can't open an encoder (no driver, no capable GPU, the
# encoder busy or out of sessions)
NVENC_FAILURE = re.compile(
    r"nvenc|NVENC|libnvidia-encode|CUDA_ERROR|OpenEncodeSessionEx|No capable devices|"
    r"Cannot load nvcuda|Driver does not support",
)


class FfmpegError(RuntimeError):
    def __init__(self, message: str, stderr: str = "", returncode: int | None = None) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode

    @property
    def nvenc(self) -> bool:
        """The failure came from NVENC (the `auto` encoder falls back to libx264)."""
        return bool(NVENC_FAILURE.search(self.stderr[-4000:]))


@dataclass(frozen=True, slots=True)
class RunResult:
    stderr: str
    seconds: float


@dataclass(frozen=True, slots=True)
class MediaInfo:
    width: int
    height: int
    fps: Fraction  # the video's r_frame_rate
    duration_s: float | None
    format_start_s: float
    video_start_s: float | None
    audio_start_s: float | None
    has_audio: bool

    @property
    def origin_s(self) -> float:
        """The audio stream's start relative to the file's start (offsets count from it)."""
        if self.audio_start_s is None:
            return 0.0
        return round(max(self.audio_start_s - self.format_start_s, 0.0), 6)


class Tools:
    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> None:
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self._version: str | None = None

    def version(self) -> str:
        """`ffmpeg -version`'s first line (part of the graph hash)."""
        if self._version is None:
            out = self._capture([self.ffmpeg, "-hide_banner", "-version"])
            self._version = out.splitlines()[0].strip() if out else "unknown"
        return self._version

    def has_encoder(self, name: str) -> bool:
        out = self._capture([self.ffmpeg, "-hide_banner", "-encoders"])
        return re.search(rf"\s{re.escape(name)}\s", out) is not None

    def nvenc_works(self) -> bool:
        """A tiny h264_nvenc encode succeeds (the driver and a session are available)."""
        if not self.has_encoder("h264_nvenc"):
            return False
        try:
            self.run(
                ["-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "color=c=black:s=256x256:r=30:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"],
                timeout_s=60,
            )  # fmt: skip
        except FfmpegError:
            return False
        return True

    def _capture(self, cmd: list[str]) -> str:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise FfmpegError(f"{cmd[0]} not usable: {exc}") from exc
        return proc.stdout

    def run(
        self, args: list[str], *, cwd: Path | str | None = None, timeout_s: float = TIMEOUT_S
    ) -> RunResult:
        import time

        started = time.monotonic()
        try:
            proc = subprocess.run(
                [self.ffmpeg, *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise FfmpegError(f"ffmpeg timed out after {timeout_s:.0f} s") from exc
        except OSError as exc:
            raise FfmpegError(f"ffmpeg not usable ({self.ffmpeg}): {exc}") from exc
        if proc.returncode != 0:
            tail = proc.stderr.strip()[-1500:]
            raise FfmpegError(
                f"ffmpeg failed ({proc.returncode}): {tail}", proc.stderr, proc.returncode
            )
        return RunResult(proc.stderr, round(time.monotonic() - started, 3))

    def probe(self, path: Path | str) -> MediaInfo:
        cmd = [
            self.ffprobe, "-v", "error", "-print_format", "json", "-show_format",
            "-show_streams", str(path),
        ]  # fmt: skip
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise FfmpegError(f"ffprobe not usable ({self.ffprobe}): {exc}") from exc
        if proc.returncode != 0:
            raise FfmpegError(f"ffprobe failed on {path}: {proc.stderr.strip()[-500:]}")
        return parse_probe(json.loads(proc.stdout))


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_probe(data: dict[str, Any]) -> MediaInfo:
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise FfmpegError("no video stream")
    rate = video.get("r_frame_rate") or video.get("avg_frame_rate") or "0/1"
    fps = Fraction(rate) if rate != "0/0" else Fraction(0)
    if fps <= 0:
        fps = Fraction(video.get("avg_frame_rate") or "30/1")
    fmt = data.get("format") or {}
    v_start = _float(video.get("start_time"))
    a_start = _float(audio.get("start_time")) if audio else None
    starts = [s for s in (v_start, a_start) if s is not None]
    format_start = _float(fmt.get("start_time"))
    if format_start is None:
        format_start = min(starts) if starts else 0.0
    return MediaInfo(
        width=int(video["width"]),
        height=int(video["height"]),
        fps=fps,
        duration_s=_float(fmt.get("duration")),
        format_start_s=format_start,
        video_start_s=v_start,
        audio_start_s=a_start,
        has_audio=audio is not None,
    )


_JSON_BLOCK = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


def parse_loudnorm(stderr: str) -> dict[str, str]:
    """The last loudnorm `print_format=json` block in ffmpeg's log, values as strings."""
    blocks = _JSON_BLOCK.findall(stderr)
    if not blocks:
        raise FfmpegError("no loudnorm measurement in the ffmpeg log", stderr)
    return {k: str(v) for k, v in json.loads(blocks[-1]).items()}


_SUMMARY = re.compile(
    r"Integrated loudness:\s+I:\s+(?P<i>\S+) LUFS\s+Threshold:\s+(?P<thresh>\S+) LUFS\s+"
    r"Loudness range:\s+LRA:\s+(?P<lra>\S+) LU.*?True peak:\s+Peak:\s+(?P<tp>\S+) dBFS",
    re.S,
)


def parse_ebur128(stderr: str) -> Loudness:
    """The last `ebur128=peak=true` summary in ffmpeg's log."""
    found = list(_SUMMARY.finditer(stderr))
    if not found:
        raise FfmpegError("no ebur128 summary in the ffmpeg log", stderr)
    m = found[-1]
    return Loudness(i=m["i"], thresh=m["thresh"], lra=m["lra"], tp=m["tp"])


def number(value: str) -> float | None:
    """A measured value as a float; None for -inf/inf/nan and -70 LUFS (silence)."""
    try:
        v = float(value)
    except ValueError:
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v
