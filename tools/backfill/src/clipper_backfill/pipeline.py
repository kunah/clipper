"""VOD media -> chunks: download a unit's segments, build `.ts`/`.aac`, upload, commit.

Per unit of the plan (`planner.plan_units`), in order:
1. download its segments (`Downloader`: <= 4 at a time, retries; lost ones re-plan the rest),
2. concatenate them in a temp file (MPEG-TS as is; fMP4 = init section + fragments, remuxed to
   `.ts` with ffmpeg when video is kept), copy the audio track out to ADTS `.aac`,
3. upload to `storage.keys` (a re-run overwrites the same keys),
4. ONE transaction: lease check, `chunks.commit_chunk(source=backfill, gaps_before=...,
   default_priority=BACKFILL)` (or the trailing gaps alone) and `vods.media_done_s` /
   `media_status`.
A crash loses at most the unit in progress: nothing of it is committed, and a re-run plans again
from `media_done_s` with the next seq (same chunk boundaries, same keys).

`run_fill` (`vod --fill-gaps`, DESIGN §17) does the same with `planner.plan_fill`'s units: chunks
inside a live stream's fill ranges, seqs from `FILL_SEQ_BASE`, and no gap rows at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_backfill import rows
from clipper_backfill.download import Downloader
from clipper_backfill.planner import EPS_S, FillRange, Unit, plan_fill, plan_units
from clipper_core import chunks, leases
from clipper_core import ffmpeg as ffm
from clipper_core.contracts import DataSource, Priority, VodMediaStatus
from clipper_core.logs import get_logger
from clipper_core.storage import keys
from clipper_twitch.hls import VodSegment

log = get_logger(__name__)


TEMP_PREFIX = "clipper-backfill-"


def remove_stale_workdirs(parent: Path | None, older_than_s: float) -> list[Path]:
    """Delete `clipper-backfill-*` temp dirs left by killed runs (a live run's lease renewals
    touch its dir, so one older than the lease TTL belongs to a dead run)."""
    removed = []
    for path in Path(parent or tempfile.gettempdir()).glob(f"{TEMP_PREFIX}*"):
        try:
            if path.is_dir() and time.time() - path.stat().st_mtime > older_than_s:
                shutil.rmtree(path, ignore_errors=True)
                removed.append(path)
        except OSError:
            continue
    return removed


class Store(Protocol):
    def put_file(self, key: str, path: Path | str, content_type: str | None = None) -> None: ...


class LeaseLost(RuntimeError):
    """Another run took this VOD's lease (ours expired): stop without committing."""


class MediaError(RuntimeError):
    """A chunk could not be built (ffmpeg failed on its main track): stop, resume later."""


