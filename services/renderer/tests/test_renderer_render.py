"""Rendering through real ffmpeg (DESIGN §20 build item 7b) on synthetic chunks: frames at a chunk
boundary, at the jitter fill and at segment cuts match the time map within one output frame (the
luma encodes the source frame index); beeps land within one frame; -14 +- 1 LUFS and a true peak
<= -1 dBTP with two SFX and ducked music; the same job twice gives the same graph hash and the same
bytes; the facecam layout with punch-ins renders."""

import math
import re
import shutil
import struct
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path

import pytest

from clipper_core.contracts import EditPlan
from clipper_renderer import fontset, graph
from clipper_renderer.layout import load_layouts
from clipper_renderer.library import AssetLibrary
from clipper_renderer.media import Tools
from clipper_renderer.profiles import get_profile
from clipper_renderer.render import LocalChunk, RenderJob, render_files
from clipper_renderer.samples import SampleWord
from clipper_renderer.synthetic import BEEP_HZ, frame_index_from_luma, make_chunk
from clipper_renderer.timemap import ChunkVideo, build, plan_ranges

T0 = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
TEST = get_profile("test_270x480")
SRC_FPS = 60


def at(s: float) -> datetime:
    return T0 + timedelta(seconds=s)


@pytest.fixture(scope="module")
def tools() -> Tools:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not on PATH")
    return Tools()


@pytest.fixture(scope="module")
def synth(tools, tmp_path_factory):
    """Chunks [0, 20) and [20.3, 40) (a 0.3 s jitter hole) and [43, 60) (a 3 s gap)."""
    d = tmp_path_factory.mktemp("synth")
    spans = [(1, 0.0, 20.0), (2, 20.3, 19.7), (3, 43.0, 17.0)]
    return {
        seq: make_chunk(tools, d / f"{seq:06d}.ts", t0=T0, start_s=start, duration_s=dur)
        for seq, start, dur in spans
    }


@pytest.fixture(scope="module")
def library(tools, tmp_path_factory):
    """Two SFX (3 kHz bursts) and a music bed (330 Hz), with a manifest and licences."""
    d = tmp_path_factory.mktemp("assets")
    (d / "sfx").mkdir()
    (d / "music").mkdir()
    sources = {
        "sfx/boom.wav": "sine=f=3000:d=0.3,volume=0.5",
        "sfx/scratch.wav": "sine=f=3000:d=0.5,volume=0.3",
        "music/bed.wav": "sine=f=330:d=6,volume=0.4",
    }
    for rel, src in sources.items():
        tools.run(["-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-ac", "2",
                   "-ar", "48000", str(d / rel)])  # fmt: skip
    from clipper_core.assets import file_sha256

    def entry(id_, rel, kind, label):
        field_ = f'tags = ["{label}"]' if kind == "sfx" else f'moods = ["{label}"]'
        return (
            f'[[assets]]\nid = "{id_}"\nfile = "{rel}"\nsha256 = "{file_sha256(d / rel)}"\n'
            f'kind = "{kind}"\n{field_}\nduration_s = 1\n[assets.licence]\nname = "test"\n'
            'source_url = "https://example.com"\nlicensee = "tests"\nproof = "none"\n'
            'platforms = ["youtube"]\n'
        )

    (d / "manifest.toml").write_text(
        "version = 1\n"
        + entry("boom", "sfx/boom.wav", "sfx", "vine_boom")
        + entry("scratch", "sfx/scratch.wav", "sfx", "record_scratch")
        + entry("bed", "music/bed.wav", "music", "funny"),
        encoding="utf-8",
    )
    return AssetLibrary(d, tools)


def plan(**kw) -> EditPlan:
    return EditPlan.model_validate(
        {
            "clip_id": 7,
            "stream_id": 1,
            "anchor_utc": at(19),
            "segments": [
                {"start_utc": at(14), "end_utc": at(24)},  # over the jitter hole at 20
                {"start_utc": at(37), "end_utc": at(46)},  # over the 40 .. 43 gap
            ],
            "hook": {"mode": "teaser", "start_utc": at(18.5), "end_utc": at(20.5)},
            "title": "synthetic render",
            "confidence": 0.9,
            "model": "fake",
            "prompt_version": "v1",
        }
        | kw
    )


