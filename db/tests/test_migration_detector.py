"""Migration 0004 (detector): candidates + chat_delay_estimates, FKs, downgrade to 0003."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from clipper_db import cli

pytestmark = pytest.mark.integration

NEW_TABLES = {"candidates", "chat_delay_estimates"}


@pytest.fixture
def engine(fresh_database):
    url = fresh_database("clipper_detector_migration_test")
    cli.upgrade(url)
    engine = create_engine(url)
    yield engine
    engine.dispose()


CANDIDATE = text(
    "INSERT INTO candidates (stream_id, channel_id, source, status, trigger, peak_utc, "
    "chat_delay_s, delay_source, moment_from, moment_to, window_from, window_to, score, "
    "detector_version, media_request_id) "
    "VALUES (:s, :c, 'live', 'emitted', 'chat', now(), 8, 'default', now() - interval '10 s', "
    "now() + interval '5 s', now() - interval '90 s', now() + interval '30 s', 5.5, 'v1', :mr) "
    "RETURNING *"
)


def test_candidates_link_media_requests_and_chat_delay_defaults(engine):
    with engine.begin() as conn:
        channel = conn.execute(text("INSERT INTO channels (login) VALUES ('x') RETURNING id"))
        channel = channel.scalar()
        stream = conn.execute(
            text(
                "INSERT INTO streams (channel_id, platform_stream_id, started_at) "
                "VALUES (:c, '1', now()) RETURNING id"
            ),
            {"c": channel},
        ).scalar()
        request = conn.execute(
            text(
                "INSERT INTO media_requests (stream_id, from_utc, to_utc, priority, requested_by) "
                "VALUES (:s, now(), now() + interval '2 min', 80, 'detector') RETURNING id"
            ),
            {"s": stream},
        ).scalar()
        row = conn.execute(CANDIDATE, {"s": stream, "c": channel, "mr": request}).one()
        estimate = conn.execute(
            text(
                "INSERT INTO chat_delay_estimates (channel_id, stream_id, method, delay_s, "
                "events, strength, valid, detector_version) "
                "VALUES (:c, :s, 'audio_events', 6.5, 18, 2.0, true, 'v1') RETURNING *"
            ),
            {"c": channel, "s": stream},
        ).one()
    assert (row.moment_type, row.features, row.media_request_id) == ("unknown", {}, request)
    assert row.created_at == row.updated_at and estimate.computed_at is not None

    with pytest.raises(IntegrityError), engine.begin() as conn:  # FK to media_requests
        conn.execute(CANDIDATE, {"s": stream, "c": channel, "mr": request + 1000})


def test_downgrade_to_0003_drops_only_the_new_tables(engine):
    with engine.connect() as conn:
        assert set(inspect(conn).get_table_names()) >= NEW_TABLES
    url = engine.url.render_as_string(hide_password=False)
    command.downgrade(cli.alembic_config(url), "0003")
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
    assert not NEW_TABLES & tables
    assert {"vods", "viewer_clips", "media_requests", "streams", "channels"} <= tables
    cli.upgrade(url)
    with engine.connect() as conn:
        assert set(inspect(conn).get_table_names()) >= NEW_TABLES
