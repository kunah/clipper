"""Database writes of the judge (DESIGN §19): `clips`, `clip_plans`, `judge_calls`, its own
`media_requests`, and the jobs it enqueues (`judge` rounds, `render`, `notify`).

Each function runs inside the caller's transaction. Clip state changes are compare-and-set on
`(status, round)`, so a duplicate job (a stale-lock requeue) can never undo or repeat a step.
Every writer of `clip_plans` locks the clip row first (`reads.clip_for_update`), so plan versions
are taken one at a time (UNIQUE `clip_id, version` is the backstop).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from clipper_core import jobs, media
from clipper_core.contracts import (
    ClipRejectReason,
    ClipStatus,
    JobKind,
    JudgePayload,
    NoticeKind,
    NotifyPayload,
    PlanAuthor,
    Priority,
    RenderPayload,
    Verdict,
    dedupe_key,
    judge_round_key,
)
from clipper_core.timeline import Interval
from clipper_judge.reads import CLIP_COLUMNS, CandidateInfo, ClipRow, clip_for_update, clip_row

REQUESTED_BY = "judge"  # media_requests.requested_by of the judge's own rows


async def upsert_clip(
    session: AsyncSession, cand: CandidateInfo, *, priority: int, now: datetime
) -> ClipRow:
    """The candidate's clip (§19 step 1), created on the first judge job: context = the
    candidate's window, anchor = T, round 0, waiting since `now`. Locked for this transaction."""
    await session.execute(
        text(
            """
            INSERT INTO clips (candidate_id, stream_id, channel_id, status, round, anchor_utc,
                               context_from, context_to, priority, waiting_since)
            VALUES (:cand, :stream, :channel, 'waiting_for_media', 0, :anchor, :ctx_from,
                    :ctx_to, :priority, :now)
            ON CONFLICT (candidate_id) DO NOTHING
            """
        ),
        {
            "cand": cand.id,
            "stream": cand.stream_id,
            "channel": cand.channel_id,
            "anchor": cand.peak_utc,
            "ctx_from": cand.window_from,
            "ctx_to": cand.window_to,
            "priority": int(priority),
            "now": now,
        },
    )
    clip = await clip_for_update(session, candidate_id=cand.id)
    assert clip is not None  # inserted above or by an earlier job
    return clip


async def _set(
    session: AsyncSession,
    clip: ClipRow,
    *,
    expect: ClipStatus,
    values: Mapping[str, Any],
) -> ClipRow | None:
    """Compare-and-set: update the clip only while it still has `expect` status and its round."""
    sets = ", ".join(f"{k} = :{k}" for k in values)
    stmt = text(
        f"UPDATE clips SET {sets}, updated_at = now()"
        " WHERE id = :id AND status = :expect AND round = :round"
        f" RETURNING {CLIP_COLUMNS}"
    )
    if "flags" in values:
        stmt = stmt.bindparams(bindparam("flags", type_=JSONB))
    row = (
        await session.execute(
            stmt, {**values, "id": clip.id, "expect": expect.value, "round": clip.round}
        )
    ).first()
    return None if row is None else clip_row(row)


async def start_judging(session: AsyncSession, clip: ClipRow, *, partial: bool) -> ClipRow | None:
    """`waiting_for_media -> judging` (same round); `partial`: judged at a deadline with part of
    the context missing (flag `partial_context`, copied into the plan)."""
    flags = dict(clip.flags or {})
    if partial:
        flags["partial_context"] = True
    return await _set(
        session,
        clip,
        expect=ClipStatus.WAITING_FOR_MEDIA,
        values={"status": ClipStatus.JUDGING.value, "flags": flags, "error": None},
    )


async def reject(
    session: AsyncSession,
    clip: ClipRow,
    reason: ClipRejectReason,
    *,
    now: datetime,
    expect: ClipStatus,
) -> ClipRow | None:
    out = await _set(
        session,
        clip,
        expect=expect,
        values={
            "status": ClipStatus.REJECTED.value,
            "reject_reason": reason.value,
            "judged_at": now,
        },
    )
    if out is not None:
        await fulfil_own_requests(session, clip.id)
    return out


