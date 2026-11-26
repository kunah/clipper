"""VOD segments -> commit units: contiguous ~60 s chunks and the gaps between them (pure).

A VOD's media time is its offset (sum of EXTINF); UTC = T0 + offset (`at`), so chunk intervals
are exact and neighbouring chunks share their boundary to the microsecond.

Rules (DESIGN §5, §17):
- A chunk is continuous media: it ends at `chunk_s` (after the segment that reaches it), before
  an EXT-X-DISCONTINUITY, before a different init section (fMP4), before a muted or lost segment,
  and at the end (of the VOD, or of `until_s`). It never contains a muted segment.
- Muted segments (`N-muted.ts`) are not fetched: each run of them becomes one `muted` gap. With
  `muted_video` (`--video`) they become video-only chunks instead (no audio, no transcription),
  each with a `muted` gap over its own range.
- Lost segments (still 404 after retries) become `not_recorded` gaps.
- Skipping ahead (`start_s` beyond `done_s`: `--start-offset`) leaves a `not_recorded` gap
  `[done_s, first segment)`.
- A gap is committed with the chunk after it (`chunks.commit_chunk(gaps_before=...)`); gaps with
  no chunk after them (a muted end) form a last unit of their own.

Each unit is one transaction, and `Unit.done_s` is the `vods.media_done_s` it commits: a re-run
plans again from there with the next `seq` and gets the same units. `done_s` must be a segment
boundary of the playlist (it is, for the rendition that committed it): another rendition whose
segments are cut elsewhere would make the next chunk overlap the last one (`PlanError`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from clipper_core.chunks import Gap
from clipper_core.contracts import GapReason
from clipper_core.timeline import Interval, ensure_utc, merge, uncovered
from clipper_twitch.hls import VodSegment

CHUNK_SECONDS = 60.0
EPS_S = 1e-3  # offsets are sums of 3-decimal EXTINFs (rounded to 6 decimals by the parser)


class PlanError(ValueError):
    """The playlist can't continue the committed media (`done_s` is not one of its segment
    boundaries: another rendition, or the VOD was re-cut)."""


def at(t0: datetime, offset_s: float) -> datetime:
    """UTC of a VOD offset, on the microsecond grid Postgres stores."""
    return ensure_utc(t0) + timedelta(microseconds=round(offset_s * 1_000_000))


@dataclass(frozen=True, slots=True)
class PlannedGap:
    start_s: float
    end_s: float
    reason: GapReason

    @property
    def seconds(self) -> float:
        return self.end_s - self.start_s

    def utc(self, t0: datetime) -> Gap:
        return Gap(at(t0, self.start_s), at(t0, self.end_s), self.reason)


@dataclass(frozen=True, slots=True)
class Unit:
    """One commit: a chunk with the gaps right before it, or (seq None) gaps only.

    A `--fill-gaps` unit (`plan_fill`) has no gaps: a chunk, or (seq None) only a watermark
    (`mark_s`) past skipped segments or the end."""

    seq: int | None
    segments: tuple[VodSegment, ...] = ()
    gaps: tuple[PlannedGap, ...] = ()
    audio: bool = True  # False: a video-only chunk over muted segments
    lost: int = 0  # lost segments inside `gaps` (`vods.flags.lost_segments` grows by it)
    mark_s: float | None = None  # `--fill-gaps`: commit `media_done_s` up to here at least

    @property
    def start_s(self) -> float:
        return self.segments[0].offset_s

    @property
    def end_s(self) -> float:
        return self.segments[-1].end_s

    @property
    def done_s(self) -> float:
        """`vods.media_done_s` after this unit."""
        ends = [g.end_s for g in self.gaps]
        if self.segments:
            ends.append(self.end_s)
        if self.mark_s is not None:
            ends.append(self.mark_s)
        return max(ends)

    def interval(self, t0: datetime) -> Interval:
        return Interval(at(t0, self.start_s), at(t0, self.end_s))


@dataclass(frozen=True, slots=True)
class Plan:
    units: list[Unit] = field(default_factory=list)
    start_s: float = 0.0  # where this plan starts (`done_s`)
    end_s: float = 0.0  # where it ends: the last unit's done_s (start_s if empty)
    vod_end_s: float = 0.0  # the VOD's end (sum of EXTINF)

    @property
    def chunks(self) -> list[Unit]:
        return [u for u in self.units if u.seq is not None]

    @property
    def gaps(self) -> list[PlannedGap]:
        return [g for u in self.units for g in u.gaps]

    @property
    def complete(self) -> bool:
        """True when committing every unit reaches the end of the VOD."""
        return self.end_s >= self.vod_end_s - EPS_S

    def fetch_segments(self) -> list[VodSegment]:
        return [s for u in self.units for s in u.segments]


def check_aligned(segments: Sequence[VodSegment], done_s: float) -> None:
    """Raise PlanError unless `done_s` is a segment boundary (or 0, or past the end)."""
    nxt = next((s for s in segments if s.end_s > done_s + EPS_S), None)
    if nxt is not None and nxt.offset_s < done_s - EPS_S:
        raise PlanError(
            f"media is committed up to {done_s:.3f} s, but this playlist's segment {nxt.index} "
            f"spans [{nxt.offset_s:.3f}, {nxt.end_s:.3f}) s: its chunks would overlap"
        )


def plan_units(
    segments: Sequence[VodSegment],
    *,
    done_s: float = 0.0,
    start_s: float = 0.0,
    first_seq: int = 0,
    chunk_s: float = CHUNK_SECONDS,
    until_s: float | None = None,
    lost: frozenset[int] = frozenset(),
    muted_video: bool = False,
) -> Plan:
    """Plan the commit units from `max(done_s, start_s)` on (see the module docstring).

    `done_s`: where committed media + gaps end (`vods.media_done_s`); `start_s`: where to start
    fetching (the segment containing it is the first); `until_s`: only segments starting before
    it (`--max-minutes`); `lost`: segment indexes that could not be downloaded. Raises
    PlanError when `done_s` falls inside a segment (`check_aligned`).
    """
    if chunk_s <= 0:
        raise ValueError("chunk_s must be > 0")
    check_aligned(segments, done_s)
    vod_end = segments[-1].end_s if segments else 0.0
    begin = max(done_s, start_s)
    todo = [
        s
        for s in segments
        if s.end_s > begin + EPS_S and (until_s is None or s.offset_s < until_s - EPS_S)
    ]
    units: list[Unit] = []
    gaps: list[PlannedGap] = []
    current: list[VodSegment] = []
    seq = first_seq
    lost_in_gaps = 0

    if todo and todo[0].offset_s > done_s + EPS_S:
        gaps.append(PlannedGap(done_s, todo[0].offset_s, GapReason.NOT_RECORDED))

    def add_gap(seg: VodSegment, reason: GapReason) -> None:
        last = gaps[-1] if gaps else None
        if last and last.reason == reason and abs(last.end_s - seg.offset_s) <= EPS_S:
            gaps[-1] = PlannedGap(last.start_s, seg.end_s, reason)
        else:
            gaps.append(PlannedGap(seg.offset_s, seg.end_s, reason))

    def close() -> None:
        nonlocal seq, current, gaps, lost_in_gaps
        if not current:
            return
        audio = not current[0].muted
        own = [] if audio else [PlannedGap(current[0].offset_s, current[-1].end_s, GapReason.MUTED)]
        units.append(Unit(seq, tuple(current), tuple(gaps + own), audio, lost_in_gaps))
        seq += 1
        current, gaps, lost_in_gaps = [], [], 0

    for seg in todo:
        if seg.index in lost:
            close()
            add_gap(seg, GapReason.NOT_RECORDED)
            lost_in_gaps += 1
            continue
        if seg.muted and not muted_video:
            close()
            add_gap(seg, GapReason.MUTED)
            continue
        if current and (
            seg.discontinuity
            or seg.init_uri != current[0].init_uri
            or seg.muted != current[0].muted
            or abs(seg.offset_s - current[-1].end_s) > EPS_S
        ):
            close()
        current.append(seg)
        if current[-1].end_s - current[0].offset_s >= chunk_s - EPS_S:
            close()
    close()
    if gaps:
        units.append(Unit(None, (), tuple(gaps), lost=lost_in_gaps))
    end = units[-1].done_s if units else done_s
    return Plan(units, start_s=done_s, end_s=end, vod_end_s=vod_end)


# --------------------------------------------------------------------------- --fill-gaps


@dataclass(frozen=True, slots=True)
class FillRange:
    """A range of an ended live stream that `vod --fill-gaps` fills from its VOD (UTC)."""

    interval: Interval
    kind: str  # "head" (before the first live chunk), "gap" (between two), "tail", "all"
    gap_row: bool  # `not_recorded` gap rows cover it all (the recorder's; head/tail: often not)


def fill_ranges(
    *,
    vod: Interval,
    chunks: Sequence[Interval],
    gaps: Sequence[tuple[Interval, str]],
) -> list[FillRange]:
    """What `--fill-gaps` may fill (DESIGN §17), sorted and disjoint: the stream's `not_recorded`
    gap rows, its unrecorded tail (the last live chunk's end -> the VOD's end) and head (the VOD's
    start -> the first live chunk), each cut to the VOD's span, minus every live chunk (with audio
    or not) and every gap row of another reason (an ad is no hole to fill). `chunks`: the live
    chunks; `gaps`: (interval, reason) of the stream's gap rows."""
    not_recorded = [iv for iv, reason in gaps if reason == GapReason.NOT_RECORDED]
    others = [iv for iv, reason in gaps if reason != GapReason.NOT_RECORDED]
    wanted = list(not_recorded)
    first = last = None
    if chunks:
        first, last = min(c.start for c in chunks), max(c.end for c in chunks)
        if vod.start < first:
            wanted.append(Interval(vod.start, max(first, vod.start)))
        if last < vod.end:
            wanted.append(Interval(min(last, vod.end), vod.end))
    else:
        wanted.append(vod)
    out: list[FillRange] = []
    for part in merge(p for w in wanted if (p := w.intersection(vod)) is not None):
        for piece in uncovered(part, [*chunks, *others]):
            if first is None or last is None:
                kind = "all"
            elif piece.end <= first:
                kind = "head"
            elif piece.start >= last:
                kind = "tail"
            else:
                kind = "gap"
            out.append(FillRange(piece, kind, not uncovered(piece, not_recorded)))
    return out


