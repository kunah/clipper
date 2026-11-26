"""Caption, title and hook looks (`EditPlan.caption_style` -> `CaptionLook`). Pure data.

Everything here is a PROPOSED taste default (DESIGN §20, "sensible defaults until the user signs
off 5 real renders"): fonts (bundled Montserrat, OFL), sizes in design pixels (1080x1920),
colours, the pop. Fonts are referenced by their family name (name ID 1) as in `fonts/README.md`.
"""

from __future__ import annotations

from dataclasses import dataclass

from clipper_core.contracts import CaptionStyle


def ass_colour(rgb: str, alpha: int = 0) -> str:
    """'#RRGGBB' (+ alpha 0 = opaque .. 255 = invisible) -> ASS '&HAABBGGRR'."""
    r, g, b = rgb[1:3], rgb[3:5], rgb[5:7]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_inline_colour(rgb: str) -> str:
    """'#RRGGBB' -> the `\\1c` form '&HBBGGRR&'."""
    return f"&H{rgb[5:7]}{rgb[3:5]}{rgb[1:3]}&".upper()


@dataclass(frozen=True, slots=True)
class CaptionLook:
    font: str
    size: int
    primary: str  # '#RRGGBB'
    active: str  # the word being spoken
    accent: str  # emphasis words (the plan's `emphasis`)
    outline: float
    outline_colour: str = "#000000"
    shadow: float = 0.0
    shadow_alpha: int = 0x80
    uppercase: bool = True
    pop: int = 118  # the active word grows to this scale (%) over `pop_ms`; 100 = no pop
    pop_ms: int = 80
    karaoke: bool = False  # words already spoken on the page keep the active colour
    highlight_scale: int = 110  # emphasis kind "highlight"
    big_scale: int = 135  # emphasis kind "big"
    spacing: float = 0.0
    max_words: int = 3  # per page
    max_chars: int = 16  # per page (joined with spaces)


@dataclass(frozen=True, slots=True)
class BoxLook:
    """A text in an opaque box (ASS BorderStyle 3): the title band, a text hook."""

    font: str
    size: int
    colour: str
    box: str
    box_alpha: int
    padding: float  # the box's margin around the text (ASS Outline with BorderStyle 3)
    margin_x: int = 90


LOOKS: dict[CaptionStyle, CaptionLook] = {
    CaptionStyle.BOLD_POP: CaptionLook(
        font="Montserrat Black",
        size=92,
        primary="#FFFFFF",
        active="#FFD21F",
        accent="#3DF06B",
        outline=7,
    ),
    CaptionStyle.CLEAN: CaptionLook(
        font="Montserrat SemiBold",
        size=74,
        primary="#FFFFFF",
        active="#FFE27A",
        accent="#7FD4FF",
        outline=3,
        shadow=3,
        uppercase=False,
        pop=100,
        highlight_scale=106,
        big_scale=120,
    ),
    CaptionStyle.KARAOKE: CaptionLook(
        font="Montserrat ExtraBold",
        size=84,
        primary="#FFFFFF",
        active="#FFD21F",
        accent="#FF5A5A",
        outline=6,
        pop=108,
        karaoke=True,
    ),
}

TITLE = BoxLook(
    font="Montserrat ExtraBold",
    size=62,
    colour="#FFFFFF",
    box="#000000",
    box_alpha=0x50,
    padding=16,
)
HOOK = BoxLook(
    font="Montserrat Black",
    size=80,
    colour="#FFFFFF",
    box="#E5243B",
    box_alpha=0x00,
    padding=18,
)
HOOK_TEXT_S = 2.6  # a text hook stays this long (at most the clip)


def title_size(title: str) -> int:
    """Long titles get a smaller font (at most ~3 lines in the band)."""
    n = len(title)
    return TITLE.size if n <= 26 else 54 if n <= 52 else 46


def fonts_used() -> set[str]:
    return {look.font for look in LOOKS.values()} | {TITLE.font, HOOK.font}
