"""renderer contract: renders (one row per render job)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-25

Additive (DESIGN §20): one new table, nothing else changes. Its writer is the renderer;
`clipper_core.lifecycle` starts reading it (the clip's latest render) as soon as it exists.

Apply it in its own run, not in the same run as 0006 (see 0006's docstring: Alembic runs all
pending migrations in one transaction). The live database has 0006 already (2026-11-25).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)

# CHECK texts are repeated in clipper_core.models; db/tests/test_migrations.py compares them.
RENDER_STATUS = "status IN ('rendering', 'done', 'failed', 'unrenderable')"
RENDER_UNRENDERABLE_REASON = "(status = 'unrenderable') = (unrenderable_reason IS NOT NULL)"
RENDER_DONE_OUTPUTS = (
    "status <> 'done' OR (video_key IS NOT NULL AND video_sha256 IS NOT NULL"
    " AND finished_at IS NOT NULL)"
)


def _jsonb(name: str, default: str) -> sa.Column:
    return sa.Column(name, pg.JSONB, server_default=sa.text(f"'{default}'::jsonb"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "renders",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("job_id", sa.BigInteger, nullable=False),
        sa.Column("clip_id", sa.BigInteger, nullable=False),
        sa.Column("plan_id", sa.BigInteger, nullable=False),
        sa.Column("profile", sa.Text, nullable=False),
        sa.Column("status", sa.Text, server_default="rendering", nullable=False),
        sa.Column("unrenderable_reason", sa.Text),
        sa.Column("layout", sa.Text),
        sa.Column("encoder", sa.Text),
        sa.Column("renderer_version", sa.Text),
        sa.Column("ffmpeg_version", sa.Text),
        sa.Column("graph_hash", sa.Text),
        _jsonb("pinned_keys", "[]"),
        sa.Column("video_key", sa.Text),
        sa.Column("preview_key", sa.Text),
        sa.Column("thumb_key", sa.Text),
        sa.Column("captions_key", sa.Text),
        sa.Column("manifest_key", sa.Text),
        sa.Column("video_sha256", sa.Text),
        sa.Column("duration_s", sa.Float),
        sa.Column("fps", sa.Float),
        sa.Column("loudness_lufs", sa.Float),
        sa.Column("true_peak_db", sa.Float),
        sa.Column("render_s", sa.Float),
        sa.Column("width", sa.Integer),
        sa.Column("height", sa.Integer),
        sa.Column("bytes", sa.BigInteger),
        sa.Column("preview_bytes", sa.BigInteger),
        _jsonb("warnings", "[]"),
        sa.Column("error", sa.Text),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", TS),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_renders")),
        sa.ForeignKeyConstraint(["clip_id"], ["clips.id"], name=op.f("fk_renders_clip_id_clips")),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["clip_plans.id"], name=op.f("fk_renders_plan_id_clip_plans")
        ),
        sa.UniqueConstraint("job_id", name=op.f("uq_renders_job_id")),
        sa.CheckConstraint(RENDER_STATUS, name=op.f("ck_renders_status")),
        sa.CheckConstraint(RENDER_UNRENDERABLE_REASON, name=op.f("ck_renders_unrenderable_reason")),
        sa.CheckConstraint(RENDER_DONE_OUTPUTS, name=op.f("ck_renders_done_outputs")),
    )
    op.create_index(op.f("ix_renders_clip_id_created_at"), "renders", ["clip_id", "created_at"])
    op.create_index(op.f("ix_renders_plan_id"), "renders", ["plan_id"])


def downgrade() -> None:
    op.drop_table("renders")
