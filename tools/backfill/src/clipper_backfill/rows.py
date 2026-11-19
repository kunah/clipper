"""The backfill tool's own rows (DESIGN §17 "Who writes what").

- `channels`: inserted inactive (`recording_mode='off'`, `active=false`) when missing, never
  changed otherwise (backfill must not start live monitoring).
- `streams` (`source='backfill'`, `status='ended'` from the start) and `vods`: created together in
  one transaction on the first run, found again on every later one.
- `chunks`/`stream_gaps` of a backfilled stream: via `clipper_core.chunks.commit_chunk` (one
  transaction per chunk, with `vods.media_done_s`), plus gap-only commits here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from clipper_backfill.planner import at
from clipper_backfill.t0 import T0Estimate
from clipper_core import jobs, media
from clipper_core.chunks import GapLike
from clipper_core.contracts import (
    DataSource,
    JobKind,
    Priority,
    StreamStatus,
    TranscribePayload,
    VodMediaStatus,
    dedupe_key,
)
from clipper_core.models import Channel, Stream, Vod
from clipper_core.timeline import Interval, ensure_utc

PLATFORM = "twitch"


class RecordedLive(Exception):
    """The VOD's broadcast is a live-recorded stream: live data wins, backfill writes no media."""

    def __init__(self, stream_id: int) -> None:
        super().__init__(f"stream {stream_id} was recorded live")
        self.stream_id = stream_id


@dataclass(frozen=True, slots=True)
class VodRow:
    id: int
    channel_id: int
    channel_login: str
    stream_id: int
    platform_video_id: str
    t0_utc: datetime
    t0_method: str
    t0_candidates: dict[str, Any]
    duration_s: float
    rendition: str | None
    media_status: str
    media_done_s: float
    chat_status: str
    flags: dict[str, Any]


@dataclass(frozen=True, slots=True)
class NewVod:
    """Everything the first run knows about a VOD before writing anything."""

    video_id: str
    login: str
    user_id: str | None
    display_name: str | None
    platform_stream_id: str
    title: str | None
    category: str | None
    created_at: datetime  # the platform's created_at (Helix, else GQL)
    duration_s: float
    t0: T0Estimate
    rendition: str | None
    media_status: str  # VodMediaStatus
    chat_status: str  # VodChatStatus
    flags: dict[str, Any]


_VOD_SELECT = (
    select(
        Vod.id,
        Vod.channel_id,
        Channel.login.label("channel_login"),
        Vod.stream_id,
        Vod.platform_video_id,
        Vod.t0_utc,
        Vod.t0_method,
        Vod.t0_candidates,
        Vod.duration_s,
        Vod.rendition,
        Vod.media_status,
        Vod.media_done_s,
        Vod.chat_status,
        Vod.flags,
    )
    .join(Channel, Channel.id == Vod.channel_id)
    .where(Channel.platform == PLATFORM)
)


async def find_vod(session: AsyncSession, video_id: str) -> VodRow | None:
    row = (await session.execute(_VOD_SELECT.where(Vod.platform_video_id == video_id))).first()
    return VodRow(**row._mapping) if row else None


async def find_channel(session: AsyncSession, *, login: str, user_id: str | None) -> int | None:
    """By Twitch user id first (logins can change), then by login."""
    if user_id:
        found = (
            await session.execute(
                select(Channel.id).where(
                    Channel.platform == PLATFORM, Channel.platform_user_id == user_id
                )
            )
        ).scalar_one_or_none()
        if found is not None:
            return found
    return (
        await session.execute(
            select(Channel.id).where(Channel.platform == PLATFORM, Channel.login == login.lower())
        )
    ).scalar_one_or_none()


async def live_stream_id(
    session: AsyncSession, *, login: str, user_id: str | None, platform_stream_id: str
) -> int | None:
    """Our live-recorded stream of this broadcast, if any."""
    channel_id = await find_channel(session, login=login, user_id=user_id)
    if channel_id is None:
        return None
    return (
        await session.execute(
            select(Stream.id).where(
                Stream.channel_id == channel_id,
                Stream.platform_stream_id == platform_stream_id,
                Stream.source == DataSource.LIVE,
            )
        )
    ).scalar_one_or_none()


