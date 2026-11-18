"""Migrations match the models, chat partitions route rows, downgrade/upgrade round-trips."""

from datetime import timedelta

import psycopg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from clipper_core.models import Base
from clipper_core.timeline import utc_now
from clipper_db import cli
from clipper_db.schema_filter import include_object

pytestmark = pytest.mark.integration


@pytest.fixture
def db_url(fresh_database):
    return fresh_database("clipper_migration_test")


def test_models_match_migrations(db_url):
    cli.upgrade(db_url)
    engine = create_engine(db_url)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"include_object": include_object, "compare_type": True}
        )
        diff = compare_metadata(ctx, Base.metadata)
    engine.dispose()
    assert diff == [], f"models and migrations differ: {diff}"


def test_chat_partitions_route_rows_and_reject_unknown_days(db_url):
    cli.upgrade(db_url)
    cli.ensure_partitions(db_url)
    cli.ensure_partitions(db_url)  # idempotent
    now = utc_now()
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO chat_messages (sent_at, msg_id, channel_id, text) "
                "VALUES (:t, 'm1', 1, 'KEKW')"
            ),
            {"t": now},
        )
        partition = conn.execute(
            text("SELECT tableoid::regclass::text FROM chat_messages WHERE msg_id = 'm1'")
        ).scalar_one()
    assert partition == f"chat_messages_p{now:%Y%m%d}"

    with pytest.raises(Exception, match="no partition"), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO chat_messages (sent_at, msg_id, channel_id, text) "
                "VALUES (:t, 'm2', 1, 'too old')"
            ),
            {"t": now - timedelta(days=400)},
        )
    engine.dispose()


def test_downgrade_and_upgrade_again(db_url):
    cli.upgrade(db_url)
    from alembic import command

    command.downgrade(cli.alembic_config(db_url), "base")
    engine = create_engine(db_url)
    with engine.connect() as conn:
        assert set(inspect(conn).get_table_names()) == {"alembic_version"}
    engine.dispose()
    cli.upgrade(db_url)


def test_wait_for_db_fails_fast_on_wrong_host():
    with pytest.raises(Exception):  # noqa: B017
        cli.wait_for_db("postgresql+psycopg://x:y@127.0.0.1:1/nope", timeout_s=0)
    assert psycopg  # driver importable in the migrate image
