"""`clipper-detector backtest`: the detector over stored streams, scored against viewer clips
(DESIGN §18.8; the metrics are in `backtest`).

    uv run clipper-detector backtest --stream 803
    uv run clipper-detector backtest --channel ohnepixel --since 2026-11-01 --json
    uv run clipper-detector backtest --stream 803 --grid z_open=3.5,4,5 --grid rho_open=1.5,2

**Read-only**: SELECTs only, on a connection whose every transaction is READ ONLY
(`-c default_transaction_read_only=on`): no candidates, leases, jobs or media requests, and
existing candidates are not read either (the run starts from nothing, like a live run from the
stream's start).

**What runs** (one scorer for live, offline and backtest): `pipeline.run_stream` over the rows
the live service would read for the stream, read with its helpers (`reads`): every whole second
in `[started_at, end)` (no `chat_metrics_1s` row = 0; `end` = `ended_at`, the channel's next
stream's start or the final cutoff, and no further than the chat's end + the longest an episode
can stay open, so a stream whose chat stopped early isn't fed hours of zeros that can't change
anything), viewers = the latest `viewer_snapshots` row at or before each second (from 120 s
before the start; backfilled streams: unknown), the prior from the previous stream
(`reads.prior_baseline`), the delay override -> learned -> default, raw chat and `chat_events`
(live streams only) of each episode's stage-2 windows. Explicit signals ("clip it" mentions,
clip links in chat) are not replayed: the drafts are those of a live run without signals, and
Helix viewer clips are the truth, never an input.

**What is scored** (`backtest` has the metrics): a stream without viewer clips, or without one
with a span, or without chat rows, is listed with a note and left out of the totals. The chat
coverage ends at the broadcast's real end when known: the `vods` row's `t0_utc + duration_s`,
else the last chunk's end (+ the delay: chat time), so offline chat that chat-ingest logged into
a stream the controller ended late is no coverage, and candidates there are left out of the
scoring (`after_broadcast`); a warning when the coverage still runs more than 10 min past the
last viewer clip. A VOD flagged `timeline_uncertain_s` adds that much to every clip's slack (a
warning) instead of being skipped.

`--grid key=v1,v2` (repeatable: the cartesian product) runs every combination on top of
`--params` (or `CLIPPER_DETECTOR_PARAMS_PATH`, else the code defaults) and prints one row per
combination, best recall@k first. The chat coverage, and with it the truth clips scored, comes
from the base params, so every combination is scored on the same clips. `restart_replay_s` and
`restart_emit_s` are refused (no effect here); `dedupe_s` and `final_lag_s` are not part of
`detector_version`, so a run with them off their defaults is labelled `<version>+dedupe_s=30`.

Output: a table per stream + a total row (stdout; logs go to stderr), `--json` instead for
machines (with every candidate and truth clip of a stream unless `--grid`). Exit 2: unknown
stream/channel, nothing to backtest, an invalid `--params`/`--lexicon`/`--grid`.

`--export-drafts FILE` (no scoring, so streams without viewer clips are fine; not with `--grid`):
every draft of the base params, emitted and suppressed, of each stream to a JSON file
(`clipper-detector-drafts` v1; each draft has the `candidates` columns and a stable id
`d<stream>-<peak epoch s>`). The judge's eval labelling takes them as cases that aren't live
candidates (`clipper-judge label prepare --drafts`), e.g. with looser `--params` for negatives.
"""

from __future__ import annotations

import argparse
import sys
import time
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_core.config import Settings
from clipper_core.contracts import DataSource, GapReason
from clipper_core.db import create_engine, session_factory
from clipper_core.logs import get_logger, setup_logging
from clipper_core.timeline import Interval, ensure_utc, merge, utc_now
from clipper_detector import reads
from clipper_detector.backtest import (
    DEDUPE,
    RATES,
    ClipRow,
    Pick,
    StreamResult,
    Truth,
    chat_coverage,
    classify_clips,
    dumps,
    grid_params,
    grid_table,
    hours_of,
    parse_grid,
    picks_of,
    ranked,
    rate_label,
    result_json,
    run_label,
    score_json,
    score_stream,
    stream_table,
    totals,
    unhashed,
)
from clipper_detector.lexicon import Lexicon
from clipper_detector.merge import CandidateDraft, resolve_delay
from clipper_detector.params import DetectorParams, detector_version, load_params
from clipper_detector.pipeline import StreamRun, run_seconds, run_stream
from clipper_detector.reads import ChannelInfo, StreamInfo
from clipper_detector.scorer import PriorBaseline, Second
from clipper_detector.service import VIEWER_LOOKBACK, final_end
from clipper_detector.settings import DetectorSettings
from clipper_detector.stage2 import windows

