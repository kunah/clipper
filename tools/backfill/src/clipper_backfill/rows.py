"""The backfill tool's own rows (DESIGN §17 "Who writes what").

- `channels`: inserted inactive (`recording_mode='off'`, `active=false`) when missing, never
  changed otherwise (backfill must not start live monitoring).
- `streams` (`source='backfill'`, `status='ended'` from the start) and `vods`: created together in
  one transaction on the first run, found again on every later one.
- `chunks`/`stream_gaps` of a backfilled stream: via `clipper_core.chunks.commit_chunk` (one
  transaction per chunk, with `vods.media_done_s`), plus gap-only commits here.
- `chat_messages`/`chat_metrics_1s` of a backfilled stream: `clipper_backfill.chat` (through
  `clipper_core.chat.ChatWriter`, one transaction per batch with `vods.chat_done_s`/
  `chat_metrics_to`). Never `chat_events`, never `viewer_snapshots` (replay has neither).
- `viewer_clips` (`upsert_clips`): the backfill tool is their only writer.
- `vod --fill-gaps` (an ended LIVE stream, DESIGN §17): only a `vods` row referencing the live
  stream (`create_fill_vod`), chunks with `source='backfill'` and seq >= `FILL_SEQ_BASE`, and chat
  rows/metrics of seconds without live chat (insert-only). Never the stream row, its live chunks,
  its gap rows or its live chat.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from clipper_backfill.planner import at
from clipper_backfill.t0 import T0Estimate
from clipper_core import jobs, media
from clipper_core.chunks import GapLike
from clipper_core.contracts import (
    FILL_SEQ_BASE,
    DataSource,
    JobKind,
    Priority,
    StreamStatus,
    TranscribePayload,
    VodMediaStatus,
    dedupe_key,
)
from clipper_core.models import Channel, Chunk, Stream, ViewerClip, Vod
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
    chat_done_s: int
    chat_metrics_to: datetime | None
    flags: dict[str, Any]
    channel_user_id: str | None = None  # the channel's Twitch user id (third-party emote sets)
    stream_source: str = str(DataSource.BACKFILL)  # `live`: a `--fill-gaps` row of a live stream

    @property
    def fill(self) -> bool:
        """A `--fill-gaps` row: its stream was recorded live (DESIGN §17)."""
        return self.stream_source == DataSource.LIVE


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
        Vod.chat_done_s,
        Vod.chat_metrics_to,
        Vod.flags,
        Channel.platform_user_id.label("channel_user_id"),
        Stream.source.label("stream_source"),
    )
    .join(Channel, Channel.id == Vod.channel_id)
    .join(Stream, Stream.id == Vod.stream_id)
    .where(Channel.platform == PLATFORM)
)


async def find_vod(session: AsyncSession, video_id: str) -> VodRow | None:
    row = (await session.execute(_VOD_SELECT.where(Vod.platform_video_id == video_id))).first()
    return VodRow(**row._mapping) if row else None


async def find_vod_of_stream(session: AsyncSession, stream_id: int) -> VodRow | None:
    row = (await session.execute(_VOD_SELECT.where(Vod.stream_id == stream_id))).first()
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


async def channel_user_id(session: AsyncSession, channel_id: int) -> str | None:
    return (
        await session.execute(select(Channel.platform_user_id).where(Channel.id == channel_id))
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


async def create_fill_vod(
    session: AsyncSession, new: NewVod, *, channel_id: int, stream_id: int
) -> VodRow:
    """The `vods` row of a `--fill-gaps` run, referencing our LIVE stream (DESIGN §17), in the
    caller's transaction. Writes nothing else: the stream row belongs to the controller.

    Raises ValueError when the stream already has a vods row of another video (`stream_id` is
    UNIQUE) or the stream is not a live one."""
    source = (
        await session.execute(select(Stream.source).where(Stream.id == stream_id))
    ).scalar_one()
    if source != DataSource.LIVE:
        raise ValueError(f"stream {stream_id} is not a live stream ({source})")
    created = (
        await session.execute(
            pg_insert(Vod)
            .values(
                channel_id=channel_id,
                stream_id=stream_id,
                platform_video_id=new.video_id,
                title=new.title,
                platform_created_at=ensure_utc(new.created_at),
                duration_s=new.duration_s,
                t0_utc=ensure_utc(new.t0.t0),
                t0_method=str(new.t0.method),
                t0_candidates=new.t0.candidates,
                rendition=new.rendition,
                media_status=new.media_status,
                chat_status=new.chat_status,
                flags=new.flags,
            )
            .on_conflict_do_nothing()
            .returning(Vod.id)
        )
    ).scalar_one_or_none()
    if created is None:
        owner = (
            await session.execute(select(Vod.platform_video_id).where(Vod.stream_id == stream_id))
        ).scalar_one_or_none()
        raise ValueError(f"stream {stream_id} already has the vods row of video {owner}")
    found = await find_vod(session, new.video_id)
    assert found is not None
    return found


@dataclass(frozen=True, slots=True)
class LiveMedia:
    """What the recorder wrote for a live stream: its chunks (with or without audio) and gaps."""

    chunks: list[Any]  # seq, start_utc, end_utc, audio_key (source = 'live'), by seq
    gaps: list[Any]  # start_utc, end_utc, reason, by start

    @property
    def max_seq(self) -> int | None:
        return max((c.seq for c in self.chunks), default=None)


async def live_media(session: AsyncSession, stream_id: int) -> LiveMedia:
    chunks = (
        await session.execute(
            text(
                "SELECT seq, start_utc, end_utc, audio_key FROM chunks "
                "WHERE stream_id = :s AND source = :live ORDER BY seq"
            ),
            {"s": stream_id, "live": str(DataSource.LIVE)},
        )
    ).all()
    gaps = (
        await session.execute(
            text(
                "SELECT start_utc, end_utc, reason FROM stream_gaps WHERE stream_id = :s "
                "AND end_utc > start_utc ORDER BY start_utc, end_utc"
            ),
            {"s": stream_id},
        )
    ).all()
    return LiveMedia(list(chunks), list(gaps))


@dataclass(frozen=True, slots=True)
class RecorderActivity:
    """What says whether the recorder may still write a live stream (`--fill-gaps` checks it),
    on the database's clock: `now` is the transaction's `now()`, the clock `created_at`/
    `last_seen` were set with; ages are seconds before it."""

    now: datetime
    last_seq: int | None  # the newest live chunk (by `created_at`); None: no live chunk
    last_age_s: float | None  # since that chunk was committed
    recorders: list[tuple[str, float]]  # (worker id, heartbeat age) listing it, newest first


async def recorder_activity(session: AsyncSession, stream_id: int) -> RecorderActivity:
    """The stream's newest live chunk, and every recorder heartbeat row (of any age) whose
    `info.recording` lists the stream. A recorder lists the streams of its channel recorders
    from the sync that starts one until the sync after it has stopped, not the chunks still in
    its finish queue or spool; when the process dies its row keeps the last list
    (`Recorder.beat`)."""
    now = (await session.execute(text("SELECT now()"))).scalar_one()
    last = (
        await session.execute(
            text(
                "SELECT seq, EXTRACT(EPOCH FROM now() - created_at) AS age_s FROM chunks "
                "WHERE stream_id = :s AND source = :live "
                "ORDER BY created_at DESC, seq DESC LIMIT 1"
            ),
            {"s": stream_id, "live": str(DataSource.LIVE)},
        )
    ).first()
    recorders = (
        await session.execute(
            text(
                "SELECT worker_id, EXTRACT(EPOCH FROM now() - last_seen) AS age_s "
                "FROM worker_heartbeats WHERE service = 'recorder' AND info -> 'recording' @> "
                "jsonb_build_array(jsonb_build_object('stream_id', CAST(:s AS bigint))) "
                "ORDER BY last_seen DESC, worker_id"
            ),
            {"s": stream_id},
        )
    ).all()
    return RecorderActivity(
        now=ensure_utc(now),
        last_seq=last.seq if last else None,
        last_age_s=float(last.age_s) if last else None,
        recorders=[(r.worker_id, float(r.age_s)) for r in recorders],
    )


async def fill_leftovers(session: AsyncSession, stream_id: int) -> int:
    """Chunks of the stream that look like a fill's (`source='backfill'` or seq >=
    `FILL_SEQ_BASE`): a first `--fill-gaps` run (no vods row yet) refuses when there are any
    (left by a deleted vods row: their T0 is unknown, the next seq would follow them)."""
    return (
        await session.execute(
            text(
                "SELECT count(*) FROM chunks WHERE stream_id = :s "
                "AND (source = :backfill OR seq >= :base)"
            ),
            {"s": stream_id, "backfill": str(DataSource.BACKFILL), "base": FILL_SEQ_BASE},
        )
    ).scalar_one()


async def next_fill_seq(session: AsyncSession, stream_id: int) -> int:
    """The next `chunks.seq` of a `--fill-gaps` run: `FILL_SEQ_BASE` + the fill chunks so far."""
    return (
        await session.execute(
            text(
                "SELECT COALESCE(MAX(seq) + 1, :base) FROM chunks "
                "WHERE stream_id = :s AND seq >= :base"
            ),
            {"s": stream_id, "base": FILL_SEQ_BASE},
        )
    ).scalar_one()


async def metric_seconds(
    session: AsyncSession, *, channel_id: int, window: Interval
) -> set[datetime]:
    """The seconds of `window` that already have a `chat_metrics_1s` row of the channel (any
    stream, or none)."""
    found = await session.execute(
        text("SELECT ts FROM chat_metrics_1s WHERE channel_id = :c AND ts >= :a AND ts < :b"),
        {"c": channel_id, "a": window.start, "b": window.end},
    )
    return {ensure_utc(r.ts) for r in found}


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


async def merge_flags(session: AsyncSession, vod_id: int, values: dict[str, Any]) -> None:
    """`vods.flags || values` in place (keys set, the rest kept)."""
    await session.execute(
        text(
            "UPDATE vods SET flags = flags || CAST(:v AS jsonb), updated_at = now() WHERE id = :id"
        ),
        {"v": json.dumps(values), "id": vod_id},
    )


async def drop_flags(session: AsyncSession, vod_id: int, *keys: str) -> None:
    """Remove keys from `vods.flags` (no-op when absent)."""
    await session.execute(
        text("UPDATE vods SET flags = flags - CAST(:k AS text[]) WHERE id = :id AND flags ?| :k"),
        {"k": list(keys), "id": vod_id},
    )


async def flag_value(session: AsyncSession, vod_id: int, key: str, *, lock: bool = False) -> Any:
    sql = "SELECT flags -> CAST(:k AS text) FROM vods WHERE id = :id"
    return (
        await session.execute(text(sql + (" FOR UPDATE" if lock else "")), {"k": key, "id": vod_id})
    ).scalar_one_or_none()


async def flag_number(session: AsyncSession, vod_id: int, key: str) -> float:
    value = await flag_value(session, vod_id, key)
    return float(value) if isinstance(value, int | float) else 0.0


async def stream_message_count(session: AsyncSession, stream_id: int) -> int:
    """The stream's backfilled chat messages (a `--fill-gaps` stream also has live ones)."""
    return (
        await session.execute(
            text("SELECT count(*) FROM chat_messages WHERE stream_id = :s AND source = :b"),
            {"s": stream_id, "b": str(DataSource.BACKFILL)},
        )
    ).scalar_one()


