"""`clipper-backfill calibrate <video>`: a broadcast we recorded live AND its VOD (DESIGN §17).

Read-only (nothing is written, no lease). It answers the time-model questions a VOD alone can't:

1. **T0 methods vs audio.** The VOD's `audio_only` segments around a few of our live chunks are
   cross-correlated with the chunks' audio (from object storage). A live chunk's start is the
   live PDT, so `T0_audio = chunk.start_utc - (VOD offset where its audio matches)` is the VOD's
   T0 on the live media clock. The match itself is sample-exact (0.125 ms at 8 kHz; < 1 ms in the
   synthetic test), but where a segment's first audio frame sits against its PDT / playlist offset
   is known only to about one AAC frame (~21 ms): neither the live `.aac` (ADTS, no timestamps)
   nor the playlist offsets carry the audio PTS. So read `T0_audio` as ± ~21 ms. Every T0
   candidate (PDT, chat anchor with truncated and with rounded offsets, created_at, path epoch) is
   shown against it.
2. **Chat clock.** Replay comments sampled over the VOD are looked up in our live chat by msg_id:
   `createdAt - tmi-sent-ts` (same clock?) and `tmi-sent-ts - (T0_audio + contentOffsetSeconds)`
   (Twitch's replay band: its lower edge is the latency compensation, its width the rounding).

Windows are picked evenly over the live chunks whose search range (± `search_s` around the T0
estimate) lies inside the VOD, has no muted segment and one init section (fMP4). Downloads:
~`2 * search_s + chunk` s of `audio_only` per window (~0.3 MB per 10 s), the init section of an
fMP4 VOD (asked for once per run: the downloader remembers it, a lost one too) plus the chat
sample pages. A window that can't be read (init section or segment lost, a download failing
after its retries, ffmpeg failing) gets a note and is skipped; the other windows and the chat
part go on.

fMP4 VODs (`EXT-X-MAP`): a window is written as init section + fragments (`.mp4`, as the media
path does) and decoded with the edit list ignored, so decoded sample 0 is the first coded sample
of the window's first fragment, as for MPEG-TS and as in the `.aac` stream copies of the media
path and the recorder. What the tests showed with ffmpeg 9.0.2 (the dev machine's): no padding up
to the first fragment's `baseMediaDecodeTime`, but without `-ignore_editlist` an edit list's
`media_time` (e.g. 1024 samples of AAC priming) is dropped at the start of EVERY window, shifting
each match by ~21 ms; the `.aac` copy keeps every coded sample either way. Not checked with the
images' ffmpeg (Debian trixie: 7.1): assumed to have the option and to behave the same (running
`test_calibrate.py` in the image would show it).
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clipper_backfill import rows
from clipper_backfill.audio_match import (
    ENERGY_FLOOR,
    MIN_SCORE,
    RATE,
    TEMP_PREFIX,
    AudioMatch,
    ReadStore,
    decode_pcm,
    locate,
    match_chunk,
    match_windows,
    median_t0,
    one_init,
    window_segments,
)
from clipper_backfill.backfill import (
    AUDIO,
    UsageError,
    VodOptions,
    _broadcast,
    parse_video_ref,
    vod_meta,
)
from clipper_backfill.t0 import ChatPoint, estimate_t0, pdt_points
from clipper_core.contracts import DataSource
from clipper_core.logs import get_logger
from clipper_core.timeline import Interval
from clipper_twitch.gql import Comment, GqlError, ReplayUnavailable
from clipper_twitch.hls import VodSegment

if TYPE_CHECKING:
    from clipper_backfill.backfill import Backfiller

log = get_logger(__name__)

# the matching itself lives in `audio_match` (shared with `vod --fill-gaps`); kept importable here
__all__ = [
    "ENERGY_FLOOR",
    "MIN_SCORE",
    "RATE",
    "TEMP_PREFIX",
    "AudioMatch",
    "Calibration",
    "calibrate",
    "decode_pcm",
    "locate",
]


@dataclass(slots=True)
class Calibration:
    video_id: str
    login: str
    stream_id: int
    platform_stream_id: str
    candidates: dict[str, datetime] = field(default_factory=dict)  # method -> T0
    matches: list[AudioMatch] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # skipped windows, missing pieces
    comments: int = 0  # sampled from the replay
    created_minus_sent: list[float] = field(default_factory=list)  # s, per matched message
    sent_minus_offset: list[float] = field(default_factory=list)  # tmi - (T0_audio + offset)

    @property
    def good(self) -> list[AudioMatch]:
        return [m for m in self.matches if m.good]

    @property
    def t0_audio(self) -> datetime | None:
        return median_t0(self.matches)

    def lines(self) -> list[str]:
        out = [
            f"calibrate video {self.video_id} ({self.login}): live stream {self.stream_id} "
            f"(broadcast {self.platform_stream_id})"
        ]
        t0a = self.t0_audio
        if self.matches:
            out.append(
                f"  {'seq':>5}  {'chunk start (live PDT)':<32} {'VOD offset':>11}  "
                f"{'T0 from it':<32} {'score':>6}"
            )
            for m in self.matches:
                out.append(
                    f"  {m.seq:>5}  {_ts(m.chunk_start):<32} {m.vod_offset_s:>11.3f}  "
                    f"{_ts(m.t0):<32} {m.score:>6.3f}"
                    + ("" if m.score >= MIN_SCORE else "  (weak: not used)")
                )
        if t0a is not None:
            spread = max(m.t0 for m in self.good) - min(m.t0 for m in self.good)
            out.append(
                f"T0_audio = {_ts(t0a)} ({len(self.good)} windows, spread "
                f"{spread.total_seconds() * 1000:.1f} ms)"
            )
        else:
            out.append("T0_audio: no usable window")
        out.append(f"  {'T0 method':<22} {'T0 (UTC)':<32} {'- T0_audio':>11}")
        for name, t0 in self.candidates.items():
            diff = f"{(t0 - t0a).total_seconds():+11.3f}" if t0a is not None else f"{'-':>11}"
            out.append(f"  {name:<22} {_ts(t0):<32} {diff} s")
        out.append(
            f"chat: {len(self.created_minus_sent)} of {self.comments} sampled comments found in "
            "our live chat (by msg_id)"
        )
        if self.created_minus_sent:
            out.append("  createdAt - tmi-sent-ts: " + _stats(self.created_minus_sent))
        if self.sent_minus_offset:
            out.append("  tmi-sent-ts - (T0_audio + offset): " + _stats(self.sent_minus_offset))
        out += [f"note: {n}" for n in self.notes]
        return out


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def _stats(values: Sequence[float]) -> str:
    ordered = sorted(values)

    def q(p: float) -> float:
        return ordered[min(len(ordered) - 1, round(p * (len(ordered) - 1)))]

    return (
        f"min {ordered[0]:+.3f}, p5 {q(0.05):+.3f}, median {statistics.median(ordered):+.3f}, "
        f"p95 {q(0.95):+.3f}, max {ordered[-1]:+.3f} s (n={len(ordered)})"
    )


def _windows(
    chunks: Sequence[Any],
    segments: Sequence[VodSegment],
    t0_guess: datetime,
    *,
    count: int,
    search_s: float,
) -> tuple[list[tuple[Any, list[VodSegment]]], int]:
    """Up to `count` (chunk, VOD segments to search) spread over the stream, and how many
    chunks were left out (`audio_match.window_segments`: outside the VOD, muted or an fMP4 init
    section change around them)."""
    usable = []
    for chunk in chunks:
        segs = window_segments(chunk, segments, t0_guess, search_s=search_s)
        if segs is not None:
            usable.append((chunk, segs))
    if not usable or count <= 0:
        return [], len(chunks) - len(usable)
    picks = sorted(
        {min(len(usable) - 1, int((i + 0.5) * len(usable) / count)) for i in range(count)}
    )
    return [usable[i] for i in picks], len(chunks) - len(usable)


def _one_init(segs: Sequence[VodSegment]) -> bool:
    """All under the same `EXT-X-MAP` (or all MPEG-TS)."""
    return one_init(segs)


async def calibrate(
    bf: Backfiller,
    store: ReadStore,
    ref: str,
    *,
    windows: int = 3,
    search_s: float = 20.0,
) -> Calibration:
    """Measure (see the module docstring). Raises UsageError when there is nothing to compare."""
    video_id = parse_video_ref(ref)
    info = await bf.gql.video(video_id)
    helix_video = await bf.helix.get_video(video_id) if bf.helix else None
    if info is None and helix_video is None:
        raise UsageError(f"video {video_id}: the platform doesn't know it")
    meta = vod_meta(video_id, info, helix_video)
    if not meta.archive:
        raise UsageError(f"video {video_id} is not a past broadcast")
    media = await bf._playlists(video_id, VodOptions(), first_run=True)
    broadcast = _broadcast(meta, media)
    async with bf.sessions() as s:
        channel_id = await rows.find_channel(s, login=meta.login, user_id=meta.user_id)
        stream = (
            await rows.find_stream(s, channel_id=channel_id, platform_stream_id=broadcast.stream_id)
            if channel_id is not None
            else None
        )
        if stream is None or stream.source != DataSource.LIVE:
            raise UsageError(
                f"video {video_id}: broadcast {broadcast.stream_id} was not recorded live by us"
            )
        chunks = await rows.stream_chunks(s, stream.id)
    cal = Calibration(video_id, meta.login, stream.id, broadcast.stream_id)

    comments = await _sample_comments(bf, video_id, meta.length_s, cal)
    audio = media.audio
    estimate = estimate_t0(
        pdt=pdt_points(audio.segments) if audio else (),
        chat=[ChatPoint(c.content_offset_s, c.created_at) for c in comments if c.created_at],
        created_at=meta.created_at,
        created_at_source=meta.created_source,
        path_epoch=broadcast.epoch,
    )
    found = estimate.candidates
    for key, name in (
        ("pdt", "pdt"),
        ("chat", "chat (truncated)"),
        ("chat_median", "chat (rounded)"),
        (meta.created_source, meta.created_source),
        ("path_epoch", "path_epoch"),
    ):
        if key in found:
            cal.candidates[name] = datetime.fromisoformat(found[key])

    if audio is None:
        cal.notes.append(f"no {AUDIO} playlist (restricted?): no audio comparison")
    elif not chunks:
        cal.notes.append("the live stream has no chunks with audio")
    else:
        picked, left_out = _windows(
            chunks, audio.segments, estimate.t0, count=windows, search_s=search_s
        )
        if left_out:
            cal.notes.append(
                f"{left_out} chunks not usable (VOD edge, muted or init section change around them)"
            )
        matches, notes = await match_windows(
            bf.downloader, store, bf.settings.ffmpeg, picked, temp_dir=bf.settings.temp_dir
        )
        cal.matches += matches
        cal.notes += notes

    t0a = cal.t0_audio
    times = [c.created_at for c in comments if c.created_at is not None]
    if times:  # the partitions to look in: hours around the sample (createdAt may be off)
        window = Interval(min(times) - timedelta(hours=2), max(times) + timedelta(hours=2))
        async with bf.sessions() as s:
            live = await rows.live_messages_by_id(
                s, channel_id=stream.channel_id, msg_ids=[c.id for c in comments], window=window
            )
        for c in comments:
            sent = live.get(c.id)
            if sent is None or c.created_at is None:
                continue
            cal.created_minus_sent.append((c.created_at - sent).total_seconds())
            if t0a is not None:
                cal.sent_minus_offset.append((sent - t0a).total_seconds() - c.content_offset_s)
    return cal


async def _sample_comments(
    bf: Backfiller, video_id: str, duration_s: float, cal: Calibration
) -> list[Comment]:
    """Comments of `t0_chat_pages` pages spread over the VOD (the T0 cross-check's sample)."""
    pages = max(1, bf.settings.t0_chat_pages)
    found: dict[str, Comment] = {}
    for i in range(pages):
        try:
            page = await bf.gql.page(video_id, offset_s=int(duration_s * i / pages))
        except ReplayUnavailable as exc:
            cal.notes.append(f"no chat replay: {exc}")
            break
        except GqlError as exc:
            cal.notes.append(f"chat sample failed: {type(exc).__name__}: {exc}"[:200])
            break
        for c in page.comments:
            found.setdefault(c.id, c)
    cal.comments = len(found)
    return list(found.values())


async def _match(
    bf: Backfiller, store: ReadStore, tmp: Path, chunk: Any, segs: list[VodSegment]
) -> AudioMatch:
    """Where the live chunk's audio lies in the VOD (`audio_match.match_chunk`). MediaError (or
    DownloadError): its window can't be read."""
    return await match_chunk(
        bf.downloader, store, bf.settings.ffmpeg if bf.settings else "ffmpeg", tmp, chunk, segs
    )
