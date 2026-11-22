"""`clipper-judge eval`: the judge's pipeline (build_request -> model -> resolver) over labelled
candidates, for prompt and model work (DESIGN §19, build item 6d).

    uv run clipper-judge eval --labels labels.toml                     # the labelled candidates
    uv run clipper-judge eval --stream 803 --labels labels.toml --report eval.json --csv eval.csv
    uv run clipper-judge eval --fixtures data/eval/ --labels labels.toml
    uv run clipper-judge eval --labels labels.toml --model claude-cli --yes   # the user's plan

**Read-only.** From the database: SELECTs on a connection whose every transaction is READ ONLY
(`-c default_transaction_read_only=on`) and object-storage reads (the words files); no clips,
plans, calls, jobs or media requests, and no S3 records. Output: a summary on stdout (logs go to
stderr), `--report` (JSON: summary + every case) and `--csv` (a row per case) on the local disk,
`--save-fixtures DIR` (each database case's round-0 inputs as a fixture file, `evalset`).

**Cases.** `--candidate`/`--stream`/`--channel` (emitted candidates, by peak) or the labels file's
ids; `--fixtures` files or directories instead of the database. A case runs as the service
judges a clip, without waiting: round 0 = the candidate's window; a `no_video` clip is rejected
without a call (unless `judge_audio_only`); a context whose transcript isn't complete is
`not_ready` (no call), or with `--partial` judged as the service does at its hard deadline (flag
`partial_context`; nothing of the core transcribed: `no_media`). Each round gets the resolver's
one retry (invalid answer or unrepairable plan, with the error stated); `need_context` grows the
context from the database (up to `max_rounds`; a fixture has no more media: the case ends
`need_context`). A model error ends the case (`error`); a usage limit (or extra usage), a
permanent error or the adapter switching itself off (auth) stops the run, the rest is `not_run`.

**Summary.** Outcomes (`clip`, `reject`, `rejected:no_video`, `need_context`, `failed`, `error`,
`not_ready`, `no_media`, `not_run`); agreement with the labels over the decided cases (confusion
table, clip precision/recall, IoU with a labelled cut); repairs per planned clip (and per code);
latency p50/p95 per call and per case; usage per case (tokens in/cached/out, the cost estimate)
and the plan's utilization deltas between consecutive readings of one window (CLI only).

`--model claude-cli` uses the user's Claude plan and needs `--yes`; it runs the startup auth
check first. The default model is `fake`. Exit 2: invalid arguments, labels or fixtures.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_core import transcripts
from clipper_core.config import Settings
from clipper_core.contracts import CLIP_MIN, CandidateStatus, EditPlan
from clipper_core.db import create_engine, session_factory
from clipper_core.logs import get_logger, setup_logging
from clipper_core.storage import ObjectStore
from clipper_core.timeline import Interval, ensure_utc, utc_now
from clipper_judge import reads
from clipper_judge.adapters import check_model, make_model
from clipper_judge.answer import AnswerError, parse_answer
from clipper_judge.evalset import (
    EvalSetError,
    Label,
    case_to_json,
    load_fixtures,
    load_labels,
    write_fixture,
)
from clipper_judge.inputs import JudgeInputs, PriorRound
from clipper_judge.model import (
    JudgeModel,
    ModelError,
    ModelUnavailableError,
    PermanentModelError,
    RateLimitReading,
    Usage,
    UsageLimitError,
)
from clipper_judge.params import JudgeParams
from clipper_judge.prompt import (
    JudgeRequest,
    build_request,
    load_prompt,
    request_hash,
    retry_request,
)
from clipper_judge.reads import CandidateInfo, ClipRow, StreamInfo
from clipper_judge.resolver import NeedsContext, Planned, Rejected, Unrepairable, resolve
from clipper_judge.settings import CLAUDE_CLI, MODELS, JudgeSettings
from clipper_judge.waiter import core_interval

log = get_logger(__name__)

READ_ONLY = {"options": "-c default_transaction_read_only=on"}  # every transaction READ ONLY
ANSWERED = ("answer:", "unrepairable:")  # call errors after an answer (the latency counts)


class UsageError(ValueError):
    pass


class _ReadOnlyStore:
    """Object storage for the eval: reads only."""

    def __init__(self, store: reads.ObjectStore) -> None:
        self._store = store

    def get_bytes(self, key: str) -> bytes:
        return self._store.get_bytes(key)

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        raise RuntimeError("clipper-judge eval is read-only")


# --------------------------------------------------------------------------- arguments


def _day(value: str) -> datetime:
    try:
        day = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date (YYYY-MM-DD): {value!r}") from None
    return ensure_utc(day if day.tzinfo else day.replace(tzinfo=UTC))


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    e = sub.add_parser(
        "eval",
        help="read-only: the judge over labelled candidates (agreement, repairs, latency, usage)",
        description="Run the judge (build_request -> model -> resolver) over candidates from the "
        "database or fixture files and compare with labels (DESIGN 19). Read-only.",
    )
    which = e.add_mutually_exclusive_group()
    which.add_argument("--candidate", type=int, nargs="+", action="extend", metavar="ID")
    which.add_argument("--stream", type=int, nargs="+", action="extend", metavar="ID")
    which.add_argument("--channel", metavar="LOGIN", help="the channel's streams (by start)")
    which.add_argument(
        "--fixtures", type=Path, nargs="+", action="extend", metavar="PATH",
        help="fixture files or directories (*.json) instead of the database",
    )  # fmt: skip
    e.add_argument("--since", type=_day, metavar="YYYY-MM-DD", help="--channel: started since")
    e.add_argument("--until", type=_day, metavar="YYYY-MM-DD", help="--channel: started before")
    e.add_argument("--labels", type=Path, metavar="FILE", help="labels (TOML or JSON)")
    e.add_argument("--limit", type=int, metavar="N", help="at most N cases")
    e.add_argument("--model", choices=MODELS, default="fake", help="the adapter (default fake)")
    e.add_argument("--yes", action="store_true", help="allow --model claude-cli (your plan)")
    e.add_argument("--claude-model", metavar="ID", help="claude-cli: another full model id")
    e.add_argument(
        "--effort", choices=("low", "medium", "high", "xhigh", "max"), help="claude-cli: --effort"
    )
    e.add_argument("--prompt", metavar="NAME", help="prompt dir (default CLIPPER_JUDGE_PROMPT)")
    e.add_argument(
        "--partial", action="store_true",
        help="judge contexts not fully transcribed (as the service at its hard deadline)",
    )  # fmt: skip
    e.add_argument("--report", type=Path, metavar="FILE", help="write the JSON report")
    e.add_argument("--csv", type=Path, metavar="FILE", help="write one CSV row per case")
    e.add_argument(
        "--save-fixtures", type=Path, metavar="DIR",
        help="write each database case's round-0 inputs as a fixture file",
    )  # fmt: skip
    return e


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="clipper-judge")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = add_parser(sub)
    args = p.parse_args(["eval", *argv])
    check_args(e, args)
    return args


def check_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if (args.since or args.until) and not args.channel:
        parser.error("--since/--until go with --channel")
    if not (args.candidate or args.stream or args.channel or args.fixtures or args.labels):
        parser.error("choose the cases: --candidate, --stream, --channel, --fixtures or --labels")
    if args.model == CLAUDE_CLI and not args.yes:
        parser.error("--model claude-cli calls Claude on your plan (it counts against its limits): "
                     "pass --yes")  # fmt: skip
    if (args.claude_model or args.effort) and args.model != CLAUDE_CLI:
        parser.error("--claude-model/--effort go with --model claude-cli")
    if args.save_fixtures and args.fixtures:
        parser.error("--save-fixtures writes database cases: not with --fixtures")
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be > 0")


def judge_settings(args: argparse.Namespace, base: JudgeSettings) -> JudgeSettings:
    update: dict[str, Any] = {"model": args.model}
    if args.claude_model:
        update["claude_model"] = args.claude_model
    if args.effort:
        update["claude_effort"] = args.effort
    if args.prompt:
        update["prompt"] = args.prompt
    # validated again (the model id rule, ...)
    return JudgeSettings.model_validate({**base.model_dump(), **update})


# --------------------------------------------------------------------------- results


@dataclass
class CallRecord:
    round: int
    attempt: int
    final: bool
    request_hash: str
    latency_s: float = 0.0
    usage: Usage = field(default_factory=Usage)
    cost_usd: float | None = None
    request_id: str | None = None
    reading: RateLimitReading | None = None
    error: str | None = None  # the model's error, or why the answer couldn't be used
    answer: Any = None  # the decoded answer (JSON) when there was one

    def as_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "attempt": self.attempt,
            "final": self.final,
            "request_hash": self.request_hash,
            "latency_s": round(self.latency_s, 3),
            "input_tokens": self.usage.input_tokens,
            "cached_input_tokens": self.usage.cached_input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cost_usd": self.cost_usd,
            "request_id": self.request_id,
            "reading": None if self.reading is None else self.reading.as_dict(),
            "error": self.error,
            "answer": self.answer,
        }


@dataclass
class CaseResult:
    id: str
    candidate_id: int | None
    label: Label | None
    outcome: str = "not_run"
    rounds: int = 0
    plan: dict[str, Any] | None = None
    repairs: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    confidence: float | None = None
    error: str | None = None
    note: str | None = None
    calls: list[CallRecord] = field(default_factory=list)

    @property
    def verdict(self) -> str | None:
        if self.outcome == "clip":
            return "clip"
        if self.outcome == "reject" or self.outcome.startswith("rejected:"):
            return "reject"
        return None

    @property
    def agree(self) -> bool | None:
        if self.label is None or self.verdict is None:
            return None
        return self.verdict == self.label.verdict

    @property
    def latency_s(self) -> float:
        return sum(c.latency_s for c in self.calls)

    def tokens(self) -> tuple[int, int, int]:
        return (
            sum(c.usage.input_tokens for c in self.calls),
            sum(c.usage.cached_input_tokens for c in self.calls),
            sum(c.usage.output_tokens for c in self.calls),
        )

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd or 0.0 for c in self.calls)

    @property
    def iou(self) -> float | None:
        if self.label is None or self.label.in_s is None or self.plan is None:
            return None
        a, b = self.label.in_s, self.label.out_s
        assert b is not None
        inter = sum(max(0.0, min(b, y) - max(a, x)) for x, y in self.plan["segments"])
        union = (b - a) + self.plan["duration_s"] - inter
        return inter / union if union > 0 else None

    def as_dict(self) -> dict[str, Any]:
        tin, tcached, tout = self.tokens()
        return {
            "id": self.id,
            "candidate_id": self.candidate_id,
            "label": None if self.label is None else self.label.as_dict(),
            "outcome": self.outcome,
            "verdict": self.verdict,
            "agree": self.agree,
            "iou": self.iou,
            "rounds": self.rounds,
            "plan": self.plan,
            "repairs": self.repairs,
            "reasons": self.reasons,
            "confidence": self.confidence,
            "error": self.error,
            "note": self.note,
            "latency_s": round(self.latency_s, 3),
            "input_tokens": tin,
            "cached_input_tokens": tcached,
            "output_tokens": tout,
            "cost_usd": self.cost_usd,
            "calls": [c.as_dict() for c in self.calls],
        }


def plan_summary(plan: EditPlan) -> dict[str, Any]:
    t = plan.anchor_utc

    def rel(dt: datetime) -> float:
        return round((dt - t).total_seconds(), 3)

    return {
        "in_s": rel(plan.in_utc),
        "out_s": rel(plan.out_utc),
        "duration_s": round(plan.duration.total_seconds(), 3),
        "segments": [[rel(s.start_utc), rel(s.end_utc)] for s in plan.segments],
        "title": plan.title,
        "hashtags": list(plan.hashtags),
        "hook": plan.hook.mode.value,
        "flags": list(plan.flags),
        "repairs": list(plan.repairs),
    }


# --------------------------------------------------------------------------- cases


@dataclass(frozen=True, slots=True)
class Skip:
    """No call for this round: the outcome and why."""

    outcome: str
    note: str = ""


# (context, round, prior rounds) -> the round's inputs, or why there are none
Loader = Callable[[Interval, int, tuple[PriorRound, ...]], Awaitable["JudgeInputs | Skip"]]


@dataclass
class Case:
    id: str
    candidate_id: int | None
    label: Label | None
    first: JudgeInputs | Skip | None = None  # fixture: the inputs (or the loader's first round)
    context: Interval | None = None  # database: round 0's context (the candidate window)
    loader: Loader | None = None  # database: loads any round


class StopEval(Exception):
    """A usage limit or a permanent error: the rest of the cases don't run."""