log = get_logger(__name__)

READ_ONLY = {"options": "-c default_transaction_read_only=on"}  # every transaction READ ONLY
ONE = timedelta(seconds=1)
LEARNED_MAX = 50  # learned delays read once; each params set takes its first learned_delay_count
PAST_LAST_CLIP = timedelta(minutes=10)  # coverage further past the last clip: a warning


class UsageError(ValueError):
    pass


# --------------------------------------------------------------------------- arguments


def _day(value: str) -> datetime:
    try:
        day = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date (YYYY-MM-DD): {value!r}") from None
    return ensure_utc(day if day.tzinfo else day.replace(tzinfo=UTC))


def _rate(value: str) -> float:
    try:
        rate = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from None
    if not rate > 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return rate


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    b = sub.add_parser(
        "backtest",
        help="read-only: replay stored streams through the detector, score against viewer clips",
        description="Replay stored streams through the detector's pipeline (as the live service "
        "would score them) and score the candidates against the streams' viewer clips (DESIGN "
        "18.8). Read-only.",
    )
    which = b.add_mutually_exclusive_group(required=True)
    which.add_argument("--stream", type=int, nargs="+", action="extend", metavar="ID")
    which.add_argument("--channel", metavar="LOGIN", help="the channel's streams (by start)")
    b.add_argument("--since", type=_day, metavar="YYYY-MM-DD", help="--channel: started since")
    b.add_argument("--until", type=_day, metavar="YYYY-MM-DD", help="--channel: started before")
    b.add_argument(
        "--params",
        type=Path,
        metavar="TOML",
        help="DetectorParams overrides (default: CLIPPER_DETECTOR_PARAMS_PATH, else the code "
        "defaults)",
    )
    b.add_argument(
        "--lexicon",
        type=Path,
        metavar="TOML",
        help="lexicon file (default: CLIPPER_DETECTOR_LEXICON_PATH, else the packaged one)",
    )
    b.add_argument(
        "--grid",
        action="append",
        default=[],
        metavar="KEY=V1,V2",
        help="run every value (repeat for a cartesian product); one row per combination",
    )
    b.add_argument(
        "--k-per-hour",
        type=_rate,
        default=4.0,
        metavar="N",
        help="k = ceil(N x chat hours) for hit@k and the sort (4; 2/h and 8/h are shown too)",
    )
    b.add_argument("--json", action="store_true", help="JSON instead of the table")
    b.add_argument(
        "--export-drafts",
        type=Path,
        metavar="FILE",
        help="no scoring: write every draft of the base params (emitted and suppressed) to FILE "
        "(JSON), also for streams without viewer clips",
    )
    return b


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="clipper-detector")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = add_parser(sub)
    args = p.parse_args(["backtest", *argv])
    check_args(b, args)
    return args


def check_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if (args.since or args.until) and not args.channel:
        parser.error("--since/--until go with --channel")
    if getattr(args, "export_drafts", None) and args.grid:
        parser.error("--export-drafts runs the base params only (no --grid)")


@dataclass(frozen=True)
class Config:
    base: DetectorParams
    params_source: str
    lexicon: Lexicon
    combos: list[tuple[dict[str, Any], DetectorParams]]
    keys: list[str]
    rates: tuple[float, ...]
    main: float

    @property
    def grid(self) -> bool:
        return bool(self.keys)


def configure(args: argparse.Namespace, settings: DetectorSettings | None = None) -> Config:
    """Params, lexicon and grid (`UsageError` on anything invalid)."""
    ds = settings if settings is not None else DetectorSettings()
    path = args.params if args.params is not None else ds.params_path
    try:
        base = load_params(path)
        lexicon = Lexicon.load(args.lexicon if args.lexicon is not None else ds.lexicon_path)
        grid = parse_grid(args.grid)
        combos = grid_params(base, grid)
    except (ValueError, OSError) as exc:  # ValidationError, LexiconError, TOMLDecodeError, Grid
        raise UsageError(str(exc)) from None
    rates = tuple(sorted({RATES[0], args.k_per_hour, RATES[-1]}))  # 2/h, the main rate, 8/h
    return Config(
        base=base,
        params_source=str(path) if path is not None else "code defaults",
        lexicon=lexicon,
        combos=combos,
        keys=[k for k, _ in grid],
        rates=rates,
        main=args.k_per_hour,
    )


