"""Database reads of the judge (DESIGN §19): candidates, streams, clips, one round's inputs, the
call cache and the usage guard's counts. Anyone may read; the judge writes only `clips`,
`clip_plans`, `judge_calls` and its own `media_requests` (`clipper_judge.writes`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from clipper_core import transcripts
from clipper_core.chat import BotFilter, load_bots
from clipper_core.contracts import RecordingMode
from clipper_core.timeline import Interval, ensure_utc, merge
from clipper_judge.inputs import JudgeInputs, PriorRound, bridge_jitter, media_ranges
from clipper_judge.inputs import words_interval_for as _words_interval_for
from clipper_judge.params import JudgeParams

# §19 step 1: a clip needs video over [moment_from - 30 s, moment_to + 30 s] (the resolver's
# `no_video` rule uses the same reach)
NEAR_MOMENT = timedelta(seconds=30)


class ObjectStore(Protocol):
    """The part of `clipper_core.storage.ObjectStore` the judge uses (tests: in memory)."""

    def get_bytes(self, key: str) -> bytes: ...

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None: ...


# --------------------------------------------------------------------------- rows


@dataclass(frozen=True, slots=True)
class CandidateInfo:
    """A `candidates` row (fits `inputs.CandidateLike`)."""

    id: int
    stream_id: int
    channel_id: int
    source: str
    status: str
    trigger: str
    peak_utc: datetime
    chat_delay_s: float
    delay_source: str
    moment_from: datetime
    moment_to: datetime
    window_from: datetime
    window_to: datetime
    score: float
    moment_type: str
    features: dict[str, Any] = field(default_factory=dict)

    @property
    def moment(self) -> Interval:
        return Interval(self.moment_from, self.moment_to)


@dataclass(frozen=True, slots=True)
class StreamInfo:
    id: int
    channel_id: int
    login: str
    recording_mode: str  # the channel's (contracts.RecordingMode)
    source: str
    title: str | None
    category: str | None
    started_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True, slots=True)
class ClipRow:
    id: int
    candidate_id: int
    stream_id: int
    channel_id: int
    status: str
    reject_reason: str | None
    round: int
    anchor_utc: datetime
    context_from: datetime
    context_to: datetime
    priority: int
    waiting_since: datetime
    plan_version: int | None
    flags: Mapping[str, Any]

    @property
    def context(self) -> Interval:
        return Interval(self.context_from, self.context_to)

    @property
    def flag_names(self) -> tuple[str, ...]:
        return tuple(sorted(k for k, v in (self.flags or {}).items() if v))


CLIP_COLUMNS = (
    "id, candidate_id, stream_id, channel_id, status, reject_reason, round, anchor_utc,"
    " context_from, context_to, priority, waiting_since, plan_version, flags"
)


def clip_row(row: Any) -> ClipRow:
    return ClipRow(**{k: getattr(row, k) for k in ClipRow.__dataclass_fields__})


async def candidate(session: AsyncSession, candidate_id: int) -> CandidateInfo | None:
    row = (
        await session.execute(
            text(
                "SELECT id, stream_id, channel_id, source, status, trigger, peak_utc,"
                " chat_delay_s, delay_source, moment_from, moment_to, window_from, window_to,"
                " score, moment_type, features FROM candidates WHERE id = :id"
            ),
            {"id": candidate_id},
        )
    ).first()
    if row is None:
        return None
    return CandidateInfo(**{k: getattr(row, k) for k in CandidateInfo.__dataclass_fields__})


async def stream(session: AsyncSession, stream_id: int) -> StreamInfo:
    row = (
        await session.execute(
            text(
                "SELECT s.id, s.channel_id, c.login, c.recording_mode, s.source, s.title,"
                " s.category, s.started_at, s.ended_at"
                " FROM streams s JOIN channels c ON c.id = s.channel_id WHERE s.id = :id"
            ),
            {"id": stream_id},
        )
    ).one()
    return StreamInfo(**{k: getattr(row, k) for k in StreamInfo.__dataclass_fields__})


async def clip_for_update(
    session: AsyncSession, *, clip_id: int | None = None, candidate_id: int | None = None
) -> ClipRow | None:
    """The clip row, locked for this transaction (by id or by candidate)."""
    where = "id = :v" if clip_id is not None else "candidate_id = :v"
    row = (
        await session.execute(
            text(f"SELECT {CLIP_COLUMNS} FROM clips WHERE {where} FOR UPDATE"),
            {"v": clip_id if clip_id is not None else candidate_id},
        )
    ).first()
    return None if row is None else clip_row(row)


async def job_key(session: AsyncSession, job_id: int) -> str | None:
    return (
        await session.execute(text("SELECT dedupe_key FROM jobs WHERE id = :id"), {"id": job_id})
    ).scalar_one_or_none()


# --------------------------------------------------------------------------- media


async def chunk_rows(session: AsyncSession, stream_id: int, interval: Interval) -> Sequence[Any]:
    return (
        await session.execute(
            text(
                "SELECT start_utc, end_utc, video_key, audio_key FROM chunks"
                " WHERE stream_id = :s AND start_utc < :b AND end_utc > :a ORDER BY start_utc"
            ),
            {"s": stream_id, "a": interval.start, "b": interval.end},
        )
    ).all()


async def media_seconds(
    session: AsyncSession, stream_id: int, interval: Interval, *, video: bool = True
) -> float:
    """Seconds of `interval` with video (or audio) chunks, jitter holes bridged."""
    ranges = bridge_jitter(
        media_ranges(await chunk_rows(session, stream_id, interval), video=video)
    )
    parts = (r.intersection(interval) for r in ranges)
    return sum(p.seconds for p in merge(p for p in parts if p is not None))


async def no_video(
    session: AsyncSession,
    cand: CandidateInfo,
    stream_info: StreamInfo,
    *,
    now: datetime,
    min_s: float,
) -> bool:
    """§19 step 1: less than `min_s` (CLIP_MIN) of video over `[moment_from - 30 s, moment_to +
    30 s]` and none to come: every chunk of that range is in (`coverage().unrecorded` empty), or
    the channel doesn't record video (`audio_only`/`off`), so later chunks won't have any."""
    near = cand.moment.expand(NEAR_MOMENT, NEAR_MOMENT)
    if await media_seconds(session, stream_info.id, near) >= min_s:
        return False
    cov = await transcripts.coverage(session, stream_info.id, near, now=now)
    return not cov.unrecorded or stream_info.recording_mode != RecordingMode.FULL


# --------------------------------------------------------------------------- one round


async def prior_rounds(session: AsyncSession, clip_id: int, before_round: int) -> list[PriorRound]:
    rows = (
        await session.execute(
            text(
                "SELECT round, context_request FROM clip_plans"
                " WHERE clip_id = :c AND verdict = 'need_context' AND NOT shadow AND round < :r"
                " ORDER BY round, version"
            ),
            {"c": clip_id, "r": before_round},
        )
    ).all()
    out: dict[int, PriorRound] = {}
    for r in rows:
        req = r.context_request or {}
        out[r.round] = PriorRound(
            r.round,
            before_s=float(req.get("before_s", 0.0)),
            after_s=float(req.get("after_s", 0.0)),
            why=str(req.get("why") or ""),
        )
    return [out[k] for k in sorted(out)]


async def load_inputs(
    session: AsyncSession,
    store: ObjectStore,
    clip: ClipRow,
    cand: CandidateInfo,
    stream_info: StreamInfo,
    *,
    params: JudgeParams,
    now: datetime,
    sfx_tags: Sequence[str] = (),
    audio_only: bool = False,
    bots: BotFilter | None = None,
) -> JudgeInputs:
    """Everything one round of `clip` reads (`inputs.JudgeInputs`): words over the context + 5 s
    (ownership applied), coverage, gap rows, video ranges (audio ranges with `audio_only`),
    raw chat over the context shifted by the candidate's chat delay, audio seconds, the earlier
    rounds' context requests."""
    ctx = clip.context
    words_interval = _words_interval_for(ctx)
    words = await transcripts.words(session, store, stream_info.id, words_interval)
    cov = await transcripts.coverage(session, stream_info.id, ctx, now=now)
    params_q = {"s": stream_info.id, "a": ctx.start, "b": ctx.end}
    gaps = (
        await session.execute(
            text(
                "SELECT start_utc, end_utc, reason FROM stream_gaps"
                " WHERE stream_id = :s AND start_utc < :b AND end_utc > :a ORDER BY start_utc, id"
            ),
            params_q,
        )
    ).all()
    video = media_ranges(await chunk_rows(session, stream_info.id, ctx), video=not audio_only)
    delay = timedelta(seconds=cand.chat_delay_s)
    chat = (
        await session.execute(
            text(
                "SELECT sent_at, msg_id, user_id, user_login, text, emotes, deleted"
                " FROM chat_messages WHERE channel_id = :c AND sent_at >= :a AND sent_at < :b"
                " ORDER BY sent_at, msg_id"
            ),
            {"c": stream_info.channel_id, "a": ctx.start + delay, "b": ctx.end + delay},
        )
    ).all()
    audio = (
        await session.execute(
            text(
                "SELECT ts, loudness_db, speech_ratio, classes FROM audio_metrics_1s"
                " WHERE stream_id = :s AND ts >= :a AND ts < :b ORDER BY ts"
            ),
            {"s": stream_info.id, "a": ctx.start - timedelta(seconds=1), "b": ctx.end},
        )
    ).all()
    return JudgeInputs(
        clip_id=clip.id,
        candidate=cand,
        channel=stream_info.login,
        stream_title=stream_info.title,
        stream_category=stream_info.category,
        stream_started_at=ensure_utc(stream_info.started_at),
        stream_ended_at=None if stream_info.ended_at is None else ensure_utc(stream_info.ended_at),
        context=ctx,
        round=clip.round,
        words=words,
        words_interval=words_interval,
        coverage=cov,
        gaps=gaps,
        video=video,
        chat=chat,
        audio=audio,
        prior_rounds=await prior_rounds(session, clip.id, clip.round),
        sfx_tags=tuple(sfx_tags),
        bots=bots if bots is not None else load_bots().for_channel(stream_info.login),
        flags=clip.flag_names,
    )


