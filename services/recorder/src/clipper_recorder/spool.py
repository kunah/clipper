"""Local spool: chunks are written to disk first, uploaded when closed, deleted when committed.

Per chunk: the media file with the raw Twitch segments appended, and `{seq:06d}.json`
(ChunkState, rewritten after every segment). The media file is `{seq:06d}.ts` for MPEG-TS
segments (they concatenate cleanly) or `{seq:06d}.mp4` for fragmented MP4 (init section, then
the fragments: also readable when cut off); the finisher remuxes .mp4 to .ts, so storage only
ever holds .ts. After a crash the next start finishes whatever is still in the spool: the media
file is cut back to the byte count in the state file, so a half-appended segment is dropped.

One spool directory per recorder process.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from clipper_core.contracts import GapReason, RecordingMode
from clipper_core.logs import get_logger
from clipper_twitch.hls import Segment

log = get_logger(__name__)


class GapState(BaseModel):
    start: datetime
    end: datetime
    reason: GapReason


class ChunkState(BaseModel):
    stream_id: int
    channel: str
    seq: int
    mode: RecordingMode
    container: Literal["ts", "mp4"] = "ts"  # what the Twitch segments are
    start: datetime
    end: datetime
    bytes: int = 0
    segments: int = 0
    closed: bool = False
    gap_before: GapState | None = None  # committed together with this chunk

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


class Spool:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path(self, stream_id: int, seq: int, suffix: str) -> Path:
        return self.root / str(stream_id) / f"{seq:06d}{suffix}"

    def media_path(self, state: ChunkState) -> Path:
        """The raw segments as downloaded."""
        return self.path(state.stream_id, state.seq, f".{state.container}")

    def video_path(self, state: ChunkState) -> Path:
        """The .ts that gets uploaded (the media file itself, or remuxed from .mp4)."""
        return self.path(state.stream_id, state.seq, ".ts")

    def audio_path(self, state: ChunkState) -> Path:
        return self.path(state.stream_id, state.seq, ".aac")

    def save(self, state: ChunkState) -> None:
        target = self.path(state.stream_id, state.seq, ".json")
        tmp = target.with_suffix(".json.tmp")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(state.model_dump_json(), encoding="utf-8")
        os.replace(tmp, target)

    def pending(self) -> list[ChunkState]:
        """Chunks left on disk (closed but not committed, or open when the process died)."""
        if not self.root.exists():
            return []
        states = []
        for path in sorted(self.root.glob("*/*.json")):
            try:
                states.append(ChunkState.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                log.exception("unreadable spool state, skipping", path=str(path))
        return states

    def latest(self, stream_id: int) -> ChunkState | None:
        """Highest-seq chunk of a stream still in the spool."""
        states = [s for s in self.pending() if s.stream_id == stream_id]
        return max(states, key=lambda s: s.seq) if states else None

    def recover(self, state: ChunkState) -> ChunkState | None:
        """Make a leftover chunk consistent. None if there is nothing worth keeping."""
        media = self.media_path(state)
        size = media.stat().st_size if media.exists() else 0
        if state.segments == 0 or state.bytes == 0 or size < state.bytes:
            log.warning("dropping incomplete spool chunk", seq=state.seq, stream_id=state.stream_id)
            self.remove(state)
            return None
        if size > state.bytes:  # a segment was half-appended when the process died
            with media.open("r+b") as f:
                f.truncate(state.bytes)
        if not state.closed:
            state = state.model_copy(update={"closed": True})
            self.save(state)
        return state

    def remove(self, state: ChunkState) -> None:
        for suffix in (".ts", ".mp4", ".aac", ".json", ".json.tmp"):
            self.path(state.stream_id, state.seq, suffix).unlink(missing_ok=True)


class ChunkWriter:
    """Appends segments to one spool chunk; the state file follows every append."""

    def __init__(self, spool: Spool, state: ChunkState, init: bytes | None = None) -> None:
        self.spool = spool
        self.init = init  # fMP4 init section the fragments of this chunk belong to
        path = spool.media_path(state)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("wb")
        if init:
            self._write(init)
        self.state = state.model_copy(update={"bytes": len(init or b"")})
        self._save()  # so a crash before the first append leaves nothing orphaned

    @classmethod
    def open(
        cls,
        spool: Spool,
        *,
        stream_id: int,
        channel: str,
        seq: int,
        mode: RecordingMode,
        first: Segment,
        gap_before: GapState | None,
        init: bytes | None = None,
    ) -> ChunkWriter:
        """Start a chunk at `first`. `init`: fMP4 init section (None for MPEG-TS segments)."""
        state = ChunkState(
            stream_id=stream_id,
            channel=channel,
            seq=seq,
            mode=mode,
            container="mp4" if init else "ts",
            start=first.start,
            end=first.start,
            gap_before=gap_before,
        )
        return cls(spool, state, init)

    @property
    def seconds(self) -> float:
        return self.state.seconds

    async def append(self, seg: Segment, data: bytes) -> None:
        await asyncio.to_thread(self._write, data)
        self.state = self.state.model_copy(
            update={
                "end": seg.start + timedelta(seconds=seg.duration),
                "bytes": self.state.bytes + len(data),
                "segments": self.state.segments + 1,
            }
        )
        self._save()

    def close(self) -> ChunkState:
        self._file.close()
        self.state = self.state.model_copy(update={"closed": True})
        self._save()
        return self.state

    def _write(self, data: bytes) -> None:
        self._file.write(data)
        self._file.flush()

    def _save(self) -> None:
        try:
            self.spool.save(self.state)
        except OSError:  # e.g. a virus scanner holding the file on Windows; next save fixes it
            log.warning("could not save spool state", seq=self.state.seq, exc_info=True)
