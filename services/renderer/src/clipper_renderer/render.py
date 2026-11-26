"""One render from files on disk (DESIGN §20, steps 3-6): time map, layout, captions, effects,
the ffmpeg graph with two-pass loudness, the outputs, the manifest. No database and no object
storage: the handler pins and downloads the chunks into `<work>/in/` and uploads the outputs.
Blocking (ffmpeg): call it from a worker thread.

    out = render_files(job, work_dir, tools)
    out.files["clip.mp4"], out.graph_hash, out.manifest

Same job + same input files -> the same graph hash; with libx264 the same bytes (NVENC output
depends on the driver).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from clipper_core.assets import file_sha256
from clipper_core.contracts import EditPlan, HookMode, RenderAsset, RenderManifest, RenderSource
from clipper_core.logs import get_logger
from clipper_renderer import __version__, captions, fontset, graph, timemap
from clipper_renderer.captions import WordLike
from clipper_renderer.fontset import FONT_LICENCE, FontFile
from clipper_renderer.layout import LayoutConfig, ResolvedLayout, resolve
from clipper_renderer.library import Verdict, pick
from clipper_renderer.media import FfmpegError, Tools, number, parse_ebur128, parse_loudnorm
from clipper_renderer.profiles import Profile

log = get_logger(__name__)

CLIP, PREVIEW, THUMB, CAPTIONS, MANIFEST = (
    "clip.mp4",
    "preview.mp4",
    "thumb.jpg",
    "captions.ass",
    "render.json",
)
COPYRIGHT_FLAG = "copyright_music_risk"
MUSIC_CUE = 0


@dataclass(frozen=True, slots=True)
class LocalChunk:
    video: timemap.ChunkVideo  # times, ids and the pinned key
    path: Path  # the downloaded file, inside `<work>/in/`


@dataclass(frozen=True)
class RenderJob:
    render_id: int
    plan_id: int
    plan: EditPlan
    profile: Profile
    chunks: Sequence[LocalChunk]
    words: Sequence[WordLike] = ()
    encoder: str = "libx264"  # libx264 | h264_nvenc
    nvenc_fallback: bool = True  # an NVENC failure renders again with libx264
    login: str | None = None
    category: str | None = None
    layouts: LayoutConfig = field(default_factory=LayoutConfig)
    layouts_sha256: str | None = None
    assets: Sequence[Verdict] = ()  # the usable library entries
    normalisation_db: Callable[[Verdict], float] = lambda v: 0.0
    fonts: Sequence[FontFile] = ()
    timeout_s: float = 1200.0


@dataclass(frozen=True)
class RenderOutput:
    files: dict[str, Path]
    manifest: RenderManifest
    graph_hash: str
    layout: ResolvedLayout
    encoder: str
    warnings: list[str]
    duration_s: float
    fps: float
    width: int
    height: int
    loudness_lufs: float | None
    true_peak_db: float | None
    video_sha256: str
    bytes: int
    preview_bytes: int
    ffmpeg_s: float


@dataclass(frozen=True, slots=True)
class _Effects:
    zooms: list[graph.ZoomOut]
    sfx: list[graph.SfxInput]
    music: graph.MusicInput | None
    assets: list[RenderAsset]
    files: dict[str, Path]  # work-relative path -> library file
    warnings: list[str]


def _effects(job: RenderJob, tm: timemap.TimeMap, lay: ResolvedLayout) -> _Effects:
    plan = job.plan
    warnings: list[str] = []
    zooms = [
        graph.ZoomOut(lay.zoom_panel(z.target).name, a, b, z.scale)
        for z in plan.zooms
        for a, b in tm.out_ranges(z.start_utc, z.end_utc)
        if b - a > 0.02
    ]
    sfx_verdicts = [v for v in job.assets if v.entry.kind == "sfx"]
    music_verdicts = [v for v in job.assets if v.entry.kind == "music"]
    sfx: list[graph.SfxInput] = []
    used: list[RenderAsset] = []
    files: dict[str, Path] = {}

    def install(v: Verdict) -> str:
        rel = f"assets/{v.entry.id}{v.path.suffix.lower()}"
        files[rel] = v.path
        return rel

    for i, cue in enumerate(plan.sfx):
        v = pick(sfx_verdicts, cue.tag, plan.clip_id, i)
        if v is None:
            warnings.append(f"sfx_missing: no usable asset tagged {cue.tag!r}")
            continue
        at = tm.clip_out(cue.at_utc)
        if at is None:
            warnings.append(f"sfx_cut: cue {i} at {cue.at_utc.isoformat()} is not in the output")
            continue
        gain = round(cue.gain_db + job.normalisation_db(v), 2)
        sfx.append(graph.SfxInput(install(v), at, gain))
        used.append(
            RenderAsset(
                id=v.entry.id,
                kind="sfx",
                sha256=v.entry.sha256,
                licence=v.entry.licence.name,
                at_s=at,
            )
        )
    music = None
    if plan.music is not None:
        if COPYRIGHT_FLAG in plan.flags:
            warnings.append(f"music_skipped: the plan flags {COPYRIGHT_FLAG}")
        elif (v := pick(music_verdicts, plan.music.mood.value, plan.clip_id, MUSIC_CUE)) is None:
            warnings.append(f"music_missing: no usable track for mood {plan.music.mood.value!r}")
        else:
            gain = round(plan.music.gain_db + job.normalisation_db(v), 2)
            music = graph.MusicInput(install(v), gain, duck=plan.music.duck)
            used.append(
                RenderAsset(
                    id=v.entry.id, kind="music", sha256=v.entry.sha256, licence=v.entry.licence.name
                )
            )
    return _Effects(zooms, sfx, music, used, files, warnings)


def _thumb_time(plan: EditPlan, tm: timemap.TimeMap) -> float:
    """The plan's thumbnail time; else a teaser's start; else the moment (T) in the clip."""
    if plan.thumbnail_utc is not None and (t := tm.clip_out(plan.thumbnail_utc)) is not None:
        return t
    if plan.hook.mode == HookMode.TEASER:
        return 0.0
    t = tm.clip_out(plan.anchor_utc)
    return t if t is not None else 0.0


def graph_hash(doc: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def render_files(job: RenderJob, work: Path, tools: Tools) -> RenderOutput:
    plan = job.plan
    work = work.resolve()
    tm = timemap.build(timemap.plan_ranges(plan), [c.video for c in job.chunks])
    used_ids = {p.chunk.chunk_id for p in tm.pieces}
    local = {c.video.chunk_id: c for c in job.chunks}
    infos = {cid: tools.probe(local[cid].path) for cid in sorted(used_ids)}
    first = infos[tm.pieces[0].chunk.chunk_id]
    fps = graph.output_fps(first.fps, job.profile)
    lay = resolve(
        job.layouts,
        job.login,
        job.category,
        plan.layout,
        source=(first.width, first.height),
        profile=job.profile,
    )
    sources = {}
    for cid, info in infos.items():
        rel = local[cid].path.resolve().relative_to(work).as_posix()
        sources[cid] = graph.SourceFile(rel, info.width, info.height, info.origin_s, info.has_audio)
    subs = captions.build_ass(job.words, plan, tm, lay)
    fx = _effects(job, tm, lay)
    warnings = [*tm.warnings, *lay.warnings, *fx.warnings]
    if subs.dropped:
        warnings.append(f"words_dropped: {subs.dropped} words cross a cut")
    for cid, info in infos.items():
        if (info.width, info.height) != (first.width, first.height):
            warnings.append(f"source_scaled: chunk {cid} is {info.width}x{info.height}")

    (work / CAPTIONS).write_bytes(subs.text.encode("utf-8"))
    fontset.install(list(job.fonts), work / "fonts")
    for rel, src in fx.files.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, work / rel)

    spec = graph.GraphSpec(
        pieces=tm.pieces,
        sources=sources,
        fps=fps,
        profile=job.profile,
        layout=lay,
        encoder=job.encoder,
        zooms=fx.zooms,
        subtitles=CAPTIONS,
        fonts_dir="fonts",
        sfx=fx.sfx,
        music=fx.music,
    )
    measure = graph.measure_command(spec)
    m = tools.run(measure.args, cwd=work, timeout_s=job.timeout_s)
    loud = parse_ebur128(m.stderr)
    if loud.silent:
        warnings.append("silent_audio: not normalised")
    seconds = m.seconds
    cmd = graph.render_command(spec, loud)
    try:
        r = tools.run(cmd.args, cwd=work, timeout_s=job.timeout_s)
    except FfmpegError as exc:
        if not (spec.encoder == "h264_nvenc" and job.nvenc_fallback and exc.nvenc):
            raise
        log.warning("NVENC failed, rendering with libx264", error=str(exc)[:300])
        warnings.append("nvenc_fallback: NVENC failed, rendered with libx264")
        spec = replace(spec, encoder="libx264")
        cmd = graph.render_command(spec, loud)
        r = tools.run(cmd.args, cwd=work, timeout_s=job.timeout_s)
    seconds += r.seconds
    if not loud.silent and parse_loudnorm(r.stderr).get("normalization_type") == "dynamic":
        warnings.append("loudness_dynamic: the gain would pass the true-peak target, limited")

    # inside the clip by at least two frames (a seek into the last frame can yield no image)
    thumb_at = max(0.0, min(_thumb_time(plan, tm), tm.duration_s - 2 / float(fps)))
    thumb = [
        "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{thumb_at:.3f}",
        "-i", CLIP, "-frames:v", "1", "-q:v", "3", "-update", "1",
        "-fflags", "+bitexact", "-flags:v", "+bitexact", THUMB,
    ]  # fmt: skip
    seconds += tools.run(thumb, cwd=work, timeout_s=120).seconds
    final = tools.run(
        ["-nostdin", "-hide_banner", "-nostats", "-i", CLIP, "-vn",
         "-af", graph.MEASURE, "-f", "null", "-"],
        cwd=work,
        timeout_s=job.timeout_s,
    )  # fmt: skip
    delivered = parse_ebur128(final.stderr)
    out_info = tools.probe(work / CLIP)
    clip_bytes = (work / CLIP).stat().st_size
    preview_bytes = (work / PREVIEW).stat().st_size
    if preview_bytes > job.profile.preview_max_bytes:
        warnings.append(f"preview_too_large: {preview_bytes} bytes")
    video_sha = file_sha256(work / CLIP)

    font_assets = [
        RenderAsset(id=f.id, kind="font", sha256=f.sha256, licence=FONT_LICENCE) for f in job.fonts
    ]
    render_sources = [
        RenderSource(
            chunk_id=p.chunk.chunk_id,
            seq=p.chunk.seq,
            key=p.chunk.key,
            start_utc=p.chunk.start_utc,
            trim_from_s=p.trim_from_s,
            trim_to_s=p.trim_to_s,
            out_at_s=p.out_at_s,
            pad_after_s=p.pad_after_s,
            teaser=p.teaser,
            origin_s=sources[p.chunk.chunk_id].origin_s,
        )
        for p in tm.pieces
    ]
    ghash = graph_hash(
        {
            "plan": plan.model_dump(mode="json"),
            "profile": job.profile.name,
            "encoder": spec.encoder,
            "sources": [s.model_dump(mode="json") for s in render_sources],
            "measure": measure.args,
            "render": cmd.args,
            "thumb": thumb,
            "captions": hashlib.sha256(subs.text.encode()).hexdigest(),
            "layout": lay.as_dict(),
            "layout_config": job.layouts_sha256,
            "assets": [a.model_dump(mode="json") for a in fx.assets],
            "fonts": [[f.name, f.sha256] for f in job.fonts],
            "renderer": __version__,
            "ffmpeg": tools.version(),
        }
    )
    loudness = None if delivered.silent else number(delivered.i)
    peak = number(delivered.tp)
    manifest = RenderManifest(
        render_id=job.render_id,
        clip_id=plan.clip_id,
        plan_id=job.plan_id,
        profile=job.profile.name,
        renderer_version=__version__,
        ffmpeg_version=tools.version(),
        encoder=spec.encoder,
        graph_hash=ghash,
        layout=lay.as_dict(),
        layout_config_sha256=job.layouts_sha256,
        sources=render_sources,
        assets=[*font_assets, *fx.assets],
        width=out_info.width,
        height=out_info.height,
        fps=float(fps),
        duration_s=round(out_info.duration_s or tm.duration_s, 3),
        loudness_lufs=loudness,
        true_peak_db=peak,
        video_sha256=video_sha,
        warnings=warnings,
    )
    (work / MANIFEST).write_bytes(manifest.model_dump_json(indent=2).encode())
    return RenderOutput(
        files={n: work / n for n in (CLIP, PREVIEW, THUMB, CAPTIONS, MANIFEST)},
        manifest=manifest,
        graph_hash=ghash,
        layout=lay,
        encoder=spec.encoder,
        warnings=warnings,
        duration_s=manifest.duration_s,
        fps=float(fps),
        width=out_info.width,
        height=out_info.height,
        loudness_lufs=loudness,
        true_peak_db=peak,
        video_sha256=video_sha,
        bytes=clip_bytes,
        preview_bytes=preview_bytes,
        ffmpeg_s=round(seconds, 3),
    )
