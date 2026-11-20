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


CHECKS_SQL = """
    SELECT conrelid::regclass::text AS tbl, conname, pg_get_constraintdef(oid) AS definition
      FROM pg_constraint WHERE contype = 'c' AND connamespace = 'public'::regnamespace
"""


def test_check_constraints_match_models(db_url, fresh_database):
    """Autogenerate doesn't compare CHECKs, and their texts are repeated in models and
    migrations: compare what Postgres made of each (migrated DB vs. create_all from models)."""
    cli.upgrade(db_url)
    checks = []
    for url, from_models in ((db_url, False), (fresh_database("clipper_models_test"), True)):
        engine = create_engine(url)
        with engine.begin() as conn:
            if from_models:
                Base.metadata.create_all(conn)
            rows = conn.execute(text(CHECKS_SQL))
            checks.append({(r.tbl, r.conname): r.definition for r in rows})
        engine.dispose()
    migrated, declared = checks
    assert ("audio_metrics_1s", "ck_audio_metrics_1s_ranges") in declared
    assert ("chunk_transcripts", "ck_chunk_transcripts_owned_range") in declared
    for name in (
        "source",
        "status",
        "suppress_reason",
        "moment_type",
        "moment_range",
        "window_range",
        "nesting",
    ):
        assert ("candidates", f"ck_candidates_{name}") in declared
    for name in ("events", "valid_has_delay"):
        assert ("chat_delay_estimates", f"ck_chat_delay_estimates_{name}") in declared
    for name in ("status", "reject_reason", "round", "context_range", "anchor_in_context"):
        assert ("clips", f"ck_clips_{name}") in declared
    for name in ("verdict", "plan", "context_request", "author"):
        assert ("clip_plans", f"ck_clip_plans_{name}") in declared
    assert migrated == declared


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
