"""Lexicon: the packaged list, matching rules, channel overrides, validation, moment types."""

import re

import pytest

from clipper_core.contracts import MomentType
from clipper_detector.lexicon import (
    Category,
    Lexicon,
    LexiconError,
    MatchKind,
    to_moment_type,
)

C = Category


@pytest.fixture(scope="module")
def lexicon() -> Lexicon:
    return Lexicon.load()


@pytest.fixture(scope="module")
def chan(lexicon):
    return lexicon.for_channel("somechannel")


def cats(match) -> dict:
    return dict(match.categories)


def test_packaged_lexicon_has_the_seed_list(lexicon, chan):
    assert lexicon.version >= 1
    expected = {
        "KEKW": C.FUNNY,
        "OMEGALUL": C.FUNNY,
        "LUL": C.FUNNY,
        "LULW": C.FUNNY,
        "ICANT": C.FUNNY,
        "Clueless": C.FUNNY,
        "Pog": C.HYPE,
        "PogChamp": C.HYPE,
        "POGGERS": C.HYPE,
        "EZ": C.HYPE,
        "D:": C.SHOCK,
        "NOWAYING": C.SHOCK,
        "Aware": C.SHOCK,
        "monkaS": C.TENSE,
        "monkaW": C.TENSE,
        "Sadge": C.SAD,
        "PepeHands": C.SAD,
        "<3": C.WHOLESOME,
        "peepoHey": C.GREETING,
    }
    for name, category in expected.items():
        assert chan.category_of(name, is_emote=True) == (category, MatchKind.EXACT), name


def test_words_are_case_insensitive_stretched_and_phrases(chan):
    assert cats(chan.match("LMAOOOO")) == {C.FUNNY: MatchKind.EXACT}
    assert cats(chan.match("WWWW")) == {C.HYPE: MatchKind.EXACT}
    assert cats(chan.match("NO WAYYYY")) == {C.SHOCK: MatchKind.EXACT}
    assert cats(chan.match("Let's GOOOO boys")) == {C.HYPE: MatchKind.EXACT}
    assert cats(chan.match("hiii chat")) == {C.GREETING: MatchKind.EXACT}
    assert cats(chan.match("YOOOO")) == {C.HYPE: MatchKind.EXACT}  # "yoo", not the greeting "yo"
    assert cats(chan.match("what game is this")) == {}  # everyday words aren't reactions


def test_emotes_are_case_sensitive_and_unknown_emotes_are_uncategorised(chan):
    assert chan.category_of("kekw") == (C.FUNNY, MatchKind.EXACT)  # also a word
    assert chan.category_of("monkas") is None  # not the emote, and no emote-like token
    assert cats(chan.match("xqcSkip", ["xqcSkip"])) == {C.UNCATEGORISED: MatchKind.EXACT}
    assert cats(chan.match("xqcSkip")) == {}  # not recognised as an emote: a plain word


def test_patterns_weigh_half(chan):
    m = chan.match("KEKWait")
    assert cats(m) == {C.FUNNY: MatchKind.PATTERN}
    assert m.weight(C.FUNNY, 0.5) == 0.5
    assert m.reaction_weight(0.5) == 0.5
    exact = chan.match("KEKW KEKWait")
    assert exact.reaction_weight(0.5) == 1.0  # the best match counts


def test_textless_and_flags(chan):
    assert chan.match("KEKW KEKW").textless
    assert chan.match("hi KEKW").textless  # greetings count as lexicon words too
    assert chan.match("Kappa Kappa", ["Kappa"]).textless  # an unknown emote is no free text
    assert chan.match("???").textless and chan.match("? ! ...").textless  # punctuation only
    assert chan.match(":D :O").textless and chan.match("D").textless  # emoticons
    assert not chan.match("Dad").textless and not chan.match(":Dsomething").textless
    assert not chan.match("Kappa Kappa").textless  # not recognised as an emote: a word
    assert not chan.match("KEKW this guy").textless
    assert chan.match("KEKW this guy").reaction
    assert chan.match("hello").greeting and not chan.match("hello").reaction
    assert not chan.match("").textless
    assert cats(chan.match("yo chat")) == {} and cats(chan.match("YO WTF")) == {C.SHOCK: 2}


@pytest.mark.parametrize(
    "text",
    [
        "I need a new glove",
        "unhappy",
        "microwave",
        "telescope",
        "lullaby",
        "pogo stick",
        "thug life",
        "Lullaby",
        "UNHAPPY",
        "Microwave",
        "the scope is wrong",
        "copes",
        "hugs",
    ],
)
def test_patterns_skip_plain_words(chan, text):
    assert cats(chan.match(text)) == {}


@pytest.mark.parametrize(
    ("token", "category"),
    [
        ("KEKWait", C.FUNNY),
        ("OMEGALULiguess", C.FUNNY),
        ("PogTasty", C.HYPE),
        ("catHug", C.WHOLESOME),
        ("widepeepoLove", C.WHOLESOME),
        ("monkaHmm", C.TENSE),
        ("Copege", None),
        ("hahahaha", C.FUNNY),
        ("loool", C.FUNNY),
    ],
)
def test_patterns_match_emote_variants_and_word_shapes(chan, token, category):
    hit = chan.category_of(token)
    assert (hit[0] if hit else None) == category
    if category is not None:
        assert hit[1] in (MatchKind.PATTERN, MatchKind.EXACT)
    # a recognised emote is emote-like whatever its case
    assert chan.category_of("COPEGE", is_emote=True) == (C.SAD, MatchKind.PATTERN)


