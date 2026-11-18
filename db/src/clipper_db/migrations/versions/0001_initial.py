"""initial schema: channels, streams, media, chat, transcripts, jobs, coordination

Revision ID: 0001
Revises:
Create Date: 2026-11-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def _created() -> sa.Column:
    return sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "channels",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("platform", sa.Text, server_default="twitch", nullable=False),
        sa.Column("login", sa.Text, nullable=False),
        sa.Column("platform_user_id", sa.Text),
        sa.Column("display_name", sa.Text),
        sa.Column("recording_mode", sa.Text, server_default="full", nullable=False),
        sa.Column("active", sa.Boolean, server_default=sa.text("true"), nullable=False),
        sa.Column("chat_delay_s", sa.Float),
        _created(),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channels")),
        sa.UniqueConstraint("platform", "login", name=op.f("uq_channels_platform_login")),
        sa.UniqueConstraint(
            "platform", "platform_user_id", name=op.f("uq_channels_platform_platform_user_id")
        ),
        sa.CheckConstraint(
            "recording_mode IN ('full', 'audio_only', 'off')",
            name=op.f("ck_channels_recording_mode"),
        ),
    )

    op.create_table(
        "streams",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("channel_id", sa.BigInteger, nullable=False),
        sa.Column("platform_stream_id", sa.Text, nullable=False),
        sa.Column("title", sa.Text),
        sa.Column("category", sa.Text),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("ended_at", TS),
        sa.Column("status", sa.Text, server_default="live", nullable=False),
        sa.Column("source", sa.Text, server_default="live", nullable=False),
        _created(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_streams")),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["channels.id"], name=op.f("fk_streams_channel_id_channels")
        ),
        sa.UniqueConstraint(
            "channel_id",
            "platform_stream_id",
            name=op.f("uq_streams_channel_id_platform_stream_id"),
        ),
        sa.CheckConstraint("status IN ('live', 'ended')", name=op.f("ck_streams_status")),
    )
    op.create_index(
        op.f("ix_streams_channel_id_started_at"), "streams", ["channel_id", "started_at"]
    )

    op.create_table(
        "viewer_snapshots",
        sa.Column("stream_id", sa.BigInteger, nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("viewers", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("stream_id", "ts", name=op.f("pk_viewer_snapshots")),
        sa.ForeignKeyConstraint(
            ["stream_id"], ["streams.id"], name=op.f("fk_viewer_snapshots_stream_id_streams")
        ),
    )

    op.create_table(
        "chunks",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("stream_id", sa.BigInteger, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("start_utc", TS, nullable=False),
        sa.Column("end_utc", TS, nullable=False),
        sa.Column("video_key", sa.Text),
        sa.Column("audio_key", sa.Text),
        sa.Column("video_bytes", sa.BigInteger),
        sa.Column("audio_bytes", sa.BigInteger),
        sa.Column("source", sa.Text, server_default="live", nullable=False),
        _created(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunks")),
        sa.ForeignKeyConstraint(
            ["stream_id"], ["streams.id"], name=op.f("fk_chunks_stream_id_streams")
        ),
        sa.UniqueConstraint("stream_id", "seq", name=op.f("uq_chunks_stream_id_seq")),
        sa.CheckConstraint("end_utc > start_utc", name=op.f("ck_chunks_positive_duration")),
    )
    op.create_index(op.f("ix_chunks_stream_id_start_utc"), "chunks", ["stream_id", "start_utc"])

    op.create_table(
        "stream_gaps",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("stream_id", sa.BigInteger, nullable=False),
        sa.Column("start_utc", TS, nullable=False),
        sa.Column("end_utc", TS, nullable=False),
        sa.Column("reason", sa.Text, server_default="unknown", nullable=False),
        _created(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stream_gaps")),
        sa.ForeignKeyConstraint(
            ["stream_id"], ["streams.id"], name=op.f("fk_stream_gaps_stream_id_streams")
        ),
    )
    op.create_index(
        op.f("ix_stream_gaps_stream_id_start_utc"), "stream_gaps", ["stream_id", "start_utc"]
    )

    # Partitioned by day; partitions are created at runtime (clipper_core.partitions).
    op.create_table(
        "chat_messages",
        sa.Column("sent_at", TS, nullable=False),
        sa.Column("msg_id", sa.Text, nullable=False),
        sa.Column("channel_id", sa.BigInteger, nullable=False),
        sa.Column("stream_id", sa.BigInteger),
        sa.Column("received_at", TS),
        sa.Column("user_id", sa.Text),
        sa.Column("user_login", sa.Text),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("emotes", pg.JSONB),
        sa.Column("badges", pg.JSONB),
        sa.Column("deleted", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("source", sa.Text, server_default="live", nullable=False),
        sa.PrimaryKeyConstraint("sent_at", "msg_id", name=op.f("pk_chat_messages")),
        postgresql_partition_by="RANGE (sent_at)",
    )
    op.create_index(
        op.f("ix_chat_messages_channel_id_sent_at"), "chat_messages", ["channel_id", "sent_at"]
    )
    op.create_index(
        op.f("ix_chat_messages_stream_id_sent_at"), "chat_messages", ["stream_id", "sent_at"]
    )

    op.create_table(
        "chat_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("channel_id", sa.BigInteger, nullable=False),
        sa.Column("stream_id", sa.BigInteger),
        sa.Column("sent_at", TS, nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("msg_id", sa.Text),
        sa.Column("user_login", sa.Text),
        sa.Column("data", pg.JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_events")),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["channels.id"], name=op.f("fk_chat_events_channel_id_channels")
        ),
        sa.ForeignKeyConstraint(
            ["stream_id"], ["streams.id"], name=op.f("fk_chat_events_stream_id_streams")
        ),
    )
    op.create_index(
        op.f("ix_chat_events_channel_id_sent_at"), "chat_events", ["channel_id", "sent_at"]
    )

    op.create_table(
        "chat_metrics_1s",
        sa.Column("channel_id", sa.BigInteger, nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("stream_id", sa.BigInteger),
        sa.Column("messages", sa.Integer, nullable=False),
        sa.Column("unique_chatters", sa.Integer, nullable=False),
        sa.Column("emote_counts", pg.JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("event_counts", pg.JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.PrimaryKeyConstraint("channel_id", "ts", name=op.f("pk_chat_metrics_1s")),
    )
    op.create_index(op.f("ix_chat_metrics_1s_stream_id_ts"), "chat_metrics_1s", ["stream_id", "ts"])

    op.create_table(
        "chunk_transcripts",
        sa.Column("chunk_id", sa.BigInteger, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("language", sa.Text),
        sa.Column("words_key", sa.Text, nullable=False),
        sa.Column("segment_count", sa.Integer, nullable=False),
        sa.Column("processing_s", sa.Float),
        _created(),
        sa.PrimaryKeyConstraint("chunk_id", name=op.f("pk_chunk_transcripts")),
        sa.ForeignKeyConstraint(
            ["chunk_id"], ["chunks.id"], name=op.f("fk_chunk_transcripts_chunk_id_chunks")
        ),
    )

    op.create_table(
        "transcript_segments",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("stream_id", sa.BigInteger, nullable=False),
        sa.Column("chunk_id", sa.BigInteger, nullable=False),
        sa.Column("start_utc", TS, nullable=False),
        sa.Column("end_utc", TS, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("language", sa.Text),
        sa.Column("avg_logprob", sa.Float),
        sa.Column("no_speech_prob", sa.Float),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column(
            "text_tsv",
            pg.TSVECTOR,
            sa.Computed("to_tsvector('simple', text)", persisted=True),
            nullable=False,
        ),
        _created(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transcript_segments")),
        sa.ForeignKeyConstraint(
            ["stream_id"], ["streams.id"], name=op.f("fk_transcript_segments_stream_id_streams")
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"], ["chunks.id"], name=op.f("fk_transcript_segments_chunk_id_chunks")
        ),
    )
    op.create_index(
        op.f("ix_transcript_segments_stream_id_start_utc"),
        "transcript_segments",
        ["stream_id", "start_utc"],
    )
    op.create_index(
        "ix_transcript_segments_text_tsv",
        "transcript_segments",
        ["text_tsv"],
        postgresql_using="gin",
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("ref_id", sa.BigInteger),
        sa.Column("dedupe_key", sa.Text),
        sa.Column("priority", sa.Integer, server_default="0", nullable=False),
        sa.Column("status", sa.Text, server_default="queued", nullable=False),
        sa.Column("payload", pg.JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("attempts", sa.Integer, server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer, server_default="5", nullable=False),
        sa.Column("run_after", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("locked_by", sa.Text),
        sa.Column("locked_at", TS),
        sa.Column("last_error", sa.Text),
        _created(),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", TS),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_jobs_dedupe_key")),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed')", name=op.f("ck_jobs_status")
        ),
    )
    op.create_index(
        "ix_jobs_pick",
        "jobs",
        ["kind", sa.text("priority DESC"), "id"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(op.f("ix_jobs_kind_ref_id"), "jobs", ["kind", "ref_id"])

    op.create_table(
        "media_requests",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("stream_id", sa.BigInteger, nullable=False),
        sa.Column("from_utc", TS, nullable=False),
        sa.Column("to_utc", TS, nullable=False),
        sa.Column("priority", sa.Integer, nullable=False),
        sa.Column("requested_by", sa.Text, nullable=False),
        sa.Column("clip_id", sa.BigInteger),
        _created(),
        sa.Column("fulfilled_at", TS),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_requests")),
        sa.ForeignKeyConstraint(
            ["stream_id"], ["streams.id"], name=op.f("fk_media_requests_stream_id_streams")
        ),
        sa.CheckConstraint("to_utc > from_utc", name=op.f("ck_media_requests_positive_duration")),
    )
    op.create_index(
        "ix_media_requests_open",
        "media_requests",
        ["stream_id", "from_utc", "to_utc"],
        postgresql_where=sa.text("fulfilled_at IS NULL"),
    )

    op.create_table(
        "leases",
        sa.Column("resource", sa.Text, nullable=False),
        sa.Column("owner", sa.Text, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("acquired_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("resource", name=op.f("pk_leases")),
    )

    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.Text, nullable=False),
        sa.Column("service", sa.Text, nullable=False),
        sa.Column("host", sa.Text, nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("last_seen", TS, nullable=False),
        sa.Column("info", pg.JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.PrimaryKeyConstraint("worker_id", name=op.f("pk_worker_heartbeats")),
    )


def downgrade() -> None:
    for table in (
        "worker_heartbeats",
        "leases",
        "media_requests",
        "jobs",
        "transcript_segments",
        "chunk_transcripts",
        "chat_metrics_1s",
        "chat_events",
        "chat_messages",  # drops its partitions too
        "stream_gaps",
        "chunks",
        "viewer_snapshots",
        "streams",
        "channels",
    ):
        op.drop_table(table)
