"""Twitch IRC message parsing (IRCv3 tags, Twitch-specific emote/badge tags)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field

_TAG_ESCAPES = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}


@dataclass(frozen=True, slots=True)
class IrcMessage:
    command: str
    params: tuple[str, ...] = ()
    tags: Mapping[str, str] = field(default_factory=dict)
    prefix: str | None = None

    @property
    def channel(self) -> str | None:
        """Channel login without '#', for channel-scoped commands."""
        if self.params and self.params[0].startswith("#"):
            return self.params[0][1:].lower()
        return None

    @property
    def text(self) -> str | None:
        """The trailing parameter (message text), if any."""
        return self.params[-1] if len(self.params) >= 2 else None

    @property
    def nick(self) -> str | None:
        if self.prefix and "!" in self.prefix:
            return self.prefix.split("!", 1)[0]
        return None


def unescape_tag(value: str) -> str:
    out: list[str] = []
    chars = iter(value)
    for ch in chars:
        if ch != "\\":
            out.append(ch)
            continue
        nxt = next(chars, None)
        if nxt is None:
            break  # a trailing lone backslash is dropped
        out.append(_TAG_ESCAPES.get(nxt, nxt))
    return "".join(out)


def parse(line: str) -> IrcMessage:
    rest = line.rstrip("\r\n")
    tags: dict[str, str] = {}
    if rest.startswith("@"):
        raw, _, rest = rest[1:].partition(" ")
        for item in raw.split(";"):
            key, _, value = item.partition("=")
            tags[key] = unescape_tag(value)
    prefix = None
    if rest.startswith(":"):
        prefix, _, rest = rest[1:].partition(" ")
    trailing: str | None = None
    if rest.startswith(":"):
        rest, trailing = "", rest[1:]
    elif " :" in rest:
        rest, _, trailing = rest.partition(" :")
    parts = rest.split()
    if trailing is not None:
        parts.append(trailing)
    if not parts:
        raise ValueError(f"no command in IRC line: {line!r}")
    return IrcMessage(command=parts[0].upper(), params=tuple(parts[1:]), tags=tags, prefix=prefix)


def strip_action(text: str) -> tuple[str, bool]:
    """'/me' messages arrive as '\\x01ACTION text\\x01'."""
    if text.startswith("\x01ACTION ") and text.endswith("\x01"):
        return text[8:-1], True
    return text, False


def parse_badges(value: str | None) -> dict[str, str]:
    """'subscriber/12,premium/1' -> {'subscriber': '12', 'premium': '1'}"""
    badges: dict[str, str] = {}
    for item in filter(None, (value or "").split(",")):
        name, _, version = item.partition("/")
        badges[name] = version
    return badges


def parse_emotes(value: str | None, text: str) -> list[tuple[str, str, int]]:
    """Twitch `emotes` tag -> [(emote_id, name, count)].

    '25:0-4,12-16/1902:6-10' with positions in Unicode code points (= Python str indices).
    """
    result: list[tuple[str, str, int]] = []
    for item in filter(None, (value or "").split("/")):
        emote_id, _, ranges = item.partition(":")
        spans = [r.split("-", 1) for r in ranges.split(",") if "-" in r]
        if not spans:
            continue
        start, end = int(spans[0][0]), int(spans[0][1])
        result.append((emote_id, text[start : end + 1], len(spans)))
    return result


def count_tokens(text: str, names: Mapping[str, str]) -> Counter[str]:
    """Count whitespace-separated tokens that are known emote names."""
    return Counter(token for token in text.split() if token in names)
