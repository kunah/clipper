"""Layouts (DESIGN §20 "Layouts"): `layouts.toml` and the geometry of the three layouts. Pure.

    config, sha = load_layouts(path)        # a missing file = no channel configured
    lay = resolve(config, login, category, plan.layout, source=(1920, 1080), profile=profile)

Rects in the config are normalised source rects (0..1), so 720p and 1080p sources share one
config; `resolve()` returns pixel rects: each panel's source crop and where it lands on the
profile's canvas, plus where the title, captions and a text hook go (design pixels, 1080x1920:
the ASS script's PlayRes).

Kinds:
- `facecam_gameplay`: the facecam rect (cropped to the top panel's aspect, centred) -> top panel
  W x round(H * split); the gameplay (the full source height, cropped at `gameplay_focus_x`) ->
  the bottom panel. Needs a facecam rect.
- `full_crop`: a 9:16 crop at `focus_x` -> the whole canvas (Just Chatting, cam-only scenes).
- `blurred_fit`: the whole frame, full width, over a cover-scaled, blurred, darkened copy; title
  above, captions below. The layout when nothing is configured.

Choice: the plan's hint wins where the config allows it (`facecam_gameplay` only with a facecam
rect, else the channel's layout with a `layout_fallback` warning); `auto` -> the category's
layout, else the channel's `default`, else `facecam_gameplay` when a facecam rect exists, else the
file's `default` (`blurred_fit`).

The numbers marked PROPOSED are taste defaults (DESIGN §20) until the user signs off renders.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from clipper_core.contracts import LayoutHint, LayoutKind
from clipper_renderer.profiles import DESIGN_W, Profile

# PROPOSED taste defaults (design pixels, 1080x1920)
SPLIT = 760 / 1920  # facecam_gameplay: top panel share
FIT_Y = 520  # blurred_fit: top of the full-width video
CAPTION_Y = 1300  # caption centre; the platform safe zone is about y 1150-1450, x 80-1000
HOOK_Y = 1000  # a text hook's centre
TITLE_TOP_Y = 230  # full_crop: the title's top edge (below the platforms' top UI)
BLUR_DARKEN = -0.18  # blurred_fit background: brightness offset (eq)


class LayoutError(ValueError):
    """`layouts.toml` can't be read or is invalid."""


class _Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NormRect(_Config):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _inside(self) -> NormRect:
        if self.x + self.w > 1 + 1e-9 or self.y + self.h > 1 + 1e-9:
            raise ValueError("the rect must lie inside the frame (x + w <= 1, y + h <= 1)")
        return self


ConfiguredKind = Literal["facecam_gameplay", "full_crop", "blurred_fit"]


class CategoryLayout(_Config):
    layout: ConfiguredKind | None = None
    facecam: NormRect | None = None
    focus_x: float | None = Field(default=None, ge=0, le=1)
    gameplay_focus_x: float | None = Field(default=None, ge=0, le=1)


class ChannelLayout(_Config):
    default: ConfiguredKind | None = None
    facecam: NormRect | None = None
    split: float | None = Field(default=None, ge=0.2, le=0.7)
    focus_x: float = Field(default=0.5, ge=0, le=1)
    gameplay_focus_x: float = Field(default=0.5, ge=0, le=1)
    categories: dict[str, CategoryLayout] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _facecam_for_its_layout(self) -> ChannelLayout:
        if self.default == "facecam_gameplay" and self.facecam is None:
            raise ValueError("default = 'facecam_gameplay' needs a facecam rect")
        for name, cat in self.categories.items():
            if cat.layout == "facecam_gameplay" and (cat.facecam or self.facecam) is None:
                raise ValueError(f"category {name!r}: facecam_gameplay needs a facecam rect")
        return self

    def category(self, name: str | None) -> CategoryLayout | None:
        if not name:
            return None
        wanted = name.casefold()
        for key, cat in self.categories.items():
            if key.casefold() == wanted:
                return cat
        return None


