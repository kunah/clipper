"""`clipper-judge label`: a blind labelling set for the judge's eval (DESIGN §19; decided by the
user 2026-11-25).

    uv run clipper-judge label prepare --stream 2415 1930 1412 \\
        --drafts data/labels/drafts_loose.json --max-drafts 25 --out data/labels/v1
    uv run clipper-judge label serve data/labels/v1 --open       # http://127.0.0.1:8765
    uv run clipper-judge eval --labels data/labels/v1/labels.json \\
        --drafts data/labels/v1/drafts.json --model claude-cli --yes

**prepare** (read-only on the database, reads object storage, needs ffmpeg on PATH): the cases
are the emitted candidates of `--stream`/`--candidate` and drafts of a detector export
(`--drafts`, `clipper-detector backtest --export-drafts`), the drafts at least `--min-gap-s` (90)
from every other case's peak in their stream, at most `--max-drafts`, spread evenly over time. A
case without video near its moment (the service's `no_video` rule) is left out. Each case's video
is its round-0 context (the candidate window) cut from the chunks' video into `video/<key>.mp4`
(540p30 H.264: NVENC when ffmpeg has it, else libx264; AAC audio), one piece per chunk, so a gap
in the recording is a jump in the video; the page maps video time to UTC through the pieces. A
video already cut for the same context is kept (`video/<key>.json`), so a rerun only adds what is
new. Writes `cases.json` (the set) and `drafts.json` (its drafts, for `eval --drafts`).

**serve**: a local page (127.0.0.1 only; nothing leaves the PC) over a prepared set: the video
with the moment marked, mark in/out, clip/reject, a note. Blind: no model answer, trigger,
score or case id is shown, and the cases come ordered by an opaque key, which mixes candidates
and drafts. Every change rewrites `labels.json` atomically (evalset's JSON form: case id ->
{verdict, in_s, out_s, note}; the cut in seconds from T, the candidate's peak).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import webbrowser
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_core.config import Settings
from clipper_core.contracts import CLIP_MIN, CandidateStatus
from clipper_core.db import create_engine, session_factory
from clipper_core.logs import get_logger, setup_logging
from clipper_core.runtime import run
from clipper_core.storage import ObjectStore
from clipper_core.timeline import Interval, ensure_utc, utc_now
from clipper_judge import reads
from clipper_judge.evalset import (
    Draft,
    EvalSetError,
    drafts_doc,
    load_drafts,
    load_labels,
    parse_label,
)
from clipper_judge.reads import CandidateInfo, StreamInfo

log = get_logger(__name__)

SET_FORMAT = "clipper-judge-label-set"
SET_VERSION = 1
READ_ONLY = {"options": "-c default_transaction_read_only=on"}
DEFAULT_PORT = 8765
MIN_PIECE_S = 0.5  # shorter pieces of a chunk are left out
HEIGHT = 540
FPS = 30
VIDEO_FORMAT = 1  # bump when the cut's settings change: videos are cut again
_KEY = re.compile(r"^k[0-9a-f]{8}$")


class LabelError(ValueError):
    """Invalid arguments or an unreadable set (exit 2)."""


# --------------------------------------------------------------------------- arguments


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    lab = sub.add_parser(
        "label",
        help="a blind labelling set for the eval: prepare (cut videos), serve (a local page)",
        description="Prepare a labelling set (candidates + detector drafts, videos cut from the "
        "recordings) and serve a local page to label it blind (DESIGN 19).",
    )
    cmds = lab.add_subparsers(dest="label_cmd", required=True, metavar="prepare|serve")
    p = cmds.add_parser("prepare", help="select the cases and cut their videos (read-only DB)")
    p.add_argument("--out", type=Path, required=True, metavar="DIR", help="the set's directory")
    p.add_argument("--stream", type=int, nargs="+", action="extend", default=[], metavar="ID")
    p.add_argument("--candidate", type=int, nargs="+", action="extend", default=[], metavar="ID")
    p.add_argument("--drafts", type=Path, metavar="FILE", help="a detector drafts export")
    p.add_argument("--max-drafts", type=int, default=25, metavar="N", help="at most N drafts (25)")
    p.add_argument(
        "--min-gap-s", type=float, default=90.0, metavar="S",
        help="a draft's peak at least S from every other case's peak in its stream (90)",
    )  # fmt: skip
    p.add_argument(
        "--encoder", choices=("auto", "h264_nvenc", "libx264"), default="auto",
        help="the video encoder (auto: NVENC when ffmpeg has it)",
    )  # fmt: skip
    p.add_argument("--ffmpeg", default="ffmpeg", metavar="PATH")
    s = cmds.add_parser("serve", help="the labelling page on 127.0.0.1")
    s.add_argument("dir", type=Path, metavar="DIR", help="a prepared set")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--open", action="store_true", help="open the page in the browser")
    return lab


def check_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.label_cmd == "prepare":
        if not (args.stream or args.candidate or args.drafts):
            parser.error("choose the cases: --stream, --candidate and/or --drafts")
        if args.max_drafts < 0:
            parser.error("--max-drafts must be >= 0")


# --------------------------------------------------------------------------- the set


def case_key(case_id: str) -> str:
    """Opaque and stable: the page's order (by key) mixes candidates and drafts."""
    return "k" + hashlib.sha1(case_id.encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True, slots=True)
