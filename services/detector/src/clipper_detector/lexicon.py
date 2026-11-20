"""Emote/word lexicon: what a chat message reacts with (DESIGN §18.2).

The data lives in `clipper_detector/data/lexicon.toml` (in git, `version = N`; the version is part
of `detector_version`). It is applied to names at read time, so chat is never re-ingested when it
changes (§15). Sections, each `category = [entries]` (a name may appear only once per section):

- `[emotes]`: exact, case-sensitive emote names (Twitch, 7TV, BTTV, FFZ, emoji).
- `[words]`: case-insensitive words and phrases ("lmao", "no way"); apostrophes are ignored and
  stretched letters fold ("LMAOOOO" -> "lmao", "WWWW" -> "w", "YOOOO" -> "yoo").
- `[patterns]`: case-insensitive regexes for tokens nothing else matched, each anchored. One
  anchored at one end (`^kek`, `love$`) is an emote variant (KEKWait, peepoLove) and is tried
  only on emote-like tokens: a recognised emote, or a token with an inner capital; so plain words
  (glove, unhappy, microwave, telescope, lullaby, pogo) never match. One anchored at both ends
  (`^l+m+a+o+$`) is a word shape, tried on every token. A pattern match counts
  `DetectorParams.pattern_weight` (0.5), not 1.
- An emote is also found with punctuation around it ("monkaS," "PepeHands.").
- `[bots] logins`: chat bots (lowercase logins); `[transcript]`: phrases for the offline pass
  (categories plus `clip`).
- `[channels.<login>]`: per-channel `emotes`/`words`/`patterns` (same layout; a listed name takes
  that category in the channel, `ignore = [...]` removes a global entry there) and `bots`
  (added to the global list).

Categories: funny, hype, shock, tense, sad, cringe, wholesome (the *emotions*, which make a
message a "reaction"), greeting, and `uncategorised` for emotes the lexicon doesn't know (they
vote for the moment type but are no reaction). `to_moment_type` maps a winning category to a
stored `MomentType`: greeting/uncategorised become `mixed` (enough votes) or `unknown`.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from importlib import resources
from pathlib import Path
from typing import Any

from clipper_core.contracts import MomentType


class Category(StrEnum):
    FUNNY = "funny"
    HYPE = "hype"
    SHOCK = "shock"
    TENSE = "tense"
    SAD = "sad"
    CRINGE = "cringe"
    WHOLESOME = "wholesome"
    GREETING = "greeting"
    UNCATEGORISED = "uncategorised"  # an emote the lexicon doesn't know


EMOTIONS = frozenset(
    {
        Category.FUNNY,
        Category.HYPE,
        Category.SHOCK,
        Category.TENSE,
        Category.SAD,
        Category.CRINGE,
        Category.WHOLESOME,
    }
)
# Categories a lexicon section may list (uncategorised is implicit).
LISTED = frozenset(Category) - {Category.UNCATEGORISED}
IGNORE = "ignore"  # channel overrides only: drop a global entry in that channel
CLIP = "clip"  # transcript phrases only: the streamer asks for a clip


def to_moment_type(category: Category | None, *, enough_votes: bool) -> MomentType:
    """The stored moment type for a winning category (None = no category reached the share)."""
    if not enough_votes:
        return MomentType.UNKNOWN
    if category is not None and category in EMOTIONS:
        return MomentType(category.value)
    return MomentType.MIXED


class MatchKind(IntEnum):
    PATTERN = 1  # a `[patterns]` regex (weighted)
    EXACT = 2  # an `[emotes]` name, a `[words]` word/phrase, or an unknown emote


class LexiconError(ValueError):
    pass


# --------------------------------------------------------------------------- normalisation

# Invisible characters chat clients add to get around Twitch's duplicate-message filter
# (Chatterino/7TV append U+E0000), zero-width joiners/spaces, the emoji variation selector.
_INVISIBLE = re.compile("[\U000e0000-\U000e007f\u200b-\u200d\u2060\u034f\ufe0f]")
_EDGE_PUNCT = '.,!?;:"()[]{}<>~*_-\u2026/\\|`'
_APOSTROPHES = str.maketrans("", "", "'\u2019\u2018")
# Emoticons typed as text (":D", ":O", ";)", ":/", "xD", "D:") and their lone letters (the D of
# ":D" typed apart): no free text, like emotes.
EMOTICON = re.compile(r"[:;=]-?[a-zA-Z0-9()\[\]|/\\*$@]{1,2}|[xX][dD]+|D:|[dDoOpPxX]")
_RUN3 = re.compile(r"(.)\1{2,}")
_RUN2 = re.compile(r"(.)\1+")


def clean_text(text: str) -> str:
    """Text without the invisible characters (for tokens, duplicates)."""
    return _INVISIBLE.sub("", text)


def tokens_of(text: str) -> list[str]:
    return clean_text(text).split()


def normalise_word(token: str) -> str:
    """Lowercase, no apostrophes, no punctuation at the ends."""
    return token.translate(_APOSTROPHES).strip(_EDGE_PUNCT).lower()


def _word_forms(word: str) -> tuple[str, ...]:
    """The word, then stretched letters folded to two, then to one ("lmaooo" -> lmaoo, lmao)."""
    two = _RUN3.sub(r"\1\1", word)
    one = _RUN2.sub(r"\1", word)
    return tuple(dict.fromkeys((word, two, one)))


def _phrase_key(words: Iterable[str]) -> tuple[str, ...]:
    return tuple(_RUN2.sub(r"\1", w) for w in words)


# --------------------------------------------------------------------------- matching


@dataclass(frozen=True, slots=True)
class MessageMatch:
    """What one message reacts with: the best match kind per category."""

    categories: Mapping[Category, MatchKind]
    tokens: int
    # no free text: every token is an emote (known or not), a lexicon word, an emoticon or only
    # punctuation ("KEKW KEKW", "LMAO", "hi", "Kappa", ":D", "???"). Mass messages like these are
    # reactions (or the
    # greeting rule's business), so the duplicate share ignores them; a copypasta with an emote
    # inside still counts
    textless: bool

    @property
    def reaction(self) -> bool:
        return any(c in EMOTIONS for c in self.categories)

    @property
    def greeting(self) -> bool:
        return Category.GREETING in self.categories

    def weight(self, category: Category, pattern_weight: float) -> float:
        kind = self.categories.get(category)
        if kind is None:
            return 0.0
        return 1.0 if kind is MatchKind.EXACT else pattern_weight

    def reaction_weight(self, pattern_weight: float) -> float:
        """1 for an exact emotion match, pattern_weight for a pattern-only one, else 0."""
        return max((self.weight(c, pattern_weight) for c in EMOTIONS), default=0.0)

    def greeting_weight(self, pattern_weight: float) -> float:
        return self.weight(Category.GREETING, pattern_weight)


@dataclass(frozen=True, slots=True)
class _Tables:
    emotes: Mapping[str, Category]
    words: Mapping[str, Category]  # single normalised words
    phrases: Mapping[tuple[str, ...], Category]  # multi-word, folded to one letter per run
    patterns: tuple[tuple[re.Pattern[str], Category, bool], ...]  # bool: a whole-word shape
    ignored: frozenset[str]  # emote names and normalised words removed in this channel


def emote_like(token: str) -> bool:
    """A token shaped like an emote name: a lowercase letter and a capital after the first
    character (KEKWait, peepoLove, PogTasty, monkaHmm). Plain words, also Capitalised or ALL CAPS
    (glove, Lullaby, UNHAPPY), are not."""
    return any(c.islower() for c in token) and any(c.isupper() for c in token[1:])


class ChannelLexicon:
    """The lexicon as one channel sees it (global entries + that channel's overrides)."""

    def __init__(self, tables: _Tables, bots: frozenset[str], version: int) -> None:
        self._t = tables
        self.bots = bots
        self.version = version
        self._max_phrase = max((len(p) for p in tables.phrases), default=0)

    def is_bot(self, login: str | None) -> bool:
        return bool(login) and login.lower() in self.bots

    def category_of(
        self, token: str, *, is_emote: bool = False
    ) -> tuple[Category, MatchKind] | None:
        """One token: exact emote (also without punctuation around it), word (stretched forms
        too), emoji, a pattern, else an unknown emote (`uncategorised`) or nothing."""
        t = self._t
        token = clean_text(token)
        if not token:
            return None
        word = normalise_word(token)
        bare = token.strip(_EDGE_PUNCT)  # "monkaS," -> "monkaS" (case kept)
        if token in t.ignored or bare in t.ignored or word in t.ignored:
            # removed in this channel: no pattern either
            return (Category.UNCATEGORISED, MatchKind.EXACT) if is_emote else None
        if (cat := t.emotes.get(token)) is not None:
            return cat, MatchKind.EXACT
        if bare and bare != token and (cat := t.emotes.get(bare)) is not None:
            return cat, MatchKind.EXACT
        if word:
            for form in _word_forms(word):
                if (cat := t.words.get(form)) is not None:
                    return cat, MatchKind.EXACT
        if not is_emote and not any(ch.isalnum() for ch in token):
            # emoji runs ("😂😂😂") and symbols: each distinct character as an emote name
            for ch in dict.fromkeys(token):
                if (cat := t.emotes.get(ch)) is not None:
                    return cat, MatchKind.EXACT
            return None
        variant = is_emote or emote_like(bare)
        for pattern, cat, whole_word in t.patterns:
            if (whole_word or variant) and pattern.search(bare):
                return cat, MatchKind.PATTERN
        return (Category.UNCATEGORISED, MatchKind.EXACT) if is_emote else None

    def match(self, text: str, emote_names: Iterable[str] = ()) -> MessageMatch:
        """Categories of one message. `emote_names`: the message's recognised emotes (from
        `chat_messages.emotes`); a name there that the lexicon doesn't know is `uncategorised`.
        Emote names in the text are found without it too (an emote set that failed to load)."""
        emotes = set(emote_names)
        tokens = tokens_of(text)
        found: dict[Category, MatchKind] = {}
        known = [False] * len(tokens)

        def add(cat: Category, kind: MatchKind) -> None:
            if kind > found.get(cat, 0):
                found[cat] = kind

        for i, token in enumerate(tokens):
            hit = self.category_of(token, is_emote=token in emotes)
            if hit is not None:
                add(*hit)
            known[i] = (
                hit is not None
                or not any(ch.isalnum() for ch in token)
                or EMOTICON.fullmatch(token) is not None
            )
        if self._max_phrase > 1 and len(tokens) > 1:
            keys = _phrase_key(normalise_word(tok) for tok in tokens)
            for size in range(2, min(self._max_phrase, len(keys)) + 1):
                for start in range(len(keys) - size + 1):
                    cat = self._t.phrases.get(keys[start : start + size])
                    if cat is not None:
                        add(cat, MatchKind.EXACT)
                        known[start : start + size] = [True] * size
        return MessageMatch(found, len(tokens), bool(tokens) and all(known))


# --------------------------------------------------------------------------- loading


class Lexicon:
    """A loaded `lexicon.toml`. `for_channel(login)` gives the merged view of one channel."""

    def __init__(
        self,
        version: int,
        tables: _Tables,
        bots: frozenset[str],
        transcript: Mapping[str, str],
        channels: Mapping[str, tuple[_Tables, frozenset[str]]],
        digest: str = "",
    ) -> None:
        self.version = version
        self.digest = digest  # 6 hex chars of the file's sha256 (an edit without a bump shows)
        self.transcript = dict(transcript)  # normalised phrase -> category or "clip"
        self._global = ChannelLexicon(tables, bots, version)
        self._channels = {
            login: ChannelLexicon(t, b, version) for login, (t, b) in channels.items()
        }

    @property
    def channels(self) -> frozenset[str]:
        return frozenset(self._channels)

    @property
    def tag(self) -> str:
        """`<version>.<digest>`, the lexicon part of `detector_version`."""
        return f"{self.version}.{self.digest}"

    def for_channel(self, login: str | None) -> ChannelLexicon:
        if login is None:
            return self._global
        return self._channels.get(login.lower(), self._global)

    def transcript_hits(self, text: str) -> list[tuple[str, str]]:
        """Transcript phrases in a sentence (offline pass): `[(phrase, category or "clip")]`."""
        words = " " + " ".join(normalise_word(t) for t in tokens_of(text)) + " "
        return [(p, c) for p, c in self.transcript.items() if f" {p} " in words]

    # ------------------------------------------------------------------ constructors

    @classmethod
    def load(cls, path: str | Path | None = None) -> Lexicon:
        """The packaged lexicon, or another file in the same format."""
        if path is None:
            data = resources.files("clipper_detector").joinpath("data/lexicon.toml").read_bytes()
            return cls.from_toml(data.decode("utf-8"))
        return cls.from_toml(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def from_toml(cls, text: str) -> Lexicon:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise LexiconError(f"lexicon is not valid TOML: {exc}") from exc
        return cls.from_mapping(data, digest=hashlib.sha256(text.encode("utf-8")).hexdigest()[:6])

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, digest: str | None = None) -> Lexicon:
        """`digest`: of the source text; default: of the mapping's canonical JSON."""
        if digest is None:
            canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:6]
        known = {"version", "emotes", "words", "patterns", "bots", "transcript", "channels"}
        if unknown := set(data) - known:
            raise LexiconError(f"unknown lexicon keys: {sorted(unknown)}")
        version = data.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise LexiconError("lexicon needs `version = N` (an integer >= 1)")

        emotes = _section(data.get("emotes"), "emotes", LISTED)
        words = _section(data.get("words"), "words", LISTED)
        patterns = _section(data.get("patterns"), "patterns", LISTED)
        bots_table = data.get("bots") or {}
        if not isinstance(bots_table, Mapping) or set(bots_table) - {"logins"}:
            raise LexiconError("`[bots]` takes only `logins = [...]`")
        bots = frozenset(_strings(bots_table.get("logins", []), "bots.logins", lower=True))
        transcript = _transcript(data.get("transcript"))
        base = _tables(emotes, words, patterns, "global")

        channels: dict[str, tuple[_Tables, frozenset[str]]] = {}
        for login, table in (data.get("channels") or {}).items():
            where = f"channels.{login}"
            if not isinstance(table, Mapping):
                raise LexiconError(f"`[{where}]` must be a table")
            if unknown := set(table) - {"emotes", "words", "patterns", "bots"}:
                raise LexiconError(f"unknown keys in `[{where}]`: {sorted(unknown)}")
            allowed = LISTED | {IGNORE}
            ch_emotes = _section(table.get("emotes"), f"{where}.emotes", allowed)
            ch_words = _section(table.get("words"), f"{where}.words", allowed)
            ch_patterns = _section(table.get("patterns"), f"{where}.patterns", LISTED)
            ch_bots = _strings(table.get("bots", []), f"{where}.bots", lower=True)
            merged = _overlay(base, ch_emotes, ch_words, ch_patterns, where)
            channels[login.lower()] = (merged, bots | frozenset(ch_bots))
        return cls(version, base, bots, transcript, channels, digest)