class LayoutConfig(_Config):
    version: Literal[1] = 1
    default: ConfiguredKind = "blurred_fit"
    split: float = Field(default=SPLIT, ge=0.2, le=0.7)
    channels: dict[str, ChannelLayout] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _default_needs_no_rect(self) -> LayoutConfig:
        if self.default == "facecam_gameplay":
            raise ValueError("the file's default can't be facecam_gameplay (no facecam rect)")
        return self

    def channel(self, login: str | None) -> ChannelLayout | None:
        if not login:
            return None
        wanted = login.casefold()
        for key, chan in self.channels.items():
            if key.casefold() == wanted:
                return chan
        return None


def load_layouts(path: Path | str | None) -> tuple[LayoutConfig, str | None]:
    """The config and the file's sha256 (None: no file, every channel gets the default)."""
    if path is None or not Path(path).exists():
        return LayoutConfig(), None
    raw = Path(path).read_bytes()
    try:
        config = LayoutConfig.model_validate(tomllib.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise LayoutError(f"{path}: {exc}") from exc
    return config, hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------------------- geometry


@dataclass(frozen=True, slots=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    def as_list(self) -> list[int]:
        return [self.x, self.y, self.w, self.h]


@dataclass(frozen=True, slots=True)
class Panel:
    name: Literal["face", "gameplay", "main"]
    src: Rect  # crop of the source frame (source pixels)
    dst: Rect  # where it lands on the canvas (profile pixels)
    focus_x: float = 0.5  # where a zoom centres horizontally (0..1 of src)


@dataclass(frozen=True, slots=True)
class TextAnchor:
    """An ASS position in design pixels: `\\an{align}\\pos(x, y)`."""

    align: int
    x: int
    y: int


@dataclass(frozen=True)
class ResolvedLayout:
    kind: LayoutKind
    canvas: tuple[int, int]  # profile pixels
    source: tuple[int, int]
    panels: tuple[Panel, ...]
    background: bool  # blurred_fit: a blurred cover copy behind the panels
    title: TextAnchor
    captions: TextAnchor
    hook: TextAnchor
    warnings: tuple[str, ...] = field(default=())

    def panel(self, name: str) -> Panel | None:
        return next((p for p in self.panels if p.name == name), None)

    def zoom_panel(self, target: str) -> Panel:
        """The panel a zoom target punches into: face -> the facecam panel, gameplay and center
        -> the gameplay panel; layouts with one panel use it for every target."""
        if target == "face":
            return self.panel("face") or self.panel("main") or self.panels[0]
        return self.panel("gameplay") or self.panel("main") or self.panels[0]

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "canvas": list(self.canvas),
            "source": list(self.source),
            "panels": [
                {"name": p.name, "src": p.src.as_list(), "dst": p.dst.as_list()}
                for p in self.panels
            ],
            "background": self.background,
            "title": [self.title.align, self.title.x, self.title.y],
            "captions": [self.captions.align, self.captions.x, self.captions.y],
            "hook": [self.hook.align, self.hook.x, self.hook.y],
        }


def _even(v: float) -> int:
    return max(2, round(v / 2.0) * 2)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def crop_to_aspect(r: Rect, aspect: float, focus_x: float = 0.5) -> Rect:
    """The largest rect of aspect `aspect` (w / h) inside `r`: centred vertically, at `focus_x`
    horizontally (0..1 of `r`); even sizes and offsets."""
    if r.w / r.h > aspect:
        w = min(_even(r.h * aspect), r.w - r.w % 2)
        h = r.h - r.h % 2
        x = r.x + _clamp(focus_x * r.w - w / 2, 0, r.w - w)
        y = r.y
    else:
        w = r.w - r.w % 2
        h = min(_even(r.w / aspect), r.h - r.h % 2)
        x = r.x
        y = r.y + (r.h - h) / 2
    return Rect(int(x) // 2 * 2, int(y) // 2 * 2, w, h)


def zoom_rect(r: Rect, scale: float, focus_x: float = 0.5) -> Rect:
    """`r` shrunk by `scale` (a punch-in), centred at `focus_x` horizontally and the middle
    vertically; even sizes."""
    w, h = _even(r.w / scale), _even(r.h / scale)
    x = r.x + _clamp(focus_x * r.w - w / 2, 0, r.w - w)
    y = r.y + (r.h - h) / 2
    return Rect(int(x) // 2 * 2, int(y) // 2 * 2, w, h)


def _first(*values: float | None) -> float:
    return next(v for v in values if v is not None)


def _px(n: NormRect, source: tuple[int, int]) -> Rect:
    sw, sh = source
    x, y = round(n.x * sw), round(n.y * sh)
    w = min(round(n.w * sw), sw - x)
    h = min(round(n.h * sh), sh - y)
    return Rect(x, y, max(w, 2), max(h, 2))


def resolve(
    config: LayoutConfig,
    login: str | None,
    category: str | None,
    hint: LayoutHint,
    *,
    source: tuple[int, int],
    profile: Profile,
) -> ResolvedLayout:
    chan = config.channel(login)
    cat = chan.category(category) if chan else None
    facecam = (cat.facecam if cat else None) or (chan.facecam if chan else None)
    configured = (
        (cat.layout if cat else None)
        or (chan.default if chan else None)
        or ("facecam_gameplay" if facecam is not None else None)
        or config.default
    )
    warnings: list[str] = []
    kind = LayoutKind(configured) if hint.kind == LayoutKind.AUTO else hint.kind
    if kind == LayoutKind.FACECAM_GAMEPLAY and facecam is None:
        fallback = LayoutKind(configured if configured != "facecam_gameplay" else "blurred_fit")
        warnings.append(f"layout_fallback: no facecam rect for {login!r}, {fallback.value} instead")
        kind = fallback
    focus_x = _first(
        hint.focus_x, cat.focus_x if cat else None, chan.focus_x if chan else None, 0.5
    )
    gameplay_focus_x = _first(
        cat.gameplay_focus_x if cat else None, chan.gameplay_focus_x if chan else None, 0.5
    )
    split = _first(chan.split if chan else None, config.split)
    return _geometry(
        kind,
        source=source,
        profile=profile,
        facecam=facecam,
        focus_x=focus_x,
        gameplay_focus_x=gameplay_focus_x,
        split=split,
        warnings=tuple(warnings),
    )


def _geometry(
    kind: LayoutKind,
    *,
    source: tuple[int, int],
    profile: Profile,
    facecam: NormRect | None,
    focus_x: float,
    gameplay_focus_x: float,
    split: float,
    warnings: tuple[str, ...],
) -> ResolvedLayout:
    sw, sh = source
    W, H = profile.width, profile.height
    k = profile.scale
    frame = Rect(0, 0, sw, sh)
    captions = TextAnchor(5, DESIGN_W // 2, CAPTION_Y)
    hook = TextAnchor(5, DESIGN_W // 2, HOOK_Y)
    if kind == LayoutKind.FACECAM_GAMEPLAY:
        assert facecam is not None
        top_h = _even(H * split)
        face = crop_to_aspect(_px(facecam, source), W / top_h)
        game = crop_to_aspect(frame, W / (H - top_h), gameplay_focus_x)
        panels = (
            Panel("face", face, Rect(0, 0, W, top_h)),
            Panel("gameplay", game, Rect(0, top_h, W, H - top_h), gameplay_focus_x),
        )
        title = TextAnchor(5, DESIGN_W // 2, round(top_h / k))  # on the seam
        return ResolvedLayout(kind, (W, H), source, panels, False, title, captions, hook, warnings)
    if kind == LayoutKind.FULL_CROP:
        main = crop_to_aspect(frame, W / H, focus_x)
        panels = (Panel("main", main, Rect(0, 0, W, H), focus_x),)
        title = TextAnchor(8, DESIGN_W // 2, TITLE_TOP_Y)
        return ResolvedLayout(kind, (W, H), source, panels, False, title, captions, hook, warnings)
    # blurred_fit: full width (a source taller than the canvas aspect is fitted by height)
    fit_h = _even(W * sh / sw)
    fit_w = W
    if fit_h > H:
        fit_h, fit_w = H, _even(H * sw / sh)
    y = min(_even(FIT_Y * k), H - fit_h)
    dst = Rect((W - fit_w) // 2 // 2 * 2, y, fit_w, fit_h)
    panels = (Panel("main", Rect(0, 0, sw - sw % 2, sh - sh % 2), dst),)
    title = TextAnchor(2, DESIGN_W // 2, max(round(y / k) - 36, 200))  # just above the video
    return ResolvedLayout(
        LayoutKind.BLURRED_FIT, (W, H), source, panels, True, title, captions, hook, warnings
    )
