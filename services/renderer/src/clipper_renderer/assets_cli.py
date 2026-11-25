"""`clipper-renderer assets check`: the asset library's verdicts and measurements (DESIGN §20)."""

from __future__ import annotations

import argparse
import sys
from typing import TextIO

from clipper_core.assets import ManifestError
from clipper_renderer.library import MANIFEST, AssetLibrary
from clipper_renderer.media import Tools
from clipper_renderer.settings import RendererSettings


def check_assets(args: argparse.Namespace, out: TextIO | None = None) -> int:
    """0: every entry usable; 1: some aren't; 2: no library or an invalid manifest."""
    out = out or sys.stdout
    rs = RendererSettings()
    directory = args.dir or rs.assets_dir
    platforms = (
        [p.strip().lower() for p in args.platforms.split(",") if p.strip()]
        if args.platforms
        else rs.asset_platforms
    )
    if not directory:
        out.write("no asset library: set CLIPPER_RENDERER_ASSETS_DIR or pass --dir\n")
        return 2
    lib = AssetLibrary(directory, Tools(rs.ffmpeg, rs.ffprobe), platforms=platforms)
    try:
        verdicts = lib.check()
    except ManifestError as exc:
        out.write(f"invalid manifest: {exc}\n")
        return 2
    if not verdicts:
        out.write(f"no entries ({directory}/{MANIFEST} missing or empty)\n")
        return 2
    bad = 0
    out.write(f"{'id':<24} {'kind':<6} {'labels':<28} {'loudness':>9} {'peak':>7}  verdict\n")
    for v in verdicts:
        labels = ",".join(v.entry.labels())
        if v.usable:
            m = lib.measure(v)
            loud = "-" if m.loudness_lufs is None else f"{m.loudness_lufs:.1f}"
            peak = "-" if m.true_peak_db is None else f"{m.true_peak_db:.1f}"
            verdict = "ok"
        else:
            bad += 1
            loud = peak = ""
            verdict = "; ".join(v.problems)
        out.write(
            f"{v.entry.id:<24} {v.entry.kind:<6} {labels:<28} {loud:>9} {peak:>7}  {verdict}\n"
        )
    out.write(
        f"{len(verdicts) - bad} usable, {bad} not (platforms: {', '.join(platforms) or '-'})\n"
    )
    return 1 if bad else 0
