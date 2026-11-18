"""Database tables. One writer service per table (noted on each class); anyone may read.

Migrations are hand-written in db/ and must stay additive (add columns/tables; never rename or
drop in the same release), because services are updated at different times.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this class attribute
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
    }


def _created_at() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


# --------------------------------------------------------------------------- channels & streams


class Channel(Base):
    """Writer: controller (and the operator adding channels)."""

    __tablename__ = "channels"
    __table_args__ = (
        UniqueConstraint("platform", "login"),
        UniqueConstraint("platform", "platform_user_id"),
        CheckConstraint("recording_mode IN ('full', 'audio_only', 'off')", name="recording_mode"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    platform: Mapped[str] = mapped_column(Text, server_default="twitch")
    login: Mapped[str] = mapped_column(Text)
    platform_user_id: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    recording_mode: Mapped[str] = mapped_column(Text, server_default="full")
    active: Mapped[bool] = mapped_column(server_default=sql_text("true"))
    chat_delay_s: Mapped[float | None] = mapped_column(Float)  # learned per channel
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class Stream(Base):
    """One broadcast. Writer: controller."""

    __tablename__ = "streams"
    __table_args__ = (
        UniqueConstraint("channel_id", "platform_stream_id"),
        Index(None, "channel_id", "started_at"),
        CheckConstraint("status IN ('live', 'ended')", name="status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    platform_stream_id: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(Text, server_default="live")
    source: Mapped[str] = mapped_column(Text, server_default="live")
    created_at: Mapped[datetime] = _created_at()


class ViewerSnapshot(Base):
    """Writer: controller (every poll)."""

    __tablename__ = "viewer_snapshots"
    __table_args__ = (PrimaryKeyConstraint("stream_id", "ts"),)

    stream_id: Mapped[int] = mapped_column(ForeignKey("streams.id"))
    ts: Mapped[datetime]
    viewers: Mapped[int] = mapped_column(Integer)


# --------------------------------------------------------------------------- media


class Chunk(Base):
    """~60 s of recorded media. Storage unit only; everything else uses UTC times.

    Writer: recorder (and backfill tool).
    """

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("stream_id", "seq"),
        Index(None, "stream_id", "start_utc"),
        CheckConstraint("end_utc > start_utc", name="positive_duration"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    stream_id: Mapped[int] = mapped_column(ForeignKey("streams.id"))
    seq: Mapped[int] = mapped_column(Integer)
    start_utc: Mapped[datetime]
    end_utc: Mapped[datetime]
    video_key: Mapped[str | None] = mapped_column(Text)  # null in audio_only mode
    audio_key: Mapped[str | None] = mapped_column(Text)
    video_bytes: Mapped[int | None] = mapped_column(BigInteger)
    audio_bytes: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(Text, server_default="live")
    created_at: Mapped[datetime] = _created_at()


class StreamGap(Base):
    """Time ranges with no media (ads, reconnects). Writer: recorder."""

    __tablename__ = "stream_gaps"
    __table_args__ = (Index(None, "stream_id", "start_utc"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    stream_id: Mapped[int] = mapped_column(ForeignKey("streams.id"))
    start_utc: Mapped[datetime]
    end_utc: Mapped[datetime]
    reason: Mapped[str] = mapped_column(Text, server_default="unknown")
    created_at: Mapped[datetime] = _created_at()


# --------------------------------------------------------------------------- chat


class ChatMessage(Base):
    """Raw chat. Partitioned by day on sent_at (see clipper_core.partitions).

    No foreign keys on purpose: this is the highest-volume table. Writer: chat-ingest, backfill.
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        PrimaryKeyConstraint("sent_at", "msg_id"),
        Index(None, "channel_id", "sent_at"),
        Index(None, "stream_id", "sent_at"),
        {"postgresql_partition_by": "RANGE (sent_at)"},
    )

    sent_at: Mapped[datetime]  # tmi-sent-ts (server time), not our receive time
    msg_id: Mapped[str] = mapped_column(Text)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    stream_id: Mapped[int | None] = mapped_column(BigInteger)
    received_at: Mapped[datetime | None]  # null for backfill
    user_id: Mapped[str | None] = mapped_column(Text)
    user_login: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    emotes: Mapped[list[Any] | None]  # [{"id": "...", "name": "KEKW", "count": 2}]
    badges: Mapped[dict[str, Any] | None]  # {"subscriber": "12", "moderator": "1"}
    deleted: Mapped[bool] = mapped_column(server_default=sql_text("false"))
    source: Mapped[str] = mapped_column(Text, server_default="live")


