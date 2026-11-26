"""renderer: `render` jobs -> 9:16 clips (see clipper_renderer.service). DESIGN §20.

    clipper-renderer                                   # the service
    clipper-renderer layout-preview <login> --at <utc> [--stream ID] [--out DIR]
    clipper-renderer assets check [--dir DIR] [--platforms youtube,tiktok]

`layout-preview` (read-only database, reads object storage, needs ffmpeg): takes the frame of the
channel's recording at `--at`, draws the configured rects on it (`frame.png`: red = the facecam
rect, orange = its crop for the facecam panel, green = the gameplay crop, blue = the full_crop
crop) and composes the layout the renderer would use (`layout.png`, 1080x1920), so a facecam
rect in `layouts.toml` can be fitted. `assets check` verifies the asset library (files, sha256,
licences, target platforms) and measures each usable entry; exit 1 if any entry is unusable.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from clipper_core.config import Settings
from clipper_core.contracts import LayoutHint, LayoutKind
from clipper_core.db import create_engine, session_factory
from clipper_core.logs import get_logger, setup_logging
from clipper_core.runtime import run, stop_event_on_signals
from clipper_core.storage import ObjectStore
from clipper_core.timeline import ensure_utc
from clipper_renderer import graph
from clipper_renderer.assets_cli import check_assets
from clipper_renderer.layout import (
    LayoutError,
    Rect,
    crop_to_aspect,
    load_layouts,
    resolve,
    to_pixels,
)
from clipper_renderer.media import FfmpegError, Tools
from clipper_renderer.profiles import get_profile
from clipper_renderer.service import SERVICE, RendererService
from clipper_renderer.settings import RendererSettings

log = get_logger("clipper_renderer")
READ_ONLY = {"options": "-c default_transaction_read_only=on"}


def choose_encoder(settings: RendererSettings, tools: Tools) -> tuple[str, bool]:
    """(encoder, NVENC fallback to libx264 allowed)."""
    if settings.encoder == "x264":
        return "libx264", False
    if settings.encoder == "nvenc":
        return "h264_nvenc", False
    if tools.nvenc_works():
        return "h264_nvenc", True
    log.warning("NVENC not usable, rendering with libx264")
    return "libx264", False


async def amain() -> int:
    settings = Settings(service_name=SERVICE)
    setup_logging(settings)
    try:
        rs = RendererSettings()
        tools = Tools(rs.ffmpeg, rs.ffprobe)
        tools.version()
        load_layouts(rs.layouts_path)
    except (ValueError, LayoutError, FfmpegError, OSError) as exc:
        log.error("invalid renderer configuration (CLIPPER_RENDERER_*)", error=str(exc))
        return 1
    encoder, fallback = await asyncio.to_thread(choose_encoder, rs, tools)
    stop = stop_event_on_signals()
    engine = create_engine(settings)
    try:
        service = RendererService(
            session_factory(engine),
            ObjectStore(settings),
            rs,
            worker_id=settings.worker_id,
            dsn=settings.database_dsn,
            tools=tools,
            encoder=encoder,
            nvenc_fallback=fallback,
        )
        return await service.run(stop)
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- layout-preview

COLOURS = {"facecam": "red", "face": "orange", "gameplay": "green", "main": "blue"}


async def _find_chunk(args: argparse.Namespace, settings: Settings, at: datetime):
    engine = create_engine(settings, connect_args=READ_ONLY)
    try:
        async with session_factory(engine)() as s:
            stream = (
                await s.execute(
                    text(
                        "SELECT s.id, s.category, ch.login FROM streams s"
                        " JOIN channels ch ON ch.id = s.channel_id"
                        " WHERE lower(ch.login) = lower(:login)"
                        " AND (CAST(:sid AS bigint) IS NULL OR s.id = :sid)"
                        " AND s.started_at <= :at ORDER BY s.started_at DESC LIMIT 1"
                    ),
                    {"login": args.login, "sid": args.stream, "at": at},
                )
            ).first()
            if stream is None:
                return None, None
            chunk = (
                await s.execute(
                    text(
                        "SELECT seq, start_utc, video_key FROM chunks WHERE stream_id = :s"
                        " AND video_key IS NOT NULL AND start_utc <= :at AND end_utc > :at"
                        " ORDER BY seq LIMIT 1"
                    ),
                    {"s": stream.id, "at": at},
                )
            ).first()
            return stream, chunk
    finally:
        await engine.dispose()


def _draw_preview(args, store, rs: RendererSettings, stream, chunk, at) -> int:
    tools = Tools(rs.ffmpeg, rs.ffprobe)
    cfg, _ = load_layouts(rs.layouts_path)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="layout-preview-") as tmp:
        src = Path(tmp) / "chunk.ts"
        store.download_file(chunk.video_key, src)
        info = tools.probe(src)
        offset = max((at - ensure_utc(chunk.start_utc)).total_seconds() + info.origin_s, 0.0)
        source = (info.width, info.height)
        profile = get_profile("short_1080x1920")
        hint = LayoutHint(kind=LayoutKind(args.layout))
        lay = resolve(cfg, stream.login, stream.category, hint, source=source, profile=profile)
        boxes: list[tuple[str, Rect]] = [(p.name, p.src) for p in lay.panels]
        chan = cfg.channel(stream.login)
        cat = chan.category(stream.category) if chan else None
        facecam = (cat.facecam if cat else None) or (chan.facecam if chan else None)
        if facecam is not None:
            boxes.insert(0, ("facecam", to_pixels(facecam, source)))
        if lay.kind != LayoutKind.FULL_CROP:
            boxes.append(("main", crop_to_aspect(Rect(0, 0, *source), 1080 / 1920)))
        draw = ",".join(
            f"drawbox=x={r.x}:y={r.y}:w={r.w}:h={r.h}:color={COLOURS[name]}@0.9:t=4"
            for name, r in boxes
        )
        # seek early on the input, exactly on the output (TS seeks land a GOP late, graph.py)
        early = max(offset - graph.SEEK_PREROLL_S, 0.0)
        seek = ["-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{early:.3f}", "-i", str(src), "-ss", f"{offset - early:.3f}",
                "-frames:v", "1"]  # fmt: skip
        tools.run([*seek, "-vf", draw, str(out_dir / "frame.png")])
        spec = graph.GraphSpec((), {}, graph.output_fps(info.fps, profile), profile, lay, "libx264")
        chains = ["[0:v:0]null[vcat]", *graph.layout_filters(spec)]
        tools.run([*seek, "-filter_complex", ";".join(chains), "-map", "[canvas]",
                   str(out_dir / "layout.png")])  # fmt: skip
    sys.stdout.write(
        f"stream {stream.id} ({stream.login}, {stream.category}) chunk {chunk.seq} at "
        f"{offset:.2f} s; layout {lay.kind.value}"
        + (f" ({'; '.join(lay.warnings)})" if lay.warnings else "")
        + f"\n{out_dir / 'frame.png'}: "
        + ", ".join(f"{COLOURS[n]} = {n} {r.as_list()}" for n, r in boxes)
        + f"\n{out_dir / 'layout.png'}: the composed 1080x1920 layout\n"
    )
    return 0


async def layout_preview(args: argparse.Namespace) -> int:
    settings = Settings(service_name=SERVICE)
    setup_logging(settings)
    rs = RendererSettings()
    at = ensure_utc(datetime.fromisoformat(args.at))
    stream, chunk = await _find_chunk(args, settings, at)
    if stream is None:
        log.error("no stream of that channel at that time", login=args.login)
        return 2
    if chunk is None:
        log.error("no chunk with video at that time", stream_id=stream.id, at=at.isoformat())
        return 2
    store = ObjectStore(settings)
    return await asyncio.to_thread(_draw_preview, args, store, rs, stream, chunk, at)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="clipper-renderer", description="Renderer (DESIGN 20). Without a command: the service."
    )
    sub = p.add_subparsers(dest="cmd", metavar="command")
    lp = sub.add_parser("layout-preview", help="draw a channel's layout rects on a recorded frame")
    lp.add_argument("login")
    lp.add_argument("--at", required=True, help="UTC time, ISO 8601 with an offset (e.g. ...Z)")
    lp.add_argument("--stream", type=int, help="the stream id (default: the channel's latest)")
    lp.add_argument("--layout", default="auto", choices=[k.value for k in LayoutKind])
    lp.add_argument("--out", default="data/layout-preview", help="output directory")
    assets = sub.add_parser("assets", help="the SFX/music library")
    asub = assets.add_subparsers(dest="assets_cmd", metavar="command", required=True)
    chk = asub.add_parser("check", help="verify files, sha256 and licences; measure loudness")
    chk.add_argument("--dir", help="the library (default CLIPPER_RENDERER_ASSETS_DIR)")
    chk.add_argument(
        "--platforms", help="comma-separated (default CLIPPER_RENDERER_ASSET_PLATFORMS)"
    )
    args = p.parse_args(list(argv))
    if args.cmd == "layout-preview":
        try:
            ensure_utc(datetime.fromisoformat(args.at.replace("Z", "+00:00")))
        except ValueError as exc:
            p.error(f"--at: {exc}")
        args.at = args.at.replace("Z", "+00:00")
    return args


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.cmd == "layout-preview":
        sys.exit(run(lambda: layout_preview(args)))
    if args.cmd == "assets":
        sys.exit(check_assets(args))
    sys.exit(run(amain))


if __name__ == "__main__":
    main()