class Piece:
    """One chunk's part of a case's video: `video_s` into the video = `start` (UTC)."""

    video_s: float
    start: datetime
    duration_s: float
    chunk_key: str
    offset_s: float  # into the chunk's file

    def as_dict(self) -> dict[str, Any]:
        return {"v": round(self.video_s, 3), "u": self.start.isoformat(), "d": self.duration_s}


@dataclass(frozen=True, slots=True)
class ChunkVideo:
    start: datetime
    end: datetime
    video_key: str


def plan_pieces(context: Interval, chunks: Sequence[ChunkVideo]) -> list[Piece]:
    """The parts of the chunks inside the context, in time order, never overlapping (pure)."""
    pieces: list[Piece] = []
    v = 0.0
    last_end: datetime | None = None
    for c in sorted(chunks, key=lambda c: (c.start, c.end)):
        a = max(ensure_utc(c.start), context.start)
        b = min(ensure_utc(c.end), context.end)
        if last_end is not None:
            a = max(a, last_end)
        dur = round((b - a).total_seconds(), 3)
        if dur < MIN_PIECE_S:
            continue
        offset = round((a - ensure_utc(c.start)).total_seconds(), 3)
        pieces.append(Piece(v, a, dur, c.video_key, offset))
        v += dur
        last_end = b
    return pieces


@dataclass
class LabelCase:
    case_id: str
    candidate: CandidateInfo
    stream: StreamInfo
    draft: Mapping[str, Any] | None = None  # the exported draft (drafts only)

    @property
    def key(self) -> str:
        return case_key(self.case_id)

    @property
    def context(self) -> Interval:
        return Interval(
            ensure_utc(self.candidate.window_from), ensure_utc(self.candidate.window_to)
        )


def spread(items: Sequence[Any], n: int) -> list[Any]:
    """At most `n` of `items`, evenly over the sequence (deterministic)."""
    if n <= 0:
        return []
    if len(items) <= n:
        return list(items)
    return [items[int(i * len(items) / n)] for i in range(n)]


def pick_drafts(
    drafts: Sequence[tuple[str, CandidateInfo, Mapping[str, Any]]],
    taken: Sequence[CandidateInfo],
    *,
    max_drafts: int,
    min_gap_s: float,
) -> list[tuple[str, CandidateInfo, Mapping[str, Any]]]:
    """Drafts away from every taken case (and from each other) in their stream, then spread."""
    gap = timedelta(seconds=min_gap_s)
    kept: list[tuple[str, CandidateInfo, Mapping[str, Any]]] = []
    peaks: dict[int, list[datetime]] = {}
    for c in taken:
        peaks.setdefault(c.stream_id, []).append(ensure_utc(c.peak_utc))
    ordered = sorted(drafts, key=lambda d: (d[1].stream_id, ensure_utc(d[1].peak_utc)))
    for d in ordered:
        peak = ensure_utc(d[1].peak_utc)
        if any(abs(peak - p) < gap for p in peaks.get(d[1].stream_id, ())):
            continue
        kept.append(d)
        peaks.setdefault(d[1].stream_id, []).append(peak)
    return spread(kept, max_drafts)


