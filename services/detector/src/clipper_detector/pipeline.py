"""Scorer -> stage 2 -> merge over in-memory rows of one stream (tests, backtest, offline replay).

The live service (build item 3) runs the same steps with database reads in between; this module
is the reference it must match. Candidate ids here are local: the position of the draft in
`StreamRun.drafts` (the database assigns real ones).
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from clipper_core.contracts import CandidateSource
from clipper_detector.lexicon import ChannelLexicon
from clipper_detector.merge import (
    CandidateDraft,
    Delay,
    FoldIn,
    PriorCandidate,
    plan_candidate,
)
from clipper_detector.params import DetectorParams
from clipper_detector.scorer import (
    ChannelScorer,
    Episode,
    MissingChat,
    PriorBaseline,
    Second,
    Suspect,
)
from clipper_detector.stage2 import ChatEventLike, ChatMessageLike, evaluate, windows


@dataclass(slots=True)
class StreamRun:
    drafts: list[CandidateDraft] = field(default_factory=list)
    folded: list[FoldIn] = field(default_factory=list)
    episodes: list[Episode] = field(default_factory=list)
    suspects: list[Suspect] = field(default_factory=list)
    missing: list[MissingChat] = field(default_factory=list)

    @property
    def emitted(self) -> list[CandidateDraft]:
        return [d for d in self.drafts if d.emitted]

    @property
    def suppressed(self) -> list[CandidateDraft]:
        return [d for d in self.drafts if not d.emitted]


class _Slicer:
    """Rows sorted by `sent_at`, sliced by time range."""

    def __init__(self, rows: Iterable) -> None:
        self.rows = sorted(rows, key=lambda r: r.sent_at)
        self.keys = [r.sent_at for r in self.rows]

    def between(self, start: datetime, end: datetime) -> list:
        return self.rows[bisect_left(self.keys, start) : bisect_left(self.keys, end)]


def run_stream(
    seconds: Iterable[Second],
    messages: Iterable[ChatMessageLike],
    events: Iterable[ChatEventLike] | None,
    *,
    params: DetectorParams,
    lexicon: ChannelLexicon,
    stream_id: int,
    channel_id: int,
    delay: Delay,
    detector_version: str,
    started_at: datetime | None = None,
    prior: PriorBaseline | None = None,
    source: CandidateSource = CandidateSource.LIVE,
) -> StreamRun:
    """All decisions for one stream's rows. `events=None`: events unknown (backfill)."""
    scorer = ChannelScorer(params, started_at=started_at, prior=prior)
    msgs = _Slicer(messages)
    evs = None if events is None else _Slicer(events)
    out = StreamRun()
    recent: list[PriorCandidate] = []

    def handle(batch: list) -> None:
        for ev in batch:
            if isinstance(ev, Suspect):
                out.suspects.append(ev)
            elif isinstance(ev, MissingChat):
                out.missing.append(ev)
            else:
                out.episodes.append(ev)
                _decide(ev)

    def _decide(episode: Episode) -> None:
        win = windows(episode, params)
        result = evaluate(
            episode,
            msgs.between(win.messages.start, win.messages.end),
            None if evs is None else evs.between(win.events.start, win.events.end),
            lexicon=lexicon,
            params=params,
        )
        decision = plan_candidate(
            episode,
            result,
            stream_id=stream_id,
            channel_id=channel_id,
            delay=delay,
            recent=recent,
            params=params,
            detector_version=detector_version,
            source=source,
        )
        if isinstance(decision, FoldIn):
            out.folded.append(decision)
            target = out.drafts[decision.into.candidate_id]
            target.features.setdefault("followups", []).append(decision.followup)
            recent[recent.index(decision.into)] = decision.extended  # cooldown from the last close
            return
        recent.append(decision.as_prior(len(out.drafts)))
        out.drafts.append(decision)

    for second in seconds:
        handle(scorer.step(second))
    handle(scorer.flush())
    return out


def run_seconds(
    seconds: Iterable[Second],
    params: DetectorParams,
    *,
    started_at: datetime | None = None,
    prior: PriorBaseline | None = None,
) -> tuple[list[Episode], list[Suspect], list[MissingChat]]:
    """Stage 1 only (scorer tests, backtest grids)."""
    scorer = ChannelScorer(params, started_at=started_at, prior=prior)
    episodes: list[Episode] = []
    suspects: list[Suspect] = []
    missing: list[MissingChat] = []
    for batch in (*(scorer.step(s) for s in seconds), scorer.flush()):
        for ev in batch:
            if isinstance(ev, Episode):
                episodes.append(ev)
            elif isinstance(ev, Suspect):
                suspects.append(ev)
            else:
                missing.append(ev)
    return episodes, suspects, missing
