"""The time map (DESIGN §20, step 3): plan times (UTC video time) -> output times, and which part
of which chunk lands where. Pure.

    tm = timemap.build(plan_ranges(plan), chunks)
    tm.pieces        # per range and chunk: trim [a - chunk.start, b - chunk.start), output offset
    tm.clip_out(t)   # a plan time in the clip proper (not the teaser) -> output seconds

Output order: a teaser hook's range first (`HookMode.TEASER`: it plays before the clip), then the
plan's segments; ranges join with hard cuts. Each range is cut from the chunks with video that
overlap it, one piece per chunk, trimmed at `a - chunk.start_utc` .. `b - chunk.start_utc` (exact
inside a chunk, §5). Between two chunks of one range:
- an overlap is cut from the later chunk;
- a hole up to `CHUNK_JITTER` is filled (the earlier piece holds its last frame and adds silence,
  `pad_after_s`), so output time stays linear in UTC: out = UTC - range start + range offset;
- a longer hole (a gap: the judge splits segments at gaps, so it shouldn't happen) is skipped: a
  hard cut and a `gap_cut` warning.
A range that starts or ends where no chunk has video is shortened (a warning beyond the jitter);
a range with no video at all raises `NoVideo` (the render is `unrenderable`/`no_video`).

Runs: maximal stretches of the output whose time is linear in UTC (the pieces of one range
between gap cuts, pads included). Every plan time is mapped through them: captions (a word must
lie inside one run, else it is dropped), zooms, SFX, the thumbnail.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from clipper_core.contracts import CHUNK_JITTER, EditPlan, HookMode
from clipper_core.timeline import Interval, ensure_utc

MIN_PIECE_S = 0.001  # shorter overlaps of a chunk with a range are ignored


class NoVideo(Exception):
    """A plan range has no chunk with video under it."""

    def __init__(self, range_index: int, interval: Interval) -> None:
        super().__init__(f"no video under range {range_index} ({interval.start} .. {interval.end})")
        self.range_index = range_index
        self.interval = interval


@dataclass(frozen=True, slots=True)
class ChunkVideo:
    """A chunk with video, as the renderer reads it (`key` = the pinned copy)."""

    chunk_id: int
    seq: int
    start_utc: datetime
    end_utc: datetime
    key: str


@dataclass(frozen=True, slots=True)
class OutRange:
    """One plan range in output order."""

    interval: Interval
    teaser: bool


@dataclass(frozen=True, slots=True)
class Piece:
    chunk: ChunkVideo
    utc_from: datetime
    utc_to: datetime
    out_at_s: float
    pad_after_s: float
    teaser: bool
    range_index: int
    cut_before: bool  # a hard cut precedes it (range start or a skipped gap)

    @property
    def trim_from_s(self) -> float:
        return _s(self.utc_from - self.chunk.start_utc)

    @property
    def trim_to_s(self) -> float:
        return _s(self.utc_to - self.chunk.start_utc)

    @property
    def duration_s(self) -> float:
        return _s(self.utc_to - self.utc_from)

    @property
    def out_to_s(self) -> float:
        """End in the output, the pad included."""
        return round(self.out_at_s + self.duration_s + self.pad_after_s, 6)


@dataclass(frozen=True, slots=True)
class Run:
    """A stretch of the output that is linear in UTC: out = out_at_s + (t - utc_from)."""

    utc_from: datetime
    utc_to: datetime  # pads included (they are real UTC time: a held frame)
    out_at_s: float
    teaser: bool

    @property
    def out_to_s(self) -> float:
        return round(self.out_at_s + _s(self.utc_to - self.utc_from), 6)

    def contains(self, t: datetime) -> bool:
        return self.utc_from <= t < self.utc_to

    def out(self, t: datetime) -> float:
        return round(self.out_at_s + _s(t - self.utc_from), 6)


@dataclass(frozen=True, slots=True)
class TimeMap:
    pieces: tuple[Piece, ...]
    runs: tuple[Run, ...]
    warnings: tuple[str, ...]

    @property
    def duration_s(self) -> float:
        return self.pieces[-1].out_to_s if self.pieces else 0.0

    @property
    def teaser_s(self) -> float:
        """Output seconds before the clip proper starts (the teaser's length, else 0)."""
        clip = [p for p in self.pieces if not p.teaser]
        return clip[0].out_at_s if clip else self.duration_s

    def out_times(self, t: datetime, *, teaser: bool | None = None) -> list[float]:
        """Every output time showing plan time `t` (a teaser shows part of the clip twice);
        `teaser` = True/False keeps only the teaser's/the clip's."""
        t = ensure_utc(t)
        return [
            r.out(t) for r in self.runs if r.contains(t) and (teaser is None or r.teaser == teaser)
        ]

    def clip_out(self, t: datetime) -> float | None:
        """Output time of plan time `t` in the clip proper (not the teaser); None if cut out."""
        times = self.out_times(t, teaser=False)
        return times[0] if times else None

    def out_ranges(
        self, start: datetime, end: datetime, *, teaser: bool | None = None
    ) -> list[tuple[float, float]]:
        """The output intervals showing plan times [start, end), in output order."""
        start, end = ensure_utc(start), ensure_utc(end)
        out = []
        for r in self.runs:
            if teaser is not None and r.teaser != teaser:
                continue
            a, b = max(start, r.utc_from), min(end, r.utc_to)
            if a < b:
                out.append((r.out(a), r.out(b)))
        return out

    def run_of(self, start: datetime, end: datetime, *, slack_s: float = 0.0) -> list[Run]:
        """The runs that hold all of [start, end) (`end` may pass a run's end by `slack_s`)."""
        start, end = ensure_utc(start), ensure_utc(end)
        slack = timedelta(seconds=slack_s)
        return [r for r in self.runs if r.utc_from <= start and end <= r.utc_to + slack]


def _s(delta: timedelta) -> float:
    return round(delta.total_seconds(), 6)


def plan_ranges(plan: EditPlan) -> list[OutRange]:
    """The plan's ranges in output order: a teaser hook first, then the segments."""
    ranges = []
    hook = plan.hook
    if hook.mode == HookMode.TEASER and hook.start_utc is not None and hook.end_utc is not None:
        ranges.append(OutRange(Interval(hook.start_utc, hook.end_utc), teaser=True))
    ranges += [OutRange(Interval(s.start_utc, s.end_utc), teaser=False) for s in plan.segments]
    return ranges


def build(
    ranges: Sequence[OutRange], chunks: Iterable[ChunkVideo], *, jitter: timedelta = CHUNK_JITTER
) -> TimeMap:
    ordered = sorted(chunks, key=lambda c: (ensure_utc(c.start_utc), c.seq))
    pieces: list[Piece] = []
    runs: list[Run] = []
    warnings: list[str] = []
    out = 0.0
    for index, rng in enumerate(ranges):
        iv = rng.interval
        range_pieces: list[Piece] = []
        prev_end: datetime | None = None
        cut = True
        for c in ordered:
            c_start, c_end = ensure_utc(c.start_utc), ensure_utc(c.end_utc)
            a = max(iv.start, c_start, prev_end) if prev_end is not None else max(iv.start, c_start)
            b = min(iv.end, c_end)
            if _s(b - a) < MIN_PIECE_S:
                continue
            if prev_end is None:
                lead = a - iv.start
                if lead > jitter:
                    warnings.append(
                        f"no_video_start: range {index} starts {_s(lead):.2f} s before its video"
                    )
            else:
                hole = a - prev_end
                if timedelta(0) < hole <= jitter:
                    last = range_pieces[-1]
                    range_pieces[-1] = _with_pad(last, _s(hole))
                    out = round(out + _s(hole), 6)
                    runs[-1] = Run(runs[-1].utc_from, a, runs[-1].out_at_s, rng.teaser)
                elif hole > jitter:
                    warnings.append(
                        f"gap_cut: range {index} skips {_s(hole):.2f} s without video at "
                        f"{prev_end.isoformat()}"
                    )
                    cut = True
            piece = Piece(c, a, b, out, 0.0, rng.teaser, index, cut)
            range_pieces.append(piece)
            if cut:
                runs.append(Run(a, b, out, rng.teaser))
            else:
                runs[-1] = Run(runs[-1].utc_from, b, runs[-1].out_at_s, rng.teaser)
            out = round(out + piece.duration_s, 6)
            prev_end = b
            cut = False
        if not range_pieces:
            raise NoVideo(index, iv)
        tail = iv.end - prev_end
        if tail > jitter:
            warnings.append(f"no_video_end: range {index} ends {_s(tail):.2f} s after its video")
        pieces += range_pieces
    return TimeMap(tuple(pieces), tuple(runs), tuple(warnings))


def _with_pad(piece: Piece, pad_s: float) -> Piece:
    return Piece(
        piece.chunk,
        piece.utc_from,
        piece.utc_to,
        piece.out_at_s,
        round(piece.pad_after_s + pad_s, 6),
        piece.teaser,
        piece.range_index,
        piece.cut_before,
    )
