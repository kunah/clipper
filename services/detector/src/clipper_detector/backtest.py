"""Backtest (DESIGN §18.8): what the detector emits on stored streams, scored against viewer clips.

Pure part (no database, no clock); `backtest_cli` reads the rows and runs `pipeline.run_stream`,
the same scorer, stage 2 and merge as the live service (one scorer, three callers).

- **Chat coverage**: the time we have chat for, `[first chat_metrics_1s row, last row + 1 s)`
  inside the stream (a backfilled stream: up to `vods.chat_metrics_to` when that is later), cut
  at the broadcast's real end when known (`backtest_cli`), minus the scorer's `MissingChat` runs
  inside it (a zero run >= 60 s while the baseline is >= 5; one lasting to the end of the data is
  the chat's end, not a hole). Hours = its length: `k` and candidates/h count chat hours, not
  broadcast hours (a partly recorded stream, e.g. 803 with chat for 5 of 9.7 h).
- **Truth** (`classify_clips`): of all the stream's viewer clips, the top 10 by views plus every
  clip with at least the 75th percentile of their views (linear interpolation). Truth clips that
  can't be scored for recall are counted per reason: `no_span` (no `start_utc`: Helix has no VOD
  offset yet), `muted` (the span overlaps a `muted` gap, §17), `outside_chat` (the span, shifted
  to chat time by the stream's delay, doesn't lie inside one coverage interval). The truth is
  picked before those exclusions: views rank the broadcast's moments, whatever part of it we
  recorded.
- **Hit**: a candidate's peak (`peak_utc`, video time) inside a clip's span +- its slack
  (`slack_s`, NULL: duration + 10 s, §17; plus the VOD's `timeline_uncertain_s` if flagged).
- **@k** for each rate (per chat hour; default 2, 4, 8): the `k = ceil(rate * hours)` emitted
  candidates with the highest score (ties: earlier peak) are what the judge would get.
  - `hit_rate` = share of them inside a truth clip *with a span, before the muted/outside
    exclusions* (a candidate on a truth moment is no miss because that clip can't be scored for
    recall); a precision lower bound (good moments nobody clipped exist). `hit_rate_any`: inside
    any viewer clip with a span. `chance` / `chance_any` = the share of the covered video time
    inside those clips' spans +- slack: what a random peak would score.
  - `recall` = views of the scored truth clips one of them hits / views of all scored truth
    clips; `moment_recall` the same over *moments*: scored truth clips whose bare spans overlap
    are one moment, weighted by its most viewed clip (five clips of one moment count once).
  - `delay_median_s` = median of (peak - clip start) over the truth clips hit (the best-ranked
    candidate inside each): a check of the chat delay.
  - `false_suppressions`: per scored truth clip that none of the k hits, the reason of the
    best-ranked suppressed candidate inside it (any reason, `weak` and `budget` too, and
    `dedupe`: a draft the live write drops within +- 60 s of an earlier candidate,
    `pipeline.Duplicate`). `suppressed_in_truth` (per stream) is the raw count: every suppressed
    candidate inside a scored truth clip.
- **Totals** pool the streams (micro): hits / candidates, hit views / truth views, span seconds /
  coverage seconds, all offsets.
"""

from __future__ import annotations

import itertools
import json
import math
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from clipper_core.timeline import Interval, merge, uncovered
from clipper_detector.params import NON_SCORING, DetectorParams
from clipper_detector.pipeline import StreamRun

