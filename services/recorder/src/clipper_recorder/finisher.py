"""Closed chunks -> audio track -> object storage -> chunk row + gap + transcription job.

Runs beside the channel recorders so slow uploads never stall segment downloads. Storage and
database errors are retried forever (the chunk stays in the spool meanwhile); after a restart the
spool is finished first. Each chunk is committed in one transaction: chunk row, the gap before it,
and its transcription job (priority raised by open media requests for that time range).
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
from pathlib import Path
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_core import jobs, media
from clipper_core.contracts import (
    DataSource,
    JobKind,
    Priority,
    RecordingMode,
    TranscribePayload,
    dedupe_key,
)
from clipper_core.logs import get_logger, log_context
from clipper_core.storage import keys
from clipper_core.timeline import Interval
from clipper_recorder.spool import ChunkState, Spool

log = get_logger(__name__)

MAX_RETRY_DELAY_S = 60.0


class Store(Protocol):
    def put_file(self, key: str, path: Path | str, content_type: str | None = None) -> None: ...


def extract_audio(ffmpeg: str, media: Path, out: Path) -> str | None:
    """Copy the first audio track into an ADTS .aac file (no re-encode).

    Returns an error message, or None on success.
    """
    try:
        args = ["-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(media)]
        args += ["-map", "0:a:0", "-c", "copy", "-f", "adts", str(out)]
        result = subprocess.run(
            [ffmpeg, *args],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        return result.stderr.strip()[-500:] or f"ffmpeg exited with {result.returncode}"
    if not out.exists() or out.stat().st_size == 0:
        return "ffmpeg produced no audio"
    return None


class Finisher:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        store: Store,
        spool: Spool,
        *,
        ffmpeg: str = "ffmpeg",
        workers: int = 2,
    ) -> None:
        self.sessions = sessions
        self.store = store
        self.spool = spool
        self.ffmpeg = ffmpeg
        self.workers = workers
        self.queue: asyncio.Queue[ChunkState] = asyncio.Queue()
        self.done = 0
        self._tasks: list[asyncio.Task] = []

    def submit(self, state: ChunkState) -> None:
        self.queue.put_nowait(state)

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._worker(), name=f"finisher-{i}") for i in range(self.workers)
        ]

    async def drain(self, timeout_s: float) -> bool:
        """Wait for queued chunks, then stop the workers. False if some are left for next start."""
        try:
            await asyncio.wait_for(self.queue.join(), timeout_s)
            drained = True
        except TimeoutError:
            drained = False
            left = self.queue.qsize()
            log.warning("finisher drain timed out, the rest stays in the spool", left=left)
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return drained

    async def _worker(self) -> None:
        while True:
            state = await self.queue.get()
            try:
                await self._finish_with_retry(state)
            finally:
                self.queue.task_done()

    async def _finish_with_retry(self, state: ChunkState) -> None:
        attempt = 0
        with log_context(channel=state.channel, stream_id=state.stream_id, seq=state.seq):
            while True:
                try:
                    await self.finish(state)
                    return
                except Exception:
                    attempt += 1
                    delay = min(2.0**attempt, MAX_RETRY_DELAY_S)
                    log.exception(
                        "finishing chunk failed, retrying", attempt=attempt, retry_in_s=delay
                    )
                    await asyncio.sleep(delay)

    async def finish(self, state: ChunkState) -> int:
        """Upload + commit one closed chunk, then delete it from the spool. Returns the chunk id."""
        media_path, audio_path = self.spool.media_path(state), self.spool.audio_path(state)
        error = await asyncio.to_thread(extract_audio, self.ffmpeg, media_path, audio_path)
        if error:
            # Permanent for this chunk (no audio track or broken data): keep the video, if any.
            log.warning("no audio for chunk, it won't be transcribed", error=error)

        video_key = audio_key = None
        video_bytes = audio_bytes = None
        if state.mode == RecordingMode.FULL:
            video_key = keys.chunk_video(state.stream_id, state.seq)
            video_bytes = media_path.stat().st_size
            await asyncio.to_thread(self.store.put_file, video_key, media_path, "video/mp2t")
        if not error:
            audio_key = keys.chunk_audio(state.stream_id, state.seq)
            audio_bytes = audio_path.stat().st_size
            await asyncio.to_thread(self.store.put_file, audio_key, audio_path, "audio/aac")

        window = Interval(state.start, state.end)
        async with self.sessions.begin() as session:
            chunk_id, created = (
                await session.execute(
                    text(
                        """
                        INSERT INTO chunks (stream_id, seq, start_utc, end_utc, video_key,
                                            audio_key, video_bytes, audio_bytes, source)
                        VALUES (:stream_id, :seq, :start, :end, :video_key, :audio_key,
                                :video_bytes, :audio_bytes, :source)
                        ON CONFLICT (stream_id, seq) DO UPDATE
                           SET video_key = EXCLUDED.video_key, audio_key = EXCLUDED.audio_key,
                               video_bytes = EXCLUDED.video_bytes,
                               audio_bytes = EXCLUDED.audio_bytes
                        RETURNING id, (xmax = 0) AS created
                        """
                    ),
                    {
                        "stream_id": state.stream_id,
                        "seq": state.seq,
                        "start": window.start,
                        "end": window.end,
                        "video_key": video_key,
                        "audio_key": audio_key,
                        "video_bytes": video_bytes,
                        "audio_bytes": audio_bytes,
                        "source": DataSource.LIVE.value,
                    },
                )
            ).one()
            gap = state.gap_before
            if created and gap is not None:  # a retry after commit must not add it twice
                await session.execute(
                    text(
                        "INSERT INTO stream_gaps (stream_id, start_utc, end_utc, reason) "
                        "VALUES (:s, :start, :end, :reason)"
                    ),
                    {
                        "s": state.stream_id,
                        "start": gap.start,
                        "end": gap.end,
                        "reason": gap.reason,
                    },
                )
            priority = None
            if audio_key:
                priority = await media.priority_for_range(
                    session, stream_id=state.stream_id, window=window, default=Priority.LIVE
                )
                await jobs.enqueue(
                    session,
                    JobKind.TRANSCRIBE,
                    payload=TranscribePayload(chunk_id=chunk_id).model_dump(),
                    priority=priority,
                    ref_id=chunk_id,
                    dedupe_key=dedupe_key(JobKind.TRANSCRIBE, chunk_id),
                )
        self.spool.remove(state)
        self.done += 1
        log.info(
            "chunk saved",
            chunk_id=chunk_id,
            start=window.start.isoformat(),
            seconds=round(window.seconds, 3),
            video_mb=round((video_bytes or 0) / 1e6, 2),
            audio_kb=round((audio_bytes or 0) / 1e3, 1),
            transcribe_priority=priority,
        )
        return chunk_id