class Evaluator:
    def __init__(
        self,
        model: JudgeModel,
        params: JudgeParams,
        *,
        prompt: str,
    ) -> None:
        self.model = model
        self.params = params
        self.prompt = prompt

    async def run(self, cases: Sequence[Case]) -> tuple[list[CaseResult], str | None]:
        """Every case in order; the reason the run stopped early, if it did."""
        results: list[CaseResult] = []
        stopped: str | None = None
        for i, case in enumerate(cases):
            result = CaseResult(case.id, case.candidate_id, case.label)
            results.append(result)
            if stopped is not None:
                continue
            try:
                await self.run_case(case, result)
            except StopEval as exc:
                stopped = str(exc)
                log.error("eval stopped", case=case.id, reason=stopped)
            log.info(
                "eval case",
                n=i + 1,
                of=len(cases),
                case=case.id,
                outcome=result.outcome,
                calls=len(result.calls),
            )
        return results, stopped

    async def run_case(self, case: Case, result: CaseResult) -> None:
        if case.loader is not None:
            assert case.context is not None
            got = await case.loader(case.context, 0, ())
        else:
            got = case.first
        if got is None:
            result.outcome, result.note = "error", "no inputs"
            return
        prior: list[PriorRound] = []
        while True:
            if isinstance(got, Skip):
                result.outcome, result.note = got.outcome, got.note or None
                return
            inputs = got
            result.rounds = inputs.round + 1
            res = await self.round(inputs, result)
            if res is None:  # the round failed (recorded)
                return
            result.repairs = list(res.repairs)
            result.reasons = list(getattr(res, "reasons", ()) or ())
            if isinstance(res, Planned):
                result.outcome = "clip"
                result.plan = plan_summary(res.plan)
                result.reasons = list(res.plan.reasons)
                result.confidence = res.plan.confidence
                return
            if isinstance(res, Rejected):
                result.outcome = "reject" if res.reason == "model" else f"rejected:{res.reason}"
                result.confidence = res.confidence
                return
            assert isinstance(res, NeedsContext)
            result.confidence = res.confidence
            prior.append(PriorRound(inputs.round, res.before_s, res.after_s, res.why))
            if case.loader is None:
                result.outcome = "need_context"
                result.note = "a fixture has no more media"
                return
            got = await case.loader(res.context, inputs.round + 1, tuple(prior))
            if isinstance(got, Skip):
                result.outcome = "need_context"
                result.note = f"round {inputs.round + 1}: {got.outcome} {got.note}".strip()
                return

    async def round(self, inputs: JudgeInputs, result: CaseResult):
        """One round as `rounds.JudgeHandler._round` judges it: the request, the resolver's one
        retry (invalid answer or unrepairable, with the error stated). None: the case failed."""
        request = build_request(inputs, self.params, prompt=self.prompt)
        error: str | None = None
        retry_final = request.final
        for attempt in range(2):
            if attempt:
                request = retry_request(
                    build_request(inputs, self.params, prompt=self.prompt, final=retry_final),
                    error or "",
                )
            call = await self.call(request, inputs.round, attempt, result)
            if call is None:
                return None
            try:
                answer = parse_answer(call, final=request.final)
            except AnswerError as exc:
                error, retry_final = str(exc), request.final
                result.calls[-1].error = f"answer: {error}"
                continue
            res = resolve(
                answer,
                inputs,
                self.params,
                model=self.model.name,
                prompt_version=request.prompt_version,
            )
            if isinstance(res, Unrepairable):
                error, retry_final = res.error, request.final or res.final
                result.calls[-1].error = f"unrepairable: {error}"
                continue
            return res
        result.outcome = "failed"
        result.error = f"no usable answer after one retry: {error}"
        return None

    async def call(
        self, request: JudgeRequest, round_: int, attempt: int, result: CaseResult
    ) -> str | None:
        h = request_hash(self.model.name, request, self.model.options)
        record = CallRecord(round_, attempt, request.final, h)
        result.calls.append(record)
        started = time.monotonic()
        try:
            response = await self.model.judge(request)
        except ModelError as exc:
            record.latency_s = exc.latency_s or (time.monotonic() - started)
            record.request_id = exc.request_id
            record.reading = exc.rate_limit
            record.error = f"{type(exc).__name__}: {exc}"
            result.outcome, result.error = "error", record.error
            if isinstance(exc, UsageLimitError | PermanentModelError | ModelUnavailableError):
                raise StopEval(record.error) from exc
            return None
        record.latency_s = response.latency_s
        record.usage = response.usage
        record.cost_usd = response.cost_usd
        record.request_id = response.request_id
        record.reading = response.rate_limit
        try:
            record.answer = json.loads(response.text)
        except ValueError:
            record.answer = response.text[:2000]
        return response.text