async def fail(
    session: AsyncSession, clip: ClipRow, error: str, *, expect: ClipStatus = ClipStatus.JUDGING
) -> ClipRow | None:
    """`judging -> failed` (or from `expect`) with the reason: the model or the plan kept
    failing, or the clip's job gave up."""
    out = await _set(
        session,
        clip,
        expect=expect,
        values={"status": ClipStatus.FAILED.value, "error": error[-2000:]},
    )
    if out is not None:
        await fulfil_own_requests(session, clip.id)
    return out


async def planned(
    session: AsyncSession, clip: ClipRow, *, version: int, now: datetime
) -> ClipRow | None:
    out = await _set(
        session,
        clip,
        expect=ClipStatus.JUDGING,
        values={"status": ClipStatus.PLANNED.value, "plan_version": version, "judged_at": now},
    )
    if out is not None:
        await fulfil_own_requests(session, clip.id)
    return out


async def next_round(
    session: AsyncSession, clip: ClipRow, *, context: Interval, now: datetime
) -> ClipRow | None:
    """`judging -> waiting_for_media` with round + 1 and the grown context (need_context)."""
    row = (
        await session.execute(
            text(
                "UPDATE clips SET status = 'waiting_for_media', round = round + 1,"
                " context_from = :a, context_to = :b, waiting_since = :now, updated_at = now()"
                " WHERE id = :id AND status = 'judging' AND round = :round"
                f" RETURNING {CLIP_COLUMNS}"
            ),
            {"a": context.start, "b": context.end, "now": now, "id": clip.id, "round": clip.round},
        )
    ).first()
    return None if row is None else clip_row(row)


# --------------------------------------------------------------------------- plans, calls


async def next_version(session: AsyncSession, clip_id: int) -> int:
    """The next `clip_plans.version` of the clip (the caller holds the clip's row lock)."""
    return (
        await session.execute(
            text("SELECT COALESCE(MAX(version), 0) + 1 FROM clip_plans WHERE clip_id = :c"),
            {"c": clip_id},
        )
    ).scalar_one()


_INSERT_PLAN = text(
    """
    INSERT INTO clip_plans (clip_id, version, round, verdict, plan, context_request, confidence,
                            reasons, repairs, model, prompt_version, judge_call_id, author,
                            parent_id, review_event_id, instruction)
    VALUES (:clip_id, :version, :round, :verdict, :plan, :context_request, :confidence,
            :reasons, :repairs, :model, :prompt_version, :judge_call_id, :author,
            :parent_id, :review_event_id, :instruction)
    RETURNING id
    """
).bindparams(
    bindparam("plan", type_=JSONB(none_as_null=True)),
    bindparam("context_request", type_=JSONB(none_as_null=True)),
    bindparam("reasons", type_=JSONB),
    bindparam("repairs", type_=JSONB),
)


async def insert_plan(
    session: AsyncSession,
    *,
    clip_id: int,
    version: int,
    round_: int,
    verdict: Verdict,
    model: str,
    prompt_version: str,
    plan: Mapping[str, Any] | None = None,
    context_request: Mapping[str, Any] | None = None,
    confidence: float | None = None,
    reasons: Sequence[str] = (),
    repairs: Sequence[str] = (),
    judge_call_id: int | None = None,
    author: PlanAuthor = PlanAuthor.JUDGE,
    parent_id: int | None = None,
    review_event_id: int | None = None,
    instruction: str | None = None,
) -> int:
    return (
        await session.execute(
            _INSERT_PLAN,
            {
                "clip_id": clip_id,
                "version": version,
                "round": round_,
                "verdict": verdict.value,
                "plan": None if plan is None else dict(plan),
                "context_request": None if context_request is None else dict(context_request),
                "confidence": confidence,
                "reasons": list(reasons),
                "repairs": list(repairs),
                "model": model,
                "prompt_version": prompt_version,
                "judge_call_id": judge_call_id,
                "author": author.value,
                "parent_id": parent_id,
                "review_event_id": review_event_id,
                "instruction": instruction,
            },
        )
    ).scalar_one()


