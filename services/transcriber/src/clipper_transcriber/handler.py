"""Transcription jobs (`JobKind.TRANSCRIBE`, payload `TranscribePayload`): one chunk per job.

    handler = TranscribeHandler(sessions, store, engines, settings)
    runner = JobRunner(sessions, dsn, worker_id, {JobKind.TRANSCRIBE: handler})

Per job, for chunk N with its neighbours P (`seq - 1`) and S (`seq + 1`) of the same stream
(DESIGN.md §16):
1. Read N, P, S and their `chunk_transcripts` rows. `plan_window` (pure) decides the window:
   where it starts (N's start, or inside P's provisional tail: a takeover), where N's ownership
   ends (N's end, or S's `owned_from` when S is already transcribed) and whether N may keep a
   provisional tail of its own. Invariant: owned ranges of contiguous chunks tile,
   `P.owned_to == N.owned_from`.
2. Download and decode N's audio (and P's when the window starts inside P), run the pipeline.
3. ONE transaction under a per-stream advisory lock: re-read P's, N's and S's transcripts and
   check they are what the plan saw (else `HandoverConflict`: plan again, up to
   `HANDOVER_ATTEMPTS` times, then the job runner retries with backoff); upload the words file
   (only now, so a stale attempt never overwrites the file of a newer committed one); upsert N's
   transcript, replace N's segments, shorten P on a takeover, upsert N's audio metrics, and
   NOTIFY `clipper_transcripts` (delivered on commit).
Undecodable audio raises `jobs.PermanentJobError` (the job fails at once, no retries).
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import numpy as np
from sqlalchemy import bindparam
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_core.contracts import (
    CHUNK_JITTER,
    TRANSCRIPTS_NOTIFY_CHANNEL,
    TranscribePayload,
    Word,
    WordsFile,
)
from clipper_core.jobs import ClaimedJob, PermanentJobError
from clipper_core.logs import get_logger, log_context
from clipper_core.storage import keys
from clipper_core.timeline import Interval, ensure_utc
from clipper_transcriber.audio import DecodeError, Pcm, decode_async, samples
from clipper_transcriber.engines import Engines
from clipper_transcriber.pipeline import (
    MetricSecond,
    Segment,
    Window,
    WindowResult,
    process_window,
    segments_from_words,
    to_us,
)
from clipper_transcriber.settings import TranscriberSettings

log = get_logger(__name__)

LANGUAGE = "en"  # English only (DESIGN.md §16)
WORDS_CONTENT_TYPE = "application/json"
LOCK_NAMESPACE = "clipper:transcriber:stream"  # advisory lock key = hash(namespace, stream id)
REALTIME_EWMA_ALPHA = 0.2
HANDOVER_ATTEMPTS = 3  # plans per job when neighbours change meanwhile (HandoverConflict)


class Store(Protocol):
    """The part of `storage.ObjectStore` the handler uses (tests pass an in-memory fake)."""

    def get_bytes(self, key: str) -> bytes: ...

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None: ...

    def download_file(self, key: str, path: Path | str) -> None: ...


class HandoverConflict(RuntimeError):
    """A neighbour's (or this chunk's own) transcript changed between planning and commit, e.g.
    another worker transcribed P or S meanwhile. Nothing was written (not even the words file);
    the handler plans again from the new state, and raises this after `HANDOVER_ATTEMPTS`."""


# --------------------------------------------------------------------------- state and plan


@dataclass(frozen=True, slots=True)
class TranscriptState:
    """A chunk's `chunk_transcripts` row, as far as the handover needs it."""

    chunk_id: int
    owned_from: datetime
    owned_to: datetime
    tail_from: datetime | None
    words_key: str
    model: str
    language: str | None = None


@dataclass(frozen=True, slots=True)
class ChunkState:
    """A `chunks` row and its transcript (None: not transcribed yet)."""

    id: int
    stream_id: int
    seq: int
    start_utc: datetime
    end_utc: datetime
    audio_key: str | None
    transcript: TranscriptState | None = None

    @property
    def own(self) -> Interval:
        return Interval(self.start_utc, self.end_utc)