# --------------------------------------------------------------------------- calls, plans


@dataclass(frozen=True, slots=True)
class CallRow:
    id: int
    answer: dict[str, Any] | None
    error: str | None


async def cached_call(
    session: AsyncSession, request_hash: str, *, reusable: Sequence[str]
) -> CallRow | None:
    """The latest call with this request hash that is the request's outcome: an answer, or an
    error of a reusable kind (`calls.ANSWER_ERROR`: the model answered, but not validly).
    Provider errors and usage limits are not outcomes: the request is sent again."""
    row = (
        await session.execute(
            text(
                "SELECT id, answer, error FROM judge_calls"
                " WHERE request_hash = :h AND NOT shadow"
                "   AND (error IS NULL OR split_part(error, ':', 1) = ANY(:kinds))"
                " ORDER BY id DESC LIMIT 1"
            ),
            {"h": request_hash, "kinds": list(reusable)},
        )
    ).first()
    return None if row is None else CallRow(row.id, row.answer, row.error)


async def error_count(session: AsyncSession, request_hash: str, kind: str) -> int:
    """How many calls of this request ended with an error of `kind` (`calls.TIMEOUT`, ...)."""
    return (
        await session.execute(
            text(
                "SELECT count(*) FROM judge_calls"
                " WHERE request_hash = :h AND NOT shadow AND split_part(error, ':', 1) = :k"
            ),
            {"h": request_hash, "k": kind},
        )
    ).scalar_one()