# --------------------------------------------------------------------------- database cases


async def select_candidates(
    session: AsyncSession, args: argparse.Namespace, labels: Mapping[str, Label]
) -> list[int]:
    if args.candidate:
        ids = list(dict.fromkeys(args.candidate))
    elif args.stream or args.channel:
        if args.stream:
            where, values = "stream_id = ANY(:streams)", {"streams": list(args.stream)}
        else:
            where = (
                "stream_id IN (SELECT s.id FROM streams s JOIN channels c ON c.id = s.channel_id"
                " WHERE lower(c.login) = lower(:login)"
                " AND (CAST(:since AS timestamptz) IS NULL OR s.started_at >= :since)"
                " AND (CAST(:until AS timestamptz) IS NULL OR s.started_at < :until))"
            )
            values = {"login": args.channel, "since": args.since, "until": args.until}
        rows = (
            await session.execute(
                text(
                    f"SELECT id FROM candidates WHERE {where} AND status = :st"
                    " ORDER BY peak_utc, id"
                ),
                {**values, "st": CandidateStatus.EMITTED.value},
            )
        ).all()
        ids = [r.id for r in rows]
        if not ids:
            raise UsageError("no emitted candidates there")
    else:
        try:
            ids = sorted({int(k) for k in labels})
        except ValueError:
            raise UsageError("database cases: the labels' ids must be candidate ids") from None
        if not ids:
            raise UsageError("the labels file has no cases")
    found = {
        r.id
        for r in (
            await session.execute(
                text("SELECT id FROM candidates WHERE id = ANY(:ids)"), {"ids": ids}
            )
        ).all()
    }
    missing = [i for i in ids if i not in found]
    if missing:
        raise UsageError(f"unknown candidate id(s): {', '.join(map(str, missing))}")
    return ids[: args.limit] if args.limit else ids