async def ensure_channel(
    session: AsyncSession, *, login: str, user_id: str | None, display_name: str | None
) -> int:
    """The channel's id; a missing channel is inserted inactive (`off`). Existing rows are never
    changed."""
    found = await find_channel(session, login=login, user_id=user_id)
    if found is not None:
        return found
    created = (
        await session.execute(
            text(
                """
                INSERT INTO channels (platform, login, platform_user_id, display_name,
                                      recording_mode, active)
                VALUES (:platform, :login, :uid, :name, 'off', false)
                ON CONFLICT DO NOTHING
                RETURNING id
                """
            ),
            {"platform": PLATFORM, "login": login.lower(), "uid": user_id, "name": display_name},
        )
    ).scalar_one_or_none()
    if created is not None:
        return created
    found = await find_channel(session, login=login, user_id=user_id)  # a concurrent insert
    if found is None:
        raise RuntimeError(f"channel {login} conflicts with another row")
    return found


async def create_vod(session: AsyncSession, new: NewVod) -> VodRow:
    """Channel (if missing) + stream + vods row, in the caller's transaction.

    Raises RecordedLive when the broadcast is one of our live streams, ValueError when the stream
    row already belongs to another VOD.
    """
    channel_id = await ensure_channel(
        session, login=new.login, user_id=new.user_id, display_name=new.display_name
    )
    t0 = ensure_utc(new.t0.t0)
    stream_id = (
        await session.execute(
            pg_insert(Stream)
            .values(
                channel_id=channel_id,
                platform_stream_id=new.platform_stream_id,
                title=new.title,
                category=new.category,
                started_at=t0,
                ended_at=at(t0, new.duration_s),
                status=str(StreamStatus.ENDED),
                source=str(DataSource.BACKFILL),
            )
            .on_conflict_do_nothing(index_elements=["channel_id", "platform_stream_id"])
            .returning(Stream.id)
        )
    ).scalar_one_or_none()
    if stream_id is None:  # the stream exists: ours from an earlier VOD row, or live-recorded
        existing = (
            await session.execute(
                select(Stream.id, Stream.source).where(
                    Stream.channel_id == channel_id,
                    Stream.platform_stream_id == new.platform_stream_id,
                )
            )
        ).one()
        if existing.source == DataSource.LIVE:
            raise RecordedLive(existing.id)
        owner = (
            await session.execute(select(Vod.platform_video_id).where(Vod.stream_id == existing.id))
        ).scalar_one_or_none()
        if owner is not None:
            raise ValueError(
                f"stream {new.platform_stream_id} already belongs to video {owner}, not "
                f"{new.video_id}"
            )
        stream_id = existing.id
    await session.execute(
        insert(Vod).values(
            channel_id=channel_id,
            stream_id=stream_id,
            platform_video_id=new.video_id,
            title=new.title,
            platform_created_at=ensure_utc(new.created_at),
            duration_s=new.duration_s,
            t0_utc=t0,
            t0_method=str(new.t0.method),
            t0_candidates=new.t0.candidates,
            rendition=new.rendition,
            media_status=new.media_status,
            chat_status=new.chat_status,
            flags=new.flags,
        )
    )
    found = await find_vod(session, new.video_id)
    assert found is not None
    return found


async def next_seq(session: AsyncSession, stream_id: int) -> int:
    return (
        await session.execute(
            text("SELECT COALESCE(MAX(seq) + 1, 0) FROM chunks WHERE stream_id = :s"),
            {"s": stream_id},
        )
    ).scalar_one()


async def insert_gaps(session: AsyncSession, stream_id: int, gaps: Sequence[GapLike]) -> None:
    """Gaps with no chunk after them (a muted end, a sub-only VOD). The caller commits them with
    `vods.media_done_s` moving past them, so a re-run never inserts them again."""
    if not gaps:
        return
    await session.execute(
        text(
            "INSERT INTO stream_gaps (stream_id, start_utc, end_utc, reason) "
            "VALUES (:s, :start, :end, :reason)"
        ),
        [
            {
                "s": stream_id,
                "start": ensure_utc(g.start),
                "end": ensure_utc(g.end),
                "reason": str(g.reason),
            }
            for g in gaps
        ],
    )


async def update_vod(session: AsyncSession, vod_id: int, **values: Any) -> None:
    """Set `vods` columns (media_done_s, media_status, ...) and `updated_at`. Never `flags`: they
    are merged (`add_to_flag`), so nothing written since the run started is lost."""
    if "flags" in values:
        raise ValueError("vods.flags are merged, not set: use add_to_flag")
    await session.execute(
        update(Vod).where(Vod.id == vod_id).values(**values, updated_at=text("now()"))
    )


