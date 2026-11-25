"""The ffmpeg command builder (DESIGN §20 steps 3-6): goldens of both passes, exact frame and
sample counts, the fps cap, the preview's bitrate, chunks without audio, silent audio."""

import os
from fractions import Fraction
from pathlib import Path

import pytest

from clipper_core.contracts import LayoutHint
from clipper_renderer import graph
from clipper_renderer.graph import (
    GraphSpec,
    Loudness,
    MusicInput,
    SfxInput,
    SourceFile,
    ZoomOut,
    measure_command,
    output_fps,
    render_command,
    total_frames,
)
from clipper_renderer.layout import LayoutConfig, load_layouts, resolve
from clipper_renderer.profiles import get_profile
from clipper_renderer.samples import sample_chunks, sample_plan
from clipper_renderer.timemap import build, plan_ranges

FIXTURES = Path(__file__).parent / "fixtures"
UPDATE = os.environ.get("CLIPPER_RENDERER_UPDATE_GOLDEN") == "1"
SHORT = get_profile("short_1080x1920")
LOUD = Loudness("-23.41", "-6.20", "5.10", "-33.60", "0.12")

FACECAM = """
[channels.xqc]
facecam = { x = 0.0, y = 0.0, w = 0.25, h = 0.3 }
"""


def golden(name: str, text: str) -> None:
    path = FIXTURES / name
    if UPDATE or not path.exists():
        path.write_bytes(text.encode("utf-8"))
    stored = path.read_bytes().decode("utf-8")
    assert text == stored, f"{name} changed (CLIPPER_RENDERER_UPDATE_GOLDEN=1 rewrites it)"


def sample_spec(tmp_path, **kw) -> GraphSpec:
    (tmp_path / "layouts.toml").write_bytes(FACECAM.encode())
    cfg, _ = load_layouts(tmp_path / "layouts.toml")
    plan = sample_plan()
    tm = build(plan_ranges(plan), sample_chunks())
    lay = resolve(cfg, "xqc", None, plan.layout, source=(1920, 1080), profile=SHORT)
    sources = {
        c.chunk_id: SourceFile(f"in/{c.seq:06d}.ts", 1920, 1080, origin_s=0.021333)
        for c in sample_chunks()
    }
    values = {
        "pieces": tm.pieces,
        "sources": sources,
        "fps": Fraction(60),
        "profile": SHORT,
        "layout": lay,
        "encoder": "libx264",
        "zooms": [
            ZoomOut("face", 0.5, 2.5, 1.3),
            ZoomOut("face", 9.5, 12.0, 1.3),
            ZoomOut("gameplay", 19.5, 21.5, 1.2),
        ],
        "sfx": [SfxInput("assets/boom.wav", 10.55, -4.5), SfxInput("assets/scratch.wav", 21.8, -1)],
        "music": MusicInput("assets/bed.wav", -20.0, duck=True),
    } | kw
    return GraphSpec(**values)


def lines(cmd: graph.Command) -> str:
    return "\n".join(cmd.args).replace(cmd.graph, "<graph>") + "\n\n" + cmd.graph + "\n"


def test_golden_commands_of_both_passes(tmp_path):
    spec = sample_spec(tmp_path)
    golden("graph_measure.txt", lines(measure_command(spec)))
    golden("graph_render.txt", lines(render_command(spec, LOUD)))
    nvenc = render_command(sample_spec(tmp_path, encoder="h264_nvenc"), LOUD)
    golden("graph_render_nvenc.txt", lines(nvenc))


def test_measure_pass_reads_the_same_inputs_without_video(tmp_path):
    spec = sample_spec(tmp_path)
    m, r = measure_command(spec), render_command(spec, LOUD)
    inputs = lambda c: [a for i, a in enumerate(c.args) if c.args[i - 1] == "-i"]  # noqa: E731
    assert inputs(m) == inputs(r)
    assert ":v:0]" not in m.graph and "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json" in m.graph
    assert m.args[-5:] == ["-map", "[aout]", "-f", "null", "-"]
    assert "measured_I=-23.41" in r.graph and "linear=true" in r.graph
    assert r.outputs == ["clip.mp4", "preview.mp4"]