class DbLoader:
    """A database case's rounds, as the service would read them (read-only)."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        store: reads.ObjectStore,
        settings: JudgeSettings,
        params: JudgeParams,
        cand: CandidateInfo,
        stream_info: StreamInfo,
        *,
        now: datetime,
        partial: bool,
    ) -> None:
        self.sessions = sessions
        self.store = store
        self.settings = settings
        self.params = params
        self.cand = cand
        self.stream_info = stream_info
        self.now = now
        self.partial = partial

    async def __call__(
        self, context: Interval, round_: int, prior: tuple[PriorRound, ...]
    ) -> JudgeInputs | Skip:
        cand, s_info = self.cand, self.stream_info
        async with self.sessions() as s:
            if (
                round_ == 0
                and not self.settings.judge_audio_only
                and await reads.no_video(
                    s, cand, s_info, now=self.now, min_s=CLIP_MIN.total_seconds()
                )
            ):
                return Skip("rejected:no_video", "no video near the moment (no call)")
            cov = await transcripts.coverage(s, s_info.id, context, now=self.now)
            partial = False
            if not cov.complete:
                pending = sum(iv.seconds for iv in cov.pending)
                if not self.partial:
                    return Skip("not_ready", f"{pending:.0f} s of the context not transcribed")
                core = core_interval(cand.moment_from, cand.moment_to, context, self.params)
                core_cov = await transcripts.coverage(s, s_info.id, core, now=self.now)
                if not core_cov.transcribed:
                    return Skip("no_media", "nothing of the core transcribed")
                partial = True
            clip = ClipRow(
                id=0,  # no clip row: nothing of a real clip (its earlier rounds) is read
                candidate_id=cand.id,
                stream_id=s_info.id,
                channel_id=s_info.channel_id,
                status="judging",
                reject_reason=None,
                round=round_,
                anchor_utc=cand.peak_utc,
                context_from=context.start,
                context_to=context.end,
                priority=0,
                waiting_since=self.now,
                plan_version=None,
                flags={"partial_context": True} if partial else {},
            )
            inputs = await reads.load_inputs(
                s,
                self.store,
                clip,
                cand,
                s_info,
                params=self.params,
                now=self.now,
                sfx_tags=self.settings.sfx_tags,
                audio_only=self.settings.judge_audio_only,
            )
        return replace(inputs, prior_rounds=prior)


async def db_cases(
    sessions: async_sessionmaker[AsyncSession],
    store: reads.ObjectStore,
    args: argparse.Namespace,
    labels: Mapping[str, Label],
    settings: JudgeSettings,
    params: JudgeParams,
    *,
    now: datetime,
) -> list[Case]:
    async with sessions() as s:
        ids = await select_candidates(s, args, labels)
        cases = []
        for cid in ids:
            cand = await reads.candidate(s, cid)
            assert cand is not None
            stream_info = await reads.stream(s, cand.stream_id)
            loader = DbLoader(
                sessions, store, settings, params, cand, stream_info, now=now, partial=args.partial
            )
            cases.append(
                Case(
                    str(cid),
                    cid,
                    labels.get(str(cid)),
                    context=Interval(ensure_utc(cand.window_from), ensure_utc(cand.window_to)),
                    loader=loader,
                )
            )
    return cases


async def save_fixtures(cases: Sequence[Case], directory: Path) -> int:
    n = 0
    for case in cases:
        assert case.loader is not None and case.context is not None
        got = await case.loader(case.context, 0, ())
        if isinstance(got, Skip):
            log.info("no fixture: no inputs", case=case.id, outcome=got.outcome)
            continue
        write_fixture(
            directory / f"{case.id}.json",
            case_to_json(case.id, got, label=case.label, meta={"source": "database"}),
        )
        n += 1
    return n


# --------------------------------------------------------------------------- summary


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear interpolation between the closest ranks (q in 0..1)."""
    if not values:
        return None
    xs = sorted(values)
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _r(x: float | None, nd: int = 3) -> float | None:
    return None if x is None else round(x, nd)


