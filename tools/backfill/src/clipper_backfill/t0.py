"""T0 of a VOD: the UTC time of VOD offset 0, from every source we have (DESIGN §17).

Pure: inputs are already fetched (playlist PDTs, sampled chat comments, the platform's
`created_at`, the CDN path's epoch); `estimate_t0` picks one and keeps all of them.

Choice (decision 2026-11-19, §17 "Time model"):
1. **PDT** (`T0Method.PDT`): the `audio_only` playlist carries EXT-X-PROGRAM-DATE-TIME on every
   segment, and it is `T0_pdt + offset`. Live media times come from PDT, so VOD media follows the
   same clock. Video renditions have no PDT; their segments line up with `audio_only`'s, so they
   use the same T0.
2. **Chat anchor** (`T0Method.CHAT`, no PDT): `x = createdAt - contentOffsetSeconds` over the
   sampled comments, whole-hour outliers (timezone bug) shifted back first, farther outliers
   dropped; T0 = median(x) - 0.5 s (offsets are whole seconds, assumed truncated). If it is more
   than 30 s off `created_at` (the old-VOD offset bug), `created_at` wins and `t0_conflict` is set.
3. **created_at** (`T0Method.HELIX`): Helix Get Videos `created_at`, or GQL `createdAt` without
   Helix credentials. Last resort.

Cross-checks become `flags`: the chat anchor sat 3.9 s after `T0_pdt` on the smoke VOD (Twitch's
replay compensates stream latency), so `|chat - pdt - 3.9 s| > 3 s` -> `t0_chat_pdt`;
`created_at` more than 30 s from the chosen T0 -> `t0_conflict`; a PDT that jumps by >= 1 s
against `T0 + offset` -> `pdt_steps` (not applied: UTC stays `T0 + offset`, the VOD needs a look).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from clipper_core.contracts import T0Method
from clipper_core.timeline import ensure_utc
from clipper_twitch.hls import VodSegment

MIN_COMMENTS = 20  # fewer sampled comments: the chat anchor is not used
# contentOffsetSeconds is a whole second; assumed truncated, so x = T0 + U(0, 1): T0 = median - 0.5
CHAT_OFFSET_ROUNDING_S = 0.5
CHAT_MINUS_PDT_S = 3.9  # chat anchor - T0_pdt on the smoke VOD (xqc 2932113307, 2026-11-19)
CHAT_PDT_TOLERANCE_S = 3.0
CREATED_AT_TOLERANCE_S = 30.0
PDT_STEP_S = 1.0  # a PDT jump at least this large against T0 + offset is a timeline step
HOUR_FIX_SLACK_S = 120.0  # x within this of median + k hours (k != 0): a timezone bug, shift
CHAT_OUTLIER_S = 30.0  # after the hour fix, x farther than this from the median is dropped
MAX_LISTED = 20  # steps listed in flags


@dataclass(frozen=True, slots=True)
class PdtPoint:
    offset_s: float  # VOD offset of the segment
    pdt: datetime  # its EXT-X-PROGRAM-DATE-TIME (UTC)


@dataclass(frozen=True, slots=True)
class ChatPoint:
    offset_s: int  # contentOffsetSeconds
    created_at: datetime  # createdAt (UTC)


@dataclass(frozen=True, slots=True)
class ChatAnchor:
    t0: datetime  # median - CHAT_OFFSET_ROUNDING_S
    median: datetime
    spread_s: float  # max - min of the kept values
    comments: int  # kept
    hour_fixed: int
    outliers: int


@dataclass(frozen=True, slots=True)
class T0Estimate:
    t0: datetime
    method: T0Method
    candidates: dict[str, Any]  # JSON-ready: `vods.t0_candidates`
    flags: dict[str, Any]  # JSON-ready: merged into `vods.flags`


def pdt_points(segments: Iterable[VodSegment]) -> list[PdtPoint]:
    """The segments that carry a PDT (all of them on `audio_only`, none on video renditions)."""
    return [PdtPoint(s.offset_s, s.program_date_time) for s in segments if s.program_date_time]


def _iso(dt: datetime) -> str:
    return ensure_utc(dt).isoformat()


def _median(values: Sequence[datetime]) -> datetime:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    a, b = ordered[mid - 1], ordered[mid]
    return a + (b - a) / 2


def pdt_anchor(points: Sequence[PdtPoint]) -> tuple[datetime, float, list[dict[str, float]]]:
    """(T0 from the first point, max |PDT - (T0 + offset)| in s, steps >= PDT_STEP_S)."""
    ordered = sorted(points, key=lambda p: p.offset_s)
    first = ordered[0]
    t0 = ensure_utc(first.pdt) - timedelta(seconds=first.offset_s)
    steps: list[dict[str, float]] = []
    max_dev = prev_dev = 0.0
    for p in ordered:
        dev = (ensure_utc(p.pdt) - t0).total_seconds() - p.offset_s
        max_dev = max(max_dev, abs(dev))
        if abs(dev - prev_dev) >= PDT_STEP_S:
            steps.append({"offset_s": round(p.offset_s, 3), "step_s": round(dev - prev_dev, 3)})
        prev_dev = dev
    return t0, max_dev, steps


def chat_anchor(points: Sequence[ChatPoint]) -> ChatAnchor | None:
    """The chat anchor, or None with fewer than MIN_COMMENTS usable comments."""
    xs = [ensure_utc(p.created_at) - timedelta(seconds=p.offset_s) for p in points]
    if len(xs) < MIN_COMMENTS:
        return None
    ref = _median(xs)
    kept: list[datetime] = []
    hour_fixed = outliers = 0
    for x in xs:
        d = (x - ref).total_seconds()
        hours = round(d / 3600)
        if hours and abs(d - hours * 3600) <= HOUR_FIX_SLACK_S:
            x -= timedelta(hours=hours)
            d -= hours * 3600
            hour_fixed += 1
        if abs(d) > CHAT_OUTLIER_S:
            outliers += 1
            continue
        kept.append(x)
    if len(kept) < MIN_COMMENTS:
        return None
    median = _median(kept)
    return ChatAnchor(
        t0=median - timedelta(seconds=CHAT_OFFSET_ROUNDING_S),
        median=median,
        spread_s=(max(kept) - min(kept)).total_seconds(),
        comments=len(kept),
        hour_fixed=hour_fixed,
        outliers=outliers,
    )


def estimate_t0(
    *,
    pdt: Sequence[PdtPoint] = (),
    chat: Sequence[ChatPoint] = (),
    created_at: datetime | None = None,
    created_at_source: str = "helix",  # "helix" | "gql_created_at": the candidate's key
    path_epoch: datetime | None = None,
) -> T0Estimate:
    """Pick T0 (PDT > chat anchor > created_at) and record every candidate and disagreement.

    Raises ValueError when there is nothing to go on.
    """
    candidates: dict[str, Any] = {}
    flags: dict[str, Any] = {}

    t0_pdt = None
    if pdt:
        t0_pdt, max_dev, steps = pdt_anchor(pdt)
        candidates["pdt"] = _iso(t0_pdt)
        candidates["pdt_points"] = len(pdt)
        candidates["pdt_max_dev_s"] = round(max_dev, 3)
        if steps:
            flags["pdt_steps"] = steps[:MAX_LISTED]
            flags["pdt_step_count"] = len(steps)

    anchor = chat_anchor(chat)
    if chat:
        candidates["comments_sampled"] = len(chat)
    if anchor:
        candidates["chat"] = _iso(anchor.t0)
        candidates["chat_median"] = _iso(anchor.median)
        candidates["chat_spread_s"] = round(anchor.spread_s, 3)
        candidates["comments"] = anchor.comments
        candidates["chat_offset_rounding"] = "truncated (assumed)"
        if anchor.hour_fixed:
            flags["chat_hour_fixed"] = anchor.hour_fixed
        if anchor.outliers:
            flags["chat_outliers"] = anchor.outliers

    if created_at is not None:
        created_at = ensure_utc(created_at)
        candidates[created_at_source] = _iso(created_at)
    if path_epoch is not None:
        candidates["path_epoch"] = _iso(path_epoch)

    if t0_pdt is not None:
        t0, method = t0_pdt, T0Method.PDT
    elif anchor is not None:
        t0, method = anchor.t0, T0Method.CHAT
        if created_at is not None:
            off = (anchor.t0 - created_at).total_seconds()
            if abs(off) > CREATED_AT_TOLERANCE_S:  # old-VOD offset bug: trust created_at
                t0, method = created_at, T0Method.HELIX
                flags["t0_conflict"] = {"chat_minus_created_at_s": round(off, 3)}
    elif created_at is not None:
        t0, method = created_at, T0Method.HELIX
    else:
        raise ValueError("no T0 candidate: no PDT, too few chat comments and no created_at")

    if t0_pdt is not None and anchor is not None:
        diff = (anchor.t0 - t0_pdt).total_seconds()
        candidates["chat_minus_pdt_s"] = round(diff, 3)
        if abs(diff - CHAT_MINUS_PDT_S) > CHAT_PDT_TOLERANCE_S:
            flags["t0_chat_pdt"] = {
                "chat_minus_pdt_s": round(diff, 3),
                "expected_s": CHAT_MINUS_PDT_S,
            }
    if created_at is not None:
        diff = (created_at - t0).total_seconds()
        candidates["created_at_minus_t0_s"] = round(diff, 3)
        if abs(diff) > CREATED_AT_TOLERANCE_S and "t0_conflict" not in flags:
            flags["t0_conflict"] = {"created_at_minus_t0_s": round(diff, 3)}
    return T0Estimate(ensure_utc(t0), method, candidates, flags)
