"""ChatIngest end to end against Postgres: IRC lines in -> rows out."""

from datetime import timedelta

import pytest
from sqlalchemy import text

from clipper_chat_ingest.service import ChatIngest
from clipper_core.timeline import utc_now
from clipper_twitch.irc import parse

pytestmark = pytest.mark.integration


async def setup_channel(sessions, login="dallas", live=True) -> tuple[int, int | None]:
    async with sessions.begin() as s:
        cid = (
            await s.execute(
                text("INSERT INTO channels (login) VALUES (:l) RETURNING id"), {"l": login}
            )
        ).scalar_one()
        sid = None
        if live:
            sid = (
                await s.execute(
                    text(
                        "INSERT INTO streams (channel_id, platform_stream_id, started_at) "
                        "VALUES (:c, 'live-1', now()) RETURNING id"
                    ),
                    {"c": cid},
                )
            ).scalar_one()
    return cid, sid


def ms(dt) -> int:
    return int(dt.timestamp() * 1000)


async def test_lines_become_messages_events_metrics_and_deletions(sessions):
    channel_id, stream_id = await setup_channel(sessions)
    ingest = ChatIngest(sessions, worker_id="test", fetch_emotes=False)
    assert await ingest.sync_channels() == {"dallas"}
    ingest.channels["dallas"].emotes = {"KEKW": "7tv"}

    now = utc_now() - timedelta(seconds=30)
    lines = [
        f"@id=m1;tmi-sent-ts={ms(now)};user-id=1 :a!a@a PRIVMSG #dallas :KEKW KEKW",
        f"@id=m2;tmi-sent-ts={ms(now)};user-id=2 :b!b@b PRIVMSG #dallas :hello",
        f"@id=m3;tmi-sent-ts={ms(now)};user-id=2 :b!b@b PRIVMSG #dallas :bad word",
        f"@msg-id=sub;login=c;tmi-sent-ts={ms(now)} :tmi.twitch.tv USERNOTICE #dallas",
        f"@login=b;target-msg-id=m3;tmi-sent-ts={ms(now)} "
        ":tmi.twitch.tv CLEARMSG #dallas :bad word",
        f"@id=x;tmi-sent-ts={ms(now)} :z!z@z PRIVMSG #notjoined :ignored",
    ]
    for line in lines:
        ingest.on_message(parse(line), now + timedelta(milliseconds=200))
    await ingest.flush()

    async with sessions() as s:
        msgs = (
            await s.execute(
                text("SELECT msg_id, stream_id, deleted, emotes FROM chat_messages ORDER BY msg_id")
            )
        ).all()
        events = (
            (await s.execute(text("SELECT type FROM chat_events ORDER BY type"))).scalars().all()
        )
        metric = (await s.execute(text("SELECT * FROM chat_metrics_1s"))).one()
        # no emotes / badges -> SQL NULL, never the JSON scalar null (breaks jsonb functions)
        json_nulls = (
            await s.execute(
                text(
                    "SELECT count(*) FROM chat_messages "
                    "WHERE jsonb_typeof(emotes) = 'null' OR jsonb_typeof(badges) = 'null'"
                )
            )
        ).scalar_one()
    assert json_nulls == 0
    assert [(r.msg_id, r.stream_id, r.deleted) for r in msgs] == [
        ("m1", stream_id, False),
        ("m2", stream_id, False),
        ("m3", stream_id, True),
    ]
    assert msgs[0].emotes == [{"name": "KEKW", "count": 2, "source": "7tv"}]
    assert events == ["clearmsg", "sub"]
    assert (metric.channel_id, metric.stream_id, metric.messages, metric.unique_chatters) == (
        channel_id,
        stream_id,
        3,
        2,
    )
    assert metric.emote_counts == {"KEKW": 2}
    assert metric.event_counts == {"sub": 1, "clearmsg": 1}

    # The same message again (e.g. replayed) is ignored, not duplicated.
    ingest.on_message(parse(lines[1]), now)
    await ingest.flush(final=True)
    async with sessions() as s:
        assert (await s.execute(text("SELECT count(*) FROM chat_messages"))).scalar_one() == 3