async def add_to_flag(session: AsyncSession, vod_id: int, key: str, amount: float) -> None:
    """`vods.flags[key] += amount` in place (`flags || {key: old + amount}`)."""
    await session.execute(
        text(
            "UPDATE vods SET flags = flags || jsonb_build_object(CAST(:key AS text), "
            "COALESCE((flags ->> CAST(:key AS text))::numeric, 0) + :amount), "
            "updated_at = now() WHERE id = :id"
        ),
        {"key": key, "amount": amount, "id": vod_id},
    )


async def enqueue_missing_jobs(session: AsyncSession, stream_id: int) -> int:
    """`transcribe` jobs for the stream's chunks with audio that have none (committed with
    `--no-transcribe` earlier). Returns how many were added."""
    missing = (
        await session.execute(
            text(
                """
                SELECT c.id, c.start_utc, c.end_utc FROM chunks c
                 WHERE c.stream_id = :s AND c.audio_key IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM jobs j
                                    WHERE j.kind = :kind AND j.ref_id = c.id)
                 ORDER BY c.seq
                """
            ),
            {"s": stream_id, "kind": str(JobKind.TRANSCRIBE)},
        )
    ).all()
    for chunk in missing:
        priority = await media.priority_for_range(
            session,
            stream_id=stream_id,
            window=Interval(chunk.start_utc, chunk.end_utc),
            default=Priority.BACKFILL,
        )
        await jobs.enqueue(
            session,
            JobKind.TRANSCRIBE,
            payload=TranscribePayload(chunk_id=chunk.id).model_dump(),
            priority=priority,
            ref_id=chunk.id,
            dedupe_key=dedupe_key(JobKind.TRANSCRIBE, chunk.id),
        )
    return len(missing)


async def mark_restricted(session: AsyncSession, vod: VodRow) -> bool:
    """Sub-only: `media_status='restricted'` and a `not_recorded` gap over what is not committed
    yet (the whole VOD on a first run). False when it was marked already, or its media is
    complete (`done` stays `done`)."""
    changed = (
        await session.execute(
            text(
                "UPDATE vods SET media_status = :st, media_done_s = GREATEST(media_done_s, "
                "duration_s), updated_at = now() WHERE id = :id "
                "AND media_status NOT IN (:st, :done) RETURNING id"
            ),
            {
                "st": str(VodMediaStatus.RESTRICTED),
                "done": str(VodMediaStatus.DONE),
                "id": vod.id,
            },
        )
    ).scalar_one_or_none()
    if changed is None:
        return False
    if vod.media_done_s < vod.duration_s:
        start, end = at(vod.t0_utc, vod.media_done_s), at(vod.t0_utc, vod.duration_s)
        await session.execute(
            text(
                "INSERT INTO stream_gaps (stream_id, start_utc, end_utc, reason) "
                "VALUES (:s, :a, :b, 'not_recorded')"
            ),
            {"s": vod.stream_id, "a": start, "b": end},
        )
    return True


async def status_rows(session: AsyncSession, video_ids: Sequence[str] = ()) -> list[Any]:
    """One summary row per backfilled VOD (all, or the given ids), oldest first."""
    where = "AND v.platform_video_id = ANY(:ids)" if video_ids else ""
    return (
        await session.execute(
            text(
                f"""
                SELECT v.platform_video_id AS video_id, c.login, v.stream_id, v.t0_utc,
                       v.t0_method, v.duration_s, v.rendition, v.media_status, v.media_done_s,
                       v.chat_status, v.flags, v.updated_at,
                       (SELECT count(*) FROM chunks k WHERE k.stream_id = v.stream_id) AS chunks,
                       (SELECT count(*) FROM stream_gaps g
                         WHERE g.stream_id = v.stream_id) AS gaps,
                       (SELECT COALESCE(SUM(EXTRACT(EPOCH FROM g.end_utc - g.start_utc)), 0)
                          FROM stream_gaps g
                         WHERE g.stream_id = v.stream_id AND g.reason = 'muted') AS muted_s,
                       (SELECT count(*) FILTER (WHERE j.status = 'done') FROM jobs j
                          JOIN chunks k ON k.id = j.ref_id
                         WHERE j.kind = 'transcribe' AND k.stream_id = v.stream_id)
                         AS transcribed,
                       (SELECT count(*) FILTER (WHERE j.status IN ('queued', 'running'))
                          FROM jobs j JOIN chunks k ON k.id = j.ref_id
                         WHERE j.kind = 'transcribe' AND k.stream_id = v.stream_id)
                         AS transcribe_pending
                  FROM vods v JOIN channels c ON c.id = v.channel_id
                 WHERE c.platform = :platform {where}
                 ORDER BY v.created_at, v.id
                """
            ),
            {"platform": PLATFORM, "ids": list(video_ids)},
        )
    ).all()