def test_seeks_start_early_and_the_graph_trims_exactly(tmp_path):
    spec = sample_spec(tmp_path)
    args = measure_command(spec).args
    ss = [float(args[i + 1]) for i, a in enumerate(args) if a == "-ss"]
    first = spec.pieces[0]
    assert ss[0] == pytest.approx(first.trim_from_s + 0.021333 - graph.SEEK_PREROLL_S)
    assert ss[1] == 0.0  # a piece near its chunk's start reads from the start
    r = render_command(spec, LOUD).graph
    assert f"[0:v:0]trim=start={graph.SEEK_PREROLL_S:.6f},setpts=PTS-5.000000/TB" in r
    assert "[1:a:0]atrim=start=0.021333,asetpts=PTS-0.021333/TB" in r


def test_frame_and_sample_counts_add_up_exactly(tmp_path):
    spec = sample_spec(tmp_path)
    for fps in (Fraction(60), Fraction(30), Fraction(30000, 1001)):
        frames = [graph._counts(p, fps)[0] for p in spec.pieces]
        samples = [graph._counts(p, fps)[1] for p in spec.pieces]
        assert sum(frames) == total_frames(spec.pieces, fps) == round(Fraction("23.8") * fps)
        assert sum(samples) == 23.8 * 48000


def test_fps_cap_and_preview_bitrate():
    test = get_profile("test_270x480")
    assert output_fps(Fraction(60), SHORT) == 60 and output_fps(Fraction(60), test) == 30
    assert output_fps(Fraction(30000, 1001), SHORT) == Fraction(30000, 1001)
    assert output_fps(Fraction(144), SHORT) == 60
    assert SHORT.preview_video_kbps(30) == 1500
    long = SHORT.preview_video_kbps(180)
    assert long < 1500 and (long + SHORT.preview_audio_kbps) * 180 / 8 * 1000 < 19_000_000
    with pytest.raises(ValueError, match="unknown render profile"):
        get_profile("square")


def test_a_chunk_without_audio_gets_silence_and_silent_audio_isnt_normalised(tmp_path):
    spec = sample_spec(tmp_path, sfx=(), music=None)
    mute = dict(spec.sources)
    mute[102] = SourceFile("in/000011.ts", 1920, 1080, has_audio=False)
    cmd = render_command(sample_spec(tmp_path, sfx=(), music=None, sources=mute), None)
    assert "anullsrc=r=48000:cl=stereo,atrim=end_sample=" in cmd.graph
    assert "[1:a:0]" not in cmd.graph and "[0:a:0]" in cmd.graph
    assert "[speech]anull[mix]" in cmd.graph and "[mix]anull,aresample" in cmd.graph
    silent = Loudness("-inf", "-inf", "0.00", "-70.00", "0.00")
    assert silent.silent and "loudnorm" not in render_command(spec, silent).graph


def test_a_source_of_another_size_is_scaled_to_the_layouts(tmp_path):
    spec = sample_spec(tmp_path)
    other = dict(spec.sources)
    other[103] = SourceFile("in/000012.ts", 1280, 720)
    cmd = render_command(sample_spec(tmp_path, sources=other), LOUD)
    assert cmd.graph.count(",scale=1920:1080,") == 1


def test_blurred_fit_and_full_crop_layout_chains(tmp_path):
    plan = sample_plan()
    fit = resolve(LayoutConfig(), None, None, plan.layout, source=(1920, 1080), profile=SHORT)
    g = render_command(sample_spec(tmp_path, layout=fit, zooms=[]), LOUD).graph
    assert "[vcat]split=2[s0][s1]" in g and "[bg][p_main]overlay=0:520[canvas]" in g
    assert "boxblur" in g and "eq=brightness=-0.18" in g
    crop = resolve(
        LayoutConfig(),
        None,
        None,
        LayoutHint(kind="full_crop"),
        source=(1920, 1080),
        profile=SHORT,
    )
    g = render_command(sample_spec(tmp_path, layout=crop, zooms=[]), LOUD).graph
    assert "[vcat]null[s0]" in g and "[p_main]null[canvas]" in g