# --------------------------------------------------------------------------- reads


@dataclass
class StreamData:
    """What the backtest reads of one stream (params-independent)."""

    stream: StreamInfo
    channel: ChannelInfo
    seconds: list[Second]  # every whole second fed, from the start
    chat_span: Interval | None  # [first chat second, chat end); None: no chat
    learned: list[float]
    clips: list[ClipRow]
    muted: list[Interval]
    uncertain_s: float | None  # vods.flags.timeline_uncertain_s (added to every clip's slack)
    broadcast_end: datetime | None = None  # video time: the VOD's end, else the last chunk's
    broadcast_end_source: str | None = None  # "vod" | "chunks"
    priors: dict[tuple[int, int], PriorBaseline | None] = field(default_factory=dict)

    @property
    def events_known(self) -> bool:
        return self.stream.live

    @property
    def viewers_known(self) -> bool:
        return self.stream.live


def _ceil_second(ts: datetime) -> datetime:
    whole = ts.replace(microsecond=0)
    return whole if whole == ts else whole + ONE


def tail_s(params: DetectorParams) -> int:
    """Seconds after the chat's end within which any open episode closes (forced at the
    latest): feeding further zeros changes nothing."""
    return params.force_close_s + params.close_after_s + params.activity_window_s + 1


async def stream_ids(session: AsyncSession, args: argparse.Namespace) -> list[int]:
    if args.stream:
        ids = list(dict.fromkeys(args.stream))
        found = {
            r.id
            for r in (
                await session.execute(
                    text("SELECT id FROM streams WHERE id = ANY(:ids)"), {"ids": ids}
                )
            ).all()
        }
        missing = [i for i in ids if i not in found]
        if missing:
            raise UsageError(f"unknown stream id(s): {', '.join(map(str, missing))}")
        return ids
    channel = (
        await session.execute(
            text("SELECT id, login FROM channels WHERE lower(login) = lower(:l) ORDER BY id"),
            {"l": args.channel},
        )
    ).first()
    if channel is None:
        raise UsageError(f"unknown channel {args.channel!r}")
    rows = (
        await session.execute(
            text(
                """
                SELECT id FROM streams
                 WHERE channel_id = :c
                   AND (CAST(:since AS timestamptz) IS NULL OR started_at >= :since)
                   AND (CAST(:until AS timestamptz) IS NULL OR started_at < :until)
                 ORDER BY started_at, id
                """
            ),
            {"c": channel.id, "since": args.since, "until": args.until},
        )
    ).all()
    if not rows:
        raise UsageError(f"{channel.login}: no streams in that range")
    return [r.id for r in rows]