class LeaseKeeper:
    """Holds `backfill_lease(video_id)` for one run: renewed in the background every third of
    its TTL and re-checked inside every commit transaction."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        resource: str,
        owner: str,
        ttl: timedelta,
    ) -> None:
        self.sessions = sessions
        self.resource = resource
        self.owner = owner
        self.ttl = ttl
        self.lost = False
        self.touch: Path | None = None  # the run's temp dir: its mtime shows the run is alive
        self._task: asyncio.Task | None = None

    async def acquire(self) -> bool:
        async with self.sessions.begin() as s:
            return await leases.acquire(s, self.resource, self.owner, self.ttl)

    async def holder(self) -> str | None:
        async with self.sessions() as s:
            return await leases.holder(s, self.resource)

    async def check(self, session: AsyncSession) -> None:
        """Renew inside the caller's transaction; raises LeaseLost when someone else holds it."""
        if self.lost or not await leases.acquire(session, self.resource, self.owner, self.ttl):
            self.lost = True
            raise LeaseLost(f"lease {self.resource} is held by another run")

    def start(self) -> None:
        self._task = asyncio.create_task(self._renew(), name=f"lease:{self.resource}")

    async def release(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if not self.lost:
            async with self.sessions.begin() as s:
                await leases.release(s, self.resource, self.owner)

    async def _renew(self) -> None:
        while True:
            await asyncio.sleep(self.ttl.total_seconds() / 3)
            try:
                if not await self.acquire():
                    self.lost = True
                    log.error("lease lost", resource=self.resource)
                    return
            except Exception:
                log.warning("lease renewal failed, retrying", exc_info=True)
            if self.touch is not None:
                with contextlib.suppress(OSError):
                    os.utime(self.touch)


@dataclass(slots=True)
class ChunkFiles:
    audio: Path | None = None
    video: Path | None = None
    lost: set[int] = field(default_factory=set)  # segment indexes that are gone (404)
    paths: list[Path] = field(default_factory=list)  # every temp file, for cleanup


@dataclass(slots=True)
class MediaResult:
    chunks: int = 0
    gaps: dict[str, float] = field(default_factory=dict)  # reason -> seconds committed
    media_s: float = 0.0  # seconds of committed chunks
    audio_bytes: int = 0
    video_bytes: int = 0
    lost_segments: int = 0
    done_s: float = 0.0
    status: str = VodMediaStatus.PENDING
    stopped: bool = False  # the stop event ended the run early


class MediaRunner:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        store: Store,
        downloader: Downloader,
        lease: LeaseKeeper,
        *,
        workdir: Path,
        ffmpeg: str = "ffmpeg",
        chunk_s: float = 60.0,
        with_video: bool = False,
        transcribe: bool = True,
        stop: asyncio.Event | None = None,
        after_commit: Callable[[Unit], Awaitable[Any]] | None = None,  # tests inject crashes
    ) -> None:
        self.sessions = sessions
        self.store = store
        self.dl = downloader
        self.lease = lease
        self.workdir = workdir
        self.ffmpeg = ffmpeg
        self.chunk_s = chunk_s
        self.with_video = with_video
        self.transcribe = transcribe
        self.stop = stop
        self.after_commit = after_commit

    async def run(
        self,
        vod: rows.VodRow,
        segments: Sequence[VodSegment],
        *,
        start_s: float = 0.0,
        until_s: float | None = None,
    ) -> MediaResult:
        """Fetch and commit from `vod.media_done_s` (or `start_s`, if later) to the end of the
        VOD or `until_s`."""
        async with self.sessions() as s:
            seq = await rows.next_seq(s, vod.stream_id)
        done_s = vod.media_done_s
        vod_end = segments[-1].end_s if segments else 0.0
        lost: set[int] = set()
        result = MediaResult(done_s=done_s, status=vod.media_status)
        while True:
            plan = plan_units(
                segments,
                done_s=done_s,
                start_s=max(start_s, done_s),
                first_seq=seq,
                chunk_s=self.chunk_s,
                until_s=until_s,
                lost=frozenset(lost),
                muted_video=self.with_video,
            )
            replan = False
            for unit in plan.units:
                if self.stop is not None and self.stop.is_set():
                    result.stopped = True
                    return result
                files = ChunkFiles()
                try:
                    if unit.seq is not None:
                        files = await self._build(vod, unit)
                        if files.lost:
                            lost |= files.lost
                            result.lost_segments = len(lost)
                            replan = True
                            break
                    status = (
                        VodMediaStatus.DONE
                        if unit.done_s >= vod_end - EPS_S
                        else VodMediaStatus.PENDING
                    )
                    await self._commit(vod, unit, files, status, result)
                finally:
                    for path in files.paths:
                        path.unlink(missing_ok=True)
                done_s = unit.done_s
                result.done_s, result.status = done_s, status
                if unit.seq is not None:
                    seq = unit.seq + 1
                if self.after_commit is not None:
                    await self.after_commit(unit)
            if replan:
                continue
            if (
                not plan.units
                and done_s >= vod_end - EPS_S
                and result.status != VodMediaStatus.DONE
            ):  # nothing left (e.g. covered by a `restricted` gap): the media is complete
                async with self.sessions.begin() as s:
                    await self.lease.check(s)
                    await rows.update_vod(s, vod.id, media_status=str(VodMediaStatus.DONE))
                result.status = VodMediaStatus.DONE
            return result

    async def run_fill(
        self,
        vod: rows.VodRow,
        segments: Sequence[VodSegment],
        ranges: Sequence[FillRange],
        *,
        budget_s: float | None = None,
    ) -> MediaResult:
        """`--fill-gaps` (DESIGN §17): commit the `planner.plan_fill` units from
        `vod.media_done_s` into the live stream (seqs from `FILL_SEQ_BASE`, no gap rows), until
        the end or until `budget_s` seconds of chunks are committed (`--max-minutes`). Lost
        segments are skipped (re-planned on the fly, `flags.lost_segments`); muted ones never
        fetched."""
        async with self.sessions() as s:
            seq = await rows.next_fill_seq(s, vod.stream_id)
        done_s = vod.media_done_s
        vod_end = segments[-1].end_s if segments else 0.0
        lost: set[int] = set()
        result = MediaResult(done_s=done_s, status=vod.media_status)
        spent = 0.0
        while True:
            plan = plan_fill(
                segments,
                ranges,
                vod.t0_utc,
                done_s=done_s,
                first_seq=seq,
                chunk_s=self.chunk_s,
                lost=frozenset(lost),
            )
            replan = False
            for unit in plan.units:
                if self.stop is not None and self.stop.is_set():
                    result.stopped = True
                    return result
                if unit.seq is not None and budget_s is not None and spent >= budget_s - EPS_S:
                    return result  # `--max-minutes`: a re-run goes on from here
                files = ChunkFiles()
                try:
                    if unit.seq is not None:
                        files = await self._build(vod, unit)
                        if files.lost:
                            lost |= files.lost
                            result.lost_segments = len(lost)
                            replan = True
                            break
                    status = (
                        VodMediaStatus.DONE
                        if unit.done_s >= vod_end - EPS_S
                        else VodMediaStatus.PENDING
                    )
                    await self._commit(vod, unit, files, status, result)
                finally:
                    for path in files.paths:
                        path.unlink(missing_ok=True)
                done_s = unit.done_s
                result.done_s, result.status = done_s, status
                if unit.seq is not None:
                    seq = unit.seq + 1
                    spent += unit.end_s - unit.start_s
                if self.after_commit is not None:
                    await self.after_commit(unit)
            if not replan:
                return result

    async def _build(self, vod: rows.VodRow, unit: Unit) -> ChunkFiles:
        files = ChunkFiles()
        init = b""
        init_uri = unit.segments[0].init_uri
        if init_uri:
            data = await self.dl.init_section(init_uri)
            if data is None:  # the fragments are useless without it
                files.lost = {s.index for s in unit.segments}
                return files
            init = data
        datas = await self.dl.segments(unit.segments)
        files.lost = {i for i, d in datas.items() if d is None}
        if files.lost:
            return files

        stem = self.workdir / f"{vod.stream_id}_{unit.seq:06d}"
        media = stem.with_suffix(".mp4" if init_uri else ".ts")
        files.paths.append(media)
        await asyncio.to_thread(write_media, media, init, [datas[s.index] for s in unit.segments])

        if unit.audio:
            audio = stem.with_suffix(".aac")
            files.paths.append(audio)
            error = await asyncio.to_thread(ffm.extract_audio, self.ffmpeg, media, audio)
            if error is None:
                files.audio = audio
            elif not self.with_video:
                raise MediaError(f"chunk {unit.seq}: no audio: {error}")
            else:
                log.warning("no audio for chunk, it won't be transcribed", error=error)
        if self.with_video:
            if not init_uri:
                files.video = media
            else:
                video = stem.with_name(stem.name + "_video.ts")
                files.paths.append(video)
                error = await asyncio.to_thread(ffm.remux_to_ts, self.ffmpeg, media, video)
                if error is None:
                    files.video = video
                elif files.audio is None:
                    raise MediaError(f"chunk {unit.seq}: no video and no audio: {error}")
                else:
                    log.error("remux to .ts failed, chunk has no video", error=error)
        return files

    async def _commit(
        self,
        vod: rows.VodRow,
        unit: Unit,
        files: ChunkFiles,
        status: str,
        result: MediaResult,
    ) -> None:
        t0 = vod.t0_utc
        audio_key = video_key = None
        audio_bytes = video_bytes = None
        if unit.seq is not None:
            if files.audio is not None:
                audio_key = keys.chunk_audio(vod.stream_id, unit.seq)
                audio_bytes = files.audio.stat().st_size
                await asyncio.to_thread(self.store.put_file, audio_key, files.audio, "audio/aac")
            if files.video is not None:
                video_key = keys.chunk_video(vod.stream_id, unit.seq)
                video_bytes = files.video.stat().st_size
                await asyncio.to_thread(self.store.put_file, video_key, files.video, "video/mp2t")
        gaps = [g.utc(t0) for g in unit.gaps]
        priority = None
        async with self.sessions.begin() as s:
            await self.lease.check(s)
            if unit.seq is not None:
                committed = await chunks.commit_chunk(
                    s,
                    stream_id=vod.stream_id,
                    seq=unit.seq,
                    interval=unit.interval(t0),
                    video_key=video_key,
                    audio_key=audio_key,
                    video_bytes=video_bytes,
                    audio_bytes=audio_bytes,
                    source=DataSource.BACKFILL,
                    gaps_before=gaps,
                    default_priority=Priority.BACKFILL,
                    transcribe=self.transcribe,
                )
                priority = committed.priority
            else:
                await rows.insert_gaps(s, vod.stream_id, gaps)
            await rows.update_vod(s, vod.id, media_done_s=unit.done_s, media_status=str(status))
            if unit.lost:
                await rows.add_to_flag(s, vod.id, "lost_segments", unit.lost)
        for gap in unit.gaps:
            result.gaps[gap.reason] = result.gaps.get(gap.reason, 0.0) + gap.seconds
        if unit.seq is not None:
            result.chunks += 1
            result.media_s += unit.end_s - unit.start_s
            result.audio_bytes += audio_bytes or 0
            result.video_bytes += video_bytes or 0
            log.info(
                "chunk saved",
                seq=unit.seq,
                offset_s=round(unit.start_s, 3),
                seconds=round(unit.end_s - unit.start_s, 3),
                gaps=[f"{g.reason}:{g.seconds:.0f}s" for g in unit.gaps],
                audio_kb=round((audio_bytes or 0) / 1e3, 1),
                video_mb=round((video_bytes or 0) / 1e6, 2),
                transcribe_priority=priority,
            )
        elif unit.gaps:
            log.info("gaps saved", gaps=[f"{g.reason}:{g.seconds:.0f}s" for g in unit.gaps])
        else:  # a `--fill-gaps` watermark (past skipped segments, or the end)
            log.info("fill watermark saved", done_s=round(unit.done_s, 3), lost=unit.lost)


def write_media(path: Path, init: bytes, parts: Sequence[bytes | None]) -> None:
    """One readable media file from consecutive segments: MPEG-TS segments concatenate as they
    are; fMP4 fragments need their init section (`init`, b"" for TS) first, or ffmpeg can't read
    them ("tfhd was found ... error reading header"). Blocking: run in a thread. Also used by
    `calibrate`."""
    with path.open("wb") as f:
        f.write(init)
        for part in parts:
            f.write(part or b"")