def utilization(calls: Sequence[CallRecord]) -> dict[str, Any]:
    """Per window: first and last utilization seen, and the sum of the increases between
    consecutive readings of the same window (same `resets_at`), per call pair."""
    out: dict[str, Any] = {}
    for name in ("five_hour", "seven_day"):
        seq = []
        for c in calls:
            w = None if c.reading is None else getattr(c.reading, name)
            if w is not None and w.utilization is not None:
                seq.append((w.utilization, w.resets_at))
        if not seq:
            out[name] = None
            continue
        delta, pairs = 0.0, 0
        for (u0, r0), (u1, r1) in itertools.pairwise(seq):
            if r0 == r1:
                delta += u1 - u0
                pairs += 1
        out[name] = {
            "first": seq[0][0],
            "last": seq[-1][0],
            "readings": len(seq),
            "delta": _r(delta, 4) if pairs else None,
            "per_call": _r(delta / pairs, 5) if pairs else None,
        }
    return out


def summarize(results: Sequence[CaseResult]) -> dict[str, Any]:
    outcomes = Counter(r.outcome for r in results)
    labelled = [r for r in results if r.label is not None]
    decided = [r for r in labelled if r.verdict is not None]
    agree = sum(1 for r in decided if r.agree)
    confusion: dict[str, dict[str, int]] = {}
    for r in labelled:
        row = confusion.setdefault(r.label.verdict, {})  # type: ignore[union-attr]
        row[r.outcome] = row.get(r.outcome, 0) + 1
    tp = sum(1 for r in decided if r.label.verdict == "clip" and r.verdict == "clip")  # type: ignore[union-attr]
    fp = sum(1 for r in decided if r.label.verdict == "reject" and r.verdict == "clip")  # type: ignore[union-attr]
    fn = sum(1 for r in decided if r.label.verdict == "clip" and r.verdict == "reject")  # type: ignore[union-attr]
    ious = [x for r in results if (x := r.iou) is not None]
    planned = [r for r in results if r.outcome == "clip"]
    repair_codes = Counter(code for r in planned for code in r.repairs)
    calls = [c for r in results for c in r.calls]
    called = [r for r in results if r.calls]
    answered = [c for c in calls if c.error is None or c.error.startswith(ANSWERED)]
    call_lat = [c.latency_s for c in answered]
    case_lat = [r.latency_s for r in called]
    tokens = [r.tokens() for r in called]
    n = len(called)
    return {
        "cases": len(results),
        "outcomes": dict(sorted(outcomes.items())),
        "agreement": {
            "labelled": len(labelled),
            "decided": len(decided),
            "agree": agree,
            "rate": _r(agree / len(decided)) if decided else None,
            "clip_precision": _r(tp / (tp + fp)) if tp + fp else None,
            "clip_recall": _r(tp / (tp + fn)) if tp + fn else None,
            "confusion": confusion,  # label -> outcome -> cases
            "cut_iou_mean": _r(sum(ious) / len(ious)) if ious else None,
            "cut_iou_cases": len(ious),
        },
        "repairs": {
            "planned": len(planned),
            "per_clip": _r(sum(len(r.repairs) for r in planned) / len(planned))
            if planned
            else None,
            "codes": dict(repair_codes.most_common()),
        },
        "latency_s": {
            "call_p50": _r(percentile(call_lat, 0.5)),
            "call_p95": _r(percentile(call_lat, 0.95)),
            "case_p50": _r(percentile(case_lat, 0.5)),
            "case_p95": _r(percentile(case_lat, 0.95)),
        },
        "usage": {
            "calls": len(calls),
            "cases_called": n,
            "calls_per_case": _r(len(calls) / n) if n else None,
            "input_tokens_per_case": _r(sum(t[0] for t in tokens) / n, 1) if n else None,
            "cached_input_tokens_per_case": _r(sum(t[1] for t in tokens) / n, 1) if n else None,
            "output_tokens_per_case": _r(sum(t[2] for t in tokens) / n, 1) if n else None,
            "cost_usd_total": _r(sum(r.cost_usd for r in called), 4),
            "cost_usd_per_case": _r(sum(r.cost_usd for r in called) / n, 4) if n else None,
            "utilization": utilization(calls),
        },
    }