async def select_cases(
    session: AsyncSession, args: argparse.Namespace, *, now: datetime
) -> list[LabelCase]:
    ids: list[int] = list(dict.fromkeys(args.candidate))
    if args.stream:
        rows = (
            await session.execute(
                text(
                    "SELECT id FROM candidates WHERE stream_id = ANY(:s) AND status = :st"
                    " ORDER BY stream_id, peak_utc, id"
                ),
                {"s": list(args.stream), "st": CandidateStatus.EMITTED.value},
            )
        ).all()
        ids += [r.id for r in rows if r.id not in ids]
    streams: dict[int, StreamInfo] = {}

    async def stream_of(sid: int) -> StreamInfo:
        if sid not in streams:
            streams[sid] = await reads.stream(session, sid)
        return streams[sid]

    cases: list[LabelCase] = []
    skipped = 0
    for cid in ids:
        cand = await reads.candidate(session, cid)
        if cand is None:
            raise LabelError(f"unknown candidate id {cid}")
        info = await stream_of(cand.stream_id)
        if await reads.no_video(session, cand, info, now=now, min_s=CLIP_MIN.total_seconds()):
            skipped += 1
            continue
        cases.append(LabelCase(str(cid), cand, info))
    if args.drafts:
        try:
            loaded = load_drafts(args.drafts)
        except EvalSetError as exc:
            raise LabelError(str(exc)) from exc
        usable = []
        for d in loaded:
            info = await stream_of(d.candidate.stream_id)
            if await reads.no_video(
                session, d.candidate, info, now=now, min_s=CLIP_MIN.total_seconds()
            ):
                skipped += 1
                continue
            usable.append((d.id, d.candidate, d.raw))
        chosen = pick_drafts(
            usable,
            [c.candidate for c in cases],
            max_drafts=args.max_drafts,
            min_gap_s=args.min_gap_s,
        )
        for draft_id, cand, raw in chosen:
            cases.append(LabelCase(draft_id, cand, await stream_of(cand.stream_id), draft=raw))
    log.info("label cases", cases=len(cases), no_video=skipped)
    return cases


async def chunk_videos(
    session: AsyncSession, stream_id: int, context: Interval
) -> list[ChunkVideo]:
    rows = (
        await session.execute(
            text(
                "SELECT start_utc, end_utc, video_key FROM chunks"
                " WHERE stream_id = :s AND video_key IS NOT NULL"
                " AND end_utc > :a AND start_utc < :b ORDER BY start_utc, seq"
            ),
            {"s": stream_id, "a": context.start, "b": context.end},
        )
    ).all()
    return [ChunkVideo(r.start_utc, r.end_utc, r.video_key) for r in rows]


# --------------------------------------------------------------------------- cutting


def pick_encoder(ffmpeg: str, choice: str) -> str:
    if choice != "auto":
        return choice
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise LabelError(f"ffmpeg not usable ({ffmpeg}): {exc}") from exc
    return "h264_nvenc" if " h264_nvenc " in out else "libx264"


SAMPLE_RATE = 48000
# as the renderer (§20): open each piece this far before it and trim by timestamp, since ffmpeg's
# MPEG-TS seek lands up to a GOP late (a plain `-ss X` lost the video of a piece near a chunk's end)
SEEK_PREROLL_S = 5.0