def test_emotes_with_punctuation_around(chan):
    assert chan.category_of("monkaS,") == (C.TENSE, MatchKind.EXACT)
    assert chan.category_of("PepeHands.") == (C.SAD, MatchKind.EXACT)
    assert chan.category_of("(KEKW)") == (C.FUNNY, MatchKind.EXACT)
    assert chan.category_of("D:") == (C.SHOCK, MatchKind.EXACT)  # the emote itself, unstripped


def test_emoji_and_invisible_characters(chan):
    assert cats(chan.match("\U0001f602\U0001f602\U0001f602")) == {C.FUNNY: MatchKind.EXACT}
    assert cats(chan.match("nice \u2764\ufe0f")) == {C.WHOLESOME: MatchKind.EXACT}
    # Chatterino/7TV append U+E0000 to get around the duplicate filter
    m = chan.match("KEKW " + chr(0xE0000))
    assert m.tokens == 1 and m.textless


def test_bots_and_channel_overrides(lexicon):
    plain, xqc = lexicon.for_channel("other"), lexicon.for_channel("XQC")
    assert plain.is_bot("Nightbot") and plain.is_bot("streamelements")
    assert not plain.is_bot("xqcbot") and xqc.is_bot("xqcbot")
    assert not plain.is_bot(None)
    assert cats(plain.match("xqcL", ["xqcL"])) == {C.UNCATEGORISED: MatchKind.EXACT}
    assert cats(xqc.match("xqcL", ["xqcL"])) == {C.WHOLESOME: MatchKind.EXACT}
    assert lexicon.for_channel(None).match("KEKW").reaction


CUSTOM = """
version = 7

[emotes]
funny = ["KEKW"]
hype = ["Pog"]

[words]
funny = ["lol"]
hype = ["lets go"]

[patterns]
funny = ["^kek"]

[bots]
logins = ["Nightbot"]

[transcript]
clip = ["clip that"]

[channels.Chan]
bots = ["chanbot"]

[channels.Chan.emotes]
ignore = ["KEKW"]
sad = ["Pog"]

[channels.Chan.words]
ignore = ["lol", "lets go"]

[channels.Chan.patterns]
sad = ["^kekw"]
"""


def test_custom_file_and_channel_overrides():
    lex = Lexicon.from_toml(CUSTOM)
    assert lex.version == 7 and lex.channels == {"chan"}
    assert re.fullmatch(r"7\.[0-9a-f]{6}", lex.tag)
    assert Lexicon.from_toml(CUSTOM + "\n# a comment\n").tag != lex.tag  # edited: new hash
    assert Lexicon.from_toml(CUSTOM).tag == lex.tag
    base, chan = lex.for_channel("x"), lex.for_channel("chan")
    assert base.category_of("KEKW") == (C.FUNNY, MatchKind.EXACT)
    # ignored in the channel: an emote stays an (uncategorised) emote, no pattern either
    assert chan.category_of("KEKW", is_emote=True) == (C.UNCATEGORISED, MatchKind.EXACT)
    assert chan.category_of("KEKW") is None
    assert chan.category_of("Pog") == (C.SAD, MatchKind.EXACT)
    assert chan.category_of("lol") is None and base.category_of("LOL") == (C.FUNNY, MatchKind.EXACT)
    assert cats(chan.match("lets go")) == {} and cats(base.match("LETS GO")) == {C.HYPE: 2}
    assert chan.category_of("KEKWait") == (C.SAD, MatchKind.PATTERN)  # channel pattern first
    assert base.category_of("KEKWait") == (C.FUNNY, MatchKind.PATTERN)
    assert chan.is_bot("nightbot") and chan.is_bot("ChanBot") and not base.is_bot("chanbot")
    assert lex.transcript_hits("Somebody CLIP THAT now") == [("clip that", "clip")]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("[emotes]\nfunny = ['KEKW']\n", "version"),
        ("version = 1\n[emotes]\nfunnyy = ['KEKW']\n", "unknown category"),
        ("version = 1\n[emotes]\nfunny = ['KEKW']\nhype = ['KEKW']\n", "twice"),
        ("version = 1\n[patterns]\nfunny = ['^(']\n", "bad regex"),
        ("version = 1\n[patterns]\nfunny = ['kek']\n", "anchored"),
        ("version = 1\n[smileys]\nfunny = ['KEKW']\n", "unknown lexicon keys"),
        ("version = 1\n[emotes]\nfunny = 'KEKW'\n", "list"),
        ("version = 1\n[transcript]\nfunny = ['!!!']\n", "empty"),
        ("version = 1\n[channels.x]\nemote = {}\n", "unknown keys"),
        ("version = 1\n[emotes]\nignore = ['KEKW']\n", "unknown category"),  # channels only
        ("version = [", "TOML"),
    ],
)
def test_invalid_lexicons_are_rejected(text, error):
    with pytest.raises(LexiconError, match=error):
        Lexicon.from_toml(text)


def test_moment_type_mapping():
    assert to_moment_type(C.FUNNY, enough_votes=True) is MomentType.FUNNY
    assert to_moment_type(C.WHOLESOME, enough_votes=True) is MomentType.WHOLESOME
    assert to_moment_type(C.GREETING, enough_votes=True) is MomentType.MIXED
    assert to_moment_type(C.UNCATEGORISED, enough_votes=True) is MomentType.MIXED
    assert to_moment_type(None, enough_votes=True) is MomentType.MIXED
    assert to_moment_type(C.FUNNY, enough_votes=False) is MomentType.UNKNOWN
    # every emotion is a stored moment type
    assert {c.value for c in Category} - {"greeting", "uncategorised"} <= set(MomentType)
