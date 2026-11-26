"""Where a live chunk's audio lies in a VOD: the audio T0 of the VOD on the live media clock.

Shared by `calibrate` (read-only report) and `vod --fill-gaps` (the T0 its chunks use, DESIGN
§17). A live chunk's `start_utc` is the live PDT, so `T0_audio = chunk.start_utc - (VOD offset
where its audio matches)`. The VOD's `audio_only` segments around the chunk (± `search_s` around
a T0 guess) are decoded (ffmpeg, mono 8 kHz) and the chunk's audio is located by normalised FFT
cross-correlation. The match is sample-exact; read the result as ± one AAC frame (~21 ms),
neither the live `.aac` (ADTS) nor the playlist offsets carry the audio PTS.

fMP4 VODs (`EXT-X-MAP`): a window is written as init section + fragments (`.mp4`, as the media
path does) and decoded with the edit list ignored, so decoded sample 0 is the first coded sample
of the window's first fragment, as for MPEG-TS and as in the `.aac` stream copies of the media
path and the recorder (see `calibrate`'s module docstring for what the tests showed).
"""

from __future__ import annotations

import asyncio
import statistics
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from botocore.exceptions import BotoCoreError, ClientError

from clipper_backfill.download import Downloader, DownloadError
from clipper_backfill.pipeline import MediaError, write_media
from clipper_core.logs import get_logger
from clipper_twitch.hls import VodSegment

log = get_logger(__name__)

RATE = 8000  # Hz, mono: enough for a sample-exact match of speech/game audio
MIN_SCORE = 0.5  # a weaker best match is reported but not used for T0_audio
ENERGY_FLOOR = 1e-6  # haystack windows quieter than this share of the loudest one score 0
# Not `clipper-backfill-*`: a backfill run's stale-dir cleanup must never remove a live calibrate.
TEMP_PREFIX = "clipper-calibrate-"


class ReadStore(Protocol):
    def get_bytes(self, key: str) -> bytes: ...


class LiveChunk(Protocol):
    """A live `chunks` row with audio (seq, start_utc, end_utc, audio_key)."""

    seq: int
    start_utc: datetime
    end_utc: datetime
    audio_key: str