def codec_args(encoder: str) -> list[str]:
    video = (
        ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "30"]
        if encoder == "h264_nvenc"
        else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28"]
    )
    return [*video, "-g", str(FPS), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-ac", "2"]


def piece_command(
    ffmpeg: str,
    source: str,
    piece: Piece,
    encoder: str,
    out: str,
    *,
    fade_in_s: float = 0.0,
    fade_out_s: float = 0.0,
) -> list[str]:
    """One piece as its own file of exactly its length on both tracks (frames on the FPS grid,
    samples at 48 kHz; a short track is padded), so the pieces concatenate without drift.
    `fade_in_s`/`fade_out_s`: an audio fade at the piece's start/end (a jump in a preview)."""
    ss = max(piece.offset_s - SEEK_PREROLL_S, 0.0)
    pre = piece.offset_s - ss
    frames = max(round(piece.duration_s * FPS), 1)
    samples = max(round(piece.duration_s * SAMPLE_RATE), 1)
    fades = ""
    if fade_in_s > 0:
        fades += f",afade=t=in:st=0:d={fade_in_s:.3f}"
    if fade_out_s > 0:
        start = max(piece.duration_s - fade_out_s, 0.0)
        fades += f",afade=t=out:st={start:.3f}:d={fade_out_s:.3f}"
    graph = (
        f"[0:v:0]trim=start={pre:.3f},setpts=PTS-{pre:.3f}/TB,scale=-2:{HEIGHT},"
        f"fps=fps={FPS}:start_time=0:round=near,"
        f"tpad=stop_mode=clone:stop_duration={piece.duration_s:.3f},trim=end_frame={frames},"
        f"setpts=N/{FPS}/TB[v];"
        f"[0:a:0]atrim=start={pre:.3f},asetpts=PTS-{pre:.3f}/TB,"
        f"aresample={SAMPLE_RATE}:async=1:first_pts=0,apad,atrim=end_sample={samples},"
        f"asetpts=N/{SAMPLE_RATE}/TB{fades}[a]"
    )
    return [
        ffmpeg, "-v", "error", "-y",
        "-ss", f"{ss:.3f}", "-t", f"{pre + piece.duration_s + 1.0:.3f}", "-i", source,
        "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
        *codec_args(encoder), out,
    ]  # fmt: skip


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {proc.stderr.strip()[-600:]}")


class Cutter:
    """Cuts a case's video from the chunks (each chunk downloaded once per run)."""

    def __init__(
        self, store: Any, ffmpeg: str, encoder: str, work: Path, *, fade_s: float = 0.0
    ) -> None:
        self.store = store
        self.ffmpeg = ffmpeg
        self.encoder = encoder
        self.work = work
        self.fade_s = fade_s  # audio fade where `cut`'s `jumps` say a piece starts/ends a jump
        self.files: dict[str, Path] = {}

    def chunk_file(self, key: str) -> Path:
        if key not in self.files:
            path = self.work / "chunks" / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".ts")
            path.parent.mkdir(parents=True, exist_ok=True)
            download = getattr(self.store, "download_file", None)
            if download is not None:  # streamed to disk (chunks are ~60 MB)
                download(key, path)
            else:
                path.write_bytes(self.store.get_bytes(key))
            self.files[key] = path
        return self.files[key]

    def cut(
        self,
        pieces: Sequence[Piece],
        out: Path,
        jumps: Sequence[tuple[bool, bool]] | None = None,
    ) -> None:
        """`jumps`: per piece, whether a jump comes before / after it (fades with `fade_s`)."""
        tmp = Path(tempfile.mkdtemp(prefix="cut-", dir=self.work))
        try:
            parts = []
            for i, p in enumerate(pieces):
                part = tmp / f"p{i:03d}.mp4"
                source = str(self.chunk_file(p.chunk_key))
                before, after = jumps[i] if jumps is not None else (False, False)
                fade_in = self.fade_s if before else 0.0
                fade_out = self.fade_s if after else 0.0
                _run(
                    piece_command(
                        self.ffmpeg,
                        source,
                        p,
                        self.encoder,
                        str(part),
                        fade_in_s=fade_in,
                        fade_out_s=fade_out,
                    )
                )
                parts.append(part)
            listing = tmp / "list.txt"
            listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
            partial = out.with_suffix(".part.mp4")
            _run(
                [
                    self.ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(partial),
                ]
            )  # fmt: skip
            os.replace(partial, out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def case_json(case: LabelCase, pieces: Sequence[Piece]) -> dict[str, Any]:
    c = case.candidate
    return {
        "key": case.key,
        "case_id": case.case_id,
        "kind": "draft" if case.draft is not None else "candidate",
        "stream_id": c.stream_id,
        "channel": case.stream.login,
        "title": case.stream.title,
        "category": case.stream.category,
        "anchor_utc": ensure_utc(c.peak_utc).isoformat(),
        "moment": [ensure_utc(c.moment_from).isoformat(), ensure_utc(c.moment_to).isoformat()],
        "context": [case.context.start.isoformat(), case.context.end.isoformat()],
        "video": f"video/{case.key}.mp4",
        "duration_s": round(sum(p.duration_s for p in pieces), 3),
        "segments": [p.as_dict() for p in pieces],
    }


async def prepare(
    args: argparse.Namespace,
    sessions: async_sessionmaker[AsyncSession],
    store: Any,
    *,
    now: datetime | None = None,
) -> int:
    """`store`: object storage (`get_bytes`, and `download_file` when it has one)."""
    now = now or utc_now()
    out: Path = args.out
    (out / "video").mkdir(parents=True, exist_ok=True)
    encoder = pick_encoder(args.ffmpeg, args.encoder)
    async with sessions() as s:
        cases = await select_cases(s, args, now=now)
        plans = {
            c.key: plan_pieces(c.context, await chunk_videos(s, c.stream.id, c.context))
            for c in cases
        }
    if not cases:
        raise LabelError("no cases with video")
    work = Path(tempfile.mkdtemp(prefix="clipper-label-"))
    cutter = Cutter(store, args.ffmpeg, encoder, work)
    entries = []
    cut = kept = 0
    try:
        for i, case in enumerate(sorted(cases, key=lambda c: c.key)):
            pieces = plans[case.key]
            if not pieces:
                log.warning("no video pieces: case left out", case=case.case_id)
                continue
            entry = case_json(case, pieces)
            video = out / entry["video"]
            sidecar = video.with_suffix(".json")
            stamp = {
                "format": VIDEO_FORMAT,
                "context": entry["context"],
                "segments": entry["segments"],
            }
            if video.exists() and sidecar.exists() and _read_json(sidecar) == stamp:
                kept += 1
            else:
                await asyncio.to_thread(cutter.cut, pieces, video)
                sidecar.write_text(json.dumps(stamp), encoding="utf-8")
                cut += 1
            entries.append(entry)
            log.info("label case ready", n=i + 1, of=len(cases), key=case.key)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    doc = {
        "format": SET_FORMAT,
        "version": SET_VERSION,
        "created_at": now.isoformat(),
        "encoder": encoder,
        "cases": entries,
    }
    _write_json(out / "cases.json", doc)
    keys = {e["key"] for e in entries}
    drafts = [c for c in cases if c.draft is not None and c.key in keys]
    _write_json(  # the set's drafts, for `eval --drafts`
        out / "drafts.json",
        drafts_doc(
            [Draft(c.case_id, c.candidate, c.draft) for c in drafts if c.draft is not None],
            source=str(args.drafts) if args.drafts else None,
        ),
    )
    n_drafts = len(drafts)
    print(
        f"{len(entries)} cases ({len(entries) - n_drafts} candidates, {n_drafts} drafts) in {out}:"
        f" {cut} videos cut, {kept} kept ({encoder})"
    )
    print(f"next: uv run clipper-judge label serve {out} --open")
    return 0


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- the page


class LabelStore:
    """`labels.json` of a set: case id -> label, rewritten atomically on every change."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.path = directory / "labels.json"
        self.lock = threading.Lock()
        doc = _read_json(directory / "cases.json")
        if not isinstance(doc, Mapping) or doc.get("format") != SET_FORMAT:
            raise LabelError(f"{directory}: no label set (cases.json); run `label prepare` first")
        self.cases: list[dict[str, Any]] = list(doc.get("cases") or [])
        self.by_key = {c["key"]: c for c in self.cases}
        self.labels: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            try:
                load_labels(self.path)  # validates
            except EvalSetError as exc:
                raise LabelError(str(exc)) from exc
            raw = _read_json(self.path) or {}
            self.labels = dict(raw.get("labels") or {})

    def public(self) -> dict[str, Any]:
        """What the page gets: no case id, kind or detector data (blind)."""
        hidden = {"case_id", "kind", "stream_id"}
        return {
            "cases": [
                {
                    **{k: v for k, v in c.items() if k not in hidden},
                    "label": self.labels.get(c["case_id"]),
                }
                for c in self.cases
            ]
        }

    def set(self, key: str, body: Mapping[str, Any]) -> dict[str, Any] | None:
        case = self.by_key.get(key)
        if case is None:
            raise LabelError(f"unknown case {key!r}")
        case_id = case["case_id"]
        with self.lock:
            if body.get("verdict") in (None, ""):
                self.labels.pop(case_id, None)
                label = None
            else:
                parsed = parse_label(
                    {
                        "verdict": body.get("verdict"),
                        "in_s": body.get("in_s"),
                        "out_s": body.get("out_s"),
                        "note": body.get("note") or "",
                    },
                    f"case {key}",
                )
                label = {**parsed.as_dict(), "at": utc_now().isoformat()}
                self.labels[case_id] = label
            _write_json(
                self.path,
                {"labels": dict(sorted(self.labels.items())), "set": str(self.dir.resolve())},
            )
        return label


def page_html() -> bytes:
    return resources.files("clipper_judge").joinpath("label_page.html").read_bytes()


def send_video(h: BaseHTTPRequestHandler, path: Path) -> None:
    """An mp4 file with byte ranges (the player seeks), 404 when it is missing; shared by the
    label page and the ideas rating page (`ideas_rate`)."""
    try:
        size = path.stat().st_size
    except OSError:
        body = json.dumps({"error": "video missing"}).encode("utf-8")
        h.send_response(HTTPStatus.NOT_FOUND)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(body)
        return
    start, end = 0, size - 1
    status = HTTPStatus.OK
    rng = h.headers.get("Range")
    m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip()) if rng else None
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
        else:  # the last N bytes
            start = max(size - int(m.group(2)), 0)
        end = min(end, size - 1)
        if start > end:
            h.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            h.send_header("Content-Range", f"bytes */{size}")
            h.end_headers()
            return
        status = HTTPStatus.PARTIAL_CONTENT
    length = end - start + 1
    h.send_response(status)
    h.send_header("Content-Type", "video/mp4")
    h.send_header("Accept-Ranges", "bytes")
    h.send_header("Content-Length", str(length))
    if status == HTTPStatus.PARTIAL_CONTENT:
        h.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    h.end_headers()
    if h.command == "HEAD":
        return
    with path.open("rb") as f:
        f.seek(start)
        left = length
        while left > 0:
            buf = f.read(min(1 << 20, left))
            if not buf:
                break
            try:
                h.wfile.write(buf)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return  # the player moved on
            left -= len(buf)


def make_handler(store: LabelStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "clipper-label"

        def log_message(self, format: str, *args: Any) -> None:
            log.debug("http", line=format % args)

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data).encode("utf-8"), "application/json")

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, page_html(), "text/html; charset=utf-8")
            elif path == "/api/cases":
                self._json(HTTPStatus.OK, store.public())
            elif path.startswith("/video/") and path.endswith(".mp4"):
                key = path[len("/video/") : -len(".mp4")]
                if not _KEY.match(key) or key not in store.by_key:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "no such video"})
                    return
                self._video(store.dir / "video" / f"{key}.mp4")
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def _video(self, path: Path) -> None:
            send_video(self, path)

        def do_POST(self) -> None:
            if self.path != "/api/label":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, Mapping):
                    raise LabelError("expected a JSON object")
                label = store.set(str(body.get("key")), body)
            except (ValueError, LabelError) as exc:  # EvalSetError is a ValueError
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._json(HTTPStatus.OK, {"label": label})

    return Handler


def serve(args: argparse.Namespace) -> int:
    store = LabelStore(args.dir)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    labelled = sum(1 for c in store.cases if c["case_id"] in store.labels)
    print(f"{len(store.cases)} cases, {labelled} labelled; labels -> {store.path}")
    print(f"open {url}  (Ctrl+C stops)")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


# --------------------------------------------------------------------------- entry


async def amain_prepare(args: argparse.Namespace) -> int:
    settings = Settings(service_name="judge-label")
    setup_logging(settings)
    engine = create_engine(settings, connect_args=READ_ONLY)
    try:
        return await prepare(args, session_factory(engine), ObjectStore(settings))
    except LabelError as exc:
        print(f"[error] {exc}")
        return 2
    finally:
        await engine.dispose()


def main(args: argparse.Namespace) -> int:
    if args.label_cmd == "prepare":
        return run(lambda: amain_prepare(args))
    try:
        return serve(args)
    except LabelError as exc:
        print(f"[error] {exc}")
        return 2
