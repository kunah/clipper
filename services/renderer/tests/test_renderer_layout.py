"""Layouts (DESIGN §20): layouts.toml, which layout a plan gets, and the pixel geometry."""

import hashlib

import pytest

from clipper_core.contracts import LayoutHint, LayoutKind
from clipper_renderer.layout import (
    LayoutConfig,
    LayoutError,
    Rect,
    crop_to_aspect,
    load_layouts,
    resolve,
    zoom_rect,
)
from clipper_renderer.profiles import get_profile

SHORT = get_profile("short_1080x1920")
TEST = get_profile("test_270x480")
HD = (1920, 1080)

LAYOUTS = """
version = 1

[channels.ohnepixel]
facecam = { x = 0.0, y = 0.0, w = 0.25, h = 0.3 }
gameplay_focus_x = 0.6

[channels.ohnepixel.categories."Just Chatting"]
layout = "full_crop"
focus_x = 0.0

[channels.xqc]
default = "full_crop"
focus_x = 0.4
"""


def config(tmp_path, text=LAYOUTS):
    path = tmp_path / "layouts.toml"
    path.write_bytes(text.encode())
    return load_layouts(path)


def even(*values: int) -> bool:
    return all(v % 2 == 0 for v in values)


def assert_even(lay):
    for p in lay.panels:
        assert even(p.src.x, p.src.y, p.src.w, p.src.h, p.dst.x, p.dst.y, p.dst.w, p.dst.h), p


def test_no_config_means_blurred_fit_with_the_whole_frame_full_width():
    cfg, sha = load_layouts(None)
    assert sha is None and cfg == LayoutConfig()
    lay = resolve(cfg, "anyone", "Fortnite", LayoutHint(), source=HD, profile=SHORT)
    assert lay.kind == LayoutKind.BLURRED_FIT and lay.background
    (main,) = lay.panels
    assert (main.src, main.dst) == (Rect(0, 0, 1920, 1080), Rect(0, 520, 1080, 608))
    assert (lay.title.align, lay.title.y) == (2, 484)  # bottom edge just above the video
    assert (lay.captions.align, lay.captions.x, lay.captions.y) == (5, 540, 1300)
    assert lay.warnings == ()
    assert_even(lay)


def test_a_missing_file_is_no_config(tmp_path):
    assert load_layouts(tmp_path / "nope.toml") == (LayoutConfig(), None)


def test_a_facecam_rect_makes_auto_facecam_gameplay(tmp_path):
    cfg, sha = config(tmp_path)
    assert sha == hashlib.sha256(LAYOUTS.encode()).hexdigest()
    lay = resolve(cfg, "OhnePixel", "Counter-Strike", LayoutHint(), source=HD, profile=SHORT)
    assert lay.kind == LayoutKind.FACECAM_GAMEPLAY and not lay.background
    face, game = lay.panels
    assert face.dst == Rect(0, 0, 1080, 760) and game.dst == Rect(0, 760, 1080, 1160)
    # the facecam rect (480x324 px) cropped to the panel's aspect 1080/760, centred
    assert face.src == Rect(10, 0, 460, 324)
    assert abs(face.src.w / face.src.h - 1080 / 760) < 0.01
    # the gameplay: full height, aspect 1080/1160, at focus 0.6
    assert game.src.h == 1080 and abs(game.src.w / game.src.h - 1080 / 1160) < 0.01
    assert game.src.x == round((0.6 * 1920 - game.src.w / 2) / 2) * 2
    assert lay.title.y == 760 and lay.title.align == 5  # on the seam
    assert_even(lay)
    assert lay.zoom_panel("face") is face and lay.zoom_panel("center") is game