async def test_privmsg_tags_are_stored_as_columns(sessions):
    await setup_channel(sessions)
    ingest = ChatIngest(sessions, worker_id="test", fetch_emotes=False)
    await ingest.sync_channels()
    now = utc_now() - timedelta(seconds=30)
    reward = "2d9a1a7c-1c6e-4f1b-9e2e-6b4d0e7f3a11"
    base = f"tmi-sent-ts={ms(now)};user-id=1;first-msg=0;returning-chatter=0"
    lines = [
        f"@id=t1;{base.replace('first-msg=0', 'first-msg=1')} :a!a@a PRIVMSG #dallas :hi all",
        f"@id=t2;{base};msg-id=highlighted-message :a!a@a PRIVMSG #dallas :look",
        f"@id=t3;{base};custom-reward-id={reward} :a!a@a PRIVMSG #dallas :hydrate",
        f"@id=t4;{base} :a!a@a PRIVMSG #dallas :plain",
        f"@id=t5;tmi-sent-ts={ms(now)} :a!a@a PRIVMSG #dallas :no first-msg tag",
        f"@id=t6;{base};msg-id=gigantified-emote-message :a!a@a PRIVMSG #dallas :Kappa",
    ]
    for line in lines:
        ingest.on_message(parse(line), now)
    await ingest.flush(final=True)
    async with sessions() as s:
        stored = (
            await s.execute(
                text(
                    "SELECT msg_id, first_message, highlighted, reward_id, msg_type "
                    "FROM chat_messages ORDER BY msg_id"
                )
            )
        ).all()
    assert [tuple(r) for r in stored] == [
        ("t1", True, False, None, None),
        ("t2", False, True, None, "highlighted-message"),
        ("t3", False, False, reward, None),
        ("t4", False, False, None, None),
        ("t5", None, False, None, None),
        ("t6", False, False, None, "gigantified-emote-message"),
    ]


async def test_new_day_partition_is_created_on_demand(sessions):
    await setup_channel(sessions, live=False)
    ingest = ChatIngest(sessions, worker_id="test", fetch_emotes=False)
    await ingest.sync_channels()
    future = utc_now() + timedelta(days=40)  # no partition exists that far ahead
    ingest.on_message(parse(f"@id=f1;tmi-sent-ts={ms(future)} :a!a@a PRIVMSG #dallas :hi"), future)
    await ingest.flush(final=True)
    async with sessions() as s:
        part = (
            await s.execute(
                text("SELECT tableoid::regclass::text FROM chat_messages WHERE msg_id = 'f1'")
            )
        ).scalar_one()
    assert part == f"chat_messages_p{future:%Y%m%d}"


async def test_failed_flush_keeps_rows_for_retry(sessions, monkeypatch):
    await setup_channel(sessions, live=False)
    ingest = ChatIngest(sessions, worker_id="test", fetch_emotes=False)
    await ingest.sync_channels()
    now = utc_now()
    ingest.on_message(parse(f"@id=r1;tmi-sent-ts={ms(now)} :a!a@a PRIVMSG #dallas :hi"), now)

    real_write = ingest.writer.write

    async def broken(*args, **kwargs):
        raise ConnectionError("db down")

    monkeypatch.setattr(ingest.writer, "write", broken)
    await ingest.flush()
    assert len(ingest.messages) == 1 and ingest.stats["flush_errors"] == 1

    monkeypatch.setattr(ingest.writer, "write", real_write)
    await ingest.flush(final=True)
    assert ingest.messages == []
    async with sessions() as s:
        assert (await s.execute(text("SELECT count(*) FROM chat_messages"))).scalar_one() == 1


async def test_sync_follows_live_stream_and_disabled_channels(sessions):
    channel_id, _ = await setup_channel(sessions, live=False)
    ingest = ChatIngest(sessions, worker_id="test", fetch_emotes=False)
    await ingest.sync_channels()
    assert ingest.channels["dallas"].stream_id is None
    async with sessions.begin() as s:
        sid = (
            await s.execute(
                text(
                    "INSERT INTO streams (channel_id, platform_stream_id, started_at) "
                    "VALUES (:c, 'new', now()) RETURNING id"
                ),
                {"c": channel_id},
            )
        ).scalar_one()
    await ingest.sync_channels()
    assert ingest.channels["dallas"].stream_id == sid
    async with sessions.begin() as s:
        await s.execute(text("UPDATE channels SET active = false"))
    assert await ingest.sync_channels() == set()
