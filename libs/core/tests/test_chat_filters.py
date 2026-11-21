"""Chat filters shared by the detector and the judge (`clipper_core.chat`, DESIGN §18, §19):
mentions (the detector's cases, copied), links, commands, normalised text, user keys, bots."""

from dataclasses import dataclass

import pytest

from clipper_core.chat import (
    BotList,
    ChannelBots,
    NoBots,
    is_command,
    is_mention,
    load_bots,
    normalised_text,
    parse_bots,
    strip_invisible,
    strip_links,
    user_key,
)

MENTIONS = (
    "CLIP IT",
    "clip that",
    "someone clip this",
    "CLIIIP IT",
    "clipit",
    "clip_it",
    "clip-it KEKW",
    "who clipped that",
    "!clip",
    "CLIP",
    "clip?",
    "CLIPPP!",
    "don\u2019t care, clip it",  # the negation is in another clause
)
NOT_MENTIONS = (
    "nice clip",
    "that clip is old",
    "watch the clip",
    "next clip pls",
    "Clip of the week",
    "don't clip that",
    "dont clip this",
    "not clipped",
    "youtube.com/clip/abc",
    "https://clips.twitch.tv/AbcDef-123",
    "clips are fun",
    "eclipse",
    "clipboard",
    "the clipper",
    "#clip",
)


def test_mention_text():
    for text in MENTIONS:
        assert is_mention(text), text
    for text in NOT_MENTIONS:
        assert not is_mention(text), text


@pytest.mark.parametrize(
    "text, kept",
    [
        ("see https://clips.twitch.tv/AbcDef-123 lol", "see   lol"),
        ("www.example.com is down", "  is down"),
        ("youtube.com/clip/abc and more", "  and more"),
        ("no link here.", "no link here."),
        ("a.b and c.de", "a.b and c.de"),  # domains without a path stay
    ],
)
def test_strip_links(text, kept):
    assert strip_links(text) == kept


def test_commands():
    assert is_command("!enter") and is_command("  !points") and is_command("!7")
    assert is_command("!Clip that")  # normalised first: case and leading spaces don't matter
    for text in ("!!!", "! KEKW", "hi !enter", "", "enter!"):
        assert not is_command(text), text


def test_normalised_text_and_invisible_characters():
    tag = chr(0xE0000)  # Chatterino/7TV append it to beat the duplicate filter
    assert strip_invisible("KEKW " + tag) == "KEKW "
    assert normalised_text("  No   WAY " + tag + chr(0x200B)) == "no way"
    assert normalised_text(normalised_text("A  b")) == normalised_text("A  b")


@dataclass
class Row:
    user_id: str | None
    user_login: str | None


def test_user_key():
    assert user_key(Row("123", "Bob")) == "123"
    assert user_key(Row(None, "Bob")) == "bob"
    assert user_key(Row(None, None)) is None and user_key(Row("", "")) is None


def test_packaged_bots():
    bots = load_bots()
    assert isinstance(bots, BotList) and bots.version >= 1 and len(bots.digest) == 64
    plain, xqc = bots.for_channel("other"), bots.for_channel("XQC")
    assert plain.is_bot("Nightbot") and plain.is_bot("streamelements")
    assert not plain.is_bot("xqcbot") and xqc.is_bot("xqcbot") and xqc.is_bot("nightbot")
    assert not plain.is_bot(None) and not plain.is_bot("")
    assert bots.for_channel(None) == plain
    assert load_bots() is bots  # cached


def test_bots_file_format(tmp_path):
    text = 'version = 3\nlogins = ["A_Bot"]\n[channels.Chan]\nlogins = ["ChanBot"]\n'
    bots = parse_bots(text)
    assert bots.version == 3 and bots.logins == {"a_bot"}
    assert bots.for_channel("chan") == ChannelBots(frozenset({"a_bot", "chanbot"}))
    path = tmp_path / "bots.toml"
    path.write_text(text, encoding="utf-8")
    assert load_bots(path) == bots
    assert parse_bots(text + "# edited\n").digest != bots.digest
    assert not NoBots().is_bot("nightbot")


@pytest.mark.parametrize(
    "text, message",
    [
        ("logins = []", "version"),
        ("version = 1\nlogins = 'x'", "list"),
        ("version = 1\nbots = []", "unknown keys"),
        ("version = 1\n[channels.x]\nbots = ['y']", "only `logins"),
        ("version = [", "TOML"),
    ],
)
def test_bad_bots_files(text, message):
    with pytest.raises(ValueError, match=message):
        parse_bots(text)
