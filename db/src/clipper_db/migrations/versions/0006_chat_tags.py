"""chat tags: chat_messages.first_message, highlighted, reward_id, msg_type (PRIVMSG IRC tags)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-22

Additive (DESIGN §18.3): four nullable columns on `chat_messages`, set by chat-ingest from a
PRIVMSG's tags: `first_message` (`first-msg=1`: the user's first message in the channel),
`highlighted` (`msg-id=highlighted-message`: the "highlight my message" channel-point
redemption), `reward_id` (`custom-reward-id`: the channel-point reward redeemed with the message),
`msg_type` (the raw `msg-id`: `highlighted-message`, `skip-subs-mode-message`,
`gigantified-emote-message`, `animated-message`, `user-intro`, ...). NULL = unknown: rows written
before this migration, rows the old chat-ingest keeps writing until its redeploy, and backfilled
chat (the GQL chat replay carries none of these tags).

Safe on the live table: one `ALTER TABLE ... ADD COLUMN ..., ADD COLUMN ...` on the partitioned
parent (a single pass over the partitions) adds the columns to every partition (partitions created
later copy the parent). Nullable columns without a default are a catalog-only change: no table
rewrite, no scan, no index build (db/tests/test_migration_chat_tags.py checks that an existing
partition keeps its file). It needs an ACCESS EXCLUSIVE lock on the parent and every partition for
a moment; `lock_timeout` (5 s per lock wait) makes the migration give up (the migrate container
fails; run it again) instead of queueing chat-ingest's inserts behind a long-running transaction.
The downgrade uses the same timeout.

Alembic runs all pending migrations in ONE transaction: apply 0006 on its own, not in the same run
as 0007+, or the locks taken here are held (and chat-ingest's inserts wait) until the whole run
commits, and a later migration's failure rolls 0006 back too.

Deploy (compose): `up --build` of any service runs `migrate` first (`depends_on: migrate:
service_completed_successfully`), so make the order explicit:
1. `docker compose up --build migrate` while the old chat-ingest runs; check that it exits 0 (the
   old chat-ingest keeps working on the new schema);
2. then `docker compose up -d --build chat-ingest` (without 0006, the new chat-ingest's inserts
   fail: rows wait in its retry buffer, the metrics of those seconds are lost).
The backfill tool doesn't need 0006: its rows have no tags, and `ChatWriter` leaves the tag columns
out of an INSERT whose rows all lack them.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOCK_TIMEOUT = "5s"  # per lock wait; tests shorten it
COLUMNS = {
    "first_message": "boolean",
    "highlighted": "boolean",
    "reward_id": "text",
    "msg_type": "text",
}


def _with_lock_timeout(sql: str) -> None:
    # SET LOCAL lasts until the transaction ends; reset it for later migrations in the same run
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(sql)
    op.execute("SET LOCAL lock_timeout TO DEFAULT")


def upgrade() -> None:
    # nullable, no default: catalog-only
    adds = ", ".join(f"ADD COLUMN {name} {type_}" for name, type_ in COLUMNS.items())
    _with_lock_timeout(f"ALTER TABLE chat_messages {adds}")


def downgrade() -> None:
    drops = ", ".join(f"DROP COLUMN {name}" for name in reversed(COLUMNS))
    _with_lock_timeout(f"ALTER TABLE chat_messages {drops}")
