"""The ASS builder (DESIGN §20 "Captions"): goldens, the paging rules, word mapping through the
time map, and that the generated files render through ffmpeg's `ass` filter with the bundled fonts
only and without warnings."""

import os
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from clipper_core.contracts import CaptionStyle
from clipper_core.timeline import Interval
from clipper_renderer import captions, fontset
from clipper_renderer.captions import ass_text, ass_time, build_ass
from clipper_renderer.layout import LayoutConfig, resolve
from clipper_renderer.profiles import get_profile
from clipper_renderer.samples import SampleWord, at, sample_chunks, sample_plan, sample_words
from clipper_renderer.timemap import ChunkVideo, OutRange, build, plan_ranges

FIXTURES = Path(__file__).parent / "fixtures"
UPDATE = os.environ.get("CLIPPER_RENDERER_UPDATE_GOLDEN") == "1"
SHORT = get_profile("short_1080x1920")
TEST = get_profile("test_270x480")


def golden(name: str, text: str) -> None:
    path = FIXTURES / name
    if UPDATE or not path.exists():
        path.write_bytes(text.encode("utf-8"))
    stored = path.read_bytes().decode("utf-8")
    assert text == stored, f"{name} changed (CLIPPER_RENDERER_UPDATE_GOLDEN=1 rewrites it)"


def layout_for(plan):
    return resolve(LayoutConfig(), None, None, plan.layout, source=(1920, 1080), profile=SHORT)


def sample_subs(**kw):
    plan = sample_plan(**kw)
    tm = build(plan_ranges(plan), sample_chunks())
    return plan, tm, build_ass(sample_words(), plan, tm, layout_for(plan))


def karaoke_subs():
    return sample_subs(
        caption_style="karaoke",
        hook={"mode": "text", "text": "HE DID IT 🎉 {really}"},
        segments=[{"start_utc": at(-8), "end_utc": at(6)}],
        zooms=[],
        sfx=[],
    )


def test_golden_bold_pop_with_a_teaser_and_two_segments():
    _, tm, subs = sample_subs()
    golden("captions_bold_pop.ass", subs.text)
    assert subs.dropped == 1  # AGAIN crosses the gap's cut
    assert subs.fonts == {"Montserrat Black", "Montserrat ExtraBold"}
    texts = [" ".join(w.text for w in p.words) for p in subs.pages]
    assert "AGAIN" not in " ".join(texts)
    assert "UNBELIEVABLE!!" in texts  # the word fix (its "!!" kept; it ends the page)
    assert "(HONESTLY)" in texts  # no override braces
    assert texts[-2:] == ["WHAT JUST", "HAPPENED"]  # > 16 characters: two pages; no emoji
    assert "{\\r}" in subs.text and "\\t(0,80,\\fscx" in subs.text
    teaser = [p for p in subs.pages if p.start_s < tm.teaser_s]
    assert teaser and all(p.end_s <= tm.teaser_s for p in teaser)


def test_golden_karaoke_with_a_text_hook():
    _, _, subs = karaoke_subs()
    golden("captions_karaoke_hook.ass", subs.text)
    assert "Hook,,0,0,0,,{\\an5\\pos(540,1000)}HE DID IT (really)" in subs.text
    assert subs.fonts == {"Montserrat ExtraBold", "Montserrat Black"}


def test_golden_clean_without_title():
    plan, tm, _ = sample_subs(caption_style="clean", hook={"mode": "none"})
    subs = build_ass(sample_words(), plan, tm, layout_for(plan), title=False)
    golden("captions_clean.ass", subs.text)
    assert "Title" not in subs.text.split("[Events]")[1]
    assert "OKAY" not in subs.text and "okay okay listen" not in subs.text  # before the clip
    assert subs.fonts == {"Montserrat SemiBold"}


def one_run(seconds: float = 60.0):
    plan = sample_plan(
        hook={"mode": "none"},
        segments=[{"start_utc": at(0), "end_utc": at(seconds)}],
        emphasis=[],
        word_fixes=[],
        zooms=[],
        sfx=[],
        thumbnail_utc=None,
    )
    tm = build(
        [OutRange(Interval(at(0), at(seconds)), False)],
        [ChunkVideo(1, 1, at(-1), at(seconds + 1), "k")],
    )
    return plan, tm


def words(*spec):
    return [SampleWord(t, at(a), at(b)) for t, a, b in spec]


def pages(plan, tm, ws, style=CaptionStyle.BOLD_POP):
    look = captions.LOOKS[style]
    out, _ = captions.map_words(ws, plan, tm, look)
    return [
        ([w.text for w in p.words], p.start_s, p.end_s) for p in captions.paginate(out, look, tm)
    ]


