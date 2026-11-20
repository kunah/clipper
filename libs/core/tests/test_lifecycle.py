"""`clipper_core.lifecycle`: §7's state from clips + later stages' facts (DESIGN §19's mapping),
pure and from the database (before and after the renders/review/publications tables exist)."""

from datetime import timedelta

import pytest
from sqlalchemy import text

from clipper_core.contracts import (
    CandidateSource,
    CandidateStatus,
    ClipRejectReason,
    ClipStatus,
    LifecycleState,
)
from clipper_core.lifecycle import lifecycle_state, lifecycle_states
from clipper_core.models import Candidate, Channel, Clip, Stream
from clipper_core.timeline import utc_now

S = LifecycleState
PUBLISHED_ALREADY = ("planned", "done", "approved")  # facts of a clip that was approved

# (clip status, latest render, review, publications) -> state; the first match wins
MAPPING = [
    # no later facts: the judge's status
    (None, None, None, (), S.CANDIDATE),
    ("waiting_for_media", None, None, (), S.WAITING_FOR_MEDIA),
    ("judging", None, None, (), S.JUDGING),
    ("rejected", None, None, (), S.REJECTED),
    ("planned", None, None, (), S.PLANNED),
    ("failed", None, None, (), S.FAILED),
    # the latest render
    ("planned", "rendering", None, (), S.RENDERING),
    ("planned", "done", None, (), S.IN_REVIEW),  # its review job is queued
    ("planned", "failed", None, (), S.FAILED),
    ("planned", "unrenderable", None, (), S.UNRENDERABLE),
    # the review
    ("planned", "done", "pending", (), S.IN_REVIEW),
    ("planned", "rendering", "revising", (), S.IN_REVIEW),  # a re-render after a revision
    ("planned", "failed", "pending", (), S.IN_REVIEW),  # a failed re-render: still in review
    ("planned", "done", "approved", (), S.APPROVED),
    ("planned", "done", "rejected", (), S.DISCARDED),
    ("planned", "done", "expired", (), S.DISCARDED),
    ("planned", "done", "withdrawn", (), S.DISCARDED),
    # publications (any of them)
    (*PUBLISHED_ALREADY, ("scheduled",), S.PUBLISHING),
    (*PUBLISHED_ALREADY, ("uploading",), S.PUBLISHING),
    (*PUBLISHED_ALREADY, ("processing",), S.PUBLISHING),
    (*PUBLISHED_ALREADY, ("unknown",), S.PUBLISHING),  # may exist: the operator decides
    (*PUBLISHED_ALREADY, ("live",), S.PUBLISHED),
    (*PUBLISHED_ALREADY, ("uploading", "live"), S.PUBLISHED),  # one live post is enough
    (*PUBLISHED_ALREADY, ("failed", "scheduled"), S.PUBLISHING),  # a retry
    # finished publications fall back to the review: approved, nothing posted (any more)
    (*PUBLISHED_ALREADY, ("failed",), S.APPROVED),
    (*PUBLISHED_ALREADY, ("cancelled", "rejected", "removed", "deleted"), S.APPROVED),
    # values added after this module was written match nothing: earlier facts decide
    ("planned", "queued_somewhere", None, (), S.PLANNED),
    ("planned", "done", "escalated", (), S.IN_REVIEW),
    (*PUBLISHED_ALREADY, ("archived",), S.APPROVED),
]


@pytest.mark.parametrize(("clip", "render", "review", "publications", "expected"), MAPPING)
def test_lifecycle_mapping(clip, render, review, publications, expected):
    assert lifecycle_state(clip, render, review, publications) == expected


def test_lifecycle_state_takes_enums_and_iterables_and_rejects_unknown_clip_statuses():
    assert lifecycle_state(ClipStatus.PLANNED) == S.PLANNED
    assert lifecycle_state("planned", publication_statuses=iter(["live"])) == S.PUBLISHED
    assert lifecycle_state("planned", publication_statuses=(s for s in ())) == S.PLANNED
    with pytest.raises(ValueError):
        lifecycle_state("superseded")
    for single in ("live", b"live"):  # one status, not a collection: would read as letters
        with pytest.raises(TypeError, match="publication_statuses"):
            lifecycle_state("planned", publication_statuses=single)


def test_every_clip_status_maps_to_its_own_state():
    assert {lifecycle_state(s) for s in ClipStatus} == {S(s.value) for s in ClipStatus}


# --------------------------------------------------------------------------- database

T0 = utc_now().replace(microsecond=0)