@dataclass(frozen=True, slots=True)
class FillPart:
    """One fill range of a `FillPlan`, for the report (offsets relative to the fill's T0)."""

    range: FillRange
    start_s: float
    end_s: float
    segments: int  # VOD segments entirely inside the range (the rest of it stays unfilled)
    inside_s: float  # their seconds (muted ones included)
    first_s: float | None  # where the first inside segment starts, the last one ends
    last_s: float | None
    muted_s: float  # inside segments skipped as muted
    chunks: int  # chunks planned from `done_s`
    planned_s: float  # their seconds

    @property
    def unfilled_edges_s(self) -> float:
        """Range seconds no inside segment covers (at most about a segment at each edge)."""
        return max(0.0, self.end_s - self.start_s - self.inside_s)


@dataclass(frozen=True, slots=True)
class FillPlan:
    units: list[Unit] = field(default_factory=list)
    parts: list[FillPart] = field(default_factory=list)
    start_s: float = 0.0  # `done_s` the plan starts at
    vod_end_s: float = 0.0

    @property
    def chunks(self) -> list[Unit]:
        return [u for u in self.units if u.seq is not None]

    @property
    def complete(self) -> bool:
        """Committing every unit brings `media_done_s` to the VOD's end (always, by design)."""
        end = self.units[-1].done_s if self.units else self.start_s
        return end >= self.vod_end_s - EPS_S

    def fetch_segments(self) -> list[VodSegment]:
        return [s for u in self.units for s in u.segments]


