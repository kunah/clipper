"""transcriber contract: owned ranges + provisional tails, segment scores, audio_metrics_1s

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-19

`chunk_transcripts.owned_from/owned_to` are NOT NULL without a default: the table is empty
everywhere because no transcriber has run before this revision.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)

# CHECK texts are repeated in clipper_core.models; db/tests/test_migrations.py compares them.
OWNED_RANGE = (
    "owned_to >= owned_from"
    " AND (tail_from IS NULL OR (tail_from >= owned_from AND tail_from < owned_to))"
)


def upgrade() -> None:
    op.add_column("chunk_transcripts", sa.Column("owned_from", TS, nullable=False))
    op.add_column("chunk_transcripts", sa.Column("owned_to", TS, nullable=False))
    op.add_column("chunk_transcripts", sa.Column("tail_from", TS))
    op.create_check_constraint(
        op.f("ck_chunk_transcripts_owned_range"), "chunk_transcripts", OWNED_RANGE
    )

    op.add_column(
        "transcript_segments",
        sa.Column("provisional", sa.Boolean, server_default=sa.text("false"), nullable=False),
    )
    op.add_column("transcript_segments", sa.Column("confidence", sa.Float))
    # The transcriber replaces a chunk's segments (delete by chunk_id) on every job.
    op.create_index(op.f("ix_transcript_segments_chunk_id"), "transcript_segments", ["chunk_id"])

    op.create_table(
        "audio_metrics_1s",
        sa.Column("stream_id", sa.BigInteger, nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("loudness_db", sa.Float, nullable=False),
        sa.Column("speech_ratio", sa.Float, nullable=False),
        sa.Column("classes", pg.JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.PrimaryKeyConstraint("stream_id", "ts", name=op.f("pk_audio_metrics_1s")),
        sa.ForeignKeyConstraint(
            ["stream_id"], ["streams.id"], name=op.f("fk_audio_metrics_1s_stream_id_streams")
        ),
        sa.CheckConstraint(
            "speech_ratio BETWEEN 0 AND 1 AND loudness_db <= 0"
            " AND loudness_db > '-Infinity'::float8",
            name=op.f("ck_audio_metrics_1s_ranges"),
        ),
    )


def downgrade() -> None:
    op.drop_table("audio_metrics_1s")
    op.drop_index(op.f("ix_transcript_segments_chunk_id"), table_name="transcript_segments")
    op.drop_column("transcript_segments", "confidence")
    op.drop_column("transcript_segments", "provisional")
    op.drop_constraint(op.f("ck_chunk_transcripts_owned_range"), "chunk_transcripts", type_="check")
    for column in ("tail_from", "owned_to", "owned_from"):
        op.drop_column("chunk_transcripts", column)
