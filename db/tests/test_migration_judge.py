"""Migration 0005 (judge): clips, judge_calls, clip_plans, the media_requests.clip_id FK, and
the downgrade to 0004."""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from clipper_db import cli

pytestmark = pytest.mark.integration

NEW_TABLES = {"clips", "judge_calls", "clip_plans"}


@pytest.fixture
def engine(fresh_database):
    url = fresh_database("clipper_judge_migration_test")
    cli.upgrade(url)
    engine = create_engine(url)
    yield engine
    engine.dispose()


def _url(engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def _stream_and_candidate(conn) -> tuple[int, int, int]:
    channel = conn.execute(text("INSERT INTO channels (login) VALUES ('x') RETURNING id")).scalar()
    stream = conn.execute(
        text(
            "INSERT INTO streams (channel_id, platform_stream_id, started_at) "
            "VALUES (:c, '1', now() - interval '1 hour') RETURNING id"
        ),
        {"c": channel},
    ).scalar()
    candidate = conn.execute(
        text(
            "INSERT INTO candidates (stream_id, channel_id, source, status, trigger, peak_utc, "
            "chat_delay_s, delay_source, moment_from, moment_to, window_from, window_to, score, "
            "detector_version) VALUES (:s, :c, 'live', 'emitted', 'chat', now(), 8, 'default', "
            "now() - interval '10 s', now() + interval '5 s', now() - interval '90 s', "
            "now() + interval '30 s', 5.5, 'v1') RETURNING id"
        ),
        {"s": stream, "c": channel},
    ).scalar()
    return channel, stream, candidate


MEDIA_REQUEST = text(
    "INSERT INTO media_requests (stream_id, from_utc, to_utc, priority, requested_by, clip_id) "
    "VALUES (:s, now(), now() + interval '1 min', 100, 'judge', :clip) RETURNING id"
)


def test_clip_plan_call_and_media_request_defaults_and_links(engine):
    with engine.begin() as conn:
        channel, stream, candidate = _stream_and_candidate(conn)
        clip = conn.execute(
            text(
                "INSERT INTO clips (candidate_id, stream_id, channel_id, anchor_utc, context_from,"
                " context_to, priority, waiting_since) VALUES (:cand, :s, :c, now(),"
                " now() - interval '90 s', now() + interval '30 s', 80, now()) RETURNING *"
            ),
            {"cand": candidate, "s": stream, "c": channel},
        ).one()
        call = conn.execute(
            text(
                "INSERT INTO judge_calls (clip_id, stream_id, model, prompt_version, request_hash,"
                " input_tokens, cached_input_tokens, output_tokens, cost_usd, latency_s)"
                " VALUES (:clip, :s, 'fake', 'v1-0000', 'h', 10, 0, 5, 0.0, 0.1) RETURNING *"
            ),
            {"clip": clip.id, "s": stream},
        ).one()
        plan = conn.execute(
            text(
                "INSERT INTO clip_plans (clip_id, version, round, verdict, plan, model,"
                " prompt_version, judge_call_id) VALUES (:clip, 1, 0, 'clip', '{\"version\": 1}',"
                " 'fake', 'v1-0000', :call) RETURNING *"
            ),
            {"clip": clip.id, "call": call.id},
        ).one()
        request = conn.execute(MEDIA_REQUEST, {"s": stream, "clip": clip.id}).scalar()
    assert (clip.status, clip.round, clip.flags, clip.plan_version) == (
        "waiting_for_media",
        0,
        {},
        None,
    )
    assert clip.created_at == clip.updated_at and call.shadow is False
    assert (plan.author, plan.reasons, plan.repairs, plan.shadow) == ("judge", [], [], False)
    assert request is not None

    for bad in (
        lambda c: c.execute(MEDIA_REQUEST, {"s": stream, "clip": clip.id + 1000}),  # new FK
        lambda c: c.execute(  # one clip per candidate
            text(
                "INSERT INTO clips (candidate_id, stream_id, channel_id, anchor_utc,"
                " context_from, context_to, priority, waiting_since)"
                " VALUES (:cand, :s, :c, now(), now() - interval '1 s', now(), 80, now())"
            ),
            {"cand": candidate, "s": stream, "c": channel},
        ),
        lambda c: c.execute(  # one plan per (clip, version)
            text(
                "INSERT INTO clip_plans (clip_id, version, round, verdict, model,"
                " prompt_version) VALUES (:clip, 1, 0, 'reject', 'fake', 'v1-0000')"
            ),
            {"clip": clip.id},
        ),
    ):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            bad(conn)


def test_indexes_and_downgrade_to_0004_drops_the_new_tables_and_the_clip_fk(engine):
    with engine.connect() as conn:
        assert set(inspect(conn).get_table_names()) >= NEW_TABLES
        fks = inspect(conn).get_foreign_keys("media_requests")
        call_indexes = {
            ix["name"]: ix["column_names"] for ix in inspect(conn).get_indexes("judge_calls")
        }
    assert {fk["referred_table"] for fk in fks} == {"streams", "clips"}
    assert call_indexes == {
        "ix_judge_calls_request_hash": ["request_hash"],
        "ix_judge_calls_clip_id": ["clip_id"],
        "ix_judge_calls_stream_id_created_at": ["stream_id", "created_at"],
        "ix_judge_calls_created_at": ["created_at"],
    }
    command.downgrade(cli.alembic_config(_url(engine)), "0004")
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        fks = inspect(conn).get_foreign_keys("media_requests")
    assert not NEW_TABLES & tables
    assert {"candidates", "media_requests", "streams", "channels"} <= tables
    assert {fk["referred_table"] for fk in fks} == {"streams"}
    with engine.begin() as conn:  # clip_id is a plain column again
        _, stream, _ = _stream_and_candidate(conn)
        conn.execute(MEDIA_REQUEST, {"s": stream, "clip": 12345})
        conn.execute(text("DELETE FROM media_requests"))
    cli.upgrade(_url(engine))
    with engine.connect() as conn:
        assert set(inspect(conn).get_table_names()) >= NEW_TABLES