async def load_stream(
    session: AsyncSession, stream_id: int, *, now: datetime, params: Sequence[DetectorParams]
) -> StreamData:
    s = (
        await session.execute(
            text(
                """
                SELECT s.id, s.channel_id, s.started_at, s.ended_at, s.source,
                       c.login, c.platform_user_id, c.chat_delay_s,
                       (SELECT min(n.started_at) FROM streams n
                         WHERE n.channel_id = s.channel_id AND n.id <> s.id
                           AND (n.started_at > s.started_at
                                OR (n.started_at = s.started_at AND n.id > s.id))) AS next_start,
                       (SELECT max(k.end_utc) FROM chunks k WHERE k.stream_id = s.id)
                           AS last_chunk_end,
                       v.flags AS vod_flags, v.chat_metrics_to, v.t0_utc, v.duration_s
                  FROM streams s JOIN channels c ON c.id = s.channel_id
                  LEFT JOIN vods v ON v.stream_id = s.id
                 WHERE s.id = :id
                """
            ),
            {"id": stream_id},
        )
    ).one()
    stream = StreamInfo(s.id, s.channel_id, s.started_at, s.ended_at, s.source)
    channel = ChannelInfo(s.channel_id, s.login, s.platform_user_id, s.chat_delay_s)
    lag = max(p.final_lag_s for p in params)
    # live assigns a second to the latest-started stream holding it: the next stream's start
    # ends this one's seconds; an open stream ends at the final cutoff
    cap = min(t for t in (s.ended_at, s.next_start, final_end(now, lag)) if t is not None)
    start = _ceil_second(stream.started_at)
    rows = await reads.metrics(session, {channel.id: start}, cap) if start < cap else []
    chat_span = None
    feed_until = start
    if rows:
        chat_end = rows[-1].ts + ONE
        if stream.source == DataSource.BACKFILL and s.chat_metrics_to is not None:
            # rows before chat_metrics_to are final: quiet seconds there are quiet chat
            chat_end = max(chat_end, min(s.chat_metrics_to, cap))
        chat_span = Interval(rows[0].ts, chat_end)
        feed_until = min(cap, _ceil_second(chat_end) + timedelta(seconds=max(map(tail_s, params))))
    viewers: list[tuple[datetime, int]] | None = None
    if stream.live:
        vrows = await reads.viewer_snapshots(
            session, {stream.id: start - VIEWER_LOOKBACK}, feed_until
        )
        viewers = [(r.ts, int(r.viewers)) for r in vrows]
    seconds = _seconds(rows, start, feed_until, viewers)
    learned = await reads.learned_delays(session, channel.id, stream.started_at, LEARNED_MAX)
    clip_rows = (
        await session.execute(
            text(
                """
                SELECT clip_id, start_utc, end_utc, duration_s, view_count, slack_s
                  FROM viewer_clips WHERE stream_id = :s ORDER BY view_count DESC, clip_id
                """
            ),
            {"s": stream.id},
        )
    ).all()
    flags = s.vod_flags or {}
    uncertain = flags.get("timeline_uncertain_s")
    uncertain_s = None if uncertain is None else float(uncertain)
    clips = [
        ClipRow(
            r.clip_id,
            r.start_utc,
            r.end_utc,
            r.duration_s,
            r.view_count,
            r.slack_s,
            uncertain_s or 0.0,
        )
        for r in clip_rows
    ]
    gap_rows = (
        await session.execute(
            text(
                "SELECT start_utc, end_utc FROM stream_gaps"
                " WHERE stream_id = :s AND reason = :r ORDER BY start_utc"
            ),
            {"s": stream.id, "r": GapReason.MUTED.value},
        )
    ).all()
    broadcast_end, end_source = None, None
    if s.t0_utc is not None and s.duration_s is not None:
        broadcast_end, end_source = s.t0_utc + timedelta(seconds=s.duration_s), "vod"
    elif s.last_chunk_end is not None:
        broadcast_end, end_source = s.last_chunk_end, "chunks"
    return StreamData(
        stream=stream,
        channel=channel,
        seconds=seconds,
        chat_span=chat_span,
        learned=learned,
        clips=clips,
        muted=[Interval(r.start_utc, r.end_utc) for r in gap_rows],
        uncertain_s=uncertain_s,
        broadcast_end=broadcast_end,
        broadcast_end_source=end_source,
    )


def _seconds(
    rows: Sequence[Any],
    start: datetime,
    until: datetime,
    viewers: list[tuple[datetime, int]] | None,
) -> list[Second]:
    """Every whole second in [start, until), as the live loop builds them (`_second`)."""
    by_ts = {r.ts: r for r in rows}
    vts = [v[0] for v in viewers] if viewers else []
    out = []
    t = start
    while t < until:
        v = None
        if viewers:
            i = bisect_right(vts, t)
            v = viewers[i - 1][1] if i else None  # the latest snapshot at or before t
        row = by_ts.get(t)
        if row is None:
            out.append(Second(t, 0, 0, v, 0))
        else:
            out.append(Second(t, row.unique_chatters, row.messages, v, row.toggles))
        t += ONE
    return out


async def prior_for(
    session: AsyncSession, data: StreamData, params: DetectorParams
) -> PriorBaseline | None:
    """The previous stream's prior (cached per the params it depends on)."""
    key = (params.min_baseline_values, params.activity_window_s)
    if key not in data.priors:
        data.priors[key] = await reads.prior_baseline(session, data.stream, params)
    return data.priors[key]