def test_categories_hints_and_fallbacks(tmp_path):
    cfg, _ = config(tmp_path)
    chatting = resolve(cfg, "ohnepixel", "just chatting", LayoutHint(), source=HD, profile=SHORT)
    assert chatting.kind == LayoutKind.FULL_CROP
    (main,) = chatting.panels
    assert main.src == Rect(0, 0, 608, 1080) and main.dst == Rect(0, 0, 1080, 1920)  # focus 0.0
    assert chatting.title.align == 8
    # the plan's hint wins where the config allows it, and its focus_x too
    hinted = resolve(
        cfg,
        "ohnepixel",
        "just chatting",
        LayoutHint(kind=LayoutKind.FULL_CROP, focus_x=1.0),
        source=HD,
        profile=SHORT,
    )
    assert hinted.panels[0].src == Rect(1312, 0, 608, 1080)
    blurred = resolve(
        cfg, "ohnepixel", None, LayoutHint(kind=LayoutKind.BLURRED_FIT), source=HD, profile=SHORT
    )
    assert blurred.kind == LayoutKind.BLURRED_FIT
    # facecam_gameplay without a rect -> the channel's layout, with a warning
    xqc = resolve(
        cfg,
        "xqc",
        None,
        LayoutHint(kind=LayoutKind.FACECAM_GAMEPLAY),
        source=HD,
        profile=SHORT,
    )
    assert xqc.kind == LayoutKind.FULL_CROP
    assert xqc.panels[0].src.x == round((0.4 * 1920 - 608 / 2) / 2) * 2
    assert [w.split(":")[0] for w in xqc.warnings] == ["layout_fallback"]
    other = resolve(
        cfg, "nobody", None, LayoutHint(kind=LayoutKind.FACECAM_GAMEPLAY), source=HD, profile=SHORT
    )
    assert other.kind == LayoutKind.BLURRED_FIT and other.warnings


def test_the_test_profile_scales_the_canvas_not_the_design_anchors(tmp_path):
    cfg, _ = config(tmp_path)
    lay = resolve(cfg, "ohnepixel", None, LayoutHint(), source=(320, 180), profile=TEST)
    face, game = lay.panels
    assert face.dst == Rect(0, 0, 270, 190) and game.dst == Rect(0, 190, 270, 290)
    assert lay.title.y == 760  # design pixels (ASS PlayRes 1080x1920)
    assert_even(lay)
    fit = resolve(LayoutConfig(), None, None, LayoutHint(), source=(320, 180), profile=TEST)
    assert fit.panels[0].dst == Rect(0, 130, 270, 152)


def test_vertical_sources_fit_by_height():
    fit = resolve(LayoutConfig(), None, None, LayoutHint(), source=(1080, 1920), profile=SHORT)
    assert fit.panels[0].dst == Rect(0, 0, 1080, 1920)
    tall = resolve(LayoutConfig(), None, None, LayoutHint(), source=(720, 1600), profile=SHORT)
    assert tall.panels[0].dst.h == 1920 and tall.panels[0].dst.w < 1080
    crop = resolve(
        LayoutConfig(),
        None,
        None,
        LayoutHint(kind=LayoutKind.FULL_CROP),
        source=(1080, 1920),
        profile=SHORT,
    )
    assert crop.panels[0].src == Rect(0, 0, 1080, 1920)


def test_crop_and_zoom_rects():
    r = Rect(100, 50, 800, 400)
    assert crop_to_aspect(r, 1.0) == Rect(300, 50, 400, 400)
    assert crop_to_aspect(r, 1.0, focus_x=0.0) == Rect(100, 50, 400, 400)
    assert crop_to_aspect(r, 4.0) == Rect(100, 150, 800, 200)
    assert zoom_rect(r, 1.25) == Rect(180, 90, 640, 320)
    assert zoom_rect(r, 1.25, focus_x=1.0) == Rect(260, 90, 640, 320)


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("[channels.x]\nfacecam = { x = 0.9, y = 0, w = 0.2, h = 0.2 }\n", "inside the frame"),
        ("[channels.x]\ndefault = 'facecam_gameplay'\n", "needs a facecam rect"),
        (
            "[channels.x]\n[channels.x.categories.Art]\nlayout = 'facecam_gameplay'\n",
            "needs a facecam rect",
        ),
        ("[channels.x]\ndefualt = 'full_crop'\n", "defualt"),
        ("default = 'facecam_gameplay'\n", "can't be facecam_gameplay"),
        ("default = 'auto'\n", "default"),
        ("[channels.x]\nsplit = 0.9\n", "split"),
        ("version = 2\n", "version"),
        ("[channels.x\n", "layouts.toml"),
    ],
)
def test_invalid_layouts_are_errors(tmp_path, text, error):
    with pytest.raises(LayoutError, match=error):
        config(tmp_path, text)


def test_the_repo_default_layouts_file_is_valid():
    from pathlib import Path

    path = Path(__file__).parents[1] / "config" / "layouts.toml"
    cfg, sha = load_layouts(path)
    assert sha is not None and cfg.default == "blurred_fit"