TRUTH_TOP = 10  # truth = the top 10 clips by views ...
TRUTH_QUANTILE = 0.75  # ... plus every clip with at least this quantile of the views
RATES = (2.0, 4.0, 8.0)  # candidates per chat hour that k is computed for
DEFAULT_SLACK_EXTRA_S = 10.0  # a clip without slack_s: duration + this (§17)
DEDUPE = "dedupe"  # the reason of a draft the live write drops
NO_SPAN, MUTED, OUTSIDE_CHAT = "no_span", "muted", "outside_chat"  # exclusion reasons
# live-loop params that change nothing here (the restart replay): refused by --grid
GRID_NO_EFFECT = frozenset({"restart_replay_s", "restart_emit_s"})


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True, slots=True)
class ClipRow:
    """A `viewer_clips` row, as far as the backtest needs it (times in video time).
    `extra_slack_s`: the VOD's `timeline_uncertain_s`, added to the slack."""

    clip_id: str
    start: datetime | None
    end: datetime | None
    duration_s: float
    views: int
    slack_s: float | None = None
    extra_slack_s: float = 0.0

    @property
    def spanned(self) -> bool:
        return self.start is not None and self.end is not None

    @property
    def slack(self) -> timedelta:
        s = self.slack_s if self.slack_s is not None else self.duration_s + DEFAULT_SLACK_EXTRA_S
        return timedelta(seconds=s + self.extra_slack_s)

    @property
    def span(self) -> Interval:
        assert self.start is not None and self.end is not None
        return Interval(self.start, max(self.start, self.end))

    @property
    def window(self) -> Interval:
        """The span +- slack: where a hit's peak may lie."""
        return self.span.expand(self.slack, self.slack)

    def holds(self, peak: datetime) -> bool:
        """The peak lies inside the span +- slack (both ends included)."""
        w = self.window
        return w.start <= peak <= w.end


@dataclass(frozen=True, slots=True)
class Pick:
    """A candidate as the backtest scores it (`peak` in video time)."""

    peak: datetime
    score: float
    emitted: bool
    reason: str | None = None  # suppress reason, or `dedupe`
    chat_peak: datetime | None = None
    moment_type: str | None = None


def picks_of(run: StreamRun) -> list[Pick]:
    """The drafts of a pipeline run, plus the ones the live dedupe drops (`dedupe`)."""
    out = [
        Pick(
            d.peak_utc,
            d.score,
            d.emitted,
            d.suppress_reason.value if d.suppress_reason else None,
            d.chat_peak_utc,
            d.moment_type.value,
        )
        for d in run.drafts
    ]
    out += [
        Pick(
            dup.draft.peak_utc,
            dup.draft.score,
            False,
            DEDUPE,
            dup.draft.chat_peak_utc,
            dup.draft.moment_type.value,
        )
        for dup in run.duplicates
    ]
    return sorted(out, key=lambda p: p.peak)


# --------------------------------------------------------------------------- coverage, truth


def chat_coverage(span: Interval | None, missing: Iterable[Interval]) -> list[Interval]:
    """The chat span without the missing-chat runs inside it. A run lasting to the span's end
    or beyond is the end of the data (the scorer is fed zeros past the chat so open episodes
    close), not a hole: the quiet seconds of a backfilled stream before `chat_metrics_to` are
    final rows."""
    if span is None:
        return []
    return uncovered(span, [m for m in missing if m.end < span.end])


def hours_of(coverage: Iterable[Interval]) -> float:
    return sum(iv.seconds for iv in coverage) / 3600