async def stage2_rows(
    session: AsyncSession,
    data: StreamData,
    message_windows: Sequence[Interval],
    event_windows: Sequence[Interval],
) -> tuple[list[Any], list[Any] | None]:
    """Raw chat (and chat_events of a live stream) of the merged stage-2 windows, in the live
    reads' order; events of a backfilled stream are unknown (None)."""
    messages: list[Any] = []
    for iv in merge(message_windows):
        messages += await reads.messages(session, data.channel.id, iv)
    events: list[Any] | None = None
    if data.events_known:
        events = []
        for iv in merge(event_windows):
            events += await reads.events(session, data.channel.id, iv)
    return messages, events


# --------------------------------------------------------------------------- one stream


@dataclass(frozen=True)
class Replayed:
    values: dict[str, Any]
    version: str  # the run label: detector_version + unhashed params off their defaults
    run: StreamRun
    result: StreamResult


async def backtest_stream(
    sessions: async_sessionmaker[AsyncSession], stream_id: int, cfg: Config, *, now: datetime
) -> list[Replayed]:
    """Every combination on one stream (one entry per combination; a stream that can't be
    scored gives entries with a note and no score)."""
    started = time.perf_counter()
    params_all = [cfg.base, *(p for _, p in cfg.combos)]
    async with sessions() as session:
        data = await load_stream(session, stream_id, now=now, params=params_all)
        lexicon = cfg.lexicon.for_channel(data.channel.login)
        note = None
        if not data.clips:
            note = "no viewer clips"
        elif not any(c.spanned for c in data.clips):
            note = "no spanned clips (no vod_offset yet)"
        elif data.chat_span is None:
            note = "no chat rows"
        if note is not None:
            log.info("backtest stream skipped", stream_id=stream_id, reason=note)
            result = StreamResult(stream_id, data.channel.login, data.stream.source, note=note)
            return [
                Replayed(v, run_label(detector_version(p, cfg.lexicon), p), StreamRun(), result)
                for v, p in cfg.combos
            ]

        # pass 1 (stage 1 only, cheap): the chat coverage from the base params, every
        # combination's stage-2 windows
        prepared = []
        msg_windows: list[Interval] = []
        ev_windows: list[Interval] = []
        base_prior = await prior_for(session, data, cfg.base)
        _, _, missing = run_seconds(
            data.seconds, cfg.base, started_at=data.stream.started_at, prior=base_prior
        )
        for values, p in cfg.combos:
            prior = await prior_for(session, data, p)
            episodes, _, _ = run_seconds(
                data.seconds, p, started_at=data.stream.started_at, prior=prior
            )
            for ep in episodes:
                win = windows(ep, p)
                msg_windows.append(win.messages)
                ev_windows.append(win.events)
            prepared.append((values, p, prior))
        messages, events = await stage2_rows(session, data, msg_windows, ev_windows)

    assert data.chat_span is not None
    base_delay = resolve_delay(data.channel.chat_delay_s, data.learned, cfg.base)
    span, cap = covered_span(data, base_delay.td)
    coverage = chat_coverage(span, [Interval(m.start, m.end) for m in missing])
    hours = hours_of(coverage)
    truth = classify_clips(
        data.clips, coverage=coverage, muted=data.muted, delay_s=base_delay.seconds
    )
    warnings = stream_warnings(data, coverage, base_delay.td)
    done = []
    for values, p, prior in prepared:
        version = detector_version(p, cfg.lexicon)
        delay = resolve_delay(data.channel.chat_delay_s, data.learned, p)
        run = run_stream(
            data.seconds,
            messages,
            events,
            params=p,
            lexicon=lexicon,
            stream_id=data.stream.id,
            channel_id=data.channel.id,
            delay=delay,
            detector_version=version,
            started_at=data.stream.started_at,
            prior=prior,
        )
        picks = picks_of(run)
        late = [] if cap is None else [q for q in picks if _chat_time(q, delay.td) >= cap]
        scored = [q for q in picks if q not in late]
        score = score_stream(scored, truth, hours=hours, rates=cfg.rates, after_broadcast=len(late))
        detail: dict[str, Any] = {
            "detector_version": version,
            "label": run_label(version, p),
            "unhashed": unhashed(p),
            "delay": {"seconds": delay.seconds, "source": delay.source.value},
            "events_known": data.events_known,
            "viewers_known": data.viewers_known,
            "prior": None if prior is None else round(prior.activity, 2),
            "timeline_uncertain_s": data.uncertain_s,
            "stream": {
                "started_at": data.stream.started_at,
                "ended_at": data.stream.ended_at,
                "broadcast_end": data.broadcast_end,
                "broadcast_end_from": data.broadcast_end_source,
                "seconds_fed": len(data.seconds),
            },
            "chat": {
                "from": data.chat_span.start,
                "to": data.chat_span.end,  # the chat rows' end, before the broadcast cap
                "missing": [[m.start, m.end] for m in missing],  # the scorer's runs
                "coverage": [[iv.start, iv.end] for iv in coverage],
            },
            "episodes": len(run.episodes),
            "folded": len(run.folded),
        }
        if not cfg.grid:
            detail["candidate_list"] = candidate_details(picks, truth, late)
            detail["truth_clips"] = truth_details(scored, truth, data.clips)
        done.append((values, run_label(version, p), run, score, detail))
    elapsed = time.perf_counter() - started
    out = [
        Replayed(
            values,
            label,
            run,
            StreamResult(
                data.stream.id,
                data.channel.login,
                data.stream.source,
                score,
                detail={**detail, "elapsed_s": round(elapsed, 2)},
                warnings=warnings,
            ),
        )
        for values, label, run, score, detail in done
    ]
    for w in warnings:
        log.warning("backtest stream warning", stream_id=stream_id, warning=w)
    log.info(
        "backtest stream",
        stream_id=stream_id,
        channel=data.channel.login,
        hours=round(hours, 2),
        combinations=len(out),
        seconds=len(data.seconds),
        messages=len(messages),
        elapsed_s=round(elapsed, 2),
    )
    return out