@dataclass(frozen=True, slots=True)
class UsageCounts:
    stream_last_hour: int  # this stream's calls in the last 60 min
    today: int  # all calls since 00:00 UTC


async def usage_counts(
    session: AsyncSession, stream_id: int, *, not_counted: Sequence[str]
) -> UsageCounts:
    """Local call counts of the usage guard (non-shadow `judge_calls` rows, database time),
    except error rows of the kinds in `not_counted` (a refused call is no call)."""
    row = (
        await session.execute(
            text(
                """
                SELECT count(*) FILTER (WHERE stream_id = :s
                                          AND created_at > now() - interval '1 hour') AS hour,
                       count(*) AS today
                  FROM judge_calls
                 WHERE NOT shadow AND created_at >= date_trunc('day', now(), 'UTC')
                   AND (error IS NULL OR NOT split_part(error, ':', 1) = ANY(:kinds))
                """
            ),
            {"s": stream_id, "kinds": list(not_counted)},
        )
    ).one()
    return UsageCounts(stream_last_hour=row.hour, today=row.today)


async def stream_hour_median(session: AsyncSession, stream_id: int) -> float | None:
    """Median score of the stream's emitted candidates of the last 60 min (database time):
    over `calls_per_stream_hour`, only candidates above it are judged (§19)."""
    return (
        await session.execute(
            text(
                "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY score) FROM candidates"
                " WHERE stream_id = :s AND status = 'emitted'"
                "   AND created_at > now() - interval '1 hour'"
            ),
            {"s": stream_id},
        )
    ).scalar_one_or_none()


@dataclass(frozen=True, slots=True)
class PlanRow:
    id: int
    clip_id: int
    version: int
    round: int
    verdict: str
    plan: dict[str, Any] | None
    shadow: bool


async def plan_row(session: AsyncSession, plan_id: int) -> PlanRow | None:
    row = (
        await session.execute(
            text(
                "SELECT id, clip_id, version, round, verdict, plan, shadow FROM clip_plans"
                " WHERE id = :id"
            ),
            {"id": plan_id},
        )
    ).first()
    return (
        None
        if row is None
        else PlanRow(**{k: getattr(row, k) for k in PlanRow.__dataclass_fields__})
    )


async def plan_for_review_event(session: AsyncSession, review_event_id: int) -> int | None:
    """The plan a revision already wrote for this review event (a retried `revise_plan`)."""
    return (
        await session.execute(
            text("SELECT id FROM clip_plans WHERE review_event_id = :e ORDER BY version LIMIT 1"),
            {"e": review_event_id},
        )
    ).scalar_one_or_none()


async def waiting_count(session: AsyncSession) -> int:
    return (
        await session.execute(text("SELECT count(*) FROM clips WHERE status = 'waiting_for_media'"))
    ).scalar_one()
