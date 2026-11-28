"""Stills of the stream's video for the judge (`inputs.Frame`; prompts with a `$frames` section).

`frame_times` (pure) spreads up to `JudgeParams.frames_n` times evenly over the candidate's core
(moment +- `core_pad_s`, inside the context), at least `frame_min_gap_s` apart, keeping only
times with video. `FrameGrabber` takes them from the chunks' video files: each chunk downloaded
once (the last few kept on disk), one ffmpeg call per still, opened `SEEK_PREROLL_S` early and
trimmed by timestamp as the label cutter and the renderer do (ffmpeg's MPEG-TS seek lands up to a
GOP late), scaled to `frame_width`, JPEG.

Used by `clipper-judge eval` (`--frames`); the live service doesn't take stills yet (it would
need ffmpeg and the chunks' video in the judge's image: DESIGN §19, proposed).
"""

from __future__ import annotations

import hashlib
import subprocess
from collections import OrderedDict
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from clipper_core.logs import get_logger
from clipper_core.timeline import Interval, ensure_utc
from clipper_judge.inputs import Frame, JudgeInputs, bridge_jitter
from clipper_judge.label import SEEK_PREROLL_S, ChunkVideo
from clipper_judge.params import DEFAULT_PARAMS, JudgeParams

log = get_logger(__name__)

KEEP_CHUNKS = 6  # chunk files kept on disk (neighbouring stills share them)
JPEG_QUALITY = 4  # ffmpeg -q:v (2 best .. 31)


def frame_times(inputs: JudgeInputs, params: JudgeParams = DEFAULT_PARAMS) -> list[datetime]:
    """Up to `frames_n` times evenly over the core inside the context (the middle of each of `n`
    equal parts), fewer when the core is short (`frame_min_gap_s`), only where there is video."""
    n = params.frames_n
    if n <= 0:
        return []
    c = inputs.candidate
    pad = timedelta(seconds=params.core_pad_s)
    core = Interval(ensure_utc(c.moment_from) - pad, ensure_utc(c.moment_to) + pad)
    span = core.intersection(inputs.context)
    if span is None:
        return []
    if params.frame_min_gap_s > 0:
        n = max(1, min(n, int(span.seconds // params.frame_min_gap_s)))
    step = span.seconds / n
    times = [span.start + timedelta(seconds=(i + 0.5) * step) for i in range(n)]
    video = bridge_jitter(sorted(inputs.video, key=lambda v: v.start))
    return [t for t in times if any(v.start <= t < v.end for v in video)]


def still_command(
    ffmpeg: str, source: str, offset_s: float, width: int, quality: int = JPEG_QUALITY
) -> list[str]:
    """One JPEG of the frame at `offset_s` into `source`, on stdout."""
    ss = max(offset_s - SEEK_PREROLL_S, 0.0)
    pre = offset_s - ss
    graph = f"trim=start={pre:.3f},setpts=PTS-STARTPTS,scale={width}:-2"
    return [
        ffmpeg, "-v", "error", "-nostdin",
        "-ss", f"{ss:.3f}", "-t", f"{pre + 2.0:.3f}", "-i", source,
        "-map", "0:v:0", "-vf", graph, "-frames:v", "1",
        "-c:v", "mjpeg", "-q:v", str(quality), "-f", "image2pipe", "pipe:1",
    ]  # fmt: skip


class FrameGrabber:
    """Stills from the chunks' video files (blocking: call it in a thread)."""

    def __init__(
        self,
        store: Any,
        work: Path,
        *,
        ffmpeg: str = "ffmpeg",
        width: int = DEFAULT_PARAMS.frame_width,
    ) -> None:
        self.store = store
        self.work = work
        self.ffmpeg = ffmpeg
        self.width = width
        self.files: OrderedDict[str, Path] = OrderedDict()
        self.grabbed = 0
        self.failed = 0

    def chunk_file(self, key: str) -> Path:
        if key in self.files:
            self.files.move_to_end(key)
            return self.files[key]
        path = self.work / "chunks" / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".ts")
        path.parent.mkdir(parents=True, exist_ok=True)
        download = getattr(self.store, "download_file", None)
        if download is not None:  # streamed to disk (chunks are ~60 MB)
            download(key, path)
        else:
            path.write_bytes(self.store.get_bytes(key))
        self.files[key] = path
        while len(self.files) > KEEP_CHUNKS:
            _, old = self.files.popitem(last=False)
            old.unlink(missing_ok=True)
        return path

    def grab(self, chunks: Sequence[ChunkVideo], times: Sequence[datetime]) -> list[Frame]:
        """A still per time that a chunk with video holds; a failed still is logged and left out."""
        out: list[Frame] = []
        ordered = sorted(chunks, key=lambda c: ensure_utc(c.start))
        for t in times:
            chunk = next((c for c in ordered if ensure_utc(c.start) <= t < ensure_utc(c.end)), None)
            if chunk is None:
                continue
            offset = (t - ensure_utc(chunk.start)).total_seconds()
            try:
                source = str(self.chunk_file(chunk.video_key))
                proc = subprocess.run(
                    still_command(self.ffmpeg, source, offset, self.width),
                    capture_output=True,
                    timeout=60,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                self.failed += 1
                log.warning("still not taken", at=t.isoformat(), error=str(exc))
                continue
            if proc.returncode != 0 or not proc.stdout:
                self.failed += 1
                log.warning(
                    "still not taken",
                    at=t.isoformat(),
                    code=proc.returncode,
                    stderr=proc.stderr.decode("utf-8", "replace")[-300:],
                )
                continue
            self.grabbed += 1
            out.append(Frame(t, proc.stdout))
        return out

    def close(self) -> None:
        for path in self.files.values():
            path.unlink(missing_ok=True)
        self.files.clear()
