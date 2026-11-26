"""`clipper-renderer layout-preview` (drawing part) and `assets check`; the encoder choice."""

import argparse
import asyncio
import io
import shutil
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from clipper_core.assets import file_sha256
from clipper_renderer import main
from clipper_renderer.assets_cli import check_assets
from clipper_renderer.media import Tools
from clipper_renderer.settings import RendererSettings
from clipper_renderer.synthetic import make_chunk

T0 = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def tools() -> Tools:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not on PATH")
    return Tools()


class FileStore:
    def __init__(self, files):
        self.files = files

    def download_file(self, key, path):
        shutil.copyfile(self.files[key], path)


def test_layout_preview_draws_the_rects_and_composes_the_layout(
    tools, tmp_path, monkeypatch, capsys
):
    chunk = make_chunk(tools, tmp_path / "c.ts", t0=T0, start_s=0, duration_s=6, size=(640, 360))
    layouts = tmp_path / "layouts.toml"
    layouts.write_bytes(b"[channels.synth]\nfacecam = { x = 0.0, y = 0.0, w = 0.25, h = 0.3 }\n")
    rs = RendererSettings(_env_file=None, layouts_path=layouts)
    args = argparse.Namespace(out=str(tmp_path / "out"), layout="auto")
    stream = SimpleNamespace(id=1, login="synth", category="Fortnite")
    row = SimpleNamespace(seq=3, start_utc=chunk.start_utc, video_key="k")
    code = main._draw_preview(
        args, FileStore({"k": chunk.path}), rs, stream, row, T0 + timedelta(seconds=4)
    )
    assert code == 0
    frame, composed = (
        tools.probe(tmp_path / "out" / "frame.png"),
        tools.probe(tmp_path / "out" / "layout.png"),
    )
    assert (frame.width, frame.height) == (640, 360)
    assert (composed.width, composed.height) == (1080, 1920)
    out = capsys.readouterr().out
    assert "layout facecam_gameplay" in out and "red = facecam [0, 0, 160, 108]" in out
    assert "green = gameplay" in out and "orange = face" in out


def test_parse_args_rejects_a_naive_time():
    with pytest.raises(SystemExit):
        main.parse_args(["layout-preview", "x", "--at", "2026-11-25T12:00:00"])
    args = main.parse_args(["layout-preview", "x", "--at", "2026-11-25T12:00:00Z"])
    assert args.at == "2026-11-25T12:00:00+00:00"


def test_assets_check_reports_usable_and_broken_entries(tools, tmp_path, monkeypatch):
    for name in ("CLIPPER_RENDERER_ASSETS_DIR", "CLIPPER_RENDERER_ASSET_PLATFORMS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    (tmp_path / "sfx").mkdir()
    tools.run(["-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=800:d=0.4",
               str(tmp_path / "sfx" / "a.wav")])  # fmt: skip
    good = file_sha256(tmp_path / "sfx" / "a.wav")
    lic = (
        '[assets.licence]\nname = "t"\nsource_url = "u"\nlicensee = "me"\nproof = "p"\n'
        'platforms = ["youtube"]\n'
    )
    (tmp_path / "manifest.toml").write_text(
        f'[[assets]]\nid = "a"\nfile = "sfx/a.wav"\nsha256 = "{good}"\nkind = "sfx"\n'
        f'tags = ["boom"]\nduration_s = 0.4\n{lic}'
        f'[[assets]]\nid = "b"\nfile = "sfx/a.wav"\nsha256 = "{"0" * 64}"\nkind = "sfx"\n'
        f'tags = ["boom"]\nduration_s = 0.4\n{lic}'
        f'[[assets]]\nid = "c"\nfile = "sfx/missing.wav"\nsha256 = "{good}"\nkind = "sfx"\n'
        f'tags = ["boom"]\nduration_s = 0.4\n{lic}',
        encoding="utf-8",
    )
    out = io.StringIO()
    code = check_assets(argparse.Namespace(dir=str(tmp_path), platforms=None), out)
    lines = out.getvalue().splitlines()
    assert code == 1
    assert lines[1].startswith("a ") and lines[1].endswith("ok")
    assert "sha256 mismatch" in lines[2] and "file missing: sfx/missing.wav" in lines[3]
    out = io.StringIO()
    code = check_assets(argparse.Namespace(dir=str(tmp_path), platforms="youtube,tiktok"), out)
    assert code == 1 and "licence doesn't cover tiktok" in out.getvalue()
    (tmp_path / "manifest.toml").write_text("[[assets]\n", encoding="utf-8")
    assert check_assets(argparse.Namespace(dir=str(tmp_path), platforms=None), io.StringIO()) == 2


def test_choose_encoder():
    class Probe(Tools):
        def __init__(self, works):
            super().__init__()
            self.works = works

        def nvenc_works(self):
            return self.works

    rs = lambda e: RendererSettings(_env_file=None, encoder=e)  # noqa: E731
    assert main.choose_encoder(rs("x264"), Probe(True)) == ("libx264", False)
    assert main.choose_encoder(rs("nvenc"), Probe(False)) == ("h264_nvenc", False)
    assert main.choose_encoder(rs("auto"), Probe(True)) == ("h264_nvenc", True)
    assert main.choose_encoder(rs("auto"), Probe(False)) == ("libx264", False)
    assert RendererSettings(_env_file=None, asset_platforms="YouTube, tiktok,").asset_platforms == [
        "youtube",
        "tiktok",
    ]


@pytest.mark.parametrize(
    ("argv", "target"),
    [([], "amain"), (["layout-preview", "x", "--at", "2026-11-25T12:00:00Z"], "layout_preview")],
)
def test_main_hands_run_a_coroutine_function(monkeypatch, argv, target):
    """`runtime.run` takes the function, not a coroutine (7c: the service crash-looped)."""
    called = []

    async def fake(*args):
        called.append(args)
        return 0

    monkeypatch.setattr(main, target, fake)
    monkeypatch.setattr(main, "run", lambda fn: asyncio.run(fn()))
    with pytest.raises(SystemExit) as exc:
        main.main(argv)
    assert exc.value.code == 0
    assert len(called) == 1
