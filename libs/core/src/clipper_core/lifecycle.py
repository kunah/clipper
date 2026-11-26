"""DESIGN §7's clip lifecycle, derived (never stored) from the facts each owner writes (§19).

    candidate -> waiting_for_media -> judging -> rejected | planned -> rendering -> in_review
    -> approved | discarded -> publishing -> published   (+ side states failed, unrenderable)

Facts, later stages first (the first match wins):
1. publications (publisher, §22, migration 0009): any `live` -> published; any `scheduled`,
   `uploading`, `processing` or `unknown` -> publishing;
2. the clip's review (`clip_reviews.status`, §21, 0008): `approved` -> approved; `rejected`,
   `expired`, `withdrawn` -> discarded; `pending`, `revising` -> in_review;
3. the clip's latest render (`renders`, §20, 0007): `rendering` -> rendering; `done` -> in_review
   (its review job is queued); `failed` -> failed; `unrenderable` -> unrenderable;
4. `clips.status` (judge, 0005); no clip row -> candidate.

Render, review and publication statuses this module doesn't know (added after it) match nothing,
so an older reader falls back to the earlier stages' facts. An unknown `clips.status` (a newer
judge) is an error in `lifecycle_state()`; `lifecycle_states()` logs a warning and leaves that
clip out. The status sets below are §20-§22's values as text: their enums come with those units
(7a, 21a, 22a), which may switch these sets over to them (the render set uses `RenderStatus`
since 7a).

`lifecycle_states()` costs two round trips: a catalog lookup (which later-stage tables exist yet:
a database not migrated that far simply has no such facts), then one query.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from clipper_core.contracts import ClipStatus, LifecycleState, RenderStatus
from clipper_core.logs import get_logger

log = get_logger(__name__)

PUBLISHED_PUBLICATIONS = frozenset({"live"})
PUBLISHING_PUBLICATIONS = frozenset({"scheduled", "uploading", "processing", "unknown"})
REVIEW_STATES = {
    "approved": LifecycleState.APPROVED,
    "rejected": LifecycleState.DISCARDED,
    "expired": LifecycleState.DISCARDED,
    "withdrawn": LifecycleState.DISCARDED,
    "pending": LifecycleState.IN_REVIEW,
    "revising": LifecycleState.IN_REVIEW,
}
RENDER_STATES = {
    RenderStatus.RENDERING.value: LifecycleState.RENDERING,
    RenderStatus.DONE.value: LifecycleState.IN_REVIEW,
    RenderStatus.FAILED.value: LifecycleState.FAILED,
    RenderStatus.UNRENDERABLE.value: LifecycleState.UNRENDERABLE,
}

# Tables of later stages (migrations 0007, 0008, 0009).
RENDERS, CLIP_REVIEWS, PUBLICATIONS = "renders", "clip_reviews", "publications"


def _collection(name: str, values: Iterable) -> Iterable:
    # a bare string is an iterable of characters: "live" would read as {"l", "i", "v", "e"}
    if isinstance(values, str | bytes):
        raise TypeError(f"{name} must be a collection of values, not a single {type(values)}")
    return values


def lifecycle_state(
    clip_status: str | None,
    render_status: str | None = None,
    review_status: str | None = None,
    publication_statuses: Iterable[str] = (),
) -> LifecycleState:
    """§7's state from the latest facts: `clips.status` (None = no clip row yet), the status of
    the clip's latest render, of its review, and of all its publications. Pure."""
    publications = set(_collection("publication_statuses", publication_statuses))
    if publications & PUBLISHED_PUBLICATIONS:
        return LifecycleState.PUBLISHED
    if publications & PUBLISHING_PUBLICATIONS:
        return LifecycleState.PUBLISHING
    if review_status in REVIEW_STATES:
        return REVIEW_STATES[review_status]
    if render_status in RENDER_STATES:
        return RENDER_STATES[render_status]
    if clip_status is None:
        return LifecycleState.CANDIDATE
    return LifecycleState(ClipStatus(clip_status).value)  # an unknown clip status raises


async def lifecycle_states(
    session: AsyncSession, clip_ids: Iterable[int]
) -> dict[int, LifecycleState]:
    """§7's state of each clip id. Left out: ids without a clip row, and clips whose status this
    code doesn't know (a newer judge; logged as a warning). Two round trips: a catalog lookup
    (which later-stage tables exist yet), then one query."""
    ids = sorted(set(_collection("clip_ids", clip_ids)))
    if not ids:
        return {}
    present = await _existing_tables(session, (RENDERS, CLIP_REVIEWS, PUBLICATIONS))
    render = (
        "(SELECT r.status FROM renders r WHERE r.clip_id = c.id"
        " ORDER BY r.created_at DESC, r.id DESC LIMIT 1)"
        if RENDERS in present
        else "NULL::text"
    )
    review = (
        "(SELECT cr.status FROM clip_reviews cr WHERE cr.clip_id = c.id)"
        if CLIP_REVIEWS in present
        else "NULL::text"
    )
    publications = (
        "ARRAY(SELECT DISTINCT p.status FROM publications p WHERE p.clip_id = c.id)"
        if PUBLICATIONS in present
        else "ARRAY[]::text[]"
    )
    rows = await session.execute(
        sql_text(
            f"SELECT c.id, c.status, {render} AS render_status, {review} AS review_status,"
            f" {publications} AS publication_statuses"
            " FROM clips c WHERE c.id = ANY(:ids)"
        ),
        {"ids": ids},
    )
    states: dict[int, LifecycleState] = {}
    for r in rows:
        try:
            states[r.id] = lifecycle_state(
                r.status, r.render_status, r.review_status, r.publication_statuses
            )
        except ValueError:
            log.warning("unknown clip status, clip left out", clip_id=r.id, status=r.status)
    return states


async def _existing_tables(session: AsyncSession, names: Iterable[str]) -> set[str]:
    names = list(names)
    found = await session.execute(
        sql_text(
            "SELECT name FROM unnest(CAST(:names AS text[])) AS name"
            " WHERE to_regclass(name) IS NOT NULL"
        ),
        {"names": names},
    )
    return set(found.scalars())