def plan_fill(
    segments: Sequence[VodSegment],
    ranges: Sequence[FillRange],
    t0: datetime,
    *,
    done_s: float = 0.0,
    first_seq: int,
    chunk_s: float = CHUNK_SECONDS,
    lost: frozenset[int] = frozenset(),
) -> FillPlan:
    """The `--fill-gaps` units from `done_s` on (pure; DESIGN §17).

    Per range, in time (= offset) order: the VOD segments whose UTC span `[at(t0, offset),
    at(t0, end))` lies entirely inside it, compared on the microsecond grid the rows use, so a fill
    chunk never overlaps a live chunk; up to one segment at each edge stays unfilled. Those are cut
    by `plan_units` (<= `chunk_s`; a discontinuity, an init section change, a muted or lost segment
    and the range's edge close a chunk), and then every planned gap is dropped: muted and lost
    segments are skipped without a gap row (a recorder's `not_recorded` row keeps covering them;
    where there is none, in an unrecorded tail or head, `transcripts.coverage()` reads the hole as
    gap once the recorder is done). Seqs run on from `first_seq` across ranges, so chunks of two
    ranges get consecutive seqs without being contiguous in time. A unit with seq None only moves
    the watermark: past lost segments (counted into `flags.lost_segments` with it), and last of
    all to the VOD's end, so a complete fill always ends with `media_done_s` = the VOD's end.
    """
    check_aligned(segments, done_s)
    vod_end = segments[-1].end_s if segments else 0.0
    units: list[Unit] = []
    parts: list[FillPart] = []
    seq = first_seq
    for r in ranges:
        inside = [
            s
            for s in segments
            if at(t0, s.offset_s) >= r.interval.start and at(t0, s.end_s) <= r.interval.end
        ]
        mine: list[Unit] = []
        if inside and inside[-1].end_s > done_s + EPS_S:
            begin = max(done_s, inside[0].offset_s)
            plan = plan_units(
                inside, done_s=begin, start_s=begin, first_seq=seq, chunk_s=chunk_s, lost=lost
            )
            for u in plan.units:
                if u.seq is not None:
                    mine.append(Unit(u.seq, u.segments, (), u.audio, u.lost))
                    seq = u.seq + 1
                elif u.lost:  # lost segments at the range's end: count them with a mark
                    mine.append(Unit(None, lost=u.lost, mark_s=u.done_s))
        units += mine
        chunks = [u for u in mine if u.seq is not None]
        parts.append(
            FillPart(
                range=r,
                start_s=(r.interval.start - ensure_utc(t0)).total_seconds(),
                end_s=(r.interval.end - ensure_utc(t0)).total_seconds(),
                segments=len(inside),
                inside_s=sum(s.duration for s in inside),
                first_s=inside[0].offset_s if inside else None,
                last_s=inside[-1].end_s if inside else None,
                muted_s=sum(s.duration for s in inside if s.muted),
                chunks=len(chunks),
                planned_s=sum(u.end_s - u.start_s for u in chunks),
            )
        )
    end = units[-1].done_s if units else done_s
    if end < vod_end - EPS_S:
        units.append(Unit(None, mark_s=vod_end))
    return FillPlan(units, parts, start_s=done_s, vod_end_s=vod_end)