def decode_pcm(ffmpeg: str, path: Path, rate: int = RATE) -> np.ndarray:
    """The first audio track as mono float32 samples at `rate` (blocking: run in a thread).

    Sample 0 is the first coded sample of the file: an `.mp4` (fMP4 init section + fragments) is
    read with its edit list ignored (`-ignore_editlist`, a mov demuxer option: not for TS/ADTS)."""
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if path.suffix == ".mp4":
        cmd += ["-ignore_editlist", "1"]
    cmd += ["-i", str(path)]
    cmd += ["-map", "0:a:0", "-ac", "1", "-ar", str(rate), "-f", "s16le", "pipe:1"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError(f"decode {path.name}: {type(exc).__name__}: {exc}") from exc
    if out.returncode != 0 or not out.stdout:
        error = out.stderr.decode("utf-8", "replace").strip()[-300:]
        raise MediaError(f"decode {path.name}: {error or 'no audio'}")
    return np.frombuffer(out.stdout, dtype="<i2").astype(np.float32)


def locate(haystack: np.ndarray, needle: np.ndarray) -> tuple[int, float]:
    """(index in `haystack` where `needle` fits best, normalised correlation there: 1 = same).

    Windows of `haystack` with almost no energy (silence, a muted stretch) score 0 instead of
    dividing by ~0; scores are clamped to [-1, 1] against rounding."""
    n, m = len(haystack), len(needle)
    if m == 0 or m > n:
        raise ValueError(f"needle of {m} samples does not fit a haystack of {n}")
    size = 1 << (n + m - 1).bit_length()
    h = haystack.astype(np.float64)
    k = needle.astype(np.float64)
    corr = np.fft.irfft(np.fft.rfft(h, size) * np.conj(np.fft.rfft(k, size)), size)[: n - m + 1]
    sums = np.concatenate(([0.0], np.cumsum(h * h)))
    energy = np.sqrt(np.maximum(sums[m:] - sums[:-m], 0.0) * float(np.dot(k, k)))
    floor = ENERGY_FLOOR * float(energy.max()) if energy.size else 0.0
    if floor <= 0.0:  # all silent (or a silent needle): nothing to match
        return 0, 0.0
    score = np.where(energy > floor, corr / np.maximum(energy, floor), 0.0)
    score = np.clip(score, -1.0, 1.0)
    best = int(np.argmax(score))
    return best, float(score[best])


@dataclass(frozen=True, slots=True)
class AudioMatch:
    seq: int
    chunk_start: datetime  # live PDT of the chunk's first segment
    vod_offset_s: float  # where the chunk's audio starts in the VOD
    score: float

    @property
    def t0(self) -> datetime:
        return self.chunk_start - timedelta(seconds=self.vod_offset_s)

    @property
    def good(self) -> bool:
        return self.score >= MIN_SCORE


def median_t0(matches: Sequence[AudioMatch]) -> datetime | None:
    """T0_audio: the median T0 of the good matches (score >= MIN_SCORE), None without one."""
    good = [m for m in matches if m.good]
    if not good:
        return None
    base = good[0].t0
    return base + timedelta(seconds=statistics.median((m.t0 - base).total_seconds() for m in good))


def spread_s(matches: Sequence[AudioMatch]) -> float:
    """max - min of the good matches' T0, in seconds (0 with fewer than two)."""
    good = [m.t0 for m in matches if m.good]
    return (max(good) - min(good)).total_seconds() if len(good) > 1 else 0.0


def one_init(segs: Sequence[VodSegment]) -> bool:
    """All under the same `EXT-X-MAP` (or all MPEG-TS)."""
    return len({s.init_uri for s in segs}) <= 1


def window_segments(
    chunk: Any, segments: Sequence[VodSegment], t0_guess: datetime, *, search_s: float
) -> list[VodSegment] | None:
    """The VOD segments to search for `chunk` (± `search_s` around where `t0_guess` puts it), or
    None when that range is not usable: outside the VOD, a muted segment in it, or an fMP4 init
    section change (fragments after it need the other init section, so they don't concatenate)."""
    vod_end = segments[-1].end_s if segments else 0.0
    start = (chunk.start_utc - t0_guess).total_seconds() - search_s
    end = (chunk.end_utc - t0_guess).total_seconds() + search_s
    if start < 0 or end > vod_end:
        return None
    segs = [s for s in segments if s.end_s > start and s.offset_s < end]
    if not segs or any(s.muted for s in segs) or not one_init(segs):
        return None
    return segs


async def match_chunk(
    downloader: Downloader,
    store: ReadStore,
    ffmpeg: str,
    tmp: Path,
    chunk: LiveChunk,
    segs: list[VodSegment],
) -> AudioMatch:
    """Where the live chunk's audio lies in the VOD segments `segs`. MediaError (or
    DownloadError): its window can't be read."""
    if not one_init(segs):  # `window_segments` leaves these out
        raise MediaError("the fMP4 init section changes inside its window")
    init = b""
    init_uri = segs[0].init_uri
    if init_uri:  # fMP4: the fragments are unreadable without it
        data = await downloader.init_section(init_uri)
        if data is None:
            raise MediaError("the VOD's fMP4 init section is gone")
        init = data
    datas = await downloader.segments(segs)
    if any(d is None for d in datas.values()):
        raise MediaError("a VOD segment of its window is gone")
    vod_path = tmp / f"vod_{chunk.seq}{'.mp4' if init_uri else '.ts'}"
    await asyncio.to_thread(write_media, vod_path, init, [datas[s.index] for s in segs])
    live_path = tmp / f"live_{chunk.seq}.aac"
    live_path.write_bytes(await asyncio.to_thread(store.get_bytes, chunk.audio_key))
    vod_pcm = await asyncio.to_thread(decode_pcm, ffmpeg, vod_path)
    live_pcm = await asyncio.to_thread(decode_pcm, ffmpeg, live_path)
    index, score = await asyncio.to_thread(locate, vod_pcm, live_pcm)
    match = AudioMatch(
        seq=chunk.seq,
        chunk_start=chunk.start_utc,
        vod_offset_s=segs[0].offset_s + index / RATE,
        score=score,
    )
    log.info(
        "audio matched",
        seq=chunk.seq,
        vod_offset_s=round(match.vod_offset_s, 4),
        score=round(score, 3),
        t0=match.t0.isoformat(),
    )
    return match


async def match_windows(
    downloader: Downloader,
    store: ReadStore,
    ffmpeg: str,
    windows: Sequence[tuple[LiveChunk, list[VodSegment]]],
    *,
    temp_dir: Path | None = None,
) -> tuple[list[AudioMatch], list[str]]:
    """Match every (chunk, segments) window in its own temp dir (`clipper-calibrate-*`): the
    matches, and a note per window that could not be read (a download failing after its retries,
    an init section or segment gone, the live audio not readable from object storage, ffmpeg
    failing, decoded shorter than the chunk)."""
    matches: list[AudioMatch] = []
    notes: list[str] = []
    if not windows:
        return matches, notes
    with tempfile.TemporaryDirectory(
        prefix=TEMP_PREFIX, dir=temp_dir, ignore_cleanup_errors=True
    ) as tmp:
        for chunk, segs in windows:
            try:
                matches.append(await match_chunk(downloader, store, ffmpeg, Path(tmp), chunk, segs))
            # DownloadError: failed for good after retries (403, 5xx, network); ValueError:
            # decoded shorter than the chunk; ClientError/BotoCoreError: object storage (a missing
            # key, the endpoint unreachable)
            except (MediaError, DownloadError, ValueError, ClientError, BotoCoreError) as exc:
                notes.append(f"chunk {chunk.seq}: {type(exc).__name__}: {exc}")
    return matches, notes
