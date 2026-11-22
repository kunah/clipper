"""`revise_plan` jobs (DESIGN §19 "Revisions", §21): an operator's change of a planned clip.

Review never writes plans: the review event (`review_events.details`, §21, migration 0008)
holds a `patch` (`contracts.PlanPatch`) or an `instruction` (free text), and review enqueues a
`revise_plan` job (ref = the event, payload `RevisePlanPayload`: event, clip, base plan) at
`JUDGE_CONTEXT`. Per job:
- a plan already written for this event -> done (a retried job);
- refused, with `notify(REVISION_REFUSED)` (deduped per event) and no new plan, when the base
  plan isn't the clip's latest plan (`clips.plan_version`; also re-checked inside the write
  transaction), the clip isn't `planned`, the patch doesn't validate (an older judge refuses
  fields it doesn't know) or can't be cut (`resolve_patch` -> `PatchRefused`: also when the
  video is gone, since the cut is clamped to the chunks that still have video);
- a patch -> `resolve_patch` (author `operator`, the contract's 3-180 s, only the moved edges
  snapped) -> one transaction: `clip_plans` v+1 (`parent_id` = the base, `review_event_id`),
  `clips.plan_version`, the `render` job at `JUDGE_CONTEXT` (a human waits).
- an instruction (one model call on the plan + the same input, author `judge`) is **not built
  yet** (6c): it is refused with a notice. It needs a prompt version with a revision section
  (6d or later).

Where revisions come from is a `RevisionSource`: `ReviewEventsSource` reads `review_events`
(refusing to run while that table doesn't exist), tests pass their own.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_core.contracts import (
    ClipStatus,
    EditPlan,
    NoticeKind,
    PlanAuthor,
    PlanPatch,
    Priority,
    RevisePlanPayload,
    Verdict,
)
from clipper_core.jobs import ClaimedJob, PermanentJobError
from clipper_core.logs import get_logger, log_context
from clipper_core.timeline import utc_now
from clipper_judge import reads, writes
from clipper_judge.params import JudgeParams
from clipper_judge.reads import ClipRow, ObjectStore, PlanRow
from clipper_judge.resolver import PatchRefused, resolve_patch
from clipper_judge.settings import JudgeSettings

log = get_logger(__name__)

Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class Revision:
    """What the operator asked for (`review_events.details`)."""

    clip_id: int | None = None  # the event's clip, if the source knows it
    patch: Mapping[str, Any] | None = None  # a PlanPatch as JSON (validated here)
    instruction: str | None = None


class RevisionSource(Protocol):
    async def load(self, session: AsyncSession, review_event_id: int) -> Revision | None: ...


class ReviewEventsSource:
    """`review_events` (§21; its migration, 0008, isn't built yet): `details.patch` or
    `details.instruction` of the event."""

    async def load(self, session: AsyncSession, review_event_id: int) -> Revision | None:
        exists = (
            await session.execute(text("SELECT to_regclass('review_events') IS NOT NULL"))
        ).scalar_one()
        if not exists:
            raise PermanentJobError("review_events doesn't exist yet (review, DESIGN §21)")
        row = (
            await session.execute(
                text("SELECT clip_id, details FROM review_events WHERE id = :id"),
                {"id": review_event_id},
            )
        ).first()
        if row is None:
            return None
        details = row.details or {}
        return Revision(
            clip_id=row.clip_id,
            patch=details.get("patch"),
            instruction=details.get("instruction"),
        )


class Refused(Exception):
    """The revision is refused: the operator gets a notice."""


class ReviseHandler:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        settings: JudgeSettings,
        *,
        source: RevisionSource | None = None,
        clock: Clock = utc_now,
        params: JudgeParams | None = None,
    ) -> None:
        self.sessions = sessions
        self.store = store
        self.settings = settings
        self.source = source or ReviewEventsSource()
        self.clock = clock
        self.params = params or settings.params()
        self.revised = 0
        self.refused = 0

    async def __call__(self, job: ClaimedJob) -> None:
        try:
            payload = RevisePlanPayload.model_validate(job.payload)
        except ValueError as exc:  # a malformed job: retrying can't help
            raise PermanentJobError(f"invalid revise_plan payload: {exc}") from exc
        with log_context(review_event_id=payload.review_event_id, clip_id=payload.clip_id):
            try:
                await self.revise(payload)
            except Refused as exc:
                await self._refuse(payload, str(exc))

    async def revise(self, p: RevisePlanPayload) -> int | None:
        """The new plan's id (None: already applied earlier). Raises `Refused`."""
        async with self.sessions() as s:
            done = await reads.plan_for_review_event(s, p.review_event_id)
            if done is not None:
                log.info("revision already applied", plan_id=done)
                return None
            revision = await self.source.load(s, p.review_event_id)
            if revision is None:
                raise PermanentJobError(f"review event {p.review_event_id} does not exist")
            clip_row = await s.execute(
                text(f"SELECT {reads.CLIP_COLUMNS} FROM clips WHERE id = :id"), {"id": p.clip_id}
            )
            row = clip_row.first()
            if row is None:
                raise PermanentJobError(f"clip {p.clip_id} does not exist")
            clip = reads.clip_row(row)
            base = await reads.plan_row(s, p.base_plan_id)
            self._check(p, revision, clip, base)
            assert base is not None and base.plan is not None
            if revision.instruction is not None:
                raise Refused(
                    "free-text revisions are not supported yet (judge build item 6c); "
                    "edit the fields instead"
                )
            try:
                patch = PlanPatch.model_validate(revision.patch)
            except ValidationError as exc:
                raise Refused(f"the patch is not valid: {_short(exc)}") from exc
            cand = await reads.candidate(s, clip.candidate_id)
            if cand is None:
                raise PermanentJobError(f"candidate {clip.candidate_id} does not exist")
            stream_info = await reads.stream(s, clip.stream_id)
            inputs = await reads.load_inputs(
                s,
                self.store,
                clip,
                cand,
                stream_info,
                params=self.params,
                now=self.clock(),
                sfx_tags=self.settings.sfx_tags,
                audio_only=self.settings.judge_audio_only,
            )
        base_plan = EditPlan.model_validate(base.plan)
        res = resolve_patch(base_plan, patch, inputs, self.params)
        if isinstance(res, PatchRefused):
            raise Refused(res.error)
        plan = res.plan
        async with self.sessions.begin() as s:
            cur = await reads.clip_for_update(s, clip_id=clip.id)
            assert cur is not None
            done = await reads.plan_for_review_event(s, p.review_event_id)  # a duplicate job
            if done is not None:
                log.info("revision already applied", plan_id=done)
                return None
            self._check(p, revision, cur, base)  # a revision or a new round may have landed
            version = await writes.next_version(s, cur.id)
            plan_id = await writes.insert_plan(
                s,
                clip_id=cur.id,
                version=version,
                round_=cur.round,
                verdict=Verdict.CLIP,
                model=plan.model,
                prompt_version=plan.prompt_version,
                plan=plan.model_dump(mode="json"),
                confidence=plan.confidence,
                reasons=plan.reasons,
                repairs=plan.repairs,
                author=PlanAuthor.OPERATOR,
                parent_id=base.id,
                review_event_id=p.review_event_id,
            )
            await s.execute(
                text(
                    "UPDATE clips SET plan_version = :v, updated_at = now()"
                    " WHERE id = :id AND status = 'planned'"
                ),
                {"v": version, "id": cur.id},
            )
            render_job = None
            if self.settings.render_jobs:
                render_job = await writes.enqueue_render(
                    s, plan_id, priority=Priority.JUDGE_CONTEXT
                )
        self.revised += 1
        log.info(
            "plan revised",
            plan_id=plan_id,
            version=version,
            parent_id=base.id,
            repairs=len(plan.repairs),
            render_job=render_job,
        )
        return plan_id

    @staticmethod
    def _check(
        p: RevisePlanPayload, revision: Revision, clip: ClipRow, base: PlanRow | None
    ) -> None:
        if revision.clip_id is not None and revision.clip_id != clip.id:
            raise Refused(f"the review event belongs to clip {revision.clip_id}, not {clip.id}")
        if clip.status != ClipStatus.PLANNED:
            raise Refused(f"the clip is {clip.status}, not planned")
        if base is None or base.clip_id != clip.id:
            raise Refused(f"plan {p.base_plan_id} is not a plan of clip {clip.id}")
        if base.shadow or base.verdict != Verdict.CLIP or base.version != clip.plan_version:
            raise Refused(
                f"plan {base.id} (v{base.version}) is not the clip's latest plan "
                f"(v{clip.plan_version}): revise the latest render"
            )
        if (revision.patch is None) == (revision.instruction is None):
            raise Refused("the review event needs either a patch or an instruction")

    async def _refuse(self, p: RevisePlanPayload, reason: str) -> None:
        async with self.sessions.begin() as s:
            await writes.notify(
                s,
                NoticeKind.REVISION_REFUSED,
                ref_id=p.review_event_id,
                text_=f"Revision refused: {reason}",
                clip_id=p.clip_id,
            )
        self.refused += 1
        log.warning("revision refused", reason=reason)


def _short(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(x) for x in e['loc']) or 'patch'}: {e['msg']}" for e in exc.errors()
    )[:500]
