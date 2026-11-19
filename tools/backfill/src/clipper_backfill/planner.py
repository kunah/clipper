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
from clipper_core.timeline import Interval, ensure_utc
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
    """One commit: a chunk with the gaps right before it, or (seq None) gaps only."""

    seq: int | None
    segments: tuple[VodSegment, ...] = ()
    gaps: tuple[PlannedGap, ...] = ()
    audio: bool = True  # False: a video-only chunk over muted segments
    lost: int = 0  # lost segments inside `gaps` (`vods.flags.lost_segments` grows by it)

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
