"""Migration 0003 (backfill): vods + viewer_clips, CHECKs (also on streams), downgrade to 0002."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from clipper_db import cli

pytestmark = pytest.mark.integration


@pytest.fixture
def engine(fresh_database):
    url = fresh_database("clipper_backfill_migration_test")
    cli.upgrade(url)
    engine = create_engine(url)
    yield engine
    engine.dispose()


def _seed(conn) -> tuple[int, int]:
    channel = conn.execute(text("INSERT INTO channels (login) VALUES ('x') RETURNING id")).scalar()
    stream = conn.execute(
        text(
            "INSERT INTO streams (channel_id, platform_stream_id, started_at, status, source) "
            "VALUES (:c, '123', now(), 'ended', 'backfill') RETURNING id"
        ),
        {"c": channel},
    ).scalar()
    return channel, stream


VOD = text(
    "INSERT INTO vods (channel_id, stream_id, platform_video_id, platform_created_at, "
    "duration_s, t0_utc, t0_method, media_status) "
    "VALUES (:c, :s, '999', now(), 3600, now(), 'chat', :status) RETURNING *"
)


def test_vods_and_viewer_clips_defaults_and_checks(engine):
    with engine.begin() as conn:
        channel, stream = _seed(conn)
        vod = conn.execute(VOD, {"c": channel, "s": stream, "status": "pending"}).one()
        clip = conn.execute(
            text(
                "INSERT INTO viewer_clips (clip_id, channel_id, stream_id, duration_s, "
                "view_count, platform_created_at) VALUES ('Slug', :c, :s, 30.0, 5, now()) "
                "RETURNING *"
            ),
            {"c": channel, "s": stream},
        ).one()
    assert (vod.chat_status, vod.media_done_s, vod.chat_done_s) == ("pending", 0, 0)
    assert vod.t0_candidates == {} and vod.flags == {} and vod.chat_metrics_to is None
    assert (clip.platform, clip.is_featured, clip.start_utc) == ("twitch", False, None)

    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(VOD, {"c": channel, "s": stream, "status": "bogus"})
    with pytest.raises(IntegrityError), engine.begin() as conn:  # one vods row per stream
        conn.execute(VOD, {"c": channel, "s": stream, "status": "done"})


STREAM_CHECKS = {"ck_streams_status", "ck_streams_source", "ck_streams_backfill_ended"}


def stream_checks(conn) -> set[str]:
    return {c["name"] for c in inspect(conn).get_check_constraints("streams")}


def test_downgrade_to_0002_drops_only_the_new_objects(engine):
    with engine.connect() as conn:
        assert stream_checks(conn) == STREAM_CHECKS
    url = engine.url.render_as_string(hide_password=False)
    command.downgrade(cli.alembic_config(url), "0002")
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        assert stream_checks(conn) == {"ck_streams_status"}
    assert "vods" not in tables and "viewer_clips" not in tables
    assert {"streams", "chunks", "stream_gaps", "audio_metrics_1s"} <= tables
    cli.upgrade(url)