def test_pages_hold_at_most_three_words_and_sixteen_characters():
    plan, tm = one_run()
    ws = words(*[(w, i * 0.3, i * 0.3 + 0.25) for i, w in enumerate(["a", "bb", "ccc", "dd", "e"])])
    assert [p[0] for p in pages(plan, tm, ws)] == [["A", "BB", "CCC"], ["DD", "E"]]
    long = words(("extraordinarily", 0, 0.4), ("so", 0.45, 0.6), ("x", 0.65, 0.8))
    assert [p[0] for p in pages(plan, tm, long)] == [["EXTRAORDINARILY"], ["SO", "X"]]


def test_pages_break_at_pauses_and_punctuation_and_last_long_enough():
    plan, tm = one_run()
    ws = words(
        ("one", 1.0, 1.2),
        ("two.", 1.25, 1.4),  # punctuation ends the page (the "." isn't shown)
        ("three", 1.45, 1.6),
        ("four", 2.5, 2.6),  # a 0.9 s pause before it: a new page; the page before ends at 1.6
    )
    assert pages(plan, tm, ws) == [
        (["ONE", "TWO"], 1.0, 1.45),  # held until the next page (it follows within 0.35 s)
        (["THREE"], 1.45, 1.85),  # ends at 1.6, kept up for the minimum 0.4 s
        (["FOUR"], 2.5, 2.9),
    ]


def test_words_outside_or_across_the_clip_and_fixes_by_start_time():
    plan, tm = one_run(10)
    ws = words(("before", -1, -0.5), ("edge", -0.2, 0.2), ("in", 1, 1.2), ("out", 9.9, 10.3))
    fixed = plan.model_copy(update={"word_fixes": []})
    look = captions.LOOKS[CaptionStyle.BOLD_POP]
    out, dropped = captions.map_words(ws, fixed, tm, look)
    assert [w.text for w in out] == ["IN"] and dropped == 2
    # a fix matches the word's start (within 1 ms), whatever the word's text
    from clipper_core.contracts import WordFix

    fix = WordFix(start_utc=at(1) + timedelta(microseconds=400), text="Pog")
    out, _ = captions.map_words(ws, fixed.model_copy(update={"word_fixes": [fix]}), tm, look)
    assert [w.text for w in out] == ["POG"]


def test_text_helpers():
    assert ass_time(0) == "0:00:00.00" and ass_time(3725.456) == "1:02:05.46"
    assert ass_text("a{\\b1}b\nc 😂  ✓ d€ 5%") == "a(/b1)b c d€ 5%"


# --------------------------------------------------------------------------- through libass


def ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe is None:
        pytest.skip("ffmpeg not on PATH")
    return exe


BUNDLED_PS = {"Montserrat-Black", "Montserrat-ExtraBold", "Montserrat-SemiBold"}


@pytest.mark.parametrize("name", ["bold_pop", "karaoke", "clean"])
def test_generated_ass_renders_with_the_bundled_fonts_only(tmp_path, name):
    exe = ffmpeg()
    if name == "bold_pop":
        _, tm, subs = sample_subs()
    elif name == "karaoke":
        _, tm, subs = karaoke_subs()
    else:
        plan, tm, _ = sample_subs(caption_style="clean", hook={"mode": "none"})
        subs = build_ass(sample_words(), plan, tm, layout_for(plan), title=False)
    (tmp_path / "captions.ass").write_bytes(subs.text.encode("utf-8"))
    fontset.install(fontset.bundled_fonts(), tmp_path / "fonts")
    cmd = [
        exe, "-nostdin", "-hide_banner", "-v", "verbose", "-f", "lavfi", "-i",
        f"color=c=gray:s={TEST.width}x{TEST.height}:r=10:d={tm.duration_s:.2f}",
        "-vf", "ass=filename=captions.ass:fontsdir=fonts", "-f", "null", "-",
    ]  # fmt: skip
    proc = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True, errors="replace")
    assert proc.returncode == 0, proc.stderr[-2000:]
    log = proc.stderr
    selected = set(re.findall(r"fontselect: \(([^,]+), \d+, \d+\) -> ([^,]+),", log))
    assert {family for family, _ in selected} == subs.fonts
    assert {ps.strip() for _, ps in selected} <= BUNDLED_PS, selected
    assert "not found" not in log and "Error opening" not in log
    warn = subprocess.run(
        [*cmd[:4], "warning", *cmd[5:]], cwd=tmp_path, capture_output=True, text=True
    )
    assert warn.returncode == 0 and warn.stderr.strip() == ""
