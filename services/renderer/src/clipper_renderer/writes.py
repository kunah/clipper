"""The renderer's writes: its `renders` rows, and the jobs it enqueues with them (`review` with a
finished render, `notify(render_failed)` with a failed one) in the same transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from clipper_core import jobs
from clipper_core.contracts import (
    JobKind,
    NoticeKind,
    NotifyPayload,
    Priority,
    RenderStatus,
    ReviewPayload,
    UnrenderableReason,
    dedupe_key,
)


@dataclass(frozen=True, slots=True)
class RenderRow:
    id: int
    job_id: int
    status: str
    pinned_keys: list[Any]


async def claim_row(
    session: AsyncSession, *, job_id: int, clip_id: int, plan_id: int, profile: str
) -> RenderRow:
    """The job's row, created on its first attempt (a retry resumes it). A `failed` row of a job
    someone requeued goes back to `rendering`."""
    await session.execute(
        text(
            "INSERT INTO renders (job_id, clip_id, plan_id, profile)"
            " VALUES (:job, :clip, :plan, :profile) ON CONFLICT (job_id) DO NOTHING"
        ),
        {"job": job_id, "clip": clip_id, "plan": plan_id, "profile": profile},
    )
    row = (
        await session.execute(
            text(
                "SELECT id, job_id, status, pinned_keys FROM renders WHERE job_id = :job FOR UPDATE"
            ),
            {"job": job_id},
        )
    ).one()
    if row.status == RenderStatus.FAILED:
        await session.execute(
            text("UPDATE renders SET status = 'rendering', finished_at = NULL WHERE id = :id"),
            {"id": row.id},
        )
        return RenderRow(row.id, row.job_id, RenderStatus.RENDERING, row.pinned_keys)
    return RenderRow(row.id, row.job_id, row.status, row.pinned_keys)


_JSON = (bindparam("keys", type_=JSONB), bindparam("warnings", type_=JSONB))


async def set_pinned(session: AsyncSession, render_id: int, keys: list[str]) -> None:
    await session.execute(
        text("UPDATE renders SET pinned_keys = :keys WHERE id = :id").bindparams(_JSON[0]),
        {"id": render_id, "keys": keys},
    )


async def set_error(session: AsyncSession, render_id: int, error: str, render_s: float) -> None:
    await session.execute(
        text("UPDATE renders SET error = :e, render_s = :s WHERE id = :id"),
        {"id": render_id, "e": error[-4000:], "s": render_s},
    )


async def mark_unrenderable(
    session: AsyncSession,
    render_id: int,
    reason: UnrenderableReason,
    *,
    detail: str,
    warnings: list[str],
) -> None:
    await session.execute(
        text(
            "UPDATE renders SET status = 'unrenderable', unrenderable_reason = :r, error = :e,"
            " warnings = :warnings, finished_at = now() WHERE id = :id"
        ).bindparams(_JSON[1]),
        {"id": render_id, "r": reason.value, "e": detail[-4000:], "warnings": warnings},
    )


async def mark_done(session: AsyncSession, render_id: int, values: dict[str, Any]) -> None:
    """`values`: the outputs' keys and measurements (columns of `renders`)."""
    cols = ", ".join(f"{k} = :{k}" for k in values)
    stmt = text(
        f"UPDATE renders SET status = 'done', error = NULL, finished_at = now(), {cols}"
        " WHERE id = :id"
    ).bindparams(bindparam("warnings", type_=JSONB), bindparam("pinned_keys", type_=JSONB))
    await session.execute(stmt, {"id": render_id, **values})


async def enqueue_review(session: AsyncSession, render_id: int, *, priority: int) -> int:
    return await jobs.enqueue(
        session,
        JobKind.REVIEW,
        payload=ReviewPayload(render_id=render_id).model_dump(),
        priority=priority,
        ref_id=render_id,
        dedupe_key=dedupe_key(JobKind.REVIEW, render_id),
    )


async def mark_failed(
    session: AsyncSession, render_id: int, *, clip_id: int, error: str, render_s: float
) -> int:
    """The row `failed` and a `notify(render_failed)` job for review (§21), together."""
    await session.execute(
        text(
            "UPDATE renders SET status = 'failed', error = :e, render_s = :s,"
            " finished_at = now() WHERE id = :id"
        ),
        {"id": render_id, "e": error[-4000:], "s": render_s},
    )
    payload = NotifyPayload(
        kind=NoticeKind.RENDER_FAILED,
        text=f"Render {render_id} of clip {clip_id} failed: {error[-600:]}"[:1000],
        clip_id=clip_id,
        severity="error",
    )
    return await jobs.enqueue(
        session,
        JobKind.NOTIFY,
        payload=payload.model_dump(mode="json"),
        priority=int(Priority.JUDGE_CONTEXT),
        ref_id=render_id,
        dedupe_key=dedupe_key(JobKind.NOTIFY, render_id, NoticeKind.RENDER_FAILED.value),
    )