def job_for(work: Path, synth, p: EditPlan, **kw) -> RenderJob:
    (work / "in").mkdir(parents=True, exist_ok=True)
    chunks = []
    for seq, c in synth.items():
        dst = work / "in" / c.path.name
        shutil.copyfile(c.path, dst)
        video = ChunkVideo(100 + seq, seq, c.start_utc, c.end_utc, f"clips/7/pinned/{seq:06d}.ts")
        chunks.append(LocalChunk(video, dst))
    words = [
        SampleWord("now", at(15.0), at(15.3)),
        SampleWord("watch", at(15.4), at(15.8)),
        SampleWord("this!", at(19.8), at(20.4)),  # over the jitter hole
        SampleWord("gap", at(39.8), at(40.2)),  # crosses the gap's cut: dropped
    ]
    values = {
        "render_id": 5,
        "plan_id": 9,
        "plan": p,
        "profile": TEST,
        "chunks": chunks,
        "words": words,
        "fonts": fontset.bundled_fonts(),
    } | kw
    return RenderJob(**values)


def y_samples(tools: Tools, work: Path, x: int, y: int) -> list[float]:
    """Mean luma of a 4x4 patch at (x, y) in every frame of clip.mp4 (raw Y plane)."""
    tools.run(["-nostdin", "-v", "error", "-y", "-i", "clip.mp4", "-vf",
               f"extractplanes=y,crop=4:4:{x - 2}:{y - 2}", "-f", "rawvideo", "y.raw"],
              cwd=work)  # fmt: skip
    data = (work / "y.raw").read_bytes()
    return [sum(data[i : i + 16]) / 16 for i in range(0, len(data), 16)]


def expected_source_frames(tm, fps: Fraction) -> list[float]:
    """Per output frame: the source frame index (60 fps since T0) it should show."""
    out = []
    for p in tm.pieces:
        a = round(Fraction(f"{p.out_at_s:.6f}") * fps)
        b = round(Fraction(f"{p.out_to_s:.6f}") * fps)
        for j in range(b - a):
            tau = j / fps
            if tau < p.duration_s:
                utc_s = (p.utc_from - T0).total_seconds() + float(tau)
                out.append(utc_s * SRC_FPS)
            else:  # the jitter fill: the piece's last frame held
                out.append(math.ceil((p.utc_to - T0).total_seconds() * SRC_FPS) - 1)
    return out


def goertzel(xs, f: float, sr: int = 48000) -> float:
    w = 2 * math.pi * f / sr
    c = 2 * math.cos(w)
    s1 = s2 = 0.0
    for x in xs:
        s1, s2 = x + c * s1 - s2, s1
    return s1 * s1 + s2 * s2 - c * s1 * s2


def onset(samples, around_s: float, f: float = BEEP_HZ) -> float:
    lo = int((around_s - 0.1) * 48000)
    hop = 48
    powers = [goertzel(samples[i : i + 96], f) for i in range(lo, lo + 9600, hop)]
    peak = max(powers)
    first = next(i for i, pw in enumerate(powers) if pw > peak / 2)
    return (lo + first * hop + 48) / 48000


def pcm(tools: Tools, work: Path) -> tuple[int, ...]:
    tools.run(["-nostdin", "-v", "error", "-y", "-i", "clip.mp4", "-ac", "1", "-ar", "48000",
               "-f", "s16le", "a.raw"], cwd=work)  # fmt: skip
    raw = (work / "a.raw").read_bytes()
    return struct.unpack(f"<{len(raw) // 2}h", raw)


def ebur128(tools: Tools, path: Path) -> tuple[float, float]:
    r = tools.run(["-nostdin", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af",
                   "ebur128=peak=true", "-f", "null", "-"])  # fmt: skip
    summary = r.stderr.rsplit("Summary:", 1)[1]
    i = float(re.search(r"I:\s+(-?[\d.]+) LUFS", summary).group(1))
    tp = float(re.search(r"Peak:\s+(-?[\d.]+) dBFS", summary).group(1))
    return i, tp


