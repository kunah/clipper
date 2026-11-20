"""Database reads of the live detector (it reads many tables, writes few: see `writes`)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from clipper_core.contracts import CandidateStatus, DataSource
from clipper_core.timeline import Interval
from clipper_detector.merge import PriorCandidate
from clipper_detector.params import DetectorParams
from clipper_detector.scorer import PriorBaseline


@dataclass(frozen=True, slots=True)
class ChannelInfo:
    id: int
    login: str
    platform_user_id: str | None
    chat_delay_s: float | None  # the operator's override


@dataclass(frozen=True, slots=True)
class StreamInfo:
    id: int
    channel_id: int
    started_at: datetime
    ended_at: datetime | None
    source: str  # contracts.DataSource

    def contains(self, ts: datetime) -> bool:
        return self.started_at <= ts and (self.ended_at is None or ts < self.ended_at)

    @property
    def live(self) -> bool:
        """Recorded live: chat_events and viewer_snapshots exist (a backfilled stream has
        neither; for it both are *unknown*, not empty, DESIGN §17/§18.3)."""
        return self.source == DataSource.LIVE


async def active_channels(session: AsyncSession) -> dict[int, ChannelInfo]:
    rows = (
        await session.execute(
            text(
                "SELECT id, login, platform_user_id, chat_delay_s FROM channels"
                " WHERE active ORDER BY id"
            )
        )
    ).all()
    return {r.id: ChannelInfo(r.id, r.login, r.platform_user_id, r.chat_delay_s) for r in rows}


async def streams(
    session: AsyncSession, channel_ids: Sequence[int], since: datetime
) -> dict[int, list[StreamInfo]]:
    """Streams of the channels that are open or ended after `since`, by start time."""
    rows = (
        await session.execute(
            text(
                """
                SELECT id, channel_id, started_at, ended_at, source FROM streams
                 WHERE channel_id = ANY(:ids) AND (ended_at IS NULL OR ended_at > :since)
                 ORDER BY channel_id, started_at, id
                """
            ),
            {"ids": list(channel_ids), "since": since},
        )
    ).all()
    out: dict[int, list[StreamInfo]] = defaultdict(list)
    for r in rows:
        out[r.channel_id].append(StreamInfo(r.id, r.channel_id, r.started_at, r.ended_at, r.source))
    return out


def _ranges(ranges: Mapping[int, datetime]) -> dict[str, Any]:
    """Bind values of per-key ranges: `JOIN unnest(:ids, :sinces) h(id, since)` reads each key
    from its own start (a channel retaken with a 15 min replay doesn't widen the others')."""
    ids = list(ranges)
    return {
        "ids": ids,
        "sinces": [ranges[i] for i in ids],
        "min_since": min(ranges.values()) if ranges else None,
    }


async def metrics(
    session: AsyncSession, ranges: Mapping[int, datetime], until: datetime
) -> list[Row]:
    """chat_metrics_1s of each channel in `[its since, until)` (on the PK), by channel and
    second: one statement for all channels. Of `event_counts` only the room-mode toggles
    (`toggles`) are needed: decoding the JSON of every row cost more than the scorer."""
    if not ranges:
        return []
    return list(
        (
            await session.execute(
                text(
                    """
                    SELECT m.channel_id, m.ts, m.messages, m.unique_chatters,
                           COALESCE((m.event_counts ->> 'roomstate')::int, 0) AS toggles
                      FROM unnest(CAST(:ids AS bigint[]), CAST(:sinces AS timestamptz[]))
                           AS h(channel_id, since)
                      JOIN chat_metrics_1s m
                        ON m.channel_id = h.channel_id AND m.ts >= h.since AND m.ts < :until
                     ORDER BY m.channel_id, m.ts
                    """
                ),
                {**_ranges(ranges), "until": until},
            )
        ).all()
    )


async def viewer_snapshots(
    session: AsyncSession, ranges: Mapping[int, datetime], until: datetime
) -> list[Row]:
    """viewer_snapshots of each stream in `[its since, until)`."""
    if not ranges:
        return []
    return list(
        (
            await session.execute(
                text(
                    """
                    SELECT v.stream_id, v.ts, v.viewers
                      FROM unnest(CAST(:ids AS bigint[]), CAST(:sinces AS timestamptz[]))
                           AS h(stream_id, since)
                      JOIN viewer_snapshots v
                        ON v.stream_id = h.stream_id AND v.ts >= h.since AND v.ts < :until
                     ORDER BY v.stream_id, v.ts
                    """
                ),
                {**_ranges(ranges), "until": until},
            )
        ).all()
    )


async def messages(session: AsyncSession, channel_id: int, window: Interval) -> list[Row]:
    """Raw chat of one channel (index `channel_id, sent_at`) for stage 2."""
    return list(
        (
            await session.execute(
                text(
                    """
                    SELECT sent_at, msg_id, user_id, user_login, text, emotes, deleted
                      FROM chat_messages
                     WHERE channel_id = :c AND sent_at >= :a AND sent_at < :b
                     ORDER BY sent_at, msg_id
                    """
                ),
                {"c": channel_id, "a": window.start, "b": window.end},
            )
        ).all()
    )


async def events(session: AsyncSession, channel_id: int, window: Interval) -> list[Row]:
    return list(
        (
            await session.execute(
                text(
                    """
                    SELECT sent_at, type, msg_id, user_login, data FROM chat_events
                     WHERE channel_id = :c AND sent_at >= :a AND sent_at < :b
                     ORDER BY sent_at, id
                    """
                ),
                {"c": channel_id, "a": window.start, "b": window.end},
            )
        ).all()
    )


async def raw_chat(
    session: AsyncSession, ranges: Mapping[int, datetime], until: datetime
) -> list[Row]:
    """Raw messages of each channel in `[its since, until)` (explicit signals); the constant
    lower bound lets Postgres skip older day partitions."""
    if not ranges:
        return []
    return list(
        (
            await session.execute(
                text(
                    """
                    SELECT m.channel_id, m.sent_at, m.msg_id, m.user_id, m.user_login, m.text,
                           m.deleted
                      FROM unnest(CAST(:ids AS bigint[]), CAST(:sinces AS timestamptz[]))
                           AS h(channel_id, since)
                      JOIN chat_messages m
                        ON m.channel_id = h.channel_id AND m.sent_at >= h.since
                       AND m.sent_at < :until AND m.sent_at >= :min_since
                     ORDER BY m.channel_id, m.sent_at, m.msg_id
                    """
                ),
                {**_ranges(ranges), "until": until},
            )
        ).all()
    )


async def learned_delays(
    session: AsyncSession, channel_id: int, before: datetime, limit: int
) -> list[float]:
    """Delays of the channel's valid chat_delay_estimates, newest stream first (by the stream's
    `started_at`, not `computed_at`), streams before `before`, without backfilled streams whose
    timeline is uncertain (DESIGN §18.5)."""
    rows = (
        await session.execute(
            text(
                """
                SELECT e.delay_s FROM chat_delay_estimates e
                  JOIN streams s ON s.id = e.stream_id
                  LEFT JOIN vods v ON v.stream_id = s.id
                 WHERE e.channel_id = :c AND e.valid AND e.delay_s IS NOT NULL
                   AND s.started_at < :before
                   AND NOT (s.source = 'backfill'
                            AND COALESCE(v.flags ? 'timeline_uncertain_s', false))
                 ORDER BY s.started_at DESC
                 LIMIT :n
                """
            ),
            {"c": channel_id, "before": before, "n": limit},
        )
    ).all()
    return [float(r.delay_s) for r in rows]


async def prior_baseline(
    session: AsyncSession, stream: StreamInfo, params: DetectorParams
) -> PriorBaseline | None:
    """The channel's previous stream: median activity A (seconds without a row count as 0) and
    median viewers. None without a previous stream or with fewer seconds than a baseline needs."""
    prev = (
        await session.execute(
            text(
                """
                SELECT id, started_at, ended_at FROM streams
                 WHERE channel_id = :c AND started_at < :t AND id <> :id
                 ORDER BY started_at DESC LIMIT 1
                """
            ),
            {"c": stream.channel_id, "t": stream.started_at, "id": stream.id},
        )
    ).first()
    if prev is None:
        return None
    end = min(prev.ended_at or stream.started_at, stream.started_at)
    start = max(prev.started_at, end - timedelta(hours=24))
    if (end - start).total_seconds() < params.min_baseline_values:
        return None
    w = int(params.activity_window_s) - 1  # an int: frame offsets can't be bound parameters
    row = (
        await session.execute(
            text(
                f"""
                WITH s AS (
                    SELECT g AS ts FROM generate_series(
                        CAST(:a AS timestamptz), CAST(:b AS timestamptz) - interval '1 second',
                        interval '1 second') g),
                m AS (
                    SELECT s.ts, COALESCE(c.unique_chatters, 0) AS u
                      FROM s LEFT JOIN chat_metrics_1s c ON c.channel_id = :c AND c.ts = s.ts),
                a AS (
                    SELECT sum(u) OVER (ORDER BY ts ROWS BETWEEN {w} PRECEDING AND CURRENT ROW)
                           AS a, count(*) OVER (ORDER BY ts) AS n FROM m)
                SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY a) AS a,
                       (SELECT count(*) FROM chat_metrics_1s
                         WHERE channel_id = :c AND ts >= :a AND ts < :b) AS rows,
                       (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY viewers)
                          FROM viewer_snapshots WHERE stream_id = :prev) AS viewers
                  FROM a WHERE n > {w}
                """
            ),
            {
                "a": start,
                "b": end,
                "c": stream.channel_id,
                "prev": prev.id,
            },
        )
    ).one()
    if row.a is None or not row.rows:
        return None
    return PriorBaseline(float(row.a), None if row.viewers is None else float(row.viewers))


@dataclass(frozen=True, slots=True)
class CandidateRef:
    id: int
    status: CandidateStatus
    peak_utc: datetime
    prior: PriorCandidate  # for cooldown and budget (chat time)


def _iso(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def candidate_ref(row: Row) -> CandidateRef:
    """A `candidates` row as cooldown/budget/dedupe input: its chat span = the episode's onset
    to its end, extended by its fold-ins (`followups`), whose peaks it carries too."""
    delay = timedelta(seconds=row.chat_delay_s)
    chat_peak = row.chat_peak_utc or row.peak_utc + delay
    features = row.features or {}
    chat = features.get("chat") or {}
    followups = features.get("followups") or ()
    ends = [e for e in (_iso(chat.get("end")), *(_iso(f.get("end")) for f in followups)) if e]
    chat_end = max([row.moment_to + delay, *ends])
    chat_start = _iso(chat.get("onset")) or row.moment_from + delay
    peaks = tuple(p for p in (_iso(f.get("peak")) for f in followups) if p)
    status = CandidateStatus(row.status)
    prior = PriorCandidate(
        row.id, status, chat_peak, chat_end, float(row.score), row.trigger, chat_start, peaks
    )
    return CandidateRef(row.id, status, row.peak_utc, prior)


CANDIDATE_COLUMNS = (
    "id, status, trigger, peak_utc, chat_peak_utc, chat_delay_s, moment_from, moment_to, score,"
    " features"
)


async def stream_candidates(session: AsyncSession, stream_id: int) -> list[CandidateRef]:
    rows = (
        await session.execute(
            text(
                f"SELECT {CANDIDATE_COLUMNS} FROM candidates WHERE stream_id = :s"
                " ORDER BY peak_utc, id"
            ),
            {"s": stream_id},
        )
    ).all()
    return [candidate_ref(r) for r in rows]


async def nearest_candidate(
    session: AsyncSession, stream_id: int, peak_utc: datetime, within_s: float
) -> CandidateRef | None:
    """The stream's candidate (any source and version) with `peak_utc` nearest to the given one,
    within +- `within_s`."""
    row = (
        await session.execute(
            text(
                f"""
                SELECT {CANDIDATE_COLUMNS} FROM candidates
                 WHERE stream_id = :s AND peak_utc BETWEEN :a AND :b
                 ORDER BY abs(extract(epoch FROM peak_utc - CAST(:p AS timestamptz))), id
                 LIMIT 1
                """
            ),
            {
                "s": stream_id,
                "p": peak_utc,
                "a": peak_utc - timedelta(seconds=within_s),
                "b": peak_utc + timedelta(seconds=within_s),
            },
        )
    ).first()
    return None if row is None else candidate_ref(row)