@dataclass(frozen=True, slots=True)
class WindowPlan:
    """What a job for chunk N transcribes and writes (from `plan_window`).

    - `window_start`: N's `owned_from` and the words file's `t0`: N's start, or earlier when N
      takes over P's provisional tail (`takeover`) or already did so in an earlier attempt
      (`retake`; P is then left as it is).
    - `owned_to` / `keep_tail`: N's end and True, or S's `owned_from` and False when the next
      contiguous chunk S is already transcribed (out of order, or a re-run of N).
    - `prev_new_owned_to`: on a takeover P's new `owned_to` (= `window_start`; P's tail_from
      becomes NULL).
    - `prev_capped_from`: when the cap applied (the tail was longer than `takeover_cap_s`), P
      keeps `[prev_capped_from, window_start)` (its old tail_from onwards) non-provisionally.
    """

    window_start: datetime
    owned_to: datetime
    keep_tail: bool
    takeover: bool = False
    retake: bool = False
    prev_new_owned_to: datetime | None = None
    prev_capped_from: datetime | None = None


def contiguous(end_utc: datetime, start_utc: datetime, tolerance: timedelta) -> bool:
    """Does a chunk starting at `start_utc` continue one that ends at `end_utc`?"""
    return abs(start_utc - end_utc) <= tolerance


def plan_window(
    chunk: ChunkState,
    prev: ChunkState | None,
    next_: ChunkState | None,
    *,
    tolerance: timedelta = CHUNK_JITTER,
    cap: timedelta = timedelta(seconds=30),
) -> WindowPlan:
    """The window of a job for `chunk` (N), from the state of N, P (`seq - 1`) and S
    (`seq + 1`). Pure: no I/O. Rules (DESIGN.md §16 steps 2 and 9):

    - Re-take: N already has a transcript starting before N's start, and the contiguous P ends
      exactly there without a tail: an earlier attempt took P's tail over, so the window starts
      at N's old `owned_from` again (P stays as it is). Without this a retry or re-run would
      leave `[owned_from, N.start)` unowned.
    - Takeover: else, if the contiguous P is transcribed and has a provisional tail, the window
      starts at `max(P.tail_from, P.start, N.start - cap)` (never after N's start); P's
      `owned_to` becomes the window start and its tail is cleared; when the cap applied, P keeps
      `[P.tail_from, window start)` non-provisionally.
    - Else the window starts at N's start.
    - Ownership ends at `min(N.end, S.owned_from)` without a tail when the contiguous S is
      already transcribed, else at N's end, with a tail if speech runs into the cut.
    Chunks are contiguous when they are at most `tolerance` apart (jitter).
    """
    start, end = chunk.start_utc, chunk.end_utc
    own = chunk.transcript
    prev_t = None
    if (
        prev is not None
        and prev.audio_key is not None
        and contiguous(prev.end_utc, start, tolerance)
    ):
        prev_t = prev.transcript

    window_start, takeover, retake = start, False, False
    prev_new_owned_to = prev_capped_from = None
    if (
        own is not None
        and prev_t is not None
        and own.owned_from < start
        and prev_t.owned_to == own.owned_from
        and prev_t.tail_from is None
    ):
        window_start, retake = own.owned_from, True
    elif prev is not None and prev_t is not None and prev_t.tail_from is not None:
        candidate = min(max(prev_t.tail_from, prev.start_utc, start - cap), start)
        if candidate >= prev_t.owned_from:  # else P's row would break its CHECK (degenerate P)
            window_start, takeover = candidate, True
            prev_new_owned_to = candidate
            if candidate > prev_t.tail_from:
                prev_capped_from = prev_t.tail_from

    next_t = None
    if next_ is not None and contiguous(end, next_.start_utc, tolerance):
        next_t = next_.transcript
    if next_t is not None:
        owned_to, keep_tail = max(min(end, next_t.owned_from), window_start), False
    else:
        owned_to, keep_tail = end, True
    return WindowPlan(
        window_start=window_start,
        owned_to=owned_to,
        keep_tail=keep_tail,
        takeover=takeover,
        retake=retake,
        prev_new_owned_to=prev_new_owned_to,
        prev_capped_from=prev_capped_from,
    )