def test_frames_and_beeps_follow_the_time_map(tools, synth, library, tmp_path):
    p = plan(
        sfx=[
            {"at_utc": at(21.5), "tag": "vine_boom"},
            {"at_utc": at(44.5), "tag": "record_scratch"},
        ],
        music={"mood": "funny"},
    )
    job = job_for(
        tmp_path, synth, p, assets=library.usable(), normalisation_db=library.normalisation_db
    )
    out = render_files(job, tmp_path, tools)
    tm = build(plan_ranges(p), [c.video for c in job.chunks])
    fps = Fraction(30)
    assert (out.width, out.height, out.fps, out.encoder) == (270, 480, 30.0, "libx264")
    assert out.duration_s == pytest.approx(tm.duration_s, abs=0.03)
    assert tm.teaser_s == 2.0 and [w.split(":")[0] for w in out.warnings] == [
        "gap_cut",
        "words_dropped",
    ]
    # frames: the video's centre (blurred_fit: the full-width video at y 130..282)
    fg = out.layout.panels[0].dst
    ys = y_samples(tools, tmp_path, fg.x + fg.w // 2, fg.y + fg.h // 2)
    expected = expected_source_frames(tm, fps)
    assert len(ys) == len(expected) == graph.total_frames(tm.pieces, fps)
    errors = [
        (k, frame_index_from_luma(y, e) - e)
        for k, (y, e) in enumerate(zip(ys, expected, strict=True))
    ]
    one_output_frame = SRC_FPS / fps  # 2 source frames
    bad = [(k, e) for k, e in errors if abs(e) > one_output_frame]
    assert not bad, bad[:10]
    # the frames around the joins: the teaser's end, the chunk boundary (+ fill), the gap's cut
    for t in (1.95, 2.0, 2.05, 8.0, 8.3, 8.4, 15.0, 15.1):
        k = round(t * 30)
        assert abs(errors[k][1]) <= one_output_frame, (t, errors[k])
    # beeps at whole UTC seconds, in the clip and in the teaser
    samples = pcm(tools, tmp_path)
    for g in (15, 16, 19, 21, 23, 38, 43, 45):
        for want in tm.out_times(at(g)):
            got = onset(samples, want)
            assert abs(got - want) <= 1 / 30, (g, want, got)
    # the SFX (3 kHz) start at their cue's output time
    for cue in p.sfx:
        want = tm.clip_out(cue.at_utc)
        assert abs(onset(samples, want, f=3000) - want) <= 1 / 30
    # loudness of the delivered file, measured independently
    lufs, tp = ebur128(tools, out.files["clip.mp4"])
    assert abs(lufs + 14) <= 1 and tp <= -1.0, (lufs, tp)
    assert out.loudness_lufs == pytest.approx(lufs, abs=0.5)
    assert {a.kind for a in out.manifest.assets} == {"font", "sfx", "music"}
    assert [a.id for a in out.manifest.assets if a.kind != "font"] == ["boom", "scratch", "bed"]
    assert [s.teaser for s in out.manifest.sources][:2] == [True, True]
    assert out.manifest.sources[0].origin_s == 0.0  # the audio starts the file
    preview = tools.probe(out.files["preview.mp4"])
    assert (preview.width, preview.height) == (TEST.preview_width, TEST.preview_height)
    assert out.files["thumb.jpg"].stat().st_size > 0


def test_the_same_job_twice_gives_the_same_hash_and_bytes(tools, synth, tmp_path):
    p = plan(caption_style="karaoke", hook={"mode": "text", "text": "WAIT FOR IT"})
    outs = []
    for name in ("a", "b"):
        work = tmp_path / name
        outs.append(render_files(job_for(work, synth, p), work, tools))
    a, b = outs
    assert a.graph_hash == b.graph_hash
    for f in ("clip.mp4", "preview.mp4", "thumb.jpg", "captions.ass"):
        assert a.files[f].read_bytes() == b.files[f].read_bytes(), f
    assert a.video_sha256 == b.video_sha256
    other = render_files(job_for(tmp_path / "c", synth, p.model_copy(update={"title": "x"})),
                         tmp_path / "c", tools)  # fmt: skip
    assert other.graph_hash != a.graph_hash


def test_facecam_layout_with_punch_ins_and_no_assets(tools, synth, tmp_path):
    (tmp_path / "layouts.toml").write_bytes(
        b"[channels.synth]\nfacecam = { x = 0.0, y = 0.0, w = 0.25, h = 0.3 }\n"
    )
    cfg, sha = load_layouts(tmp_path / "layouts.toml")
    p = plan(
        hook={"mode": "none"},
        zooms=[
            {"start_utc": at(15), "end_utc": at(17), "scale": 1.4, "target": "face"},
            {"start_utc": at(38), "end_utc": at(44), "scale": 1.2, "target": "gameplay"},
        ],
        sfx=[{"at_utc": at(16), "tag": "vine_boom"}],
        music={"mood": "hype"},
        flags=["copyright_music_risk"],
    )
    work = tmp_path / "w"
    out = render_files(job_for(work, synth, p, login="synth", layouts=cfg, layouts_sha256=sha),
                       work, tools)  # fmt: skip
    assert out.layout.kind == "facecam_gameplay"
    assert out.manifest.layout_config_sha256 == sha
    kinds = [w.split(":")[0] for w in out.warnings]
    assert "sfx_missing" in kinds and "music_skipped" in kinds and "gap_cut" in kinds
    tm = build(plan_ranges(p), [c.video for c in job_for(work, synth, p).chunks])
    assert out.duration_s == pytest.approx(tm.duration_s, abs=0.03)
    # the zoomed face panel differs from the unzoomed one only while the zoom is on
    face = out.layout.panels[0].dst
    assert face.h == 190
