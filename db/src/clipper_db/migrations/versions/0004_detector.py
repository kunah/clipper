"""detector contract: candidates (detector output), chat_delay_estimates (learned chat delay)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-20

Additive: two new tables, nothing else changes (DESIGN §18). `candidates.media_request_id`
links the detector's own `media_requests` rows; `media_requests.clip_id` stays for the judge
(its FK comes with 0005). No index on `media_request_id` yet: the first migration that deletes
`media_requests` rows adds one. `channels.chat_delay_s` keeps its column and becomes the
operator's manual override; learned delays go to `chat_delay_estimates` (one writer per table).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)

# CHECK texts are repeated in clipper_core.models; db/tests/test_migrations.py compares them.
CANDIDATE_SOURCE = "source IN ('live', 'offline')"
CANDIDATE_STATUS = "status IN ('emitted', 'suppressed')"
CANDIDATE_SUPPRESS_REASON = "status <> 'suppressed' OR suppress_reason IS NOT NULL"
CANDIDATE_MOMENT_TYPE = (
    "moment_type IN ('funny', 'hype', 'shock', 'tense', 'sad', 'cringe', 'wholesome',"
    " 'mixed', 'unknown')"
)
CANDIDATE_MOMENT_RANGE = "moment_to >= moment_from"
CANDIDATE_WINDOW_RANGE = "window_to > window_from"
CANDIDATE_NESTING = (
    "window_from <= moment_from AND moment_to <= window_to"
    " AND peak_utc BETWEEN window_from AND window_to"
)
ESTIMATE_EVENTS = "events >= 0"
ESTIMATE_VALID_HAS_DELAY = "NOT valid OR (delay_s IS NOT NULL AND strength IS NOT NULL)"


def _now(name: str) -> sa.Column:
    return sa.Column(name, TS, server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "candidates",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("stream_id", sa.BigInteger, nullable=False),
        sa.Column("channel_id", sa.BigInteger, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("suppress_reason", sa.Text),
        sa.Column("trigger", sa.Text, nullable=False),
        sa.Column("peak_utc", TS, nullable=False),
        sa.Column("chat_peak_utc", TS),
        sa.Column("chat_delay_s", sa.Float, nullable=False),
        sa.Column("delay_source", sa.Text, nullable=False),
        sa.Column("moment_from", TS, nullable=False),
        sa.Column("moment_to", TS, nullable=False),
        sa.Column("window_from", TS, nullable=False),
        sa.Column("window_to", TS, nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("moment_type", sa.Text, server_default="unknown", nullable=False),
        sa.Column("features", pg.JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("detector_version", sa.Text, nullable=False),
        sa.Column("media_request_id", sa.BigInteger),
        _now("created_at"),
        _now("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidates")),
        sa.ForeignKeyConstraint(
            ["stream_id"], ["streams.id"], name=op.f("fk_candidates_stream_id_streams")
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["channels.id"], name=op.f("fk_candidates_channel_id_channels")
        ),
        sa.ForeignKeyConstraint(
            ["media_request_id"],
            ["media_requests.id"],
            name=op.f("fk_candidates_media_request_id_media_requests"),
        ),
        # also serves lookups by (stream_id, peak_utc)
        sa.UniqueConstraint(
            "stream_id", "peak_utc", "source", name=op.f("uq_candidates_stream_id_peak_utc_source")
        ),
        sa.CheckConstraint(CANDIDATE_SOURCE, name=op.f("ck_candidates_source")),
        sa.CheckConstraint(CANDIDATE_STATUS, name=op.f("ck_candidates_status")),
        sa.CheckConstraint(CANDIDATE_SUPPRESS_REASON, name=op.f("ck_candidates_suppress_reason")),
        sa.CheckConstraint(CANDIDATE_MOMENT_TYPE, name=op.f("ck_candidates_moment_type")),
        sa.CheckConstraint(CANDIDATE_MOMENT_RANGE, name=op.f("ck_candidates_moment_range")),
        sa.CheckConstraint(CANDIDATE_WINDOW_RANGE, name=op.f("ck_candidates_window_range")),
        sa.CheckConstraint(CANDIDATE_NESTING, name=op.f("ck_candidates_nesting")),
    )
    op.create_index(
        op.f("ix_candidates_channel_id_created_at"), "candidates", ["channel_id", "created_at"]
    )

    op.create_table(
        "chat_delay_estimates",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("channel_id", sa.BigInteger, nullable=False),
        sa.Column("stream_id", sa.BigInteger, nullable=False),
        sa.Column("method", sa.Text, nullable=False),
        sa.Column("delay_s", sa.Float),
        sa.Column("events", sa.Integer, nullable=False),
        sa.Column("strength", sa.Float),
        sa.Column("valid", sa.Boolean, nullable=False),
        sa.Column("detector_version", sa.Text, nullable=False),
        _now("computed_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_delay_estimates")),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["channels.id"],
            name=op.f("fk_chat_delay_estimates_channel_id_channels"),
        ),
        sa.ForeignKeyConstraint(
            ["stream_id"], ["streams.id"], name=op.f("fk_chat_delay_estimates_stream_id_streams")
        ),
        sa.UniqueConstraint(
            "stream_id", "method", name=op.f("uq_chat_delay_estimates_stream_id_method")
        ),
        sa.CheckConstraint(ESTIMATE_EVENTS, name=op.f("ck_chat_delay_estimates_events")),
        sa.CheckConstraint(
            ESTIMATE_VALID_HAS_DELAY, name=op.f("ck_chat_delay_estimates_valid_has_delay")
        ),
    )
    op.create_index(
        op.f("ix_chat_delay_estimates_channel_id_computed_at"),
        "chat_delay_estimates",
        ["channel_id", "computed_at"],
    )


def downgrade() -> None:
    op.drop_table("chat_delay_estimates")
    op.drop_table("candidates")