async def enqueue_missing_jobs(session: AsyncSession, stream_id: int) -> int:
    """`transcribe` jobs for the stream's backfilled chunks with audio that have none (committed
    with `--no-transcribe` earlier; a `--fill-gaps` stream's live chunks are the recorder's).
    Returns how many were added."""
    missing = (
        await session.execute(
            text(
                """
                SELECT c.id, c.start_utc, c.end_utc FROM chunks c
                 WHERE c.stream_id = :s AND c.audio_key IS NOT NULL AND c.source = :backfill
                   AND NOT EXISTS (SELECT 1 FROM jobs j
                                    WHERE j.kind = :kind AND j.ref_id = c.id)
                 ORDER BY c.seq
                """
            ),
            {
                "s": stream_id,
                "kind": str(JobKind.TRANSCRIBE),
                "backfill": str(DataSource.BACKFILL),
            },
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
    """One summary row per backfilled VOD (all, or the given ids), oldest first. For a
    `--fill-gaps` row (`stream_source = 'live'`) every count is the fill's own: its chunks and
    their jobs, no gaps (it writes none; the recorder's are not counted), and as chat seconds the
    seconds of its messages (each has the metrics row the fill wrote)."""
    where = "AND v.platform_video_id = ANY(:ids)" if video_ids else ""
    return (
        await session.execute(
            text(
                f"""
                SELECT v.platform_video_id AS video_id, c.login, v.stream_id, v.t0_utc,
                       v.t0_method, v.duration_s, v.rendition, v.media_status, v.media_done_s,
                       v.chat_status, v.chat_done_s, v.chat_metrics_to, v.flags, v.updated_at,
                       s.source AS stream_source,
                       CASE WHEN s.source = 'backfill'
                            THEN (SELECT count(*) FROM chat_metrics_1s m
                                   WHERE m.stream_id = v.stream_id)
                            ELSE (SELECT count(DISTINCT date_trunc('second', cm.sent_at))
                                    FROM chat_messages cm
                                   WHERE cm.stream_id = v.stream_id AND cm.source = 'backfill')
                       END AS chat_seconds,
                       (SELECT count(*) FROM viewer_clips vc
                         WHERE vc.stream_id = v.stream_id) AS clips,
                       (SELECT count(*) FROM chunks k WHERE k.stream_id = v.stream_id
                           AND k.source = 'backfill') AS chunks,
                       (SELECT count(*) FROM stream_gaps g
                         WHERE g.stream_id = v.stream_id AND s.source = 'backfill') AS gaps,
                       (SELECT COALESCE(SUM(EXTRACT(EPOCH FROM g.end_utc - g.start_utc)), 0)
                          FROM stream_gaps g
                         WHERE g.stream_id = v.stream_id AND g.reason = 'muted'
                           AND s.source = 'backfill') AS muted_s,
                       (SELECT count(*) FILTER (WHERE j.status = 'done') FROM jobs j
                          JOIN chunks k ON k.id = j.ref_id
                         WHERE j.kind = 'transcribe' AND k.stream_id = v.stream_id
                           AND k.source = 'backfill') AS transcribed,
                       (SELECT count(*) FILTER (WHERE j.status IN ('queued', 'running'))
                          FROM jobs j JOIN chunks k ON k.id = j.ref_id
                         WHERE j.kind = 'transcribe' AND k.stream_id = v.stream_id
                           AND k.source = 'backfill') AS transcribe_pending
                  FROM vods v JOIN channels c ON c.id = v.channel_id
                  JOIN streams s ON s.id = v.stream_id
                 WHERE c.platform = :platform {where}
                 ORDER BY v.created_at, v.id
                """
            ),
            {"platform": PLATFORM, "ids": list(video_ids)},
        )
    ).all()


# --------------------------------------------------------------------------- channel list (reads)


@dataclass(frozen=True, slots=True)
class OurStream:
    """Our stream of an archive's broadcast (live-recorded or backfilled) and its chunks."""

    id: int
    source: str  # DataSource
    status: str  # StreamStatus
    chunks: int
    first_start: datetime | None  # of the first chunk (None: no chunks)
    last_end: datetime | None


@dataclass(frozen=True, slots=True)
class OurVod:
    """The archive's `vods` row: backfill progress."""

    stream_id: int
    duration_s: float
    media_status: str
    media_done_s: float
    chat_status: str
    chat_done_s: int


@dataclass(frozen=True, slots=True)
class Have:
    """What we already have of one archive (`clipper-backfill channel`)."""

    stream: OurStream | None
    vod: OurVod | None


async def what_we_have(
    session: AsyncSession,
    *,
    login: str,
    user_id: str | None,
    archives: Sequence[tuple[str, str | None]],
) -> dict[str, Have]:
    """{video id: Have} for a channel's archives, given as (video id, Helix `stream_id`) pairs:
    our stream of the broadcast (`streams.platform_stream_id`) and/or the video's `vods` row.
    Archives we have nothing of are left out. SELECTs only."""
    channel_id = await find_channel(session, login=login, user_id=user_id)
    if channel_id is None or not archives:
        return {}
    vods = {
        r.platform_video_id: OurVod(
            r.stream_id, r.duration_s, r.media_status, r.media_done_s, r.chat_status, r.chat_done_s
        )
        for r in await session.execute(
            select(
                Vod.platform_video_id,
                Vod.stream_id,
                Vod.duration_s,
                Vod.media_status,
                Vod.media_done_s,
                Vod.chat_status,
                Vod.chat_done_s,
            ).where(
                Vod.channel_id == channel_id, Vod.platform_video_id.in_([v for v, _ in archives])
            )
        )
    }
    found = await session.execute(
        select(
            Stream.id,
            Stream.platform_stream_id,
            Stream.source,
            Stream.status,
            func.count(Chunk.id).label("chunks"),
            func.min(Chunk.start_utc).label("first_start"),
            func.max(Chunk.end_utc).label("last_end"),
        )
        .outerjoin(Chunk, Chunk.stream_id == Stream.id)
        .where(
            Stream.channel_id == channel_id,
            or_(
                Stream.platform_stream_id.in_([s for _, s in archives if s]),
                Stream.id.in_([v.stream_id for v in vods.values()]),
            ),
        )
        .group_by(Stream.id)
    )
    by_id: dict[int, OurStream] = {}
    by_platform_id: dict[str, OurStream] = {}
    for r in found:
        first, last = r.first_start, r.last_end
        stream = OurStream(
            r.id,
            r.source,
            r.status,
            r.chunks,
            ensure_utc(first) if first else None,
            ensure_utc(last) if last else None,
        )
        by_id[r.id] = by_platform_id[r.platform_stream_id] = stream
    have: dict[str, Have] = {}
    for video_id, platform_stream_id in archives:
        vod = vods.get(video_id)
        stream = by_platform_id.get(platform_stream_id or "")
        if stream is None and vod is not None:
            stream = by_id.get(vod.stream_id)
        if stream is not None or vod is not None:
            have[video_id] = Have(stream, vod)
    return have


# --------------------------------------------------------------------------- chat


@dataclass(frozen=True, slots=True)
class ForeignChat:
    """chat_metrics_1s rows of the channel in a VOD's range that are not the VOD's own."""

    rows: int
    live: bool  # some are live chat (of a live stream, or of none: chat while offline)
    first: datetime
    last: datetime


async def foreign_chat_metrics(
    session: AsyncSession, *, channel_id: int, stream_id: int, window: Interval
) -> ForeignChat | None:
    """Guard 2 (DESIGN §17): the channel's rows in `window` that belong to another stream or to
    none (chat-ingest logs active channels while offline too). Backfilled chat would collide with
    them on the PK `(channel_id, ts)`: live data wins, the VOD's chat is skipped."""
    found = (
        await session.execute(
            text(
                """
                SELECT count(*) AS n, min(m.ts) AS first, max(m.ts) AS last,
                       bool_or(s.id IS NULL OR s.source = :live) AS live
                  FROM chat_metrics_1s m LEFT JOIN streams s ON s.id = m.stream_id
                 WHERE m.channel_id = :c AND m.ts >= :a AND m.ts < :b
                   AND m.stream_id IS DISTINCT FROM :s
                """
            ),
            {
                "c": channel_id,
                "s": stream_id,
                "a": window.start,
                "b": window.end,
                "live": str(DataSource.LIVE),
            },
        )
    ).one()
    if not found.n:
        return None
    return ForeignChat(found.n, bool(found.live), found.first, found.last)


# --------------------------------------------------------------------------- viewer clips


@dataclass(frozen=True, slots=True)
class StreamRow:
    id: int
    channel_id: int
    platform_stream_id: str
    started_at: datetime
    ended_at: datetime | None
    source: str
    status: str = str(StreamStatus.ENDED)


_STREAM_SELECT = select(
    Stream.id,
    Stream.channel_id,
    Stream.platform_stream_id,
    Stream.started_at,
    Stream.ended_at,
    Stream.source,
    Stream.status,
)


async def channel_streams(session: AsyncSession, channel_id: int) -> list[StreamRow]:
    """All our streams of a channel (live and backfilled), oldest first."""
    found = await session.execute(
        _STREAM_SELECT.where(Stream.channel_id == channel_id).order_by(Stream.started_at, Stream.id)
    )
    return [StreamRow(**r._mapping) for r in found]


async def find_stream(
    session: AsyncSession, *, channel_id: int, platform_stream_id: str
) -> StreamRow | None:
    """Our stream (live or backfilled) of this broadcast."""
    found = (
        await session.execute(
            _STREAM_SELECT.where(
                Stream.channel_id == channel_id, Stream.platform_stream_id == platform_stream_id
            )
        )
    ).first()
    return StreamRow(**found._mapping) if found else None


async def stream_by_id(session: AsyncSession, stream_id: int) -> StreamRow | None:
    found = (await session.execute(_STREAM_SELECT.where(Stream.id == stream_id))).first()
    return StreamRow(**found._mapping) if found else None


async def clip_offsets(session: AsyncSession, clip_ids: Sequence[str]) -> dict[str, int | None]:
    """{clip id: stored vod_offset_s} of the clips we have already."""
    if not clip_ids:
        return {}
    found = await session.execute(
        select(ViewerClip.clip_id, ViewerClip.vod_offset_s).where(
            ViewerClip.platform == PLATFORM, ViewerClip.clip_id.in_(list(clip_ids))
        )
    )
    return {r.clip_id: r.vod_offset_s for r in found}


_CLIP_LINK = ("platform_video_id", "vod_offset_s", "start_utc", "end_utc")
_CLIP_SPAN = ("slack_s", "time_source")
_CLIP_REFRESH = ("duration_s", "title", "creator_login", "view_count", "is_featured")


async def upsert_clips(session: AsyncSession, clips: Sequence[dict[str, Any]]) -> None:
    """Insert or refresh `viewer_clips` rows (PK platform + clip_id; `clips` must not repeat
    an id: one statement can't update a row twice).

    A refresh updates counters and titles; the VOD link and the time span (`platform_video_id`,
    `vod_offset_s`, `start_utc`, `end_utc`, `slack_s`, `time_source`) take the new value only
    when it is known, so a re-run fills a null `vod_offset_s` and never erases one. `stream_id`
    keeps the first known value (two overlapping stream windows can't take turns)."""
    if not clips:
        return
    stmt = pg_insert(ViewerClip).values(list(clips))
    table = ViewerClip.__table__.c
    set_: dict[str, Any] = {
        c: func.coalesce(stmt.excluded[c], table[c]) for c in (*_CLIP_LINK, *_CLIP_SPAN)
    }
    set_["stream_id"] = func.coalesce(table.stream_id, stmt.excluded.stream_id)
    set_.update({c: stmt.excluded[c] for c in _CLIP_REFRESH})
    set_["fetched_at"] = func.now()
    await session.execute(
        stmt.on_conflict_do_update(index_elements=["platform", "clip_id"], set_=set_)
    )


# --------------------------------------------------------------------------- calibrate (reads)


async def stream_chunks(session: AsyncSession, stream_id: int) -> list[Any]:
    """The stream's live chunks with audio, in order (seq, start_utc, end_utc, audio_key); never
    the chunks a `--fill-gaps` run cut from the VOD itself."""
    return (
        await session.execute(
            text(
                "SELECT seq, start_utc, end_utc, audio_key FROM chunks "
                "WHERE stream_id = :s AND audio_key IS NOT NULL AND source = :live ORDER BY seq"
            ),
            {"s": stream_id, "live": str(DataSource.LIVE)},
        )
    ).all()


async def live_messages_by_id(
    session: AsyncSession, *, channel_id: int, msg_ids: Sequence[str], window: Interval
) -> dict[str, datetime]:
    """{msg_id: sent_at (tmi-sent-ts)} of the channel's live chat messages among `msg_ids`, sent
    in `window` (keeps the partition scan small)."""
    if not msg_ids:
        return {}
    found = await session.execute(
        text(
            "SELECT msg_id, sent_at FROM chat_messages WHERE channel_id = :c "
            "AND msg_id = ANY(:ids) AND sent_at >= :a AND sent_at < :b AND source = :live"
        ),
        {
            "c": channel_id,
            "ids": list(msg_ids),
            "a": window.start,
            "b": window.end,
            "live": str(DataSource.LIVE),
        },
    )
    return {r.msg_id: r.sent_at for r in found}