def summary_text(meta: Mapping[str, Any], s: Mapping[str, Any]) -> str:
    a, rep, lat, use = s["agreement"], s["repairs"], s["latency_s"], s["usage"]
    lines = [
        f"judge eval: {s['cases']} case(s), model {meta['model']} {meta['options']}, "
        f"prompt {meta['prompt_version']}, source {meta['source']}",
        "outcomes: " + ", ".join(f"{k} {v}" for k, v in s["outcomes"].items()),
        f"agreement: {a['agree']}/{a['decided']} decided of {a['labelled']} labelled"
        f" (rate {a['rate']}, clip precision {a['clip_precision']}, recall {a['clip_recall']},"
        f" cut IoU {a['cut_iou_mean']} over {a['cut_iou_cases']})",
        f"repairs: {rep['per_clip']} per planned clip ({rep['planned']} planned)"
        + (
            ": " + ", ".join(f"{k} {v}" for k, v in list(rep["codes"].items())[:8])
            if rep["codes"]
            else ""
        ),
        f"latency: call p50 {lat['call_p50']} s, p95 {lat['call_p95']} s; case p50"
        f" {lat['case_p50']} s, p95 {lat['case_p95']} s",
        f"usage per case: {use['calls_per_case']} calls, in {use['input_tokens_per_case']},"
        f" cached {use['cached_input_tokens_per_case']}, out {use['output_tokens_per_case']}"
        f" tokens; cost estimate {use['cost_usd_total']} USD total",
    ]
    for name, u in use["utilization"].items():
        if u is not None:
            lines.append(
                f"utilization {name}: {u['first']} -> {u['last']} ({u['readings']} readings),"
                f" +{u['delta']} between them, {u['per_call']} per call"
            )
    if meta.get("stopped"):
        lines.append(f"STOPPED EARLY: {meta['stopped']}")
    return "\n".join(lines)