async def make_clips(sessions, statuses: list[str]) -> list[int]:
    async with sessions.begin() as s:
        channel = Channel(login="lifecycle-test")
        s.add(channel)
        await s.flush()
        stream = Stream(channel_id=channel.id, platform_stream_id="s1", started_at=T0)
        s.add(stream)
        await s.flush()
        ids = []
        for i, status in enumerate(statuses):
            peak = T0 + timedelta(minutes=10 + 2 * i)
            cand = Candidate(
                stream_id=stream.id,
                channel_id=channel.id,
                source=CandidateSource.LIVE,
                status=CandidateStatus.EMITTED,
                trigger="chat",
                peak_utc=peak,
                chat_delay_s=8.0,
                delay_source="default",
                moment_from=peak - timedelta(seconds=10),
                moment_to=peak + timedelta(seconds=5),
                window_from=peak - timedelta(seconds=90),
                window_to=peak + timedelta(seconds=30),
                score=6.5,
                detector_version="test",
            )
            s.add(cand)
            await s.flush()
            clip = Clip(
                candidate_id=cand.id,
                stream_id=stream.id,
                channel_id=channel.id,
                status=status,
                reject_reason=ClipRejectReason.MODEL if status == ClipStatus.REJECTED else None,
                anchor_utc=peak,
                context_from=cand.window_from,
                context_to=cand.window_to,
                priority=80,
                waiting_since=utc_now(),
            )
            s.add(clip)
            await s.flush()
            ids.append(clip.id)
    return ids


@pytest.mark.integration
async def test_lifecycle_states_from_clips_before_the_later_tables_exist(sessions):
    statuses = [s.value for s in ClipStatus]
    ids = await make_clips(sessions, statuses)
    async with sessions() as s:
        later = (
            await s.execute(text("SELECT to_regclass('renders'), to_regclass('publications')"))
        ).one()
        states = await lifecycle_states(s, [*ids, ids[0], 999_999])
        assert await lifecycle_states(s, []) == {}
        with pytest.raises(TypeError, match="clip_ids"):
            await lifecycle_states(s, "12")
    assert later == (None, None)  # 0006/0008 aren't there yet
    assert states == {i: S(status) for i, status in zip(ids, statuses, strict=True)}


# The later stages' tables as §20-§22 define them (only the columns lifecycle reads). TEMP tables
# shadow real ones of the same name for this connection and vanish with the transaction.
LATER_TABLES = [
    "CREATE TEMP TABLE renders (id bigserial PRIMARY KEY, clip_id bigint NOT NULL,"
    " status text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()) ON COMMIT DROP",
    "CREATE TEMP TABLE clip_reviews (clip_id bigint PRIMARY KEY, status text NOT NULL)"
    " ON COMMIT DROP",
    "CREATE TEMP TABLE publications (id bigserial PRIMARY KEY, clip_id bigint NOT NULL,"
    " status text NOT NULL) ON COMMIT DROP",
]


@pytest.mark.integration
async def test_lifecycle_states_read_renders_reviews_and_publications_once_they_exist(sessions):
    names = [
        "rendering",
        "rerendered",
        "render_failed",
        "review",
        "discarded",
        "published",
        "planned",
        "rejected",
    ]
    ids = dict(
        zip(
            names,
            await make_clips(sessions, ["planned"] * 7 + ["rejected"]),
            strict=True,
        )
    )
    renders = [  # (clip, status, age): the latest render per clip counts
        ("rendering", "rendering", 0),
        ("rerendered", "failed", 60),
        ("rerendered", "done", 0),
        ("render_failed", "done", 60),
        ("render_failed", "failed", 0),
        ("review", "done", 0),
        ("discarded", "done", 0),
        ("published", "done", 0),
    ]
    async with sessions() as s:
        for ddl in LATER_TABLES:
            await s.execute(text(ddl))
        for clip, status, age in renders:
            await s.execute(
                text(
                    "INSERT INTO renders (clip_id, status, created_at)"
                    " VALUES (:c, :st, now() - CAST(:age AS int) * interval '1 second')"
                ),
                {"c": ids[clip], "st": status, "age": age},
            )
        reviews = (("review", "pending"), ("discarded", "expired"), ("published", "approved"))
        for clip, status in reviews:
            await s.execute(
                text("INSERT INTO clip_reviews (clip_id, status) VALUES (:c, :st)"),
                {"c": ids[clip], "st": status},
            )
        for status in ("failed", "live"):
            await s.execute(
                text("INSERT INTO publications (clip_id, status) VALUES (:c, :st)"),
                {"c": ids["published"], "st": status},
            )
        states = await lifecycle_states(s, ids.values())
    assert {name: states[i] for name, i in ids.items()} == {
        "rendering": S.RENDERING,
        "rerendered": S.IN_REVIEW,
        "render_failed": S.FAILED,
        "review": S.IN_REVIEW,
        "discarded": S.DISCARDED,
        "published": S.PUBLISHED,
        "planned": S.PLANNED,
        "rejected": S.REJECTED,
    }


@pytest.mark.integration
async def test_lifecycle_states_leave_out_clips_with_a_status_from_a_newer_judge(sessions, caplog):
    async with sessions() as s:  # a TEMP `clips` without the CHECK shadows the real table
        await s.execute(text("CREATE TEMP TABLE clips (id bigint, status text) ON COMMIT DROP"))
        await s.execute(text("INSERT INTO clips VALUES (1, 'planned'), (2, 'superseded')"))
        states = await lifecycle_states(s, [1, 2])
    assert states == {1: S.PLANNED}
    assert "unknown clip status, clip left out" in caplog.messages