def _chat_time(p: Pick, delay: timedelta) -> datetime:
    return p.chat_peak if p.chat_peak is not None else p.peak + delay


def covered_span(data: StreamData, delay: timedelta) -> tuple[Interval | None, datetime | None]:
    """The chat span cut at the broadcast's end in chat time (None when unknown): offline chat
    logged into a stream ended late is no coverage."""
    span = data.chat_span
    if span is None or data.broadcast_end is None:
        return span, None
    cap = data.broadcast_end + delay
    if cap >= span.end:
        return span, cap
    return (Interval(span.start, cap) if cap > span.start else None), cap


def stream_warnings(
    data: StreamData, coverage: Sequence[Interval], delay: timedelta
) -> tuple[str, ...]:
    out = []
    if data.uncertain_s is not None:
        out.append(
            f"timeline uncertain: vods.flags.timeline_uncertain_s = {data.uncertain_s:g} s"
            " added to every clip's slack"
        )
    ends = [c.end for c in data.clips if c.end is not None]
    if coverage and ends:
        past = coverage[-1].end - (max(ends) + delay)
        if past > PAST_LAST_CLIP:
            out.append(
                f"chat coverage runs {past.total_seconds() / 60:.0f} min past the last viewer"
                " clip (offline chat? check chat.coverage)"
            )
    return tuple(out)


def _first_holding(clips: Sequence[ClipRow], peak: datetime) -> str | None:
    return next((c.clip_id for c in clips if c.holds(peak)), None)


def _status(p: Pick) -> str:
    if p.emitted:
        return "emitted"
    return "deduped" if p.reason == DEDUPE else "suppressed"


def candidate_details(
    picks: Sequence[Pick], truth: Truth, late: Sequence[Pick] = ()
) -> list[dict[str, Any]]:
    rank = {id(p): i + 1 for i, p in enumerate(ranked(q for q in picks if q not in late))}
    return [
        {
            "peak_utc": p.peak,
            "chat_peak_utc": p.chat_peak,
            "score": round(p.score, 3),
            "status": _status(p),
            "reason": p.reason,
            "moment_type": p.moment_type,
            "rank": rank.get(id(p)),
            "after_broadcast": p in late,
            "truth_clip": _first_holding(truth.with_span, p.peak),
            "clip": _first_holding(truth.any_clips, p.peak),
        }
        for p in picks
    ]