def _strings(value: Any, where: str, *, lower: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise LexiconError(f"`{where}` must be a list of non-empty strings")
    return [v.strip().lower() if lower else v.strip() for v in value]


def _section(value: Any, where: str, allowed: frozenset[str]) -> dict[str, str]:
    """`category = [entries]` -> {entry: category}; an entry may appear only once."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise LexiconError(f"`[{where}]` must be a table of `category = [entries]`")
    out: dict[str, str] = {}
    for category, entries in value.items():
        if category not in allowed:
            raise LexiconError(f"unknown category {category!r} in `[{where}]`")
        for entry in _strings(entries, f"{where}.{category}"):
            if entry in out:
                raise LexiconError(f"{entry!r} is listed twice in `[{where}]`")
            out[entry] = category
    return out


def _split_words(words: Mapping[str, str], where: str):
    single: dict[str, str] = {}
    phrases: dict[tuple[str, ...], str] = {}
    for entry, category in words.items():
        parts = [normalise_word(p) for p in tokens_of(entry)]
        if not parts or not all(parts):
            raise LexiconError(f"word {entry!r} in `[{where}]` is empty after normalising")
        if len(parts) == 1:
            single[parts[0]] = category
        else:
            phrases[_phrase_key(parts)] = category
    return single, phrases


def _compile(
    patterns: Mapping[str, str], where: str
) -> list[tuple[re.Pattern[str], Category, bool]]:
    out = []
    for regex, category in patterns.items():
        starts = regex.startswith("^")
        ends = regex.endswith("$") and not regex.endswith("\\$")
        if not (starts or ends):
            raise LexiconError(f"pattern {regex!r} in `[{where}]` must be anchored (^... or ...$)")
        try:
            out.append((re.compile(regex, re.IGNORECASE), Category(category), starts and ends))
        except re.error as exc:
            raise LexiconError(f"bad regex {regex!r} in `[{where}]`: {exc}") from exc
    return out


def _tables(
    emotes: Mapping[str, str], words: Mapping[str, str], patterns: Mapping[str, str], where: str
) -> _Tables:
    single, phrases = _split_words(words, f"{where} words")
    return _Tables(
        emotes={clean_text(k): Category(v) for k, v in emotes.items()},
        words={k: Category(v) for k, v in single.items()},
        phrases={k: Category(v) for k, v in phrases.items()},
        patterns=tuple(_compile(patterns, f"{where} patterns")),
        ignored=frozenset(),
    )


def _overlay(
    base: _Tables,
    emotes: Mapping[str, str],
    words: Mapping[str, str],
    patterns: Mapping[str, str],
    where: str,
) -> _Tables:
    """A channel's overrides on top of the global tables (channel patterns are tried first)."""
    ch_emotes = dict(base.emotes)
    ignored: set[str] = set()
    for name, category in emotes.items():
        name = clean_text(name)
        ch_emotes.pop(name, None)
        if category == IGNORE:
            ignored.add(name)
        else:
            ch_emotes[name] = Category(category)
    ch_words = dict(base.words)
    ch_phrases = dict(base.phrases)
    kept = {k: v for k, v in words.items() if v != IGNORE}
    for entry in (k for k, v in words.items() if v == IGNORE):
        parts = [normalise_word(p) for p in tokens_of(entry)]
        if len(parts) == 1:
            ch_words.pop(parts[0], None)
            ignored.add(parts[0])
        else:
            ch_phrases.pop(_phrase_key(parts), None)
    single, phrases = _split_words(kept, f"{where}.words")
    ch_words.update({k: Category(v) for k, v in single.items()})
    ch_phrases.update({k: Category(v) for k, v in phrases.items()})
    return _Tables(
        emotes=ch_emotes,
        words=ch_words,
        phrases=ch_phrases,
        patterns=(*_compile(patterns, f"{where}.patterns"), *base.patterns),
        ignored=frozenset(ignored),
    )


def _transcript(value: Any) -> dict[str, str]:
    raw = _section(value, "transcript", LISTED | {CLIP})
    out: dict[str, str] = {}
    for phrase, category in raw.items():
        key = " ".join(normalise_word(t) for t in tokens_of(phrase))
        if not key.strip():
            raise LexiconError(f"transcript phrase {phrase!r} is empty after normalising")
        out[key] = category
    return out
