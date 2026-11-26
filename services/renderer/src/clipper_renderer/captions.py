"""The ASS file burned into a render (DESIGN §20 "Captions"): word captions, the title band and a
text hook. Pure and deterministic (golden-file tested).

    subs = build_ass(words, plan, timemap, layout)
    subs.text          # the .ass file
    subs.dropped       # words that crossed a cut (not shown)

Words come from `transcripts.words(stream, [in, out))`; the plan's `word_fixes` replace a word's
text, its `emphasis` words get the accent colour and a larger size. Each word is mapped to output
time through the time map's runs: a word must lie inside one run (it may end up to 50 ms after
it), else it crossed a cut and is dropped; a word inside a teaser shows in the teaser and again in
the clip.

Pages: 1..`max_words` words, at most `max_chars` characters joined, a new page at a cut, after a
pause > 0.35 s or after punctuation. A page stays on screen from its first word's start until the
next page (when that follows within 0.35 s) or its last word's end, at least 0.4 s. One Dialogue
event per word slice (`\\an5\\pos`), the active word coloured and popped (`\\t` over `pop_ms`),
not `\\k` (that only sweeps a fill). `PlayResX/Y` = 1080x1920 (design pixels); libass scales to
the profile.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from clipper_core.contracts import EditPlan, HookMode
from clipper_core.timeline import ensure_utc
from clipper_renderer.layout import ResolvedLayout, TextAnchor
from clipper_renderer.profiles import DESIGN_H, DESIGN_W
from clipper_renderer.styles import (
    HOOK,
    HOOK_TEXT_S,
    LOOKS,
    TITLE,
    BoxLook,
    CaptionLook,
    ass_colour,
    ass_inline_colour,
    title_size,
)
from clipper_renderer.timemap import TimeMap

PAUSE_S = 0.35  # a longer pause starts a new page; a shorter one keeps the page up
MIN_PAGE_S = 0.4
WORD_SLACK_S = 0.05  # a word may end this far after its run (snapped out-points)
MATCH_S = 0.001  # word fixes and emphasis match a word's start within this
BREAK_AFTER = ".!?,;:"
STRIP_TRAILING = ".,;:"


class WordLike(Protocol):
    text: str
    start_utc: datetime
    end_utc: datetime


@dataclass(frozen=True, slots=True)
class OutWord:
    text: str  # as displayed
    start_s: float
    end_s: float
    run: int  # index into timemap.runs
    emphasis: str | None  # None | "highlight" | "big"
    breaks_after: bool  # the spoken text ended with punctuation


@dataclass(frozen=True, slots=True)
class Page:
    words: tuple[OutWord, ...]
    start_s: float
    end_s: float


@dataclass(frozen=True)
class Subtitles:
    text: str
    pages: tuple[Page, ...]
    dropped: int  # words that crossed a cut
    fonts: frozenset[str] = field(default_factory=frozenset)  # family names referenced


# Characters the bundled fonts don't have (emoji, other symbols, private use, controls) would make
# libass fall back to a system font (none in the container: blank boxes): they are left out.
DROP_CATEGORIES = frozenset({"So", "Cs", "Co", "Cn", "Cc", "Cf"})


def ass_text(value: str) -> str:
    """Text safe inside an ASS event: no override blocks, escapes, line breaks or characters
    outside the bundled fonts (emoji, symbols); runs of spaces collapsed."""
    for old, new in (("\\", "/"), ("{", "("), ("}", ")"), ("\r", " "), ("\n", " "), ("\t", " ")):
        value = value.replace(old, new)
    kept = "".join(ch for ch in value if unicodedata.category(ch) not in DROP_CATEGORIES)
    return " ".join(kept.split())


def ass_time(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _display(text: str, look: CaptionLook) -> str:
    t = ass_text(text)
    while t and t[-1] in STRIP_TRAILING:
        t = t[:-1]
    return t.upper() if look.uppercase else t


def _lookup(table: dict[datetime, str], t: datetime) -> str | None:
    if t in table:
        return table[t]
    for k, v in table.items():
        if abs((k - t).total_seconds()) <= MATCH_S:
            return v
    return None


def map_words(
    words: Sequence[WordLike], plan: EditPlan, timemap: TimeMap, look: CaptionLook
) -> tuple[list[OutWord], int]:
    fixes = {ensure_utc(f.start_utc): f.text for f in plan.word_fixes}
    emphasis = {ensure_utc(e.start_utc): e.kind for e in plan.emphasis}
    out: list[OutWord] = []
    dropped = 0
    for w in sorted(words, key=lambda w: (w.start_utc, w.end_utc)):
        start, end = ensure_utc(w.start_utc), ensure_utc(w.end_utc)
        spoken = _lookup(fixes, start) or w.text
        text = _display(spoken, look)
        if not text:
            continue
        runs = [
            (i, r)
            for i, r in enumerate(timemap.runs)
            if r.utc_from <= start and end <= r.utc_to + timedelta(seconds=WORD_SLACK_S)
        ]
        if not runs:
            # a word partly inside the clip (it crossed a cut or the clip's edge) is dropped;
            # words wholly outside every run are simply not part of the clip
            if any(r.utc_from < end and start < r.utc_to for r in timemap.runs):
                dropped += 1
            continue
        for i, r in runs:
            a = r.out(start)
            b = min(r.out(end), r.out_to_s)
            if b <= a:
                continue
            out.append(
                OutWord(
                    text,
                    a,
                    b,
                    i,
                    _lookup(emphasis, start),
                    ass_text(spoken)[-1:] in tuple(BREAK_AFTER),
                )
            )
    out.sort(key=lambda w: (w.start_s, w.run))
    return out, dropped


def paginate(words: Sequence[OutWord], look: CaptionLook, timemap: TimeMap) -> list[Page]:
    groups: list[list[OutWord]] = []
    for w in words:
        cur = groups[-1] if groups else None
        if (
            cur is None
            or w.run != cur[-1].run
            or w.start_s - cur[-1].end_s > PAUSE_S
            or cur[-1].breaks_after
            or len(cur) >= look.max_words
            or len(" ".join(x.text for x in [*cur, w])) > look.max_chars
        ):
            groups.append([w])
        else:
            cur.append(w)
    pages: list[Page] = []
    for i, g in enumerate(groups):
        start = g[0].start_s
        end = g[-1].end_s
        run_end = timemap.runs[g[0].run].out_to_s
        nxt = groups[i + 1] if i + 1 < len(groups) else None
        limit = run_end
        if nxt is not None and nxt[0].run == g[0].run:
            limit = min(limit, nxt[0].start_s)
            if nxt[0].start_s - end <= PAUSE_S:
                end = nxt[0].start_s
        end = min(max(end, start + MIN_PAGE_S), limit)
        if end > start:
            pages.append(Page(tuple(g), round(start, 6), round(end, 6)))
    return pages


def _word_markup(w: OutWord, look: CaptionLook, *, active: bool, spoken: bool) -> str:
    base = {"big": look.big_scale, "highlight": look.highlight_scale}.get(w.emphasis or "", 100)
    colour = look.accent if w.emphasis else None
    if active or (spoken and look.karaoke):
        colour = colour or look.active
    tags = []
    if colour:
        tags.append(f"\\1c{ass_inline_colour(colour)}")
    if base != 100 or (active and look.pop != 100):
        tags.append(f"\\fscx{base}\\fscy{base}")
    if active and look.pop != 100:
        grown = round(base * look.pop / 100)
        tags.append(f"\\t(0,{look.pop_ms},\\fscx{grown}\\fscy{grown})")
    if not tags:
        return w.text
    return "{" + "".join(tags) + "}" + w.text + "{\\r}"


def _pos(anchor: TextAnchor) -> str:
    return f"\\an{anchor.align}\\pos({anchor.x},{anchor.y})"


def _style(name: str, look: CaptionLook) -> str:
    return (
        f"Style: {name},{look.font},{look.size},{ass_colour(look.primary)},&H000000FF,"
        f"{ass_colour(look.outline_colour)},{ass_colour('#000000', look.shadow_alpha)},"
        f"0,0,0,0,100,100,{look.spacing:g},0,1,{look.outline:g},{look.shadow:g},5,80,80,0,1"
    )


def _box_style(name: str, look: BoxLook, size: int) -> str:
    return (
        f"Style: {name},{look.font},{size},{ass_colour(look.colour)},&H000000FF,"
        f"{ass_colour(look.box, look.box_alpha)},{ass_colour(look.box, look.box_alpha)},"
        f"0,0,0,0,100,100,0,0,3,{look.padding:g},0,5,{look.margin_x},{look.margin_x},0,1"
    )


def build_ass(
    words: Sequence[WordLike],
    plan: EditPlan,
    timemap: TimeMap,
    layout: ResolvedLayout,
    *,
    title: bool = True,
) -> Subtitles:
    look = LOOKS[plan.caption_style]
    out_words, dropped = map_words(words, plan, timemap, look)
    pages = paginate(out_words, look, timemap)
    duration = timemap.duration_s
    tsize = title_size(plan.title)
    lines = [
        "[Script Info]",
        "; clipper-renderer captions (DESIGN §20)",
        "ScriptType: v4.00+",
        f"PlayResX: {DESIGN_W}",
        f"PlayResY: {DESIGN_H}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour,"
        " BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle,"
        " BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        _style("Caption", look),
        _box_style("Title", TITLE, tsize),
        _box_style("Hook", HOOK, HOOK.size),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    fonts = {look.font}
    if title:
        fonts.add(TITLE.font)
        lines.append(
            f"Dialogue: 1,{ass_time(0)},{ass_time(duration)},Title,,0,0,0,,"
            f"{{{_pos(layout.title)}}}{ass_text(plan.title)}"
        )
    hook = plan.hook
    if hook.mode == HookMode.TEXT and hook.text:
        fonts.add(HOOK.font)
        lines.append(
            f"Dialogue: 2,{ass_time(0)},{ass_time(min(HOOK_TEXT_S, duration))},Hook,,0,0,0,,"
            f"{{{_pos(layout.hook)}}}{ass_text(hook.text)}"
        )
    for page in pages:
        n = len(page.words)
        for i, w in enumerate(page.words):
            a = page.start_s if i == 0 else w.start_s
            b = page.end_s if i == n - 1 else page.words[i + 1].start_s
            if ass_time(b) == ass_time(a) or b <= a:
                continue
            text = " ".join(
                _word_markup(x, look, active=(j == i), spoken=(j < i))
                for j, x in enumerate(page.words)
            )
            lines.append(
                f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Caption,,0,0,0,,"
                f"{{{_pos(layout.captions)}}}{text}"
            )
    return Subtitles("\n".join(lines) + "\n", tuple(pages), dropped, frozenset(fonts))
