"""emotes.message_emotes: parsed Twitch emotes + third-party sets matched by token."""

from clipper_twitch.emotes import message_emotes
from clipper_twitch.irc import parse_emotes


def test_twitch_emotes_from_the_irc_tag_and_third_party_tokens():
    text = "KEKW KEKW Clap Kappa lol"
    got = message_emotes(
        text, parse_emotes("25:15-19", text), {"KEKW": "7tv"}, {"Clap": "7tv", "KEKW": "bttv"}
    )
    assert got == [
        {"name": "Kappa", "count": 1, "source": "twitch", "id": "25"},
        {"name": "KEKW", "count": 2, "source": "7tv"},  # channel set wins over global
        {"name": "Clap", "count": 1, "source": "7tv"},
    ]


def test_parsed_emotes_from_any_source_and_no_double_count():
    # e.g. chat replay fragments: (emote_id, name, count); a Twitch emote is not also counted
    # as a third-party one with the same name
    got = message_emotes("LUL LUL", [("425618", "LUL", 2)], {"LUL": "bttv"}, {})
    assert got == [{"name": "LUL", "count": 2, "source": "twitch", "id": "425618"}]
    assert message_emotes("hello", [], {}, {}) == []
