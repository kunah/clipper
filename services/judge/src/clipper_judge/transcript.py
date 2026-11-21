"""Words and sentences of a clip's context, shared by the input builder and the resolver.

Words come from `clipper_core.transcripts.words()` (UTC, ownership applied). Sentences are built
here with the transcriber's rule (a sentence ends after a word ending with . ? !, before a pause
longer than 1 s, and before a word that would make it longer than 30 s), so the input builder and
the resolver number them alike: `S1`, `S2`, ... in time order over the context.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from clipper_core.timeline import Interval


class WordLike(Protocol):
    """A `clipper_core.transcripts.TranscriptWord` (or a test stub)."""

    @property
    def text(self) -> str: ...
    @property
    def start_utc(self) -> datetime: ...
    @property
    def end_utc(self) -> datetime: ...
    @property
    def confidence(self) -> float | None: ...


_CLOSERS = "\"')]}»”’"  # noqa: RUF001 - closing quotes/brackets after a full stop
_ABBREVIATIONS = frozenset(
    {"mr.", "mrs.", "ms.", "dr.", "st.", "vs.", "jr.", "sr.", "e.g.", "i.e."}
)


def _char_class(*ranges: tuple[int, int]) -> str:
    """A regex character class of code point ranges (built from numbers: no escapes in text)."""
    parts = (re.escape(chr(a)) + ("" if a == b else "-" + re.escape(chr(b))) for a, b in ranges)
    return "[" + "".join(parts) + "]"


_EDGE = "\"'.,!?;:()[]{}<>«»“”‘’-…*~"  # noqa: RUF001
_APOSTROPHES = str.maketrans("", "", "'’‘")  # noqa: RUF001


def ends_sentence(text: str) -> bool:
    """The word closes a sentence: ends with . ? ! (before closing quotes), not "Mr."."""
    t = text.strip().rstrip(_CLOSERS)
    return bool(t) and t[-1] in ".?!" and t.lower() not in _ABBREVIATIONS


def norm_word(text: str) -> str:
    """For matching a word the model names: lowercase, no apostrophes, no edge punctuation."""
    return text.translate(_APOSTROPHES).strip().strip(_EDGE).lower()


_CONTROL = re.compile(_char_class((0x00, 0x1F), (0x7F, 0x9F), (0x2028, 0x2029)))
# zero-width and bidi controls: invisible, or reordering how the text around them reads
_HIDDEN = re.compile(
    _char_class(
        (0x00AD, 0x00AD),  # soft hyphen
        (0x034F, 0x034F),  # combining grapheme joiner
        (0x061C, 0x061C),  # Arabic letter mark
        (0x180E, 0x180E),  # Mongolian vowel separator
        (0x200B, 0x200F),  # zero-width space/joiners, LRM, RLM
        (0x202A, 0x202E),  # bidi embeddings and overrides
        (0x2060, 0x2064),  # word joiner, invisible operators
        (0x2066, 0x2069),  # bidi isolates
        (0xFEFF, 0xFEFF),  # zero-width no-break space (BOM)
        (0xE0000, 0xE007F),  # tag characters (7TV/Chatterino's duplicate bypass)
    )
)
_SPACES = re.compile(r"\s+")


def clean_line(text: str) -> str:
    """Text on one line: control characters become spaces, zero-width and bidi characters go,
    single spaces, trimmed. For the plan's texts (title, hook, reasons): "I <3 this" stays."""
    return _SPACES.sub(" ", _CONTROL.sub(" ", _HIDDEN.sub("", text))).strip()


def clean_text(text: str) -> str:
    """Untrusted text for the model's input: `clean_line`, and angle brackets become single
    angle quotation marks (U+2039/U+203A), so stream content can't open or close a `<data>`
    fence."""
    return clean_line(text).replace("<", "‹").replace(">", "›")  # noqa: RUF001


def sort_words[W: WordLike](words: Iterable[W]) -> list[W]:
    return sorted(words, key=lambda w: (w.start_utc, w.end_utc, w.text))


@dataclass(frozen=True, slots=True)
class Sentence:
    id: str  # "S1", "S2", ... in time order over the context
    words: tuple[WordLike, ...]

    @property
    def start_utc(self) -> datetime:
        return self.words[0].start_utc

    @property
    def end_utc(self) -> datetime:
        return max(w.end_utc for w in self.words)

    @property
    def text(self) -> str:
        return " ".join(t for t in (clean_line(w.text) for w in self.words) if t)