def window_pcm(
    own: Pcm,
    own_start: datetime,
    window_start: datetime,
    prev: Pcm | None = None,
    prev_start: datetime | None = None,
) -> Pcm:
    """The window's audio: P's audio from `window_start` up to N's start, then N's audio.

    P's part has exactly `samples(N.start - window_start)` samples (cut where P's audio runs
    past N's start, zero-padded where it ends early or a jitter hole lies between the chunks),
    so N's audio starts at the window offset of N's start.
    """
    lead = samples((ensure_utc(own_start) - ensure_utc(window_start)).total_seconds())
    if lead <= 0:
        return own
    if prev is None or prev_start is None:
        raise ValueError("the window starts before the chunk: the previous chunk's audio needed")
    part = np.zeros(lead, dtype=np.float32)
    a = samples((ensure_utc(window_start) - ensure_utc(prev_start)).total_seconds())
    src_a, src_b = max(a, 0), min(a + lead, len(prev))
    if src_b > src_a:
        part[src_a - a : src_b - a] = prev[src_a:src_b]
    return np.concatenate([part, np.asarray(own, dtype=np.float32)])


def stream_lock_key(stream_id: int) -> int:
    """Advisory lock key (signed 64 bit) serialising transcript commits of one stream."""
    digest = hashlib.blake2b(f"{LOCK_NAMESPACE}:{stream_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


# --------------------------------------------------------------------------- database


_NEIGHBOURHOOD = sql_text(
    """
    SELECT c.id, c.stream_id, c.seq, c.start_utc, c.end_utc, c.audio_key,
           ct.chunk_id AS t_chunk_id, ct.owned_from, ct.owned_to, ct.tail_from,
           ct.words_key, ct.model, ct.language
      FROM chunks n
      JOIN chunks c ON c.stream_id = n.stream_id AND c.seq BETWEEN n.seq - 1 AND n.seq + 1
      LEFT JOIN chunk_transcripts ct ON ct.chunk_id = c.id
     WHERE n.id = :chunk_id
    """
)


@dataclass(frozen=True, slots=True)
class Neighbourhood:
    """A chunk with the previous and next chunk of its stream (None: not recorded yet)."""

    prev: ChunkState | None
    chunk: ChunkState
    next: ChunkState | None

    def changed(self, now: Neighbourhood | None) -> list[str]:
        """Which transcripts the plan depends on differ in `now` ("previous", "own", "next")."""
        if now is None:
            return ["own"]
        names = ("previous", "own", "next")
        pairs = zip(names, self.transcripts(), now.transcripts(), strict=True)
        return [name for name, a, b in pairs if a != b]

    def transcripts(self) -> tuple[TranscriptState | None, ...]:
        return tuple(
            c.transcript if c is not None else None for c in (self.prev, self.chunk, self.next)
        )


def _chunk_state(row: Any) -> ChunkState:
    transcript = None
    if row.t_chunk_id is not None:
        transcript = TranscriptState(
            chunk_id=row.t_chunk_id,
            owned_from=ensure_utc(row.owned_from),
            owned_to=ensure_utc(row.owned_to),
            tail_from=ensure_utc(row.tail_from) if row.tail_from is not None else None,
            words_key=row.words_key,
            model=row.model,
            language=row.language,
        )
    return ChunkState(
        id=row.id,
        stream_id=row.stream_id,
        seq=row.seq,
        start_utc=ensure_utc(row.start_utc),
        end_utc=ensure_utc(row.end_utc),
        audio_key=row.audio_key,
        transcript=transcript,
    )


async def load_neighbourhood(session: AsyncSession, chunk_id: int) -> Neighbourhood | None:
    """Chunk `chunk_id` with the previous and next chunk of its stream (by seq), each with its
    transcript. None when the chunk doesn't exist."""
    rows = (await session.execute(_NEIGHBOURHOOD, {"chunk_id": chunk_id})).all()
    by_id = {row.id: _chunk_state(row) for row in rows}
    chunk = by_id.get(chunk_id)
    if chunk is None:
        return None
    by_seq = {c.seq: c for c in by_id.values()}
    return Neighbourhood(by_seq.get(chunk.seq - 1), chunk, by_seq.get(chunk.seq + 1))


_UPSERT_TRANSCRIPT = sql_text(
    """
    INSERT INTO chunk_transcripts (chunk_id, model, language, words_key, segment_count,
                                   processing_s, owned_from, owned_to, tail_from)
    VALUES (:chunk_id, :model, :language, :words_key, :segment_count,
            :processing_s, :owned_from, :owned_to, :tail_from)
    ON CONFLICT (chunk_id) DO UPDATE
       SET model = EXCLUDED.model, language = EXCLUDED.language,
           words_key = EXCLUDED.words_key, segment_count = EXCLUDED.segment_count,
           processing_s = EXCLUDED.processing_s, owned_from = EXCLUDED.owned_from,
           owned_to = EXCLUDED.owned_to, tail_from = EXCLUDED.tail_from
    """
)

_INSERT_SEGMENT = sql_text(
    """
    INSERT INTO transcript_segments (stream_id, chunk_id, start_utc, end_utc, text, language,
                                     avg_logprob, no_speech_prob, confidence, model, provisional)
    VALUES (:stream_id, :chunk_id, :start_utc, :end_utc, :text, :language,
            :avg_logprob, :no_speech_prob, :confidence, :model, :provisional)
    """
)

_UPSERT_METRIC = sql_text(
    """
    INSERT INTO audio_metrics_1s (stream_id, ts, loudness_db, speech_ratio, classes)
    VALUES (:stream_id, :ts, :loudness_db, :speech_ratio, :classes)
    ON CONFLICT (stream_id, ts) DO UPDATE
       SET loudness_db = EXCLUDED.loudness_db, speech_ratio = EXCLUDED.speech_ratio,
           classes = EXCLUDED.classes
    """
).bindparams(bindparam("classes", type_=JSONB))


def segment_row(
    segment: Segment,
    t0: datetime,
    *,
    stream_id: int,
    chunk_id: int,
    model: str,
    language: str | None,
) -> dict[str, Any]:
    """A `transcript_segments` row; the segment's times are seconds after `t0`."""
    return {
        "stream_id": stream_id,
        "chunk_id": chunk_id,
        "start_utc": t0 + timedelta(microseconds=to_us(segment.start)),
        "end_utc": t0 + timedelta(microseconds=to_us(segment.end)),
        "text": segment.text,
        "language": language,
        "avg_logprob": segment.avg_logprob,
        "no_speech_prob": segment.no_speech_prob,
        "confidence": segment.confidence,
        "model": model,
        "provisional": segment.provisional,
    }


async def _insert_segments(session: AsyncSession, rows: Sequence[Mapping[str, Any]]) -> None:
    if rows:
        await session.execute(_INSERT_SEGMENT, [dict(r) for r in rows])


def transcript_rows(
    chunk: ChunkState, plan: WindowPlan, result: WindowResult
) -> tuple[datetime | None, list[dict[str, Any]]]:
    """N's `tail_from` and segment rows as they are written. Only segments starting inside the
    owned range are kept; a tail that would break the CHECK (`owned_from <= tail_from <
    owned_to`) is dropped, and then no segment stays provisional."""
    tail_from = result.tail_from_utc
    if tail_from is not None and not plan.window_start <= tail_from < plan.owned_to:
        tail_from = None
    rows = [
        segment_row(
            s,
            plan.window_start,
            stream_id=chunk.stream_id,
            chunk_id=chunk.id,
            model=result.model,
            language=LANGUAGE,
        )
        for s in result.segments
    ]
    rows = [r for r in rows if plan.window_start <= r["start_utc"] < plan.owned_to]
    for r in rows:
        r["provisional"] = tail_from is not None and r["provisional"]
    return tail_from, rows


async def _write_transcript(
    session: AsyncSession,
    chunk: ChunkState,
    plan: WindowPlan,
    result: WindowResult,
    *,
    words_key: str,
    tail_from: datetime | None,
    segments: Sequence[Mapping[str, Any]],
) -> None:
    """Upsert N's `chunk_transcripts` row and replace its segments."""
    await session.execute(
        _UPSERT_TRANSCRIPT,
        {
            "chunk_id": chunk.id,
            "model": result.model,
            "language": LANGUAGE,
            "words_key": words_key,
            "segment_count": len(segments),
            "processing_s": result.timings.total_s,
            "owned_from": plan.window_start,
            "owned_to": plan.owned_to,
            "tail_from": tail_from,
        },
    )
    await session.execute(
        sql_text("DELETE FROM transcript_segments WHERE chunk_id = :id"), {"id": chunk.id}
    )
    await _insert_segments(session, segments)


async def _take_over_previous(
    session: AsyncSession,
    prev: ChunkState,
    plan: WindowPlan,
    rebuilt: Sequence[Mapping[str, Any]],
) -> None:
    """P after a takeover: owned up to the window start, no tail; its segments from the old
    tail on (the provisional ones), and any starting at or after its new `owned_to` (a tail
    inside a jitter overlap, past N's start), are replaced by `rebuilt` (non-provisional, only
    when the cap applied)."""
    assert prev.transcript is not None and prev.transcript.tail_from is not None
    await session.execute(
        sql_text(
            """
            DELETE FROM transcript_segments
             WHERE chunk_id = :id
               AND (provisional OR start_utc >= least(:tail_from, :owned_to))
            """
        ),
        {
            "id": prev.id,
            "tail_from": prev.transcript.tail_from,
            "owned_to": plan.prev_new_owned_to,
        },
    )
    await _insert_segments(session, rebuilt)
    await session.execute(
        sql_text(
            """
            UPDATE chunk_transcripts
               SET owned_to = :owned_to, tail_from = NULL,
                   segment_count = (SELECT count(*) FROM transcript_segments
                                     WHERE chunk_id = :id)
             WHERE chunk_id = :id
            """
        ),
        {"id": prev.id, "owned_to": plan.prev_new_owned_to},
    )


async def _write_metrics(
    session: AsyncSession, stream_id: int, metrics: Sequence[MetricSecond]
) -> None:
    if metrics:
        await session.execute(
            _UPSERT_METRIC,
            [
                {
                    "stream_id": stream_id,
                    "ts": m.ts,
                    "loudness_db": m.loudness_db,
                    "speech_ratio": m.speech_ratio,
                    "classes": dict(m.classes),
                }
                for m in metrics
            ],
        )


async def _notify(session: AsyncSession, stream_id: int) -> None:
    await session.execute(
        sql_text("SELECT pg_notify(:channel, :payload)"),
        {"channel": TRANSCRIPTS_NOTIFY_CHANNEL, "payload": str(stream_id)},
    )


# --------------------------------------------------------------------------- handler


@dataclass
class JobStats:
    """Counters since the process started (reported by the heartbeat)."""

    done: int = 0
    failed: int = 0  # failed attempts (each one is retried until its max attempts)
    realtime_factor: float | None = None  # EWMA of window audio s / processing s
    current_chunk: int | None = None

    def record_realtime(self, factor: float, alpha: float = REALTIME_EWMA_ALPHA) -> None:
        if not math.isfinite(factor) or factor <= 0:
            return
        old = self.realtime_factor
        self.realtime_factor = factor if old is None else (1 - alpha) * old + alpha * factor


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one job wrote."""

    chunk: ChunkState
    plan: WindowPlan
    result: WindowResult
    words_key: str
    segments: int


class TranscribeHandler:
    """Handles `transcribe` jobs (see the module docstring). One per worker process."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        store: Store,
        engines: Engines,
        settings: TranscriberSettings,
        *,
        stats: JobStats | None = None,
    ) -> None:
        self.sessions = sessions
        self.store = store
        self.engines = engines
        self.settings = settings
        self.config = settings.pipeline_config()
        self.stats = stats if stats is not None else JobStats()
        self.tolerance = timedelta(seconds=settings.contiguity_tolerance_s)
        self.cap = timedelta(seconds=settings.takeover_cap_s)

    async def __call__(self, job: ClaimedJob) -> None:
        payload = TranscribePayload.model_validate(job.payload)
        self.stats.current_chunk = payload.chunk_id
        try:
            await self.transcribe(payload.chunk_id)
        except Exception:
            self.stats.failed += 1
            raise
        else:
            self.stats.done += 1
        finally:
            self.stats.current_chunk = None

    async def transcribe(self, chunk_id: int) -> Outcome | None:
        """Transcribe one chunk and commit it. None when there is nothing to do (no such chunk,
        or a chunk without audio). Plans again when neighbours change meanwhile; raises
        HandoverConflict after `HANDOVER_ATTEMPTS` plans, PermanentJobError for undecodable
        audio, and audio.AudioError, storage and database errors (the job runner retries)."""
        audio_cache: dict[str, Pcm] = {}  # decoded once per job, also across re-plans
        for attempt in range(1, HANDOVER_ATTEMPTS + 1):
            async with self.sessions() as session:
                before = await load_neighbourhood(session, chunk_id)
            if before is None:
                log.warning("chunk not found, nothing to transcribe", chunk_id=chunk_id)
                return None
            chunk = before.chunk
            with log_context(chunk_id=chunk.id, stream_id=chunk.stream_id, seq=chunk.seq):
                if chunk.audio_key is None:
                    log.info("chunk has no audio, nothing to transcribe")
                    return None
                try:
                    return await self._transcribe(before, audio_cache)
                except HandoverConflict as exc:
                    if attempt == HANDOVER_ATTEMPTS:
                        raise
                    log.warning(
                        "handover conflict, planning again", attempt=attempt, error=str(exc)
                    )
        raise AssertionError("unreachable")

    async def _transcribe(self, before: Neighbourhood, audio_cache: dict[str, Pcm]) -> Outcome:
        chunk, prev = before.chunk, before.prev
        plan = plan_window(chunk, prev, before.next, tolerance=self.tolerance, cap=self.cap)
        log.debug(
            "window planned",
            window_from=plan.window_start,
            owned_to=plan.owned_to,
            keep_tail=plan.keep_tail,
            takeover=plan.takeover,
            retake=plan.retake,
        )
        started = time.perf_counter()
        source = prev if plan.window_start < chunk.start_utc else None
        pcm = await self._window_audio(chunk, source, plan, audio_cache)
        audio_s = time.perf_counter() - started

        window = Window(
            pcm,
            plan.window_start,
            chunk.own,
            owned_to=plan.owned_to,
            keep_tail=plan.keep_tail,
        )
        result = await process_window(window, self.engines, self.config)

        words_key = keys.chunk_words(chunk.stream_id, chunk.seq)
        file = WordsFile(
            chunk_id=chunk.id,
            stream_id=chunk.stream_id,
            seq=chunk.seq,
            model=result.model,
            t0=plan.window_start,
            words=[
                Word(text=w.text, start=w.start, end=w.end, confidence=w.confidence)
                for w in result.words
            ],
        )
        tail_from, segments = transcript_rows(chunk, plan, result)

        rebuilt: list[dict[str, Any]] = []
        async with self.sessions.begin() as session:
            await session.execute(
                sql_text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": stream_lock_key(chunk.stream_id)},
            )
            now = await load_neighbourhood(session, chunk.id)
            changed = before.changed(now)
            if changed:
                raise HandoverConflict(
                    f"chunk {chunk.id}: {', '.join(changed)} transcript changed meanwhile"
                )
            # Under the lock and after the check, so no other attempt for this stream commits in
            # between: the words file always belongs to the newest committed row.
            if plan.takeover and plan.prev_capped_from is not None:
                assert prev is not None
                rebuilt = await self._rebuild_previous(prev, plan)
            await asyncio.to_thread(
                self.store.put_bytes, words_key, file.model_dump_json().encode(), WORDS_CONTENT_TYPE
            )
            await _write_transcript(
                session,
                chunk,
                plan,
                result,
                words_key=words_key,
                tail_from=tail_from,
                segments=segments,
            )
            if plan.takeover:
                assert prev is not None
                await _take_over_previous(session, prev, plan, rebuilt)
            await _write_metrics(session, chunk.stream_id, result.metrics)
            await _notify(session, chunk.stream_id)

        timings = result.timings
        rtf = timings.realtime_factor if math.isfinite(timings.realtime_factor) else None
        if rtf is not None:
            self.stats.record_realtime(rtf)
        log.info(
            "chunk transcribed",
            window_from=plan.window_start,
            lead_s=round((chunk.start_utc - plan.window_start).total_seconds(), 3),
            owned_to=plan.owned_to,
            tail_from=tail_from,
            takeover=plan.takeover,
            retake=plan.retake,
            capped=plan.prev_capped_from is not None,
            keep_tail=plan.keep_tail,
            words=len(result.words),
            segments=len(segments),
            prev_segments_rebuilt=len(rebuilt),
            metrics=len(result.metrics),
            audio_s=timings.audio_s,
            speech_input_s=timings.speech_input_s,
            processing_s=timings.total_s,
            realtime_factor=round(rtf, 1) if rtf is not None else None,
            fetch_decode_s=round(audio_s, 3),
        )
        return Outcome(chunk, plan, result, words_key, len(segments))

    async def _window_audio(
        self,
        chunk: ChunkState,
        prev: ChunkState | None,
        plan: WindowPlan,
        cache: dict[str, Pcm],
    ) -> Pcm:
        """N's audio, with P's from the window start prepended when the window starts in P.
        Decoded audio is kept in `cache` (by audio key) for the rest of the job."""
        assert chunk.audio_key is not None
        needed = [chunk.audio_key]
        if prev is not None:
            if prev.audio_key is None:  # plan_window never takes over from such a chunk
                raise ValueError(f"previous chunk {prev.id} has no audio")
            needed.append(prev.audio_key)
        missing = [key for key in needed if key not in cache]
        if missing:
            with tempfile.TemporaryDirectory(
                prefix="clipper-transcriber-", ignore_cleanup_errors=True
            ) as tmp:
                paths = [Path(tmp) / f"{i}{_suffix(key)}" for i, key in enumerate(missing)]
                # Wait for every download/decode before the directory goes away, also when one
                # of them fails (the other's thread would still be writing into it).
                results = await asyncio.gather(
                    *(self._fetch_and_decode(k, p) for k, p in zip(missing, paths, strict=True)),
                    return_exceptions=True,
                )
            for r in results:
                if isinstance(r, BaseException):
                    raise r
            cache.update(zip(missing, results, strict=True))
        own = cache[chunk.audio_key]
        if prev is None:
            return own
        assert prev.audio_key is not None
        prev_pcm = cache[prev.audio_key]
        return window_pcm(own, chunk.start_utc, plan.window_start, prev_pcm, prev.start_utc)

    async def _fetch_and_decode(self, key: str, path: Path) -> Pcm:
        await asyncio.to_thread(self.store.download_file, key, path)
        try:
            return await decode_async(
                path, ffmpeg=self.settings.ffmpeg, timeout_s=self.settings.decode_timeout_s
            )
        except DecodeError as exc:
            # A broken file never decodes: fail the job at once. coverage() keeps showing the
            # chunk as untranscribed (correct: its audio is unusable).
            log.error("audio file can't be decoded", audio_key=key, error=str(exc))
            raise PermanentJobError(f"audio {key} can't be decoded: {exc}") from exc

    async def _rebuild_previous(self, prev: ChunkState, plan: WindowPlan) -> list[dict[str, Any]]:
        """Non-provisional segments for what P keeps of its old tail after a capped takeover:
        sentences of P's words starting in `[P.tail_from, window start)`."""
        t = prev.transcript
        assert t is not None and plan.prev_capped_from is not None
        blob = await asyncio.to_thread(self.store.get_bytes, t.words_key)
        file = WordsFile.model_validate_json(blob)
        lo, hi = plan.prev_capped_from, plan.window_start
        words = [w for w in file.words if lo <= file.t0 + timedelta(seconds=w.start) < hi]
        segments = segments_from_words(
            words, max_s=self.config.sentence_max_s, pause_s=self.config.sentence_pause_s
        )
        return [
            segment_row(
                s,
                file.t0,
                stream_id=prev.stream_id,
                chunk_id=prev.id,
                model=t.model,
                language=t.language,
            )
            for s in segments
        ]


def _suffix(key: str) -> str:
    return PurePosixPath(key).suffix or ".bin"
