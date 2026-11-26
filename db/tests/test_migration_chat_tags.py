"""Migration 0006 (chat tags): four nullable columns on the partitioned `chat_messages`, on old and
new partitions, without rewriting a partition; old rows read NULL; the lock timeout gives up
instead of waiting; the downgrade to 0005; `ChatWriter` still writes untagged (backfill) rows to a
database at 0005."""

from datetime import UTC, date, datetime

import psycopg
import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from clipper_core.chat import ChatWriter, MessageRow
from clipper_core.config import Settings
from clipper_core.db import create_engine as create_async_engine
from clipper_core.db import session_factory
from clipper_core.partitions import partition_ddl, partition_name
from clipper_db import cli

pytestmark = pytest.mark.integration

COLUMNS = {
    "first_message": "BOOLEAN",
    "highlighted": "BOOLEAN",
    "reward_id": "TEXT",
    "msg_type": "TEXT",
}
OLD_DAY, NEW_DAY = date(2026, 11, 1), date(2026, 12, 24)
OLD, NEW = partition_name(OLD_DAY), partition_name(NEW_DAY)
INSERT_OLD = text(
    "INSERT INTO chat_messages (sent_at, msg_id, channel_id, text) "
    "VALUES ('2026-11-01 12:00:00+00', 'old', 1, 'before 0006')"
)


@pytest.fixture
def engine(fresh_database):
    """A database at 0005 with one chat partition and one row in it."""
    url = fresh_database("clipper_chat_tags_migration_test")
    command.upgrade(cli.alembic_config(url), "0005")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(partition_ddl(OLD_DAY)))
        conn.execute(INSERT_OLD)
    yield engine
    engine.dispose()


def _url(engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def tag_columns(conn, table: str) -> dict[str, tuple[str, bool]]:
    return {
        c["name"]: (str(c["type"]), c["nullable"])
        for c in inspect(conn).get_columns(table)
        if c["name"] in COLUMNS
    }


def filenode(conn, table: str) -> int:
    return conn.execute(text("SELECT pg_relation_filenode(:t)"), {"t": table}).scalar_one()


def version(conn) -> str:
    return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def test_upgrade_adds_nullable_columns_to_every_partition_without_a_rewrite(engine):
    with engine.connect() as conn:
        assert tag_columns(conn, "chat_messages") == {} and tag_columns(conn, OLD) == {}
        before = filenode(conn, OLD)
    cli.upgrade(_url(engine))

    want = {name: (type_, True) for name, type_ in COLUMNS.items()}
    with engine.begin() as conn:
        conn.execute(text(partition_ddl(NEW_DAY)))  # a partition made after the migration
        assert tag_columns(conn, "chat_messages") == want
        assert tag_columns(conn, OLD) == want and tag_columns(conn, NEW) == want
        assert filenode(conn, OLD) == before  # catalog-only: the partition wasn't rewritten
        conn.execute(
            text(
                "INSERT INTO chat_messages (sent_at, msg_id, channel_id, text, first_message, "
                "highlighted, reward_id, msg_type) VALUES ('2026-12-24 08:00:00+00', 'new', 1, "
                "'hi', true, false, 'reward-uuid', null), ('2026-11-01 12:00:01+00', 'old2', 1, "
                "'hi', null, true, null, 'highlighted-message')"
            )
        )
        stored = conn.execute(
            text(
                "SELECT msg_id, tableoid::regclass::text AS part, first_message, highlighted, "
                "reward_id, msg_type FROM chat_messages ORDER BY sent_at"
            )
        ).all()
    assert [tuple(r) for r in stored] == [
        ("old", OLD, None, None, None, None),  # written before 0006: unknown
        ("old2", OLD, None, True, None, "highlighted-message"),
        ("new", NEW, True, False, "reward-uuid", None),
    ]


def test_a_held_lock_makes_the_upgrade_give_up_and_stay_at_0005(engine, monkeypatch):
    cfg = cli.alembic_config(_url(engine))
    script = ScriptDirectory.from_config(cfg)  # alembic loads a fresh module per instance
    monkeypatch.setattr(script.get_revision("0006").module, "LOCK_TIMEOUT", "200ms")
    monkeypatch.setattr(ScriptDirectory, "from_config", staticmethod(lambda config: script))
    with engine.connect() as holder, holder.begin():
        holder.execute(text("LOCK TABLE chat_messages IN ACCESS SHARE MODE"))  # a long reader
        with pytest.raises(OperationalError) as failed:
            command.upgrade(cfg, "head")
    assert isinstance(failed.value.orig, psycopg.errors.LockNotAvailable)
    with engine.connect() as conn:
        assert version(conn) == "0005" and tag_columns(conn, "chat_messages") == {}
    monkeypatch.undo()
    command.upgrade(cfg, "0006")  # once the lock is gone it goes through
    with engine.connect() as conn:
        assert version(conn) == "0006"


def test_downgrade_to_0005_drops_the_columns_and_keeps_the_rows(engine):
    cli.upgrade(_url(engine))
    command.downgrade(cli.alembic_config(_url(engine)), "0005")
    with engine.begin() as conn:
        assert tag_columns(conn, "chat_messages") == {} and tag_columns(conn, OLD) == {}
        assert conn.execute(text("SELECT msg_id FROM chat_messages")).scalars().all() == ["old"]
        conn.execute(text(partition_ddl(NEW_DAY)))
    cli.upgrade(_url(engine))
    with engine.connect() as conn:
        assert set(tag_columns(conn, OLD)) == set(tag_columns(conn, NEW)) == set(COLUMNS)


def _row(i: int, **kw) -> MessageRow:
    sent_at = datetime(2026, 11, 1, 13, 0, i, tzinfo=UTC)
    return MessageRow(sent_at, f"b{i}", 1, None, None, "u", "u", "hi", None, None, **kw)


async def test_chat_writer_writes_untagged_rows_to_a_database_before_0006(engine):
    """The backfill doesn't need 0006: its rows have no tags, so the INSERT leaves the columns
    out. A batch with a tagged row (chat-ingest) does need it."""
    sessions_engine = create_async_engine(Settings(database_url=_url(engine), _env_file=None))
    sessions = session_factory(sessions_engine)
    writer = ChatWriter()
    try:
        async with sessions.begin() as s:
            await writer.write(s, [_row(i, source="backfill") for i in range(3)], [], [], [])
        tagged = [_row(3), _row(4, first_message=False, highlighted=False)]
        with pytest.raises(ProgrammingError) as failed:
            async with sessions.begin() as s:
                await writer.write(s, tagged, [], [], [])
    finally:
        await sessions_engine.dispose()
    assert isinstance(failed.value.orig, psycopg.errors.UndefinedColumn)
    with engine.connect() as conn:
        stored = conn.execute(text("SELECT msg_id, source FROM chat_messages ORDER BY msg_id"))
        assert [tuple(r) for r in stored] == [
            ("b0", "backfill"),
            ("b1", "backfill"),
            ("b2", "backfill"),
            ("old", "live"),
        ]
        assert version(conn) == "0005"
