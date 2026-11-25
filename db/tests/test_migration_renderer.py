"""Migration 0007 (renderer): `renders` on a database at 0006 with a planned clip, its FKs and
indexes, and the downgrade to 0006."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from clipper_db import cli

pytestmark = pytest.mark.integration


@pytest.fixture
def engine(fresh_database):
    """A database at 0006 with a channel, stream, candidate, planned clip and its plan."""
    url = fresh_database("clipper_renderer_migration_test")
    command.upgrade(cli.alembic_config(url), "0006")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO channels (login) VALUES ('x')"))
        conn.execute(
            text(
                "INSERT INTO streams (channel_id, platform_stream_id, started_at)"
                " VALUES (1, '1', now() - interval '1 hour')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO candidates (stream_id, channel_id, source, status, trigger,"
                " peak_utc, chat_delay_s, delay_source, moment_from, moment_to, window_from,"
                " window_to, score, detector_version) VALUES (1, 1, 'live', 'emitted', 'chat',"
                " now(), 8, 'default', now() - interval '10 s', now() + interval '5 s',"
                " now() - interval '90 s', now() + interval '30 s', 5.5, 'v1')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO clips (candidate_id, stream_id, channel_id, status, anchor_utc,"
                " context_from, context_to, priority, waiting_since, plan_version) VALUES"
                " (1, 1, 1, 'planned', now(), now() - interval '90 s', now() + interval '30 s',"
                " 80, now(), 1)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO clip_plans (clip_id, version, round, verdict, plan, model,"
                " prompt_version) VALUES (1, 1, 0, 'clip', '{\"version\": 1}', 'fake', 'v1-0')"
            )
        )
    yield engine
    engine.dispose()


def _url(engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def test_upgrade_adds_renders_and_the_downgrade_drops_it(engine):
    cli.upgrade(_url(engine))
    with engine.connect() as conn:
        insp = inspect(conn)
        assert "renders" in insp.get_table_names()
        fks = {fk["referred_table"] for fk in insp.get_foreign_keys("renders")}
        indexes = {ix["name"]: ix["column_names"] for ix in insp.get_indexes("renders")}
    assert fks == {"clips", "clip_plans"}
    assert indexes == {
        "ix_renders_clip_id_created_at": ["clip_id", "created_at"],
        "ix_renders_plan_id": ["plan_id"],
        "uq_renders_job_id": ["job_id"],  # the UNIQUE constraint's index
    }
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO renders (job_id, clip_id, plan_id, profile) VALUES (7, 1, 1, 'p')"
                " RETURNING status, pinned_keys, warnings"
            )
        ).one()
    assert tuple(row) == ("rendering", [], [])
    with pytest.raises(IntegrityError, match="fk_renders_plan_id_clip_plans"), engine.begin() as c:
        c.execute(
            text("INSERT INTO renders (job_id, clip_id, plan_id, profile) VALUES (8, 1, 99, 'p')")
        )
    command.downgrade(cli.alembic_config(_url(engine)), "0006")
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        plans = conn.execute(text("SELECT count(*) FROM clip_plans")).scalar_one()
    assert "renders" not in tables and {"clips", "clip_plans"} <= tables
    assert plans == 1
    cli.upgrade(_url(engine))
