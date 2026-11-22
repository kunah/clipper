"""judge contract: clips, clip_plans (one row per round/revision), judge_calls (every model call)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-20

Additive (DESIGN §19): three new tables and one FK on an existing nullable column
(`media_requests.clip_id`, NULL in every row so far: no writer set it before the judge).
`clip_plans.review_event_id` gets its FK with review's migration (0008, §21).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)

# CHECK texts are repeated in clipper_core.models; db/tests/test_migrations.py compares them.
CLIP_STATUS = "status IN ('waiting_for_media', 'judging', 'rejected', 'planned', 'failed')"
CLIP_REJECT_REASON = "(status = 'rejected') = (reject_reason IS NOT NULL)"
CLIP_ROUND = "round >= 0"
CLIP_CONTEXT_RANGE = "context_to > context_from"
CLIP_ANCHOR_IN_CONTEXT = "anchor_utc BETWEEN context_from AND context_to"
PLAN_VERDICT = "verdict IN ('clip', 'reject', 'need_context')"
PLAN_PLAN = "(verdict = 'clip') = (plan IS NOT NULL)"
PLAN_CONTEXT_REQUEST = "(verdict = 'need_context') = (context_request IS NOT NULL)"
PLAN_AUTHOR = "author IN ('judge', 'operator')"


def _now(name: str) -> sa.Column:
    return sa.Column(name, TS, server_default=sa.func.now(), nullable=False)


def _jsonb(name: str, default: str) -> sa.Column:
    return sa.Column(name, pg.JSONB, server_default=sa.text(f"'{default}'::jsonb"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "clips",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("candidate_id", sa.BigInteger, nullable=False),
        sa.Column("stream_id", sa.BigInteger, nullable=False),
        sa.Column("channel_id", sa.BigInteger, nullable=False),
        sa.Column("status", sa.Text, server_default="waiting_for_media", nullable=False),
        sa.Column("reject_reason", sa.Text),
        sa.Column("round", sa.Integer, server_default="0", nullable=False),
        sa.Column("anchor_utc", TS, nullable=False),
        sa.Column("context_from", TS, nullable=False),
        sa.Column("context_to", TS, nullable=False),
        sa.Column("priority", sa.Integer, nullable=False),
        sa.Column("waiting_since", TS, nullable=False),
        sa.Column("plan_version", sa.Integer),
        _jsonb("flags", "{}"),
        sa.Column("error", sa.Text),
        sa.Column("judged_at", TS),
        _now("created_at"),
        _now("updated_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clips")),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["candidates.id"], name=op.f("fk_clips_candidate_id_candidates")
        ),
        sa.ForeignKeyConstraint(
            ["stream_id"], ["streams.id"], name=op.f("fk_clips_stream_id_streams")
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["channels.id"], name=op.f("fk_clips_channel_id_channels")
        ),
        sa.UniqueConstraint("candidate_id", name=op.f("uq_clips_candidate_id")),
        sa.CheckConstraint(CLIP_STATUS, name=op.f("ck_clips_status")),
        sa.CheckConstraint(CLIP_REJECT_REASON, name=op.f("ck_clips_reject_reason")),
        sa.CheckConstraint(CLIP_ROUND, name=op.f("ck_clips_round")),
        sa.CheckConstraint(CLIP_CONTEXT_RANGE, name=op.f("ck_clips_context_range")),
        sa.CheckConstraint(CLIP_ANCHOR_IN_CONTEXT, name=op.f("ck_clips_anchor_in_context")),
    )
    op.create_index(op.f("ix_clips_stream_id_anchor_utc"), "clips", ["stream_id", "anchor_utc"])
    # the waiter's sweep: waiting clips per stream
    op.create_index(
        "ix_clips_waiting",
        "clips",
        ["stream_id"],
        postgresql_where=sa.text("status = 'waiting_for_media'"),
    )

    op.create_table(
        "judge_calls",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("clip_id", sa.BigInteger),
        sa.Column("stream_id", sa.BigInteger, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("prompt_version", sa.Text, nullable=False),
        sa.Column("request_hash", sa.Text, nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False),
        sa.Column("cached_input_tokens", sa.Integer, nullable=False),
        sa.Column("output_tokens", sa.Integer, nullable=False),
        sa.Column("cost_usd", sa.Float, nullable=False),
        sa.Column("latency_s", sa.Float, nullable=False),
        sa.Column("answer", pg.JSONB),
        sa.Column("error", sa.Text),
        sa.Column("io_key", sa.Text),
        sa.Column("provider_request_id", sa.Text),
        sa.Column("shadow", sa.Boolean, server_default=sa.text("false"), nullable=False),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_judge_calls")),
        sa.ForeignKeyConstraint(
            ["clip_id"], ["clips.id"], name=op.f("fk_judge_calls_clip_id_clips")
        ),
        sa.ForeignKeyConstraint(
            ["stream_id"], ["streams.id"], name=op.f("fk_judge_calls_stream_id_streams")
        ),
    )
    op.create_index(op.f("ix_judge_calls_request_hash"), "judge_calls", ["request_hash"])
    op.create_index(op.f("ix_judge_calls_clip_id"), "judge_calls", ["clip_id"])
    op.create_index(
        op.f("ix_judge_calls_stream_id_created_at"), "judge_calls", ["stream_id", "created_at"]
    )
    op.create_index(op.f("ix_judge_calls_created_at"), "judge_calls", ["created_at"])

    op.create_table(
        "clip_plans",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("clip_id", sa.BigInteger, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("round", sa.Integer, nullable=False),
        sa.Column("verdict", sa.Text, nullable=False),
        sa.Column("plan", pg.JSONB),
        sa.Column("context_request", pg.JSONB),
        sa.Column("confidence", sa.Float),
        _jsonb("reasons", "[]"),
        _jsonb("repairs", "[]"),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("prompt_version", sa.Text, nullable=False),
        sa.Column("shadow", sa.Boolean, server_default=sa.text("false"), nullable=False),
        sa.Column("judge_call_id", sa.BigInteger),
        sa.Column("author", sa.Text, server_default="judge", nullable=False),
        sa.Column("parent_id", sa.BigInteger),
        sa.Column("review_event_id", sa.BigInteger),  # FK comes with 0008 (review_events)
        sa.Column("instruction", sa.Text),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clip_plans")),
        sa.ForeignKeyConstraint(
            ["clip_id"], ["clips.id"], name=op.f("fk_clip_plans_clip_id_clips")
        ),
        sa.ForeignKeyConstraint(
            ["judge_call_id"],
            ["judge_calls.id"],
            name=op.f("fk_clip_plans_judge_call_id_judge_calls"),
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["clip_plans.id"], name=op.f("fk_clip_plans_parent_id_clip_plans")
        ),
        sa.UniqueConstraint("clip_id", "version", name=op.f("uq_clip_plans_clip_id_version")),
        sa.CheckConstraint(PLAN_VERDICT, name=op.f("ck_clip_plans_verdict")),
        sa.CheckConstraint(PLAN_PLAN, name=op.f("ck_clip_plans_plan")),
        sa.CheckConstraint(PLAN_CONTEXT_REQUEST, name=op.f("ck_clip_plans_context_request")),
        sa.CheckConstraint(PLAN_AUTHOR, name=op.f("ck_clip_plans_author")),
    )

    # the "FK added when clips table exists" of 0001; every row's clip_id is NULL so far
    op.create_foreign_key(
        op.f("fk_media_requests_clip_id_clips"), "media_requests", "clips", ["clip_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_media_requests_clip_id_clips"), "media_requests", type_="foreignkey"
    )
    op.drop_table("clip_plans")
    op.drop_table("judge_calls")
    op.drop_table("clips")