CSV_COLUMNS = (
    "id", "candidate_id", "label", "outcome", "verdict", "agree", "iou", "rounds", "calls",
    "in_s", "out_s", "duration_s", "title", "repairs", "confidence", "latency_s",
    "input_tokens", "cached_input_tokens", "output_tokens", "cost_usd", "error", "note",
)  # fmt: skip


def write_csv(path: Path, results: Sequence[CaseResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for r in results:
            tin, tcached, tout = r.tokens()
            plan = r.plan or {}
            w.writerow(
                [
                    r.id, r.candidate_id, None if r.label is None else r.label.verdict,
                    r.outcome, r.verdict, r.agree, _r(r.iou), r.rounds, len(r.calls),
                    plan.get("in_s"), plan.get("out_s"), plan.get("duration_s"),
                    plan.get("title"), ";".join(r.repairs), r.confidence,
                    round(r.latency_s, 3), tin, tcached, tout, round(r.cost_usd, 6),
                    r.error, r.note,
                ]
            )  # fmt: skip


# --------------------------------------------------------------------------- entry


async def run_eval(
    args: argparse.Namespace,
    *,
    settings: JudgeSettings,
    model: JudgeModel,
    sessions: async_sessionmaker[AsyncSession] | None = None,
    store: reads.ObjectStore | None = None,
    out: IO[str] | None = None,
    now: datetime | None = None,
) -> int:
    out = out or sys.stdout
    now = now or utc_now()
    labels = load_labels(args.labels) if args.labels else {}
    params = settings.params()
    prompt_version = load_prompt(settings.prompt).version
    if args.fixtures:
        fixtures = load_fixtures(args.fixtures)
        cases = [
            Case(f.id, f.inputs.candidate.id, labels.get(f.id, f.label), first=f.inputs)
            for f in fixtures
        ]
        if args.limit:
            cases = cases[: args.limit]
        source = "fixtures"
    else:
        assert sessions is not None and store is not None
        cases = await db_cases(sessions, store, args, labels, settings, params, now=now)
        source = "database"
        if args.save_fixtures:
            n = await save_fixtures(cases, args.save_fixtures)
            print(f"{n} fixture(s) written to {args.save_fixtures}", file=sys.stderr)
    if not await check_model(model):
        return 1
    started = utc_now()
    results, stopped = await Evaluator(model, params, prompt=settings.prompt).run(cases)
    meta = {
        "model": model.name,
        "options": dict(model.options),
        "prompt_version": prompt_version,
        "source": source,
        "started_at": started.isoformat(),
        "finished_at": utc_now().isoformat(),
        "partial": bool(getattr(args, "partial", False)),
        "params": {
            "min_clip_s": params.min_clip_s,
            "max_clip_s": params.max_clip_s,
            "max_rounds": params.max_rounds,
        },
        "stopped": stopped,
    }
    summary = summarize(results)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        report = {"meta": meta, "summary": summary, "cases": [r.as_dict() for r in results]}
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=1, default=str) + "\n",
            encoding="utf-8",
        )
    if args.csv:
        write_csv(args.csv, results)
    print(summary_text(meta, summary), file=out)
    return 1 if stopped else 0


def base_settings() -> JudgeSettings:
    """`CLIPPER_JUDGE_*` (environment, `.env`): the model's settings, prompt, params."""
    return JudgeSettings()


async def amain(args: argparse.Namespace) -> int:
    db = Settings(service_name="judge-eval")
    setup_logging(db, stream=sys.stderr)  # stdout carries only the report
    try:
        js = judge_settings(args, base_settings())
        load_prompt(js.prompt)
        model = make_model(js)
    except (ValueError, OSError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    engine = None
    sessions = store = None
    if not args.fixtures:
        engine = create_engine(db, connect_args=READ_ONLY)
        sessions = session_factory(engine)
        store = _ReadOnlyStore(ObjectStore(db))
    try:
        return await run_eval(args, settings=js, model=model, sessions=sessions, store=store)
    except (UsageError, EvalSetError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    finally:
        if engine is not None:
            await engine.dispose()
