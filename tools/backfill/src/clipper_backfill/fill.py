"""`vod <id> --fill-gaps`: fill an ended live stream's unrecorded parts from its VOD (DESIGN §17).

The pure planning lives in `planner` (`fill_ranges`, `plan_fill`), the commits in
`pipeline.MediaRunner.run_fill` and `chat.ChatRunner.run_ranges`; this module measures the T0
the fill uses and formats the report.

**T0** (first run only, then frozen in `vods.t0_utc`): `UTC = T0 + VOD offset` must line up with
the live recording at every seam, so T0 is measured against the live chunks next to the fill
ranges: for each range, the live chunk ending at its start and the one starting at its end (for
the tail: the last live chunks), nearest first, up to `tries` per seam: a chunk whose search
window (± `search_s` around where `T0_pdt` puts it) leaves the VOD or holds a muted segment, or
whose match fails (download, storage or decode error, a score below `audio_match.MIN_SCORE`),
makes way for the next one. Each live chunk is matched once (seams share chunks). `T0_audio` = the
median of the good matches. Every candidate goes into `vods.t0_candidates` (`audio`,
`pdt_minus_audio_s`, ...), every window tried into `vods.flags.fill_audio`.

**Trust** (a wrong T0 would be frozen by the first run): a run writes with `T0_audio` only when at
least `min_windows` windows match, at most `spread_s` apart, and within `max_pdt_diff_s` of
`T0_pdt` (measured 16 ms apart on 2934862154). Otherwise it refuses (`FillT0.refusal`, exit 2),
and the dry run shows why; the user may then pass `--accept-t0 audio` (the median anyway) or
`--accept-t0 pdt` (`T0_pdt`, `flags.fill_t0_pdt`). A fill never uses the chat anchor or
`created_at`: without PDT only an accepted `T0_audio` can be used.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from clipper_backfill.audio_match import (
    AudioMatch,
    LiveChunk,
    ReadStore,
    match_windows,
    median_t0,
    spread_s,
    window_segments,
)
from clipper_backfill.download import Downloader
from clipper_backfill.planner import FillPlan, FillRange
from clipper_backfill.t0 import T0Estimate
from clipper_core.contracts import CHUNK_JITTER, T0Method
from clipper_core.timeline import ensure_utc
from clipper_twitch.hls import VodSegment

ACCEPT_T0 = ("audio", "pdt")  # `--accept-t0`


@dataclass(frozen=True, slots=True)
class FillT0:
    """The T0 a fill writes with (`t0`), or why it refuses to (`refusal`), and how it was found."""

    t0: datetime | None  # None: a run refuses to write (see `refusal`)
    method: T0Method | None
    audio: datetime | None  # T0_audio: the median of the good matches
    pdt: datetime | None  # T0_pdt
    candidates: dict[str, Any]  # `vods.t0_candidates`
    flags: dict[str, Any]  # merged into `vods.flags`
    matches: list[AudioMatch] = field(default_factory=list)  # every window matched, by seq
    tried: int = 0  # windows matched (usable ones)
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # why T0_audio isn't trusted ([]: it is)
    accepted: str | None = None  # `--accept-t0`
    refusal: str | None = None

    @property
    def spread_s(self) -> float:
        return spread_s(self.matches)

    @property
    def good(self) -> list[AudioMatch]:
        return [m for m in self.matches if m.good]

    @property
    def shown(self) -> tuple[datetime, T0Method]:
        """The T0 the dry run plans with: the one a run would use, else T0_audio, else T0_pdt."""
        if self.t0 is not None and self.method is not None:
            return self.t0, self.method
        if self.audio is not None:
            return self.audio, T0Method.AUDIO
        assert self.pdt is not None, "measure_t0 refuses without both"
        return self.pdt, T0Method.PDT


def seam_chunks(
    chunks: Sequence[LiveChunk], ranges: Sequence[FillRange], *, tries: int
) -> list[list[LiveChunk]]:
    """Per seam (a range's start or end next to live media), the live chunks with audio to try
    for the T0 match, nearest first (at most `tries`)."""
    seams: list[list[LiveChunk]] = []
    for r in ranges:
        before = sorted(
            (c for c in chunks if ensure_utc(c.end_utc) <= r.interval.start + CHUNK_JITTER),
            key=lambda c: c.end_utc,
            reverse=True,
        )
        after = sorted(
            (c for c in chunks if ensure_utc(c.start_utc) >= r.interval.end - CHUNK_JITTER),
            key=lambda c: c.start_utc,
        )
        seams += [side[:tries] for side in (before, after) if side]
    return seams


async def measure_t0(
    downloader: Downloader,
    store: ReadStore,
    *,
    ffmpeg: str,
    temp_dir: Path | None,
    chunks: Sequence[LiveChunk],
    ranges: Sequence[FillRange],
    segments: Sequence[VodSegment],
    estimate: T0Estimate,
    accept: str | None = None,
    search_s: float = 20.0,
    tries: int = 3,
    min_windows: int = 2,
    spread_flag_s: float = 0.05,
    max_pdt_diff_s: float = 0.5,
) -> FillT0:
    """The fill's T0 (see the module docstring). `chunks`: the live chunks with audio;
    `segments`: the `audio_only` playlist's; `estimate`: `t0.estimate_t0` (its T0, the PDT one
    when there is PDT, places the search windows); `accept`: `--accept-t0`."""
    if accept is not None and accept not in ACCEPT_T0:
        raise ValueError(f"accept must be one of {ACCEPT_T0}, not {accept!r}")
    results: dict[int, AudioMatch | None] = {}  # seq -> its match (None: no usable one)
    notes: list[str] = []
    tried = 0
    for side in seam_chunks(chunks, ranges, tries=tries):
        for chunk in side:
            if chunk.seq not in results:
                segs = window_segments(chunk, segments, estimate.t0, search_s=search_s)
                if segs is None:
                    results[chunk.seq] = None
                    notes.append(f"chunk {chunk.seq}: its window leaves the VOD or is muted")
                    continue
                found, failed = await match_windows(
                    downloader, store, ffmpeg, [(chunk, segs)], temp_dir=temp_dir
                )
                tried += 1
                notes += failed
                results[chunk.seq] = found[0] if found else None
                if found and not found[0].good:
                    notes.append(f"chunk {chunk.seq}: weak match (score {found[0].score:.3f})")
            match = results[chunk.seq]
            if match is not None and match.good:
                break
        else:
            seqs = ", ".join(str(c.seq) for c in side)
            notes.append(f"no good match near live chunks {seqs}")
    matches = sorted((m for m in results.values() if m is not None), key=lambda m: m.seq)

    candidates = dict(estimate.candidates)
    flags: dict[str, Any] = dict(estimate.flags)
    audio_flag: dict[str, Any] = {
        "windows": tried,
        "matches": [
            {
                "seq": m.seq,
                "vod_offset_s": round(m.vod_offset_s, 4),
                "score": round(m.score, 3),
                "t0": m.t0.isoformat(),
            }
            for m in matches
        ],
    }
    if notes:
        audio_flag["notes"] = notes[:10]
    flags["fill_audio"] = audio_flag
    pdt = datetime.fromisoformat(candidates["pdt"]) if "pdt" in candidates else None
    t0a = median_t0(matches)
    good = [m for m in matches if m.good]
    spread = spread_s(matches)
    problems: list[str] = []
    if t0a is None:
        problems.append("no live audio window matched")
    else:
        candidates["audio"] = t0a.isoformat()
        candidates["audio_windows"] = len(good)
        candidates["audio_spread_s"] = round(spread, 4)
        audio_flag["spread_ms"] = round(spread * 1000, 1)
        if len(good) < min_windows:
            problems.append(f"only {len(good)} matching window(s), {min_windows} needed")
        if spread > spread_flag_s:
            flags["fill_t0_spread"] = {
                "spread_ms": round(spread * 1000, 1),
                "t0s": [m.t0.isoformat() for m in good],
            }
            problems.append(
                f"the windows disagree by {spread * 1000:.1f} ms (> {spread_flag_s * 1000:.0f} ms)"
            )
        if pdt is None:
            problems.append("no PDT to check T0_audio against")
        else:
            diff = (pdt - t0a).total_seconds()
            candidates["pdt_minus_audio_s"] = round(diff, 4)
            if abs(diff) > max_pdt_diff_s:
                problems.append(f"T0_pdt - T0_audio = {diff:+.3f} s (> {max_pdt_diff_s} s)")

    t0 = method = refusal = None
    if accept is None:
        if problems:
            refusal = (
                f"T0 not trustworthy: {'; '.join(problems)}. Check `--dry-run` and run again "
                "with --accept-t0 audio|pdt"
            )
        else:
            t0, method = t0a, T0Method.AUDIO
    elif accept == "audio":
        if t0a is None:
            refusal = "--accept-t0 audio: no live audio window matched"
        else:
            t0, method = t0a, T0Method.AUDIO
    elif pdt is None:
        refusal = "--accept-t0 pdt: the audio_only playlist has no PDT (a fill never uses the "
        refusal += "chat anchor or created_at)"
    else:
        t0, method = pdt, T0Method.PDT
        flags["fill_t0_pdt"] = {"reason": "--accept-t0 pdt", "problems": problems}
    if accept is not None and t0 is not None:
        flags["fill_t0_accepted"] = {"t0": accept, "problems": problems}
    return FillT0(
        t0=t0,
        method=method,
        audio=t0a,
        pdt=pdt,
        candidates=candidates,
        flags=flags,
        matches=matches,
        tried=tried,
        notes=notes,
        problems=problems,
        accepted=accept,
        refusal=refusal,
    )


# --------------------------------------------------------------------------- report


def hms(seconds: float) -> str:
    whole = round(seconds)
    return f"{whole // 3600}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


def _clock(dt: datetime) -> str:
    return ensure_utc(dt).strftime("%H:%M:%S.%f")[:-3] + "Z"


def t0_lines(measured: FillT0) -> list[str]:
    t0, method = measured.shown
    head = f"T0 {t0.isoformat()} ({method})"
    if measured.audio is not None:
        head += (
            f": {len(measured.good)} of {measured.tried} live chunk windows matched, spread "
            f"{measured.spread_s * 1000:.1f} ms"
        )
        diff = measured.candidates.get("pdt_minus_audio_s")
        if diff is not None:
            head += f"; T0_pdt - T0_audio = {diff * 1000:+.1f} ms"
    else:
        head += ": no live audio window matched"
    lines = [head]
    if measured.refusal:
        lines.append(f"T0 check: a run refuses to write: {measured.refusal}")
    elif measured.accepted:
        lines.append(
            f"T0 check: --accept-t0 {measured.accepted}"
            + (f" (despite: {'; '.join(measured.problems)})" if measured.problems else "")
        )
    else:
        lines.append("T0 check: trusted (the windows agree with each other and with T0_pdt)")
    for m in measured.matches:
        lines.append(
            f"  window: live chunk {m.seq} at {_clock(m.chunk_start)} = VOD offset "
            f"{m.vod_offset_s:.3f} s, score {m.score:.3f}, T0 {m.t0.isoformat()}"
            + ("" if m.good else " (weak: not used)")
        )
    lines += [f"  note: {n}" for n in measured.notes]
    return lines


def range_lines(plan: FillPlan) -> list[str]:
    total = sum(p.end_s - p.start_s for p in plan.parts)
    lines = [f"fill ranges: {len(plan.parts)} ({hms(total)})"]
    for p in plan.parts:
        r = p.range
        inside = (
            f"{p.segments} segments inside ({p.first_s:.3f}..{p.last_s:.3f} s)"
            if p.segments
            else "no segment fits inside"
        )
        lines.append(
            f"  {r.kind:<4} {_clock(r.interval.start)}..{_clock(r.interval.end)} "
            f"({hms(p.end_s - p.start_s)}) = offsets {p.start_s:.3f}..{p.end_s:.3f} s: {inside}"
            + (f", {p.muted_s:.0f} s muted (skipped)" if p.muted_s else "")
            + f", unfilled edges {p.unfilled_edges_s:.1f} s"
            + ("" if r.gap_row else ", no gap row")
        )
    return lines


def plan_lines(plan: FillPlan, *, audio_only: bool, bytes_per_s: float) -> list[str]:
    chunks = plan.chunks
    if not chunks:
        return [f"plan: no chunk to fill from {plan.start_s:.1f} s (watermark to the end only)"]
    seconds = sum(u.end_s - u.start_s for u in chunks)
    return [
        f"plan: {len(chunks)} chunks ({hms(seconds)}) from {plan.start_s:.1f} s, seq "
        f"{chunks[0].seq}..{chunks[-1].seq} (source backfill, no gap rows)",
        f"  downloads: {len(plan.fetch_segments())} segments"
        + (f", ~{seconds * bytes_per_s / 1e6:.1f} MB" if audio_only else ""),
    ]