class ChatEvent(Base):
    """Subs, gifts, raids, bits, clears... Needed by the fake-spike filter. Writer: chat-ingest."""

    __tablename__ = "chat_events"
    __table_args__ = (Index(None, "channel_id", "sent_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    stream_id: Mapped[int | None] = mapped_column(ForeignKey("streams.id"))
    sent_at: Mapped[datetime]
    type: Mapped[str] = mapped_column(Text)  # sub, resub, subgift, raid, bits, clearchat, ...
    msg_id: Mapped[str | None] = mapped_column(Text)
    user_login: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(server_default=sql_text("'{}'::jsonb"))


class ChatMetricSecond(Base):
    """Per-second chat stats; the live detector reads these. Writer: chat-ingest."""

    __tablename__ = "chat_metrics_1s"
    __table_args__ = (PrimaryKeyConstraint("channel_id", "ts"), Index(None, "stream_id", "ts"))

    channel_id: Mapped[int] = mapped_column(BigInteger)
    ts: Mapped[datetime]  # start of the second
    stream_id: Mapped[int | None] = mapped_column(BigInteger)
    messages: Mapped[int] = mapped_column(Integer)
    unique_chatters: Mapped[int] = mapped_column(Integer)
    emote_counts: Mapped[dict[str, Any]] = mapped_column(server_default=sql_text("'{}'::jsonb"))
    event_counts: Mapped[dict[str, Any]] = mapped_column(server_default=sql_text("'{}'::jsonb"))


# --------------------------------------------------------------------------- transcripts


class ChunkTranscript(Base):
    """Marks a chunk as transcribed; words live in object storage. Writer: transcriber."""

    __tablename__ = "chunk_transcripts"

    chunk_id: Mapped[int] = mapped_column(ForeignKey("chunks.id"), primary_key=True)
    model: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    words_key: Mapped[str] = mapped_column(Text)
    segment_count: Mapped[int] = mapped_column(Integer)
    processing_s: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = _created_at()


class TranscriptSegment(Base):
    """Sentence-level text with full-text search. Writer: transcriber."""

    __tablename__ = "transcript_segments"
    __table_args__ = (
        Index(None, "stream_id", "start_utc"),
        Index("ix_transcript_segments_text_tsv", "text_tsv", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    stream_id: Mapped[int] = mapped_column(ForeignKey("streams.id"))
    chunk_id: Mapped[int] = mapped_column(ForeignKey("chunks.id"))
    start_utc: Mapped[datetime]
    end_utc: Mapped[datetime]
    text: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    avg_logprob: Mapped[float | None] = mapped_column(Float)
    no_speech_prob: Mapped[float | None] = mapped_column(Float)
    model: Mapped[str] = mapped_column(Text)
    text_tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', text)", persisted=True)
    )
    created_at: Mapped[datetime] = _created_at()


# --------------------------------------------------------------------------- coordination


class Job(Base):
    """Work queue. Workers claim with FOR UPDATE SKIP LOCKED, highest priority first.

    Writers: whoever enqueues (insert) and the worker that claimed it (status updates).
    """

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'done', 'failed')", name="status"),
        Index(
            "ix_jobs_pick",
            "kind",
            sql_text("priority DESC"),
            "id",
            postgresql_where=sql_text("status = 'queued'"),
        ),
        Index(None, "kind", "ref_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    ref_id: Mapped[int | None] = mapped_column(BigInteger)  # e.g. chunk id
    dedupe_key: Mapped[str | None] = mapped_column(Text, unique=True)
    priority: Mapped[int] = mapped_column(Integer, server_default="0")
    status: Mapped[str] = mapped_column(Text, server_default="queued")
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=sql_text("'{}'::jsonb"))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="5")
    run_after: Mapped[datetime] = mapped_column(server_default=func.now())
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None]


class MediaRequest(Base):
    """A request: "I need transcripts for this time range at this priority."

    Raises the priority of matching queued jobs, and of chunks recorded later inside the range.
    Writers: detector, judge (anyone who needs a window).
    """

    __tablename__ = "media_requests"
    __table_args__ = (
        Index(
            "ix_media_requests_open",
            "stream_id",
            "from_utc",
            "to_utc",
            postgresql_where=sql_text("fulfilled_at IS NULL"),
        ),
        CheckConstraint("to_utc > from_utc", name="positive_duration"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    stream_id: Mapped[int] = mapped_column(ForeignKey("streams.id"))
    from_utc: Mapped[datetime]
    to_utc: Mapped[datetime]
    priority: Mapped[int] = mapped_column(Integer)
    requested_by: Mapped[str] = mapped_column(Text)
    clip_id: Mapped[int | None] = mapped_column(BigInteger)  # FK added when clips table exists
    created_at: Mapped[datetime] = _created_at()
    fulfilled_at: Mapped[datetime | None]


class Lease(Base):
    """Exclusive ownership with expiry, e.g. 'record:channel:42'. Writer: the lease holder."""

    __tablename__ = "leases"

    resource: Mapped[str] = mapped_column(Text, primary_key=True)
    owner: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime]
    acquired_at: Mapped[datetime] = _created_at()


class WorkerHeartbeat(Base):
    """Liveness of every running worker. Writer: each worker for its own row."""

    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(Text, primary_key=True)
    service: Mapped[str] = mapped_column(Text)
    host: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime]
    last_seen: Mapped[datetime]
    info: Mapped[dict[str, Any]] = mapped_column(server_default=sql_text("'{}'::jsonb"))
