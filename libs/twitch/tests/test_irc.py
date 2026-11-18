import pytest

from clipper_twitch.irc import (
    count_tokens,
    parse,
    parse_badges,
    parse_emotes,
    strip_action,
    unescape_tag,
)

PRIVMSG = (
    "@badge-info=;badges=turbo/1;color=#0D4200;display-name=ronni;emotes=25:0-4,12-16/1902:6-10;"
    "id=b34ccfc7-4977-403a-8a94-33c6bac34fb8;mod=0;room-id=1337;subscriber=0;"
    "tmi-sent-ts=1507246572675;turbo=1;user-id=1337;user-type=global_mod "
    ":ronni!ronni@ronni.tmi.twitch.tv PRIVMSG #Ronni :Kappa Keepo Kappa"
)


def test_parse_privmsg():
    msg = parse(PRIVMSG + "\r\n")
    assert msg.command == "PRIVMSG"
    assert msg.channel == "ronni"
    assert msg.nick == "ronni"
    assert msg.text == "Kappa Keepo Kappa"
    assert msg.tags["tmi-sent-ts"] == "1507246572675"
    assert msg.tags["badge-info"] == ""
    assert parse_emotes(msg.tags["emotes"], msg.text) == [("25", "Kappa", 2), ("1902", "Keepo", 1)]


def test_parse_without_tags_or_prefix():
    ping = parse("PING :tmi.twitch.tv")
    assert (ping.command, ping.params, ping.prefix) == ("PING", ("tmi.twitch.tv",), None)
    join = parse(":me!me@me.tmi.twitch.tv JOIN #chan")
    assert join.command == "JOIN" and join.channel == "chan" and join.text is None
    numeric = parse(":tmi.twitch.tv 001 justinfan123 :Welcome, GLHF!")
    assert numeric.command == "001" and numeric.text == "Welcome, GLHF!"


def test_trailing_with_colons_and_empty_trailing():
    msg = parse(":u!u@u PRIVMSG #c :a :b: c")
    assert msg.text == "a :b: c"
    assert parse(":u!u@u PRIVMSG #c :").text == ""


def test_tag_unescaping():
    assert (
        unescape_tag(r"ronni\shas\ssubscribed\sfor\s6\smonths!")
        == "ronni has subscribed for 6 months!"
    )
    assert unescape_tag(r"a\:b\\c\rd\ne") == "a;b\\c\rd\ne"
    assert unescape_tag("trailing\\") == "trailing"
    msg = parse(r"@system-msg=hello\sworld;flag :tmi.twitch.tv USERNOTICE #c")
    assert msg.tags == {"system-msg": "hello world", "flag": ""}


def test_emote_positions_are_code_points():
    text = "🔥 Kappa"  # emoji is one code point
    assert parse_emotes("25:2-6", text) == [("25", "Kappa", 1)]
    assert parse_emotes("", text) == [] and parse_emotes(None, text) == []


def test_badges_action_and_tokens():
    assert parse_badges("subscriber/12,premium/1") == {"subscriber": "12", "premium": "1"}
    assert parse_badges("") == {}
    assert strip_action("\x01ACTION waves\x01") == ("waves", True)
    assert strip_action("hello") == ("hello", False)
    assert count_tokens("KEKW KEKW lol OMEGALUL", {"KEKW": "7tv", "OMEGALUL": "bttv"}) == {
        "KEKW": 2,
        "OMEGALUL": 1,
    }


def test_empty_line_is_rejected():
    with pytest.raises(ValueError):
        parse("@a=b ")