def truth_details(
    picks: Sequence[Pick], truth: Truth, clips: Sequence[ClipRow]
) -> list[dict[str, Any]]:
    """Every truth clip (scored or excluded) with the best rank of a candidate inside it."""
    order = ranked(picks)
    chosen = {c.clip_id for c in truth.clips} | set(truth.excluded_ids)
    out = []
    for c in clips:
        if c.clip_id not in chosen:
            continue
        best = None
        if c.spanned:
            best = next((i + 1 for i, p in enumerate(order) if c.holds(p.peak)), None)
        out.append(
            {
                "clip_id": c.clip_id,
                "views": c.views,
                "start_utc": c.start,
                "end_utc": c.end,
                "slack_s": c.slack.total_seconds(),
                "excluded": truth.excluded_ids.get(c.clip_id),
                "best_rank": best,
            }
        )
    return out


# --------------------------------------------------------------------------- the command


# --------------------------------------------------------------------------- drafts export

DRAFTS_FORMAT = "clipper-detector-drafts"
DRAFTS_VERSION = 1


def draft_id(d: CandidateDraft) -> str:
    """Stable across exports of the same stream: the stream and the peak (whole seconds)."""
    return f"d{d.stream_id}-{int(ensure_utc(d.peak_utc).timestamp())}"


def draft_json(d: CandidateDraft) -> dict[str, Any]:
    return {
        "id": draft_id(d),
        "stream_id": d.stream_id,
        "channel_id": d.channel_id,
        "source": str(d.source),
        "status": str(d.status),
        "suppress_reason": None if d.suppress_reason is None else str(d.suppress_reason),
        "trigger": d.trigger,
        "peak_utc": d.peak_utc,
        "chat_peak_utc": d.chat_peak_utc,
        "chat_delay_s": d.chat_delay_s,
        "delay_source": str(d.delay_source),
        "moment_from": d.moment_from,
        "moment_to": d.moment_to,
        "window_from": d.window_from,
        "window_to": d.window_to,
        "score": d.score,
        "moment_type": str(d.moment_type),
        "features": d.features,
        "detector_version": d.detector_version,
    }


async def replay_base(
    sessions: async_sessionmaker[AsyncSession], stream_id: int, cfg: Config, *, now: datetime
) -> StreamRun:
    """The base params over one stream, without scoring (viewer clips not needed)."""
    p = cfg.base
    async with sessions() as session:
        data = await load_stream(session, stream_id, now=now, params=[p])
        if data.chat_span is None:
            log.info("drafts: no chat rows", stream_id=stream_id)
            return StreamRun()
        lexicon = cfg.lexicon.for_channel(data.channel.login)
        prior = await prior_for(session, data, p)
        episodes, _, _ = run_seconds(
            data.seconds, p, started_at=data.stream.started_at, prior=prior
        )
        wins = [windows(ep, p) for ep in episodes]
        messages, events = await stage2_rows(
            session, data, [w.messages for w in wins], [w.events for w in wins]
        )
    return run_stream(
        data.seconds,
        messages,
        events,
        params=p,
        lexicon=lexicon,
        stream_id=data.stream.id,
        channel_id=data.channel.id,
        delay=resolve_delay(data.channel.chat_delay_s, data.learned, p),
        detector_version=detector_version(p, cfg.lexicon),
        started_at=data.stream.started_at,
        prior=prior,
    )


async def export_drafts(
    args: argparse.Namespace,
    sessions: async_sessionmaker[AsyncSession],
    cfg: Config,
    *,
    out: IO[str] | None = None,
    now: datetime | None = None,
) -> int:
    out = out or sys.stdout
    now = now or utc_now()
    async with sessions() as session:
        ids = await stream_ids(session, args)
    drafts: list[dict[str, Any]] = []
    for sid in ids:
        run = await replay_base(sessions, sid, cfg, now=now)
        emitted = len(run.emitted)
        drafts += [draft_json(d) for d in run.drafts]
        print(
            f"stream {sid}: {len(run.drafts)} drafts ({emitted} emitted, "
            f"{len(run.drafts) - emitted} suppressed), {len(run.episodes)} episodes",
            file=out,
        )
    doc = {
        "format": DRAFTS_FORMAT,
        "version": DRAFTS_VERSION,
        "params": cfg.params_source,
        "detector_version": detector_version(cfg.base, cfg.lexicon),
        "streams": ids,
        "drafts": drafts,
    }
    args.export_drafts.parent.mkdir(parents=True, exist_ok=True)
    args.export_drafts.write_text(dumps(doc) + "\n", encoding="utf-8")
    print(f"{len(drafts)} drafts written to {args.export_drafts}", file=out)
    return 0


