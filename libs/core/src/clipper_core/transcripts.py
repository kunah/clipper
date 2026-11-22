"""Reading transcripts: UTC words with the ownership rule applied, and how far transcripts cover a
time range. Read-only: only the transcriber writes `chunk_transcripts`, `transcript_segments`
and the words files.

Ownership rule (DESIGN.md §16): a chunk's words file is authoritative only for
`[owned_from, owned_to)` of its `chunk_transcripts` row, and a word belongs to the chunk whose
owned range contains the word's *start*. Words at or after `tail_from` are provisional: the last
sentence ran into the cut, and the next contiguous chunk will re-transcribe it and take that range
over (this chunk's `owned_to` then becomes the next chunk's `owned_from`).

    ws = await transcripts.words(session, store, stream_id, Interval(a, b))
    cov = await transcripts.coverage(session, stream_id, Interval.around(t, before, after))
    if cov.complete: ...  # nothing left to wait for
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from clipper_core.contracts import CHUNK_JITTER, WordsFile
from clipper_core.timeline import Interval, ensure_utc, merge, uncovered, utc_now

# After a stream ended, the recorder finishes and uploads its last chunk within this time.
ENDED_GRACE = timedelta(minutes=5)


class ObjectReader(Protocol):
    """The part of `storage.ObjectStore` needed here (tests pass an in-memory fake)."""

    def get_bytes(self, key: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class TranscriptWord:
    text: str
    start_utc: datetime
    end_utc: datetime
    confidence: float | None  # engine's word probability 0..1, if it gives one
    chunk_id: int  # the chunk whose words file it came from
    provisional: bool  # at or after that chunk's tail_from: the next chunk will redo it


@dataclass(frozen=True, slots=True)
class Coverage:
    """How far transcripts cover `interval`.

    Every part is merged, sorted and inside `interval`. `transcribed`, `gap`, `untranscribed`
    and `unrecorded` don't overlap and together make up the whole interval; `provisional` is a
    subset of `transcribed`, `pending` the union of the last two.
    """

    interval: Interval
    transcribed: tuple[Interval, ...]  # owned by some chunk's transcript (incl. provisional)
    provisional: tuple[Interval, ...]  # transcribed, but the next chunk will redo it
    gap: tuple[Interval, ...]  # nothing will ever be transcribed: no media, no audio, off air
    untranscribed: tuple[Interval, ...]  # a chunk with audio is there, its transcript isn't yet
    unrecorded: tuple[Interval, ...]  # no chunk there (yet)

    @property
    def pending(self) -> tuple[Interval, ...]:
        """Everything still to wait for: untranscribed + unrecorded."""
        return tuple(merge(self.untranscribed + self.unrecorded))

    @property
    def complete(self) -> bool:
        """Nothing pending. Provisional parts count as transcribed."""
        return not self.untranscribed and not self.unrecorded

    @property
    def pending_seconds(self) -> float:
        return sum(iv.seconds for iv in self.untranscribed + self.unrecorded)


async def words(
    session: AsyncSession, store: ObjectReader, stream_id: int, interval: Interval
) -> list[TranscriptWord]:
    """Words of a stream whose start lies in `interval`, ownership applied, sorted by start.

    Loads only the words files of transcripts whose owned range overlaps `interval`. Should two
    owned ranges ever overlap (timestamp jitter between chunks), the later one wins, so a word is
    never returned twice.
    """
    if interval.start == interval.end:
        return []
    rows = (
        await session.execute(
            sql_text(
                """
                SELECT ct.chunk_id, ct.words_key, ct.owned_from, ct.owned_to, ct.tail_from
                  FROM chunk_transcripts ct
                  JOIN chunks c ON c.id = ct.chunk_id
                 WHERE c.stream_id = :stream_id
                   AND ct.owned_from < :end AND ct.owned_to > :start
                   AND ct.owned_to > ct.owned_from
                 ORDER BY ct.owned_from, ct.chunk_id
                """
            ),
            {"stream_id": stream_id, "start": interval.start, "end": interval.end},
        )
    ).all()
    if not rows:
        return []
    blobs = await asyncio.gather(*(asyncio.to_thread(store.get_bytes, r.words_key) for r in rows))

    result: list[TranscriptWord] = []
    for i, (row, blob) in enumerate(zip(rows, blobs, strict=True)):
        file = WordsFile.model_validate_json(blob)
        if file.chunk_id != row.chunk_id:
            raise ValueError(
                f"words file {row.words_key} belongs to chunk {file.chunk_id}, not {row.chunk_id}"
            )
        lo = max(ensure_utc(row.owned_from), interval.start)
        hi = min(ensure_utc(row.owned_to), interval.end)
        if i + 1 < len(rows):
            hi = min(hi, ensure_utc(rows[i + 1].owned_from))
        tail_from = ensure_utc(row.tail_from) if row.tail_from is not None else None
        for word in file.words:
            start = file.t0 + timedelta(seconds=word.start)
            if lo <= start < hi:
                result.append(
                    TranscriptWord(
                        text=word.text,
                        start_utc=start,
                        end_utc=file.t0 + timedelta(seconds=word.end),
                        confidence=word.confidence,
                        chunk_id=row.chunk_id,
                        provisional=tail_from is not None and start >= tail_from,
                    )
                )
    result.sort(key=lambda w: w.start_utc)  # stable: file order kept for equal starts
    return result


async def coverage(
    session: AsyncSession,
    stream_id: int,
    interval: Interval,
    *,
    now: datetime | None = None,
    ended_grace: timedelta = ENDED_GRACE,
    max_jitter: timedelta = CHUNK_JITTER,
) -> Coverage:
    """Split `interval` into the parts of `Coverage`:

    - transcribed: union of owned ranges; `provisional` = their `[tail_from, owned_to)` parts.
    - untranscribed: inside a chunk with audio but not owned by any transcript (the chunk has no
      `chunk_transcripts` row yet: its job is queued, running, or failed).
    - gap: `stream_gaps` rows, chunks without audio (never transcribed), time before the stream
      started (or before its first chunk, if that starts earlier) and, once the stream has
      ended, after `ended_at` (or after its last chunk, if that ends later). From
      `ended_at + ended_grace` on (`now` defaults to the current time) the recorder writes
      nothing more, so everything after the stream's last chunk is gap, and so is a hole between
      chunks that has no gap row (the recorder never leaves one; a backfill `--fill-gaps` run
      does at the unfilled edges of a stream's unrecorded tail or head, DESIGN §17).
    - unrecorded: the rest, i.e. no chunk there yet (still being recorded, or a gap whose row
      isn't written yet: the recorder writes it with the chunk after the gap).
    Holes up to `max_jitter` with known parts on both sides are chunk-boundary jitter: next to an
    untranscribed chunk they are untranscribed (resolved once it is transcribed), between two
    transcribed parts transcribed, otherwise gap.

    Raises LookupError if the stream doesn't exist.
    """
    stream = (
        await session.execute(
            sql_text(
                """
                SELECT s.started_at, s.ended_at,
                       (SELECT max(c.end_utc) FROM chunks c WHERE c.stream_id = s.id) AS last_end
                  FROM streams s WHERE s.id = :id
                """
            ),
            {"id": stream_id},
        )
    ).first()
    if stream is None:
        raise LookupError(f"stream {stream_id} does not exist")

    # Look a little beyond the interval, so jitter holes at its edges are recognised too.
    outer = interval.expand(max_jitter, max_jitter)
    params = {"stream_id": stream_id, "start": outer.start, "end": outer.end}
    chunks = (
        await session.execute(
            sql_text(
                """
                SELECT c.start_utc, c.end_utc, c.audio_key IS NOT NULL AS has_audio,
                       ct.owned_from, ct.owned_to, ct.tail_from
                  FROM chunks c
                  LEFT JOIN chunk_transcripts ct ON ct.chunk_id = c.id
                 WHERE c.stream_id = :stream_id
                   AND ((c.start_utc < :end AND c.end_utc > :start)
                        OR (ct.owned_from < :end AND ct.owned_to > :start))
                """
            ),
            params,
        )
    ).all()
    gaps = (
        await session.execute(
            sql_text(
                """
                SELECT start_utc, end_utc FROM stream_gaps
                 WHERE stream_id = :stream_id AND start_utc < :end AND end_utc > :start
                   AND end_utc > start_utc
                """
            ),
            params,
        )
    ).all()

    owned = [c for c in chunks if c.owned_from is not None]
    transcribed = _clipped((Interval(c.owned_from, c.owned_to) for c in owned), outer)
    provisional = _clipped(
        (Interval(c.tail_from, c.owned_to) for c in owned if c.tail_from is not None), outer
    )
    audio = _clipped((Interval(c.start_utc, c.end_utc) for c in chunks if c.has_audio), outer)
    untranscribed = [part for a in audio for part in uncovered(a, transcribed)]

    never = [Interval(g.start_utc, g.end_utc) for g in gaps]
    never += [Interval(c.start_utc, c.end_utc) for c in chunks if not c.has_audio]
    media_start = min([stream.started_at, *(c.start_utc for c in chunks)])
    if outer.start < media_start:
        never.append(Interval(outer.start, media_start))
    recorder_done = False
    if stream.ended_at is not None:
        last_end = max(stream.started_at, stream.last_end or stream.started_at)
        recorder_done = ensure_utc(now or utc_now()) >= stream.ended_at + ended_grace
        off_air = last_end if recorder_done else max(stream.ended_at, last_end)
        if off_air < outer.end:
            never.append(Interval(off_air, outer.end))
    known = transcribed + untranscribed
    gap = [part for g in _clipped(never, outer) for part in uncovered(g, known)]

    unrecorded: list[Interval] = []
    t_ends = {iv.end for iv in transcribed}
    t_starts = {iv.start for iv in transcribed}
    u_edges = {t for iv in untranscribed for t in (iv.start, iv.end)}
    for hole in uncovered(outer, known + gap):
        inside = outer.start < hole.start and hole.end < outer.end  # known parts on both sides
        if not inside or hole.duration > max_jitter:
            # nothing will be recorded there any more: no row, and the recorder is done
            (gap if recorder_done else unrecorded).append(hole)
        elif hole.start in u_edges or hole.end in u_edges:
            untranscribed.append(hole)
        elif hole.start in t_ends and hole.end in t_starts:
            transcribed.append(hole)
        else:
            gap.append(hole)

    return Coverage(
        interval=interval,
        transcribed=tuple(_clipped(transcribed, interval)),
        provisional=tuple(_clipped(provisional, interval)),
        gap=tuple(_clipped(gap, interval)),
        untranscribed=tuple(_clipped(untranscribed, interval)),
        unrecorded=tuple(_clipped(unrecorded, interval)),
    )


def _clipped(intervals: Iterable[Interval], bounds: Interval) -> list[Interval]:
    """The parts of `intervals` inside `bounds`, merged and sorted."""
    parts = (iv.intersection(bounds) for iv in intervals)
    return merge(p for p in parts if p is not None)
