from datetime import UTC, datetime, timedelta

from clipper_chat_ingest.aggregator import SecondAggregator
from clipper_chat_ingest.mapping import ChannelState, map_message
from clipper_twitch.irc import parse

T = datetime(2026, 11, 18, 20, 0, 0, tzinfo=UTC)
TS = int(T.timestamp() * 1000)
CHANNEL = ChannelState(
    channel_id=1,
    login="dallas",
    twitch_id="42",
    stream_id=7,
    emotes={"KEKW": "7tv", "OMEGALUL": "bttv"},
)
GLOBAL = {"Clap": "7tv"}


def m(line: str, received: datetime = T + timedelta(seconds=1)):
    return map_message(parse(line), received, CHANNEL, GLOBAL)


def test_privmsg_with_twitch_and_third_party_emotes():
    out = m(
        f"@id=abc;tmi-sent-ts={TS + 250};user-id=99;badges=subscriber/12;emotes=25:15-19 "
        ":bob!bob@bob.tmi.twitch.tv PRIVMSG #dallas :KEKW KEKW Clap Kappa lol"
    )
    row = out.message
    assert (row.msg_id, row.user_id, row.user_login, row.stream_id) == ("abc", "99", "bob", 7)
    assert row.sent_at == T + timedelta(milliseconds=250)
    assert row.badges == {"subscriber": "12"}
    assert {(e["name"], e["count"], e["source"]) for e in row.emotes} == {
        ("Kappa", 1, "twitch"),
        ("KEKW", 2, "7tv"),
        ("Clap", 1, "7tv"),
    }
    assert out.events == []


def test_action_bits_and_missing_timestamp():
    out = m("@id=x;bits=100 :bob!bob@b PRIVMSG #dallas :\x01ACTION cheers\x01")
    assert out.message.text == "cheers" and out.message.badges == {"_action": "1"}
    assert out.message.sent_at == T + timedelta(seconds=1)  # falls back to received time
    (bits,) = out.events
    assert bits.type == "bits" and bits.data == {"bits": 100}


def test_usernotice_resub_and_raid():
    resub = m(
        f"@id=n1;login=ronni;msg-id=resub;msg-param-cumulative-months=6;"
        rf"system-msg=ronni\shas\ssubscribed;tmi-sent-ts={TS} "
        ":tmi.twitch.tv USERNOTICE #dallas :Great stream"
    )
    (ev,) = resub.events
    assert (ev.type, ev.user_login, ev.msg_id) == ("resub", "ronni", "n1")
    assert ev.data == {
        "cumulative-months": "6",
        "system_msg": "ronni has subscribed",
        "text": "Great stream",
    }
    raid = m(
        f"@msg-id=raid;msg-param-viewerCount=1500;login=raider;tmi-sent-ts={TS} "
        ":tmi.twitch.tv USERNOTICE #dallas"
    )
    assert raid.events[0].type == "raid" and raid.events[0].data["viewerCount"] == "1500"


def test_moderation_events():
    clear = m(f"@login=bob;target-msg-id=abc;tmi-sent-ts={TS} :tmi.twitch.tv CLEARMSG #dallas :x")
    assert clear.delete_msg_id == "abc" and clear.events[0].type == "clearmsg"
    timeout = m(
        f"@ban-duration=600;target-user-id=99;tmi-sent-ts={TS} "
        ":tmi.twitch.tv CLEARCHAT #dallas :bob"
    )
    assert timeout.events[0].type == "timeout" and timeout.events[0].data["duration_s"] == 600
    ban = m(f"@target-user-id=99;tmi-sent-ts={TS} :tmi.twitch.tv CLEARCHAT #dallas :bob")
    assert ban.events[0].type == "ban"
    wipe = m(f"@tmi-sent-ts={TS} :tmi.twitch.tv CLEARCHAT #dallas")
    assert wipe.events[0].type == "clearchat"


def test_roomstate_initial_is_not_an_event_but_changes_are():
    initial = m(
        "@emote-only=0;followers-only=-1;r9k=0;room-id=42;slow=0;subs-only=0 "
        ":tmi.twitch.tv ROOMSTATE #dallas"
    )
    assert initial.room_id == "42" and initial.events == []
    change = m("@emote-only=1;room-id=42 :tmi.twitch.tv ROOMSTATE #dallas")
    assert change.events[0].type == "roomstate" and change.events[0].data == {"emote-only": "1"}


def test_other_commands_are_ignored():
    assert m(":tmi.twitch.tv 366 justinfan1 #dallas :End of /NAMES list").message is None


def test_aggregator_buckets_seconds_and_waits_for_grace():
    agg = SecondAggregator(grace=timedelta(seconds=5))
    agg.add_message(1, 7, T + timedelta(milliseconds=100), "u1", [("KEKW", 2)])
    agg.add_message(1, 7, T + timedelta(milliseconds=900), "u1", [("KEKW", 1)])
    agg.add_message(1, 7, T + timedelta(milliseconds=950), "u2", [])
    agg.add_message(1, 7, T + timedelta(seconds=1), "u3", [])
    agg.add_event(1, 7, T, "sub")
    agg.add_message(2, None, T, "u9", [])

    assert agg.pop_ready(T + timedelta(seconds=5)) == []  # still inside grace
    rows = agg.pop_ready(T + timedelta(seconds=7))  # second S is ready once now - grace >= S + 1s
    by_key = {(r.channel_id, r.ts): r for r in rows}
    first = by_key[(1, T)]
    assert (first.messages, first.unique_chatters, first.stream_id) == (3, 2, 7)
    assert first.emote_counts == {"KEKW": 3} and first.event_counts == {"sub": 1}
    assert by_key[(2, T)].messages == 1
    assert (1, T + timedelta(seconds=1)) in by_key

    agg.add_message(1, 7, T + timedelta(milliseconds=500), "late", [])  # second already written
    assert agg.late_dropped == 1
    agg.add_message(1, 7, T + timedelta(seconds=10), "u1", [])
    assert [r.ts for r in agg.pop_all()] == [T + timedelta(seconds=10)]
    assert agg.pending == 0