async def run_backtest(
    args: argparse.Namespace,
    sessions: async_sessionmaker[AsyncSession],
    cfg: Config,
    *,
    out: IO[str] | None = None,
    now: datetime | None = None,
) -> int:
    out = out or sys.stdout
    now = now or utc_now()
    async with sessions() as session:
        ids = await stream_ids(session, args)
    per_stream = [await backtest_stream(sessions, sid, cfg, now=now) for sid in ids]
    # by combination: [combination][stream]
    by_combo = [[s[i] for s in per_stream] for i in range(len(cfg.combos))]

    if cfg.grid:
        rows = []
        for entries in by_combo:
            scored = [e.result.score for e in entries if e.result.score is not None]
            rows.append((entries[0].values, entries[0].version, totals(scored, cfg.rates)))
        if args.json:
            out.write(dumps(grid_json(cfg, by_combo)) + "\n")
        else:
            print(header(cfg, by_combo[0][0].version, len(ids)), file=out)
            print("\n".join(grid_table(rows, cfg.keys, cfg.rates, cfg.main)), file=out)
        return 0

    (entries,) = by_combo
    results = [e.result for e in entries]
    if args.json:
        scored = [r.score for r in results if r.score is not None]
        doc = {
            **meta(cfg, entries[0].version),
            "streams": [result_json(r) for r in results],
            "total": score_json(totals(scored, cfg.rates)) if scored else None,
        }
        out.write(dumps(doc) + "\n")
    else:
        print(header(cfg, entries[0].version, len(ids)), file=out)
        print("\n".join(stream_table(results, cfg.rates, cfg.main)), file=out)
    return 0


def meta(cfg: Config, label: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "params": cfg.params_source,
        "lexicon": cfg.lexicon.tag,
        "k_per_hour": cfg.main,
        "rates": [rate_label(r) for r in cfg.rates],
        "truth": "top 10 by views + all >= p75 of the stream's clips",
    }
    if label is not None:
        out["detector_version"] = label.split("+", 1)[0]
        out["label"] = label  # + the unhashed params off their defaults
    return out


def grid_json(cfg: Config, by_combo: Sequence[Sequence[Replayed]]) -> dict[str, Any]:
    combos = []
    for entries in by_combo:
        scored = [e.result.score for e in entries if e.result.score is not None]
        combos.append(
            {
                "params": entries[0].values,
                "detector_version": entries[0].version.split("+", 1)[0],
                "label": entries[0].version,
                "total": score_json(totals(scored, cfg.rates)) if scored else None,
                "streams": [result_json(e.result) for e in entries],
            }
        )
    key = rate_label(cfg.main)

    def order(c: dict[str, Any]) -> tuple[float, float]:
        at = (c["total"] or {}).get("at", {}).get(key, {})
        recall, hit = at.get("recall"), at.get("hit_rate")
        return (-(recall if recall is not None else -1.0), -(hit or 0.0))

    return {**meta(cfg), "grid_keys": cfg.keys, "grid": sorted(combos, key=order)}


def header(cfg: Config, version: str, streams: int) -> str:
    what = f"{len(cfg.combos)} combinations" if cfg.grid else f"detector {version}"
    return (
        f"backtest: {streams} stream(s), {what}; params: {cfg.params_source};"
        f" lexicon {cfg.lexicon.tag}\n"
        f"k = ceil({rate_label(cfg.main)} x chat hours); truth = top 10 by views + all >= p75;"
        " hit = peak (video time) in a clip's span +- slack"
    )


async def amain(args: argparse.Namespace) -> int:
    settings = Settings(service_name="detector-backtest")
    setup_logging(settings, stream=sys.stderr)  # stdout carries only the report
    try:
        cfg = configure(args)
    except UsageError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    engine = create_engine(settings, connect_args=READ_ONLY)
    try:
        if getattr(args, "export_drafts", None):
            return await export_drafts(args, session_factory(engine), cfg)
        return await run_backtest(args, session_factory(engine), cfg)
    except UsageError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()
