"""Database writes of the detector: `candidates`, its own `media_requests` and jobs (DESIGN §18.5).

Each function runs inside the caller's transaction (`sessions.begin()`), so a candidate, its
media request, the bumped transcription priorities and its judge job commit together. Candidate
writes of one stream are serialised by a transaction-level advisory lock (a lease handover can
leave two processes deciding the same stream for a moment).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

from clipper_core import jobs, media, transcripts
from clipper_core.contracts import (
    CandidateStatus,
    JobKind,
    JudgePayload,
    Priority,
    dedupe_key,
)
from clipper_core.models import Candidate
from clipper_core.timeline import Interval
from clipper_detector.explicit import Signal, boost, merge_data
from clipper_detector.merge import CandidateDraft
from clipper_detector.params import DetectorParams
from clipper_detector.reads import CandidateRef, nearest_candidate

REQUESTED_BY = "detector"


async def lock_stream(session: AsyncSession, stream_id: int) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"detect:stream:{stream_id}"}
    )


@dataclass(frozen=True, slots=True)
class Emitted:
    media_request_id: int
    judge_job_id: int | None


async def _emit(
    session: AsyncSession, candidate_id: int, stream_id: int, window: Interval, judge_jobs: bool
) -> Emitted:
    """Media request (bumps queued transcription jobs of the window) + link + judge job."""
    request_id = await media.request_media(
        session,
        stream_id=stream_id,
        window=window,
        priority=Priority.TRIGGER,
        requested_by=REQUESTED_BY,
    )
    await session.execute(
        text("UPDATE candidates SET media_request_id = :m, updated_at = now() WHERE id = :id"),
        {"m": request_id, "id": candidate_id},
    )
    job_id = None
    if judge_jobs:
        job_id = await jobs.enqueue(
            session,
            JobKind.JUDGE,
            payload=JudgePayload(candidate_id=candidate_id).model_dump(),
            priority=Priority.TRIGGER,
            ref_id=candidate_id,
            dedupe_key=dedupe_key(JobKind.JUDGE, candidate_id),
        )
    return Emitted(request_id, job_id)


@dataclass(frozen=True, slots=True)
class Written:
    candidate_id: int | None  # None: not written (a duplicate)
    duplicate_of: CandidateRef | None = None
    emitted: Emitted | None = None


async def write_candidate(
    session: AsyncSession, draft: CandidateDraft, *, judge_jobs: bool, dedupe_s: float
) -> Written:
    """Dedupe against every candidate of the stream within +- `dedupe_s` (any source and
    version), insert ON CONFLICT DO NOTHING, and only for an inserted emitted row: media request
    + judge job. Suppressed rows get nothing else."""
    await lock_stream(session, draft.stream_id)
    dup = await nearest_candidate(session, draft.stream_id, draft.peak_utc, dedupe_s)
    if dup is not None:
        return Written(None, dup)
    stmt = (
        insert(Candidate)
        .values(**draft.row())
        .on_conflict_do_nothing(index_elements=["stream_id", "peak_utc", "source"])
        .returning(Candidate.id)
    )
    candidate_id = (await session.execute(stmt)).scalar_one_or_none()
    if candidate_id is None:  # raced past the dedupe (same peak, same source)
        dup = await nearest_candidate(session, draft.stream_id, draft.peak_utc, 0)
        return Written(None, dup)
    emitted = None
    if draft.emitted:
        emitted = await _emit(session, candidate_id, draft.stream_id, draft.window, judge_jobs)
    return Written(candidate_id, None, emitted)


async def _features_for_update(session: AsyncSession, candidate_id: int):
    """The candidate row, locked: the stream's advisory lock first (the order
    `write_candidate` takes them in), then the row."""
    stream_id = (
        await session.execute(
            text("SELECT stream_id FROM candidates WHERE id = :id"), {"id": candidate_id}
        )
    ).scalar_one_or_none()
    if stream_id is None:
        return None
    await lock_stream(session, stream_id)
    return (
        await session.execute(
            text(
                "SELECT id, stream_id, status, score, features,"
                " window_from, window_to FROM candidates WHERE id = :id FOR UPDATE"
            ),
            {"id": candidate_id},
        )
    ).first()


async def _set_features(
    session: AsyncSession,
    candidate_id: int,
    features: Mapping[str, Any],
    *,
    score: float | None = None,
    status: CandidateStatus | None = None,
) -> None:
    await session.execute(
        text(
            """
            UPDATE candidates
               SET features = :f, score = COALESCE(:score, score),
                   status = COALESCE(:status, status), updated_at = now()
             WHERE id = :id
            """
        ).bindparams(bindparam("f", type_=JSONB)),
        {
            "id": candidate_id,
            "f": dict(features),
            "score": score,
            "status": None if status is None else status.value,
        },
    )


async def add_followup(
    session: AsyncSession, candidate_id: int, followup: Mapping[str, Any]
) -> bool:
    """Append a folded-in episode to `features.followups` (idempotent by its chat peak: a
    restart replay folds the same episode again). False if it was there already."""
    row = await _features_for_update(session, candidate_id)
    if row is None:
        return False
    features = dict(row.features or {})
    followups = list(features.get("followups") or [])
    if any(f.get("peak") == followup.get("peak") for f in followups):
        return False
    followups.append(dict(followup))
    features["followups"] = followups
    await _set_features(session, candidate_id, features)
    return True


@dataclass(frozen=True, slots=True)
class Attached:
    candidate_id: int
    status: CandidateStatus
    score: float
    boost: float
    reemitted: bool
    emitted: Emitted | None = None


async def attach_signals(
    session: AsyncSession,
    candidate_id: int,
    signals: Iterable[Signal],
    *,
    params: DetectorParams,
    judge_jobs: bool,
    now: datetime,
) -> Attached | None:
    """Explicit signals onto an existing candidate: `features.explicit[kind]` (merged), score +
    the boost of each kind not attached before, and a *suppressed* candidate is re-emitted: an
    in-place UPDATE to `emitted` that keeps `suppress_reason`, with its media request and judge
    job in this transaction (DESIGN §18.4)."""
    signals = list(signals)
    row = await _features_for_update(session, candidate_id) if signals else None
    if row is None:
        return None
    features = dict(row.features or {})
    explicit = dict(features.get("explicit") or {})
    added = 0.0
    for s in signals:
        if s.kind not in explicit:
            added += boost(s.kind, params)
        explicit[s.kind] = merge_data(s.kind, explicit.get(s.kind), s.data)
    features["explicit"] = explicit
    reemit = row.status == CandidateStatus.SUPPRESSED
    if reemit:
        features["reemitted"] = {
            "by": sorted({s.kind for s in signals}),
            "at": now.isoformat(),
        }
    score = float(row.score) + added
    status = CandidateStatus.EMITTED if reemit else CandidateStatus(row.status)
    await _set_features(session, candidate_id, features, score=score, status=status)
    emitted = None
    if reemit:
        window = Interval(row.window_from, row.window_to)
        emitted = await _emit(session, candidate_id, row.stream_id, window, judge_jobs)
    return Attached(candidate_id, status, score, added, reemit, emitted)


async def sweep_media_requests(
    session: AsyncSession, *, now: datetime, fulfil_after: timedelta
) -> int:
    """Mark the detector's open media requests fulfilled once `coverage(window)` is complete,
    or `fulfil_after` after the window's end. Returns how many."""
    rows = (
        await session.execute(
            text(
                """
                SELECT id, stream_id, from_utc, to_utc FROM media_requests
                 WHERE requested_by = :by AND fulfilled_at IS NULL
                 ORDER BY id
                """
            ),
            {"by": REQUESTED_BY},
        )
    ).all()
    done: list[int] = []
    for r in rows:
        if r.to_utc + fulfil_after <= now:
            done.append(r.id)
            continue
        cov = await transcripts.coverage(
            session, r.stream_id, Interval(r.from_utc, r.to_utc), now=now
        )
        if cov.complete:
            done.append(r.id)
    await media.mark_fulfilled(session, done)
    return len(done)
