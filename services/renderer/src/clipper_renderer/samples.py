"""A hand-made render for tests and goldens (DESIGN §20): three chunks around T with a 0.3 s jitter
hole at T and a 3.7 s gap at T + 60 s, a transcript and a plan with a teaser, two segments (the
first over the jitter hole, the second over the gap), emphasis and a word fix. Every value is
fixed: the golden files are built from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from clipper_core.contracts import EditPlan
from clipper_renderer.timemap import ChunkVideo

T = datetime(2026, 11, 20, 20, 15, 42, 600_000, tzinfo=UTC)
WORD_S = 0.3
WORD_GAP_S = 0.05


def at(rel_s: float) -> datetime:
    return T + timedelta(seconds=rel_s)


@dataclass(frozen=True, slots=True)
class SampleWord:
    text: str
    start_utc: datetime
    end_utc: datetime


SCRIPT = [
    (-9.0, "okay okay listen to me"),
    (-6.2, "I have a plan, trust me."),
    (-3.0, "we jump right here and"),
    (-0.6, "NO WAY that actually worked!"),  # over the jitter hole at T .. T + 0.3 s
    (2.4, "chat did you see that"),
    (4.5, "unbelievable {honestly}"),  # braces must not reach the ASS as override blocks
    (54.0, "alright one more try"),
    (59.8, "AGAIN"),  # crosses the gap at T + 60 s: dropped
    (64.0, "what just happened 😂"),  # the emoji isn't in the bundled fonts: left out
]


def sample_words() -> list[SampleWord]:
    out = []
    for start, sentence in SCRIPT:
        t = start
        for word in sentence.split():
            out.append(SampleWord(word, at(round(t, 3)), at(round(t + WORD_S, 3))))
            t += WORD_S + WORD_GAP_S
    return out


def sample_chunks() -> list[ChunkVideo]:
    return [
        ChunkVideo(101, 10, at(-60), at(0), "clips/7/pinned/000010.ts"),
        ChunkVideo(102, 11, at(0.3), at(60), "clips/7/pinned/000011.ts"),
        ChunkVideo(103, 12, at(63.7), at(120), "clips/7/pinned/000012.ts"),
    ]


def plan_data(**kw: Any) -> dict[str, Any]:
    return {
        "clip_id": 7,
        "stream_id": 3,
        "anchor_utc": T,
        "segments": [
            {"start_utc": at(-8), "end_utc": at(6)},
            {"start_utc": at(55), "end_utc": at(66)},
        ],
        "hook": {"mode": "teaser", "start_utc": at(-1.5), "end_utc": at(1.0)},
        "title": "he really thought that would work",
        "hashtags": ["jumpking"],
        "caption_style": "bold_pop",
        "emphasis": [
            {"start_utc": at(-0.25), "text": "WAY", "kind": "big"},
            {"start_utc": at(4.5), "text": "unbelievable", "kind": "highlight"},
        ],
        "word_fixes": [{"start_utc": at(4.5), "text": "unbelievable!!"}],
        "zooms": [
            {"start_utc": at(-1), "end_utc": at(1.5), "scale": 1.3, "target": "face"},
            {"start_utc": at(58), "end_utc": at(61), "scale": 1.2, "target": "gameplay"},
        ],
        "sfx": [
            {"at_utc": at(-0.25), "tag": "vine_boom", "gain_db": -3},
            {"at_utc": at(64), "tag": "record_scratch"},
        ],
        "music": {"mood": "funny"},
        "thumbnail_utc": at(0.5),
        "moment_type": "funny",
        "confidence": 0.86,
        "reasons": ["the jump finally works", "chat KEKW x40"],
        "model": "fake",
        "prompt_version": "v1-00000000",
    } | kw


def sample_plan(**kw: Any) -> EditPlan:
    return EditPlan.model_validate(plan_data(**kw))