async def insert_call(
    session: AsyncSession,
    *,
    clip_id: int | None,
    stream_id: int,
    model: str,
    prompt_version: str,
    request_hash: str,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_s: float,
    answer: Mapping[str, Any] | None,
    error: str | None,
    io_key: str | None,
    provider_request_id: str | None,
) -> int:
    """One `judge_calls` row; the caller commits it alone, right after the call (§19)."""
    return (
        await session.execute(
            text(
                """
                INSERT INTO judge_calls (clip_id, stream_id, model, prompt_version, request_hash,
                                         input_tokens, cached_input_tokens, output_tokens,
                                         cost_usd, latency_s, answer, error, io_key,
                                         provider_request_id)
                VALUES (:clip_id, :stream_id, :model, :prompt_version, :request_hash,
                        :input_tokens, :cached_input_tokens, :output_tokens, :cost_usd,
                        :latency_s, :answer, :error, :io_key, :provider_request_id)
                RETURNING id
                """
            ).bindparams(bindparam("answer", type_=JSONB(none_as_null=True))),
            {
                "clip_id": clip_id,
                "stream_id": stream_id,
                "model": model,
                "prompt_version": prompt_version,
                "request_hash": request_hash,
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
                "latency_s": latency_s,
                "answer": None if answer is None else dict(answer),
                "error": None if error is None else error[-4000:],
                "io_key": io_key,
                "provider_request_id": provider_request_id,
            },
        )
    ).scalar_one()


# --------------------------------------------------------------------------- media, jobs


async def fulfil_own_requests(session: AsyncSession, clip_id: int) -> list[int]:
    """Mark the clip's own media requests fulfilled (never the detector's: its sweep does)."""
    ids = list(
        (
            await session.execute(
                text(
                    "SELECT id FROM media_requests"
                    " WHERE clip_id = :c AND requested_by = :by AND fulfilled_at IS NULL"
                ),
                {"c": clip_id, "by": REQUESTED_BY},
            )
        ).scalars()
    )
    await media.mark_fulfilled(session, ids)
    return ids


async def request_context(session: AsyncSession, clip: ClipRow) -> int:
    """`request_media(context, JUDGE_CONTEXT)` for a clip's grown context (need_context): queued
    transcriptions in it are bumped now, chunks recorded later in it are enqueued at 100. The
    clip's earlier requests are covered by this one, so they are marked fulfilled."""
    await fulfil_own_requests(session, clip.id)
    return await media.request_media(
        session,
        stream_id=clip.stream_id,
        window=clip.context,
        priority=Priority.JUDGE_CONTEXT,
        requested_by=REQUESTED_BY,
        clip_id=clip.id,
    )


async def enqueue_round(session: AsyncSession, clip: ClipRow) -> int:
    """The judge job of the clip's current round (`judge:{cand}:r{round}`), at its priority."""
    return await jobs.enqueue(
        session,
        JobKind.JUDGE,
        payload=JudgePayload(candidate_id=clip.candidate_id).model_dump(),
        priority=clip.priority,
        ref_id=clip.candidate_id,
        dedupe_key=judge_round_key(clip.candidate_id, clip.round),
    )


async def enqueue_render(session: AsyncSession, plan_id: int, *, priority: int) -> int:
    return await jobs.enqueue(
        session,
        JobKind.RENDER,
        payload=RenderPayload(plan_id=plan_id).model_dump(),
        priority=priority,
        ref_id=plan_id,
        dedupe_key=dedupe_key(JobKind.RENDER, plan_id),
    )


async def notify(
    session: AsyncSession,
    kind: NoticeKind,
    *,
    ref_id: int,
    text_: str,
    clip_id: int | None = None,
    severity: str = "warning",
    priority: int = Priority.JUDGE_CONTEXT,
) -> int:
    """A `notify` job for review (§21: only review talks to the messenger), deduped per
    `(ref, kind)`. Nothing runs them until review exists: they queue."""
    payload = NotifyPayload(kind=kind, text=text_[:1000], clip_id=clip_id, severity=severity)
    return await jobs.enqueue(
        session,
        JobKind.NOTIFY,
        payload=payload.model_dump(mode="json"),
        priority=priority,
        ref_id=ref_id,
        dedupe_key=dedupe_key(JobKind.NOTIFY, ref_id, kind.value),
    )