def split_sentences(
    words: Sequence[WordLike], *, pause_s: float = 1.0, max_s: float = 30.0
) -> list[Sentence]:
    """Words (sorted by start) into sentences, the transcriber's rule (module docstring)."""
    groups: list[list[WordLike]] = []
    cur: list[WordLike] = []
    for w in words:
        if cur and (
            ends_sentence(cur[-1].text)
            or (w.start_utc - cur[-1].end_utc).total_seconds() > pause_s
            or (w.end_utc - cur[0].start_utc).total_seconds() > max_s
        ):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return [Sentence(f"S{i}", tuple(g)) for i, g in enumerate(groups, start=1)]


class Transcript:
    """The context's words (sorted) and sentences, with the lookups the resolver snaps with.

    `words` and `sentences` hold the words starting inside the context. The lookups (speech
    blocks, the previous word's end, the next word's start, a word by its start) see every word
    given, so a word cut by the context's edges is found too: callers pass words of a few seconds
    around the context (`JudgeInputs.words_interval`).

    `blocks`: the words merged where they overlap or touch (engines emit overlapping words, and
    touching ones leave no pause to cut in). A cut strictly inside a block would split speech;
    block edges never lie inside another block."""

    def __init__(
        self, words: Iterable[WordLike], context: Interval, *, pause_s: float, max_s: float
    ) -> None:
        every = tuple(w for w in sort_words(words) if w.end_utc >= w.start_utc)
        self.words: tuple[WordLike, ...] = tuple(
            w for w in every if context.start <= w.start_utc < context.end
        )
        self.sentences: tuple[Sentence, ...] = tuple(
            split_sentences(self.words, pause_s=pause_s, max_s=max_s)
        )
        self.by_id = {s.id: s for s in self.sentences}
        self._all = every
        self._starts = [w.start_utc for w in every]
        self._word_starts = sorted({w.start_utc for w in every})
        self._word_ends = sorted({w.end_utc for w in every})
        self._sentence_starts = sorted({s.start_utc for s in self.sentences})
        self._sentence_ends = sorted({s.end_utc for s in self.sentences})
        self.blocks: tuple[tuple[datetime, datetime], ...] = tuple(_speech_blocks(every))
        self._block_starts = [b[0] for b in self.blocks]

    # ------------------------------------------------------------------ lookups

    def block_at(self, t: datetime) -> tuple[datetime, datetime] | None:
        """The speech block with start < t < end: a cut at t would split speech."""
        i = bisect_right(self._block_starts, t) - 1
        if i >= 0 and self.blocks[i][0] < t < self.blocks[i][1]:
            return self.blocks[i]
        return None

    def word_at(self, start: datetime, tolerance: timedelta = timedelta(0)) -> WordLike | None:
        """The word starting at `start` (within `tolerance`), the first in time order."""
        i = bisect_left(self._starts, start - tolerance)
        if i < len(self._all) and self._all[i].start_utc <= start + tolerance:
            return self._all[i]
        return None

    def end_before(self, t: datetime) -> datetime | None:
        """The latest end of a word that starts before t (the previous word's end)."""
        hi = bisect_left(self._starts, t)
        return max((w.end_utc for w in self._all[:hi]), default=None)

    def start_after(self, t: datetime) -> datetime | None:
        """The earliest start of a word that starts after t (the next word's start)."""
        i = bisect_right(self._starts, t)
        return self._starts[i] if i < len(self._starts) else None

    def words_in(self, start: datetime, end: datetime) -> list[WordLike]:
        """Words starting in [start, end)."""
        return list(self._all[bisect_left(self._starts, start) : bisect_left(self._starts, end)])

    def sentence_starts(self) -> Sequence[datetime]:
        return self._sentence_starts

    def sentence_ends(self) -> Sequence[datetime]:
        return self._sentence_ends

    def word_starts(self) -> Sequence[datetime]:
        return self._word_starts

    def word_ends(self) -> Sequence[datetime]:
        return self._word_ends


def nearest(
    points: Sequence[datetime], t: datetime, window: timedelta, lo: datetime, hi: datetime
) -> datetime | None:
    """The point of the sorted `points` closest to t within `window` and [lo, hi]; a tie goes
    to the earlier one."""
    a = bisect_left(points, max(t - window, lo))
    b = bisect_right(points, min(t + window, hi))
    best: datetime | None = None
    for p in points[a:b]:
        if best is None or abs(p - t) < abs(best - t):
            best = p
    return best


def _speech_blocks(words: Sequence[WordLike]) -> list[tuple[datetime, datetime]]:
    """Words (sorted by start) merged where they overlap or touch."""
    blocks: list[tuple[datetime, datetime]] = []
    for w in words:
        if blocks and w.start_utc <= blocks[-1][1]:
            if w.end_utc > blocks[-1][1]:
                blocks[-1] = (blocks[-1][0], w.end_utc)
        else:
            blocks.append((w.start_utc, w.end_utc))
    return blocks
