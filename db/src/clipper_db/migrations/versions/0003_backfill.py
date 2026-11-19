"""backfill contract: vods (archives + time anchor), viewer_clips (ground truth), streams CHECKs

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-19

Additive: two new tables and two CHECKs on `streams`:
- `source IN ('live', 'backfill')`;
- `source = 'live' OR status = 'ended'`: a backfilled stream is never `live`. The recorder and
  chat-ingest pick up every `status = 'live'` row (no source filter) and the controller never
  ends a backfilled one, so a backfilled row left `live` would be recorded into forever. The
  backfill tool creates its streams as `ended` from the start. Every existing row is
  `source = 'live'` (the default; nothing else wrote streams before), so both hold already.
`stream_gaps.reason` has no CHECK, so the new gap reason `muted` needs no DDL; `streams`, `chunks`
and `chat_messages` already have `source`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)

# CHECK texts are repeated in clipper_core.models; db/tests/test_migrations.py compares them.
VOD_MEDIA_STATUS = "media_status IN ('pending', 'done', 'restricted', 'gone', 'off')"
VOD_CHAT_STATUS = "chat_status IN ('pending', 'done', 'skipped', 'unavailable', 'off')"
STREAM_SOURCE = "source IN ('live', 'backfill')"
STREAM_BACKFILL_ENDED = "source = 'live' OR status = 'ended'"


def _now(name: str) -> sa.Column:
    return sa.Column(name, TS, server_default=sa.func.now(), nullable=False)


def _jsonb(name: str) -> sa.Column:
    return sa.Column(name, pg.JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False)


def upgrade() -> None:
    op.create_check_constraint(op.f("ck_streams_source"), "streams", STREAM_SOURCE)
    op.create_check_constraint(op.f("ck_streams_backfill_ended"), "streams", STREAM_BACKFILL_ENDED)

    op.create_table(
        "vods",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("channel_id", sa.BigInteger, nullable=False),
        sa.Column("stream_id", sa.BigInteger, nullable=False),
        sa.Column("platform_video_id", sa.Text, nullable=False),
        sa.Column("title", sa.Text),
        sa.Column("platform_created_at", TS, nullable=False),
        sa.Column("duration_s", sa.Float, nullable=False),
        sa.Column("t0_utc", TS, nullable=False),
        sa.Column("t0_method", sa.Text, nullable=False),
        _jsonb("t0_candidates"),
        sa.Column("rendition", sa.Text),
        sa.Column("media_status", sa.Text, server_default="pending", nullable=False),
        sa.Column("media_done_s", sa.Float, server_default="0", nullable=False),
        sa.Column("chat_status", sa.Text, server_default="pending", nullable=False),
        sa.Column("chat_done_s", sa.Integer, server_default="0", nullable=False),
        sa.Column("chat_metrics_to", TS),
        _jsonb("flags"),
        _now("created_at"),
        _now("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vods")),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["channels.id"], name=op.f("fk_vods_channel_id_channels")
        ),
        sa.ForeignKeyConstraint(
            ["stream_id"], ["streams.id"], name=op.f("fk_vods_stream_id_streams")
        ),
        sa.UniqueConstraint(
            "channel_id", "platform_video_id", name=op.f("uq_vods_channel_id_platform_video_id")
        ),
        sa.UniqueConstraint("stream_id", name=op.f("uq_vods_stream_id")),
        sa.CheckConstraint(VOD_MEDIA_STATUS, name=op.f("ck_vods_media_status")),
        sa.CheckConstraint(VOD_CHAT_STATUS, name=op.f("ck_vods_chat_status")),
    )

    op.create_table(
        "viewer_clips",
        sa.Column("platform", sa.Text, server_default="twitch", nullable=False),
        sa.Column("clip_id", sa.Text, nullable=False),
        sa.Column("channel_id", sa.BigInteger, nullable=False),
        sa.Column("stream_id", sa.BigInteger),
        sa.Column("platform_video_id", sa.Text),
        sa.Column("vod_offset_s", sa.Integer),
        sa.Column("duration_s", sa.Float, nullable=False),
        sa.Column("start_utc", TS),
        sa.Column("end_utc", TS),
        sa.Column("slack_s", sa.Float),
        sa.Column("time_source", sa.Text),
        sa.Column("title", sa.Text),
        sa.Column("creator_login", sa.Text),
        sa.Column("view_count", sa.Integer, nullable=False),
        sa.Column("is_featured", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("platform_created_at", TS, nullable=False),
        _now("fetched_at"),
        sa.PrimaryKeyConstraint("platform", "clip_id", name=op.f("pk_viewer_clips")),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["channels.id"], name=op.f("fk_viewer_clips_channel_id_channels")
        ),
        sa.ForeignKeyConstraint(
            ["stream_id"], ["streams.id"], name=op.f("fk_viewer_clips_stream_id_streams")
        ),
    )
    op.create_index(
        op.f("ix_viewer_clips_stream_id_start_utc"), "viewer_clips", ["stream_id", "start_utc"]
    )
    op.create_index(
        op.f("ix_viewer_clips_channel_id_platform_created_at"),
        "viewer_clips",
        ["channel_id", "platform_created_at"],
    )


def downgrade() -> None:
    op.drop_table("viewer_clips")
    op.drop_table("vods")
    op.drop_constraint(op.f("ck_streams_backfill_ended"), "streams", type_="check")
    op.drop_constraint(op.f("ck_streams_source"), "streams", type_="check")