def overlap_s(spans: Iterable[Interval], within: Sequence[Interval]) -> float:
    """Seconds of the union of `spans` inside `within` (disjoint intervals)."""
    total = 0.0
    for iv in merge(spans):
        for w in within:
            part = iv.intersection(w)
            if part is not None:
                total += part.seconds
    return total


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear interpolation between order statistics (numpy's default)."""
    s = sorted(values)
    if not s:
        return None
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def select_truth(
    clips: Iterable[ClipRow], *, top: int = TRUTH_TOP, quantile: float = TRUTH_QUANTILE
) -> list[ClipRow]:
    """Top `top` by views plus every clip at or above the `quantile` of the views, by views
    (ties: clip id); all clips are ranked, with a span or not."""
    ranked = sorted(clips, key=lambda c: (-c.views, c.clip_id))
    bar = percentile([c.views for c in ranked], quantile)
    return [c for i, c in enumerate(ranked) if i < top or (bar is not None and c.views >= bar)]


def moments_of(clips: Iterable[ClipRow]) -> list[tuple[ClipRow, ...]]:
    """Clips whose bare spans overlap (transitively) form one moment; in time order."""
    groups: list[list[ClipRow]] = []
    end: datetime | None = None
    for c in sorted((c for c in clips if c.spanned), key=lambda c: (c.span.start, c.clip_id)):
        if groups and end is not None and c.span.start <= end:
            groups[-1].append(c)
            end = max(end, c.span.end)
        else:
            groups.append([c])
            end = c.span.end
    return [tuple(g) for g in groups]


@dataclass(frozen=True, slots=True)
class Truth:
    clips: tuple[ClipRow, ...]  # the scored truth clips (recall), by views
    with_span: tuple[ClipRow, ...]  # the truth with a span, before muted/outside (precision)
    any_clips: tuple[ClipRow, ...]  # every clip with a span (hit_rate_any)
    total: int  # viewer clips of the stream
    no_span: int  # of them without a span
    selected: int  # truth clips before the exclusions
    excluded: Mapping[str, int] = field(default_factory=dict)  # truth clips not scored, by reason
    excluded_ids: Mapping[str, str] = field(default_factory=dict)  # clip id -> reason
    coverage_s: float = 0.0  # chat coverage
    span_s: float = 0.0  # of it, inside `with_span` +- slack (video time)
    span_any_s: float = 0.0  # of it, inside `any_clips` +- slack

    @property
    def moments(self) -> list[tuple[ClipRow, ...]]:
        return moments_of(self.clips)

    @property
    def chance(self) -> float | None:
        return _ratio(self.span_s, self.coverage_s)

    @property
    def chance_any(self) -> float | None:
        return _ratio(self.span_any_s, self.coverage_s)


def exclusion(
    clip: ClipRow, coverage: Sequence[Interval], muted: Sequence[Interval], delay: timedelta
) -> str | None:
    """Why a truth clip can't be scored for recall: `no_span`, `muted`, `outside_chat`; None:
    it can."""
    if not clip.spanned:
        return NO_SPAN
    span = clip.span
    if any(span.start < m.end and m.start < span.end for m in muted):
        return MUTED
    chat = span.shift(delay)  # chat time: video + delay
    if not any(iv.start <= chat.start and chat.end <= iv.end for iv in coverage):
        return OUTSIDE_CHAT
    return None


def classify_clips(
    clips: Sequence[ClipRow],
    *,
    coverage: Sequence[Interval],
    muted: Sequence[Interval],
    delay_s: float,
) -> Truth:
    """`coverage` in chat time; spans in video time (= chat time - delay)."""
    d = timedelta(seconds=delay_s)
    truth = select_truth(clips)
    reasons = {c.clip_id: exclusion(c, coverage, muted, d) for c in truth}
    excluded = Counter(r for r in reasons.values() if r is not None)
    with_span = tuple(c for c in truth if c.spanned)
    any_clips = tuple(c for c in clips if c.spanned)
    video = [iv.shift(-d) for iv in coverage]
    return Truth(
        clips=tuple(c for c in truth if reasons[c.clip_id] is None),
        with_span=with_span,
        any_clips=any_clips,
        total=len(clips),
        no_span=len(clips) - len(any_clips),
        selected=len(truth),
        excluded=dict(sorted(excluded.items())),
        excluded_ids={k: r for k, r in reasons.items() if r is not None},
        coverage_s=sum(iv.seconds for iv in coverage),
        span_s=overlap_s((c.window for c in with_span), video),
        span_any_s=overlap_s((c.window for c in any_clips), video),
    )


# --------------------------------------------------------------------------- metrics


def top_k(rate: float, hours: float) -> int:
    """ceil(rate x hours); the epsilon keeps 4/h x 2.0 h at 8."""
    return max(0, math.ceil(rate * hours - 1e-9))


def _ratio(a: float, b: float) -> float | None:
    return a / b if b else None


def _add(maps: Iterable[Mapping[str, int]]) -> dict[str, int]:
    c: Counter[str] = Counter()
    for m in maps:
        c.update(m)
    return dict(sorted(c.items()))


@dataclass(frozen=True, slots=True)
class AtK:
    rate: float
    k: int
    considered: int  # emitted candidates taken (min(k, emitted))
    hits: int  # of those, inside a truth clip with a span (before muted/outside)
    hits_any: int  # of those, inside any viewer clip with a span
    truth: int  # scored truth clips
    truth_hit: int  # of them, hit by one of the considered
    truth_views: int
    hit_views: int
    moments: int = 0  # moments of the scored truth clips
    moments_hit: int = 0
    moment_views: int = 0  # each moment weighted by its most viewed clip
    moment_hit_views: int = 0
    offsets: tuple[float, ...] = ()  # peak - clip start (s) per truth clip hit
    false_suppressions: Mapping[str, int] = field(default_factory=dict)  # missed clips by reason

    @property
    def hit_rate(self) -> float | None:
        return _ratio(self.hits, self.considered)

    @property
    def hit_rate_any(self) -> float | None:
        return _ratio(self.hits_any, self.considered)

    @property
    def recall(self) -> float | None:
        return _ratio(self.hit_views, self.truth_views)

    @property
    def moment_recall(self) -> float | None:
        return _ratio(self.moment_hit_views, self.moment_views)

    @property
    def delay_median_s(self) -> float | None:
        return statistics.median(self.offsets) if self.offsets else None

    def plus(self, other: AtK) -> AtK:
        return AtK(
            self.rate,
            self.k + other.k,
            self.considered + other.considered,
            self.hits + other.hits,
            self.hits_any + other.hits_any,
            self.truth + other.truth,
            self.truth_hit + other.truth_hit,
            self.truth_views + other.truth_views,
            self.hit_views + other.hit_views,
            self.moments + other.moments,
            self.moments_hit + other.moments_hit,
            self.moment_views + other.moment_views,
            self.moment_hit_views + other.moment_hit_views,
            self.offsets + other.offsets,
            _add((self.false_suppressions, other.false_suppressions)),
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "considered": self.considered,
            "hits": self.hits,
            "hit_rate": _round(self.hit_rate),
            "hits_any": self.hits_any,
            "hit_rate_any": _round(self.hit_rate_any),
            "truth": self.truth,
            "truth_hit": self.truth_hit,
            "truth_views": self.truth_views,
            "hit_views": self.hit_views,
            "recall": _round(self.recall),
            "moments": self.moments,
            "moments_hit": self.moments_hit,
            "moment_recall": _round(self.moment_recall),
            "delay_median_s": _round(self.delay_median_s, 1),
            "false_suppressions": dict(self.false_suppressions),
        }


def ranked(picks: Iterable[Pick], *, emitted: bool = True) -> list[Pick]:
    """Emitted (or the other) candidates, best first (score, then the earlier peak)."""
    return sorted((p for p in picks if p.emitted is emitted), key=lambda p: (-p.score, p.peak))


def at_k(
    order: Sequence[Pick],
    truth: Truth,
    *,
    rate: float,
    hours: float,
    suppressed: Sequence[Pick] = (),
) -> AtK:
    """`order`: emitted candidates, ranked; `suppressed`: the others, ranked."""
    k = top_k(rate, hours)
    top = order[:k]
    hits = sum(1 for p in top if any(c.holds(p.peak) for c in truth.with_span))
    hits_any = sum(1 for p in top if any(c.holds(p.peak) for c in truth.any_clips))
    hit_views = truth_hit = 0
    offsets: list[float] = []
    hit_ids: set[str] = set()
    missed: Counter[str] = Counter()
    for c in truth.clips:
        best = next((p for p in top if c.holds(p.peak)), None)  # `top` is ranked
        if best is not None:
            assert c.start is not None
            hit_ids.add(c.clip_id)
            truth_hit += 1
            hit_views += c.views
            offsets.append((best.peak - c.start).total_seconds())
            continue
        lost = next((p for p in suppressed if c.holds(p.peak)), None)
        if lost is not None:
            missed[lost.reason or "?"] += 1
    groups = truth.moments
    weights = [max(c.views for c in g) for g in groups]
    got = [any(c.clip_id in hit_ids for c in g) for g in groups]
    return AtK(
        rate,
        k,
        len(top),
        hits,
        hits_any,
        len(truth.clips),
        truth_hit,
        sum(c.views for c in truth.clips),
        hit_views,
        len(groups),
        sum(got),
        sum(weights),
        sum(w for w, g in zip(weights, got, strict=True) if g),
        tuple(offsets),
        dict(sorted(missed.items())),
    )


def suppressed_in_truth(picks: Iterable[Pick], truth: Truth) -> dict[str, int]:
    """Every suppressed candidate inside a scored truth clip, by reason (raw count)."""
    found = Counter(
        p.reason or "?"
        for p in picks
        if not p.emitted and any(c.holds(p.peak) for c in truth.clips)
    )
    return dict(sorted(found.items()))


@dataclass(frozen=True, slots=True)
class StreamScore:
    hours: float
    emitted: int
    suppressed: Mapping[str, int]  # by reason (not counting `dedupe`)
    deduped: int
    truth: Truth
    at: Mapping[float, AtK]
    suppressed_in_truth: Mapping[str, int]
    after_broadcast: int = 0  # candidates after the broadcast's end, left out of the scoring

    @property
    def candidates_per_hour(self) -> float | None:
        return _ratio(self.emitted, self.hours)

    @property
    def chance(self) -> float | None:
        return self.truth.chance

    @property
    def chance_any(self) -> float | None:
        return self.truth.chance_any


def score_stream(
    picks: Sequence[Pick],
    truth: Truth,
    *,
    hours: float,
    rates: Sequence[float] = RATES,
    after_broadcast: int = 0,
) -> StreamScore:
    order = ranked(picks)
    others = ranked(picks, emitted=False)
    suppressed = Counter(p.reason or "?" for p in others if p.reason != DEDUPE)
    return StreamScore(
        hours=hours,
        emitted=len(order),
        suppressed=dict(sorted(suppressed.items())),
        deduped=sum(1 for p in others if p.reason == DEDUPE),
        truth=truth,
        at={r: at_k(order, truth, rate=r, hours=hours, suppressed=others) for r in rates},
        suppressed_in_truth=suppressed_in_truth(picks, truth),
        after_broadcast=after_broadcast,
    )


@dataclass(frozen=True, slots=True)
class Totals:
    streams: int
    hours: float
    emitted: int
    suppressed: Mapping[str, int]
    deduped: int
    clips: int
    no_span: int
    selected: int
    scored: int
    excluded: Mapping[str, int]
    at: Mapping[float, AtK]
    suppressed_in_truth: Mapping[str, int]
    coverage_s: float
    span_s: float
    span_any_s: float
    after_broadcast: int

    @property
    def candidates_per_hour(self) -> float | None:
        return _ratio(self.emitted, self.hours)

    @property
    def chance(self) -> float | None:
        return _ratio(self.span_s, self.coverage_s)

    @property
    def chance_any(self) -> float | None:
        return _ratio(self.span_any_s, self.coverage_s)


def totals(scores: Sequence[StreamScore], rates: Sequence[float] = RATES) -> Totals:
    at = {}
    for r in rates:
        acc = AtK(r, 0, 0, 0, 0, 0, 0, 0, 0)
        for s in scores:
            acc = acc.plus(s.at[r])
        at[r] = acc
    return Totals(
        streams=len(scores),
        hours=sum(s.hours for s in scores),
        emitted=sum(s.emitted for s in scores),
        suppressed=_add(s.suppressed for s in scores),
        deduped=sum(s.deduped for s in scores),
        clips=sum(s.truth.total for s in scores),
        no_span=sum(s.truth.no_span for s in scores),
        selected=sum(s.truth.selected for s in scores),
        scored=sum(len(s.truth.clips) for s in scores),
        excluded=_add(s.truth.excluded for s in scores),
        at=at,
        suppressed_in_truth=_add(s.suppressed_in_truth for s in scores),
        coverage_s=sum(s.truth.coverage_s for s in scores),
        span_s=sum(s.truth.span_s for s in scores),
        span_any_s=sum(s.truth.span_any_s for s in scores),
        after_broadcast=sum(s.after_broadcast for s in scores),
    )


# --------------------------------------------------------------------------- params, grid


def unhashed(params: DetectorParams) -> dict[str, Any]:
    """Live-loop params (`NON_SCORING`, not in `detector_version`) that differ from the code
    defaults: `dedupe_s` changes the drafts and `final_lag_s` an open stream's end, without a
    new version string, so the output names them."""
    defaults = DetectorParams()
    off = [k for k in sorted(NON_SCORING) if getattr(params, k) != getattr(defaults, k)]
    return {k: getattr(params, k) for k in off}


def run_label(version: str, params: DetectorParams) -> str:
    """`detector_version`, plus `+key=value` for each unhashed param off its default."""
    return version + "".join(f"+{k}={_value(v)}" for k, v in unhashed(params).items())


class GridError(ValueError):
    pass


def parse_grid(specs: Sequence[str]) -> list[tuple[str, list[str]]]:
    """`key=v1,v2,...` per spec; keys must be `DetectorParams` fields with an effect here, each
    given once."""
    out: list[tuple[str, list[str]]] = []
    fields = DetectorParams.model_fields
    for spec in specs:
        key, sep, values = spec.partition("=")
        key = key.strip()
        vals = [v.strip() for v in values.split(",") if v.strip()]
        if not sep or not key or not vals:
            raise GridError(f"--grid wants key=v1,v2,...: {spec!r}")
        if key not in fields:
            raise GridError(f"--grid: {key!r} is not a detector param (DetectorParams field)")
        if key in GRID_NO_EFFECT:
            raise GridError(f"--grid: {key!r} only steers the live restart replay: no effect here")
        if any(k == key for k, _ in out):
            raise GridError(f"--grid: {key!r} given twice")
        out.append((key, vals))
    return out


def grid_params(
    base: DetectorParams, grid: Sequence[tuple[str, Sequence[str]]]
) -> list[tuple[dict[str, Any], DetectorParams]]:
    """Every combination (the cartesian product, in the order given) on top of `base`,
    validated like a TOML file (`GridError` names the first invalid one)."""
    if not grid:
        return [({}, base)]
    keys = [k for k, _ in grid]
    out = []
    for combo in itertools.product(*(vals for _, vals in grid)):
        raw = dict(zip(keys, combo, strict=True))
        try:
            params = DetectorParams.model_validate({**base.model_dump(), **raw})
        except ValueError as exc:  # pydantic's ValidationError: drop its header and links
            lines = [line.strip() for line in str(exc).splitlines()[1:]]
            detail = "; ".join(
                line.split(" [type=")[0]
                for line in lines
                if line and not line.startswith("For further information")
            )
            raise GridError(f"--grid {raw}: {detail or exc}") from None
        out.append(({k: getattr(params, k) for k in keys}, params))
    return out


# --------------------------------------------------------------------------- output


def _round(x: float | None, digits: int = 3) -> float | None:
    return None if x is None else round(x, digits)


def _f(x: float | None, fmt: str = ".2f") -> str:
    return "-" if x is None else format(x, fmt)


def _delay(x: float | None) -> str:
    return "-" if x is None else f"{x:+.1f}"


def _reasons(m: Mapping[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in m.items()) or "-"


def _value(v: Any) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def rate_label(rate: float) -> str:
    return f"{rate:g}/h"


@dataclass(frozen=True, slots=True)
class StreamResult:
    """One stream of a run: scored, or skipped with a note."""

    stream_id: int
    channel: str
    source: str
    score: StreamScore | None = None
    note: str | None = None  # why it wasn't scored
    detail: Mapping[str, Any] = field(default_factory=dict)  # JSON extras (times, candidates)
    warnings: tuple[str, ...] = ()


def _stream_cells(
    sid: str, channel: str, s: StreamScore | Totals, rates: Sequence[float], main: float
) -> list[str]:
    if isinstance(s, Totals):
        clips, selected, scored, excluded = s.clips, s.selected, s.scored, s.excluded
    else:
        t = s.truth
        clips, selected, scored, excluded = t.total, t.selected, len(t.clips), t.excluded
    m = s.at[main]
    return [
        sid,
        channel,
        f"{s.hours:.2f}",
        str(clips),
        str(selected),
        str(scored),
        str(excluded.get(OUTSIDE_CHAT, 0)),
        str(excluded.get(MUTED, 0)),
        str(excluded.get(NO_SPAN, 0)),
        _f(s.candidates_per_hour, ".1f"),
        str(m.k),
        _f(m.hit_rate),
        _f(s.chance),
        *(_f(s.at[r].recall) for r in rates),
        _f(m.moment_recall),
        _delay(m.delay_median_s),
        _reasons(m.false_suppressions),
    ]


def _table(head: Sequence[str], rows: Sequence[Sequence[str]], left: int = 0) -> list[str]:
    """Columns padded to their widest cell; the last column and the first `left` are
    left-aligned, the rest right-aligned."""
    widths = [max(len(r[i]) for r in (head, *rows)) for i in range(len(head))]
    out = []
    for r in (head, *rows):
        cells = []
        for i, cell in enumerate(r):
            last = i == len(r) - 1
            if last:
                cells.append(cell)
            elif i < left:
                cells.append(cell.ljust(widths[i]))
            else:
                cells.append(cell.rjust(widths[i]))
        out.append("  ".join(cells).rstrip())
    return out


def stream_table(results: Sequence[StreamResult], rates: Sequence[float], main: float) -> list[str]:
    head = [
        "stream",
        "channel",
        "hours",
        "clips",
        "truth",
        "scored",
        "outside",
        "muted",
        "nospan",
        "cand/h",
        "k",
        "hit@k",
        "chance",
        *(f"recall@{rate_label(r)}" for r in rates),
        f"moments@{rate_label(main)}",
        "delay_s",
        "false suppressions",
    ]
    rows: list[list[str]] = []
    notes: list[str] = []
    for r in results:
        notes += [f"stream {r.stream_id} ({r.channel}): warning: {w}" for w in r.warnings]
        if r.score is None:
            notes.append(f"stream {r.stream_id} ({r.channel}): not scored, {r.note}")
            continue
        rows.append(_stream_cells(str(r.stream_id), r.channel, r.score, rates, main))
    scored = [r.score for r in results if r.score is not None]
    if scored:
        rows.append(_stream_cells("total", "", totals(scored, rates), rates, main))
    lines = _table(head, rows, left=2) if rows else []
    return lines + notes


def grid_table(
    combos: Sequence[tuple[Mapping[str, Any], str, Totals]],
    keys: Sequence[str],
    rates: Sequence[float],
    main: float,
) -> list[str]:
    """One row per combination, best recall@k (main rate) first."""
    head = [
        *keys,
        "streams",
        "hours",
        "truth",
        "hit@k",
        "chance",
        *(f"recall@{rate_label(r)}" for r in rates),
        f"moments@{rate_label(main)}",
        "delay_s",
        "cand/h",
        "false_supp",
        "version",
    ]

    def order(item: tuple[Mapping[str, Any], str, Totals]) -> tuple[float, float]:
        m = item[2].at[main]
        return (-(m.recall if m.recall is not None else -1.0), -(m.hit_rate or 0.0))

    rows = []
    for values, version, t in sorted(combos, key=order):
        m = t.at[main]
        rows.append(
            [
                *(_value(values[k]) for k in keys),
                str(t.streams),
                f"{t.hours:.2f}",
                str(t.scored),
                _f(m.hit_rate),
                _f(t.chance),
                *(_f(t.at[r].recall) for r in rates),
                _f(m.moment_recall),
                _delay(m.delay_median_s),
                _f(t.candidates_per_hour, ".1f"),
                str(sum(m.false_suppressions.values())),
                version,
            ]
        )
    return _table(head, rows)


def score_json(s: StreamScore | Totals) -> dict[str, Any]:
    if isinstance(s, Totals):
        clips = {
            "total": s.clips,
            "no_span": s.no_span,
            "truth": s.selected,
            "scored": s.scored,
            "excluded": dict(s.excluded),
        }
        extra: dict[str, Any] = {"streams": s.streams}
    else:
        t = s.truth
        clips = {
            "total": t.total,
            "no_span": t.no_span,
            "truth": t.selected,
            "scored": len(t.clips),
            "excluded": dict(t.excluded),
        }
        extra = {}
    return {
        **extra,
        "hours": round(s.hours, 4),
        "candidates": {
            "emitted": s.emitted,
            "per_hour": _round(s.candidates_per_hour, 2),
            "suppressed": dict(s.suppressed),
            "deduped": s.deduped,
            "after_broadcast": s.after_broadcast,
        },
        "clips": clips,
        "chance": _round(s.chance),
        "chance_any": _round(s.chance_any),
        "at": {rate_label(r): a.as_json() for r, a in s.at.items()},
        "suppressed_in_truth": dict(s.suppressed_in_truth),
    }


def result_json(r: StreamResult) -> dict[str, Any]:
    out: dict[str, Any] = {"stream_id": r.stream_id, "channel": r.channel, "source": r.source}
    if r.score is None:
        out["note"] = r.note
    else:
        out.update(score_json(r.score))
    out["warnings"] = list(r.warnings)
    out.update(r.detail)
    return out


def _jsonable(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def dumps(value: Any) -> str:
    return json.dumps(value, indent=2, default=_jsonable, ensure_ascii=False)
