"""Post-stream clip ideas, a read-only trial (DESIGN §15, 2026-11-26): one model call reads a whole
stream and lists the clips worth cutting from it (stories, callbacks, running bits) that the live,
one-window judge can't see.

    clipper-judge ideas --stream 1412                                # input.md only, no call
    clipper-judge ideas --stream 1412 --model claude-cli --yes       # one call on your plan
    clipper-judge ideas --stream 1412 --previews                     # + a rough cut per idea
    clipper-judge ideas --stream 1412 --requests data/labels/v1/requests.json ...  # editor notes

Read-only: the database through `default_transaction_read_only`, object storage for the words
files and the previews' chunks. Writes into `--out` (default `data/ideas/<stream>/<prompt>/`):
`input.md` (the system prompt and the document), `response.json` (the call: request sha, model,
usage, latency, the raw answer), `ideas.json` (the answer with UTC times and issues), `ideas.md`
(for reading) and, with `--previews`, `previews/NN-kind.mp4` (each idea's beats in their order,
540p, cut from the chunks; pauses cut out unless `--no-tighten`, see `tighten`). A `response.json`
for the same request (sha of the canonical request) and model is reused, no call, unless `--force`;
without `--model` an answer to an older document is shown with a warning, so `--previews` alone
works on an earlier answer.

The document (`prompts/ideas-v{N}/`): meta, the editor's requests (v2: notes from a label set,
`--requests`; instructions, outside the data fences), the live detector's emitted candidates, then a
timeline on the stream clock (h:mm:ss since `streams.started_at`, about the VOD's timestamp) over
the recorded video: one header line per minute (chat on video time through the detector's filters:
messages, users, busyness against the median minute, top emotes and repeated messages; notable
sounds, loud seconds, long music), then the minute's sentences (`transcript_segments`, not
provisional), `[h:mm:ss N s no speech]` for pauses of `pause_mark_s` or more, holes as
`[h:mm:ss→h:mm:ss not recorded]`. The prompt names no channel or game (DESIGN §15: general).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import cache
from importlib import resources
from pathlib import Path
from string import Template
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_core import transcripts
from clipper_core.chat import (
    BotFilter,
    NoBots,
    is_command,
    load_bots,
    normalised_text,
    strip_links,
    user_key,
)
from clipper_core.config import Settings
from clipper_core.contracts import SoundGroup
from clipper_core.db import create_engine, session_factory
from clipper_core.logs import get_logger, setup_logging
from clipper_core.storage import ObjectStore
from clipper_core.timeline import Interval, ensure_utc, merge, uncovered, utc_now
from clipper_judge.adapters import check_model, make_model
from clipper_judge.inputs import AudioSecondLike, ChatMessageLike, bridge_jitter, media_ranges
from clipper_judge.model import JudgeModel, JudgeResponse, ModelError
from clipper_judge.prompt import JudgeRequest
from clipper_judge.settings import CLAUDE_CLI, JudgeSettings
from clipper_judge.transcript import clean_text

log = get_logger(__name__)

PROMPT = "ideas-v2"
IDEAS_FORMAT = 2  # bump with every change to the document's line formats
SCHEMA_VERSION = 2
IDEAS_FILE_FORMAT = "clipper-ideas-v1"  # ideas.json, read by ideas_rate
KINDS = ("story", "callback", "running_bit", "moment")
ROLES = ("hook", "setup", "build", "payoff", "reaction", "context")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
CLIP_MAX_S = 180.0  # contracts.CLIP_MAX
LONG_BEAT_S = 60.0
MAX_OUTPUT_TOKENS = 32000
CHARS_PER_TOKEN = 1.95  # measured: stream 1412's document, 187k characters = 96k tokens
READ_ONLY = {"options": "-c default_transaction_read_only=on"}
_TIMES = chr(0xD7)  # multiplication sign
_ARROW = chr(0x2192)  # rightwards arrow
_SKIP_SOUNDS = frozenset({SoundGroup.SPEECH.value, SoundGroup.MUSIC.value})


class IdeasError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class IdeasParams:
    minute_s: int = 60
    top_n: int = 3  # top emotes / messages per minute
    message_chars: int = 60  # a top message is cut to this length
    min_repeats: int = 2  # a top message was sent at least this often (else one user's line)
    sound_min: float = 0.5  # a sound class this strong is notable
    max_sounds: int = 4  # notable sounds shown per minute (strongest)
    loud_db: float = 9.0  # a second this far above the stream's median loudness is notable
    music_min: float = 0.6  # a second with music this strong counts as music
    music_min_s: int = 20  # ... and this many of them in a minute are shown
    max_ideas: int = 30  # the requests' ideas + the model's own
    max_beats: int = 12
    pause_mark_s: float = 5.0  # the timeline marks pauses between sentences this long
    # previews (`tighten`): pauses longer than max_pause_s inside a beat are cut out
    max_pause_s: float = 0.6
    pad_before_s: float = 0.15  # kept before a word
    pad_after_s: float = 0.25  # kept after a word
    min_piece_s: float = 0.4  # shorter kept pieces are dropped (unless all are)


DEFAULT_PARAMS = IdeasParams()


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True, slots=True)
class SentenceRow:
    start_utc: datetime
    end_utc: datetime
    text: str


@dataclass(frozen=True, slots=True)
class LiveMoment:
    """An emitted candidate of the live detector."""

    start_utc: datetime
    end_utc: datetime
    moment_type: str
    viewer_clips: int


@dataclass(frozen=True, slots=True)
class EditorRequest:
    """A note of the human editor on a part of the stream (a labelled case): an instruction."""

    case_id: str
    around_utc: datetime  # the case's T
    note: str
    watched: Interval | None = None  # the part of the stream the editor saw
    marked: Interval | None = None  # the cut the editor marked there


@dataclass(frozen=True)
class StreamInputs:
    stream_id: int
    channel: str
    title: str | None
    category: str | None
    started_at: datetime
    ended_at: datetime | None
    recorded: Sequence[Interval]  # video ranges
    sentences: Sequence[SentenceRow]
    chat: Sequence[ChatMessageLike]
    audio: Sequence[AudioSecondLike]
    moments: Sequence[LiveMoment] = ()
    chat_delay_s: float = 0.0
    bots: BotFilter = field(default_factory=NoBots)
    requests: Sequence[EditorRequest] = ()

    def request_ids(self) -> dict[str, EditorRequest]:
        """R1, R2, ... in time order."""
        ordered = sorted(self.requests, key=lambda r: (ensure_utc(r.around_utc), r.case_id))
        return {f"R{k}": r for k, r in enumerate(ordered, start=1)}


# --------------------------------------------------------------------------- clock


def clock(seconds: float) -> str:
    s = max(0, math.floor(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


_CLOCK = re.compile(r"^\s*(?:(\d+):)?(\d+):(\d{1,2}(?:\.\d+)?)\s*$")


def parse_clock(value: str) -> float | None:
    """'h:mm:ss' (or 'm:ss', 'mm:ss.s') -> seconds; None when unreadable."""
    m = _CLOCK.match(value or "")
    if not m:
        return None
    hours, minutes, seconds = int(m[1] or 0), int(m[2]), float(m[3])
    if seconds >= 60 or (m[1] is not None and minutes >= 60):
        return None
    return hours * 3600 + minutes * 60 + seconds


# --------------------------------------------------------------------------- the document


class _Doc:
    def __init__(self, inputs: StreamInputs, params: IdeasParams) -> None:
        self.i = inputs
        self.p = params
        self.t0 = ensure_utc(inputs.started_at)
        self.delay = timedelta(seconds=inputs.chat_delay_s)
        self.recorded = bridge_jitter(merge(inputs.recorded))

    def secs(self, ts: datetime) -> float:
        return (ensure_utc(ts) - self.t0).total_seconds()

    def clk(self, ts: datetime) -> str:
        return clock(self.secs(ts))

    def span(self, a: datetime, b: datetime) -> str:
        return f"{self.clk(a)}{_ARROW}{self.clk(b)}"

    def minute(self, ts: datetime) -> int:
        return math.floor(self.secs(ts) / self.p.minute_s)

    def minute_start(self, k: int) -> datetime:
        return self.t0 + timedelta(seconds=k * self.p.minute_s)

    def is_recorded(self, ts: datetime) -> bool:
        return any(r.start <= ts < r.end for r in self.recorded)

    def same_range(self, a: datetime, b: datetime) -> bool:
        return any(r.start <= a <= r.end and r.start <= b < r.end for r in self.recorded)

    # ------------------------------------------------------------------ meta

    def meta(self) -> str:
        i = self.i
        recorded_s = sum(r.seconds for r in self.recorded)
        ended = "still live" if i.ended_at is None else f"ended {self.clk(i.ended_at)} later"
        spans = ", ".join(self.span(r.start, r.end) for r in self.recorded) or "none"
        return "\n".join(
            [
                f"channel: {clean_text(i.channel)}",
                f"stream title: {_quote(i.title) if i.title else 'none'}",
                f"category: {_quote(i.category) if i.category else 'none'}",
                f"went live: {self.t0.isoformat()} ({ended})",
                f"recorded with video: {spans} ({recorded_s / 3600:.1f} h)",
                f"chat delay: {i.chat_delay_s:.1f} s (chat below is shifted to video time)",
            ]
        )

    def requests(self) -> str:
        lines = []
        for rid, r in self.i.request_ids().items():
            seen = []
            if r.watched is not None:
                seen.append(f"watched {self.span(r.watched.start, r.watched.end)}")
            if r.marked is not None:
                seen.append(f"marked {self.span(r.marked.start, r.marked.end)}")
            where = f"{rid} around {self.clk(r.around_utc)}"
            if seen:
                where += f" ({', '.join(seen)})"
            note = _quote(r.note) if r.note.strip() else "(no note: the editor marked it a clip)"
            lines.append(f"{where}: {note}")
        return "\n".join(lines) or "(none)"

    def moments(self) -> str:
        lines = []
        for m in sorted(self.i.moments, key=lambda m: (m.start_utc, m.end_utc)):
            if not any(r.overlaps(Interval(m.start_utc, m.end_utc)) for r in self.recorded):
                continue
            clips = f", viewer clips {m.viewer_clips}" if m.viewer_clips else ""
            lines.append(f"{self.span(m.start_utc, m.end_utc)} {clean_text(m.moment_type)}{clips}")
        return "\n".join(lines) or "(none)"

    # ------------------------------------------------------------------ timeline

    def timeline(self) -> str:
        if not self.recorded:
            return "(nothing recorded)"
        chat = self.chat_by_minute()
        sounds = self.sounds_by_minute()
        tick = timedelta(microseconds=1)
        minutes = sorted(
            {
                k
                for r in self.recorded
                for k in range(self.minute(r.start), self.minute(r.end - tick) + 1)
            }
        )
        counts = [len(chat.get(k, ())) for k in minutes]
        median = statistics.median(counts) if counts else 0
        items: list[tuple[datetime, int, str]] = []
        for k in minutes:
            header = self.header(k, chat.get(k, ()), median, sounds.get(k, ()))
            items.append((self.minute_start(k), 1, header))
        for hole in self.holes():
            items.append((hole.start, 0, f"[{self.span(hole.start, hole.end)} not recorded]"))
        spoken_to: datetime | None = None
        for s in sorted(self.i.sentences, key=lambda s: (ensure_utc(s.start_utc), s.text)):
            start, end = ensure_utc(s.start_utc), ensure_utc(s.end_utc)
            line = clean_text(s.text)
            if not line or not self.is_recorded(start):
                continue
            if spoken_to is not None and self.same_range(spoken_to, start):
                pause = (start - spoken_to).total_seconds()
                if pause >= self.p.pause_mark_s:
                    marker = f"[{self.clk(spoken_to)} {pause:.0f} s no speech]"
                    items.append((spoken_to, 2, marker))
            items.append((start, 2, f"{self.clk(start)} {line}"))
            spoken_to = end if spoken_to is None else max(spoken_to, end)
        return "\n".join(line for _, _, line in sorted(items, key=lambda x: (x[0], x[1])))

    def holes(self) -> list[Interval]:
        out = []
        first = self.recorded[0]
        if first.start - self.t0 >= timedelta(seconds=self.p.minute_s):
            out.append(Interval(self.t0, first.start))
        for a, b in zip(self.recorded, self.recorded[1:], strict=False):
            out.append(Interval(a.end, b.start))
        ended = self.i.ended_at
        if ended is not None and ensure_utc(ended) - self.recorded[-1].end >= timedelta(
            seconds=self.p.minute_s
        ):
            out.append(Interval(self.recorded[-1].end, ensure_utc(ended)))
        return out

    def chat_by_minute(self) -> dict[int, list[tuple[datetime, ChatMessageLike]]]:
        out: dict[int, list[tuple[datetime, ChatMessageLike]]] = defaultdict(list)
        for m in self.i.chat:
            if m.deleted or self.i.bots.is_bot(m.user_login) or is_command(m.text):
                continue  # a giveaway's "!enter" flood is not a busy minute
            video_t = ensure_utc(m.sent_at) - self.delay
            if self.is_recorded(video_t):
                out[self.minute(video_t)].append((video_t, m))
        return out

    def header(
        self,
        k: int,
        msgs: Sequence[tuple[datetime, ChatMessageLike]],
        median: float,
        sounds: Sequence[str],
    ) -> str:
        p = self.p
        label = self.clk(self.minute_start(k))[:-3]  # h:mm
        users = {user_key(m) for _, m in msgs} - {None}
        busy = f", {_TIMES}{len(msgs) / median:.1f}" if median > 0 else ""
        parts = [f"== {label}", f"chat {len(msgs)} msgs, {len(users)} users{busy}"]
        emotes: Counter[str] = Counter()
        texts: Counter[str] = Counter()
        first: dict[str, datetime] = {}
        for video_t, m in sorted(msgs, key=lambda vm: (vm[0], vm[1].msg_id)):
            for e in m.emotes or ():
                name = clean_text(str(e.get("name", ""))) if isinstance(e, Mapping) else ""
                if name:
                    emotes[name] += max(_int(e.get("count", 1)), 1)
            t = normalised_text(strip_links(m.text))
            if t and not is_command(t) and not _emotes_only(m):
                texts[t] += 1
                first.setdefault(t, video_t)
        top_emotes = sorted(emotes.items(), key=lambda kv: (-kv[1], kv[0]))[: p.top_n]
        if top_emotes:
            parts.append("emotes: " + ", ".join(f"{n} {c}" for n, c in top_emotes))
        repeated = [(t, n) for t, n in texts.items() if n >= p.min_repeats]
        top = sorted(repeated, key=lambda kv: (-kv[1], first[kv[0]], kv[0]))[: p.top_n]
        if top:
            shown = (f"{_quote(_cut(t, p.message_chars))} {_TIMES}{n}" for t, n in top)
            parts.append("top: " + ", ".join(shown))
        parts.extend(sounds)
        return " | ".join(parts)

    def sounds_by_minute(self) -> dict[int, list[str]]:
        p = self.p
        rows: dict[datetime, AudioSecondLike] = {}
        for r in self.i.audio:
            ts = ensure_utc(r.ts)
            if self.is_recorded(ts):
                rows.setdefault(ts, r)
        if not rows:
            return {}
        median = statistics.median(r.loudness_db for r in rows.values())
        by_minute: dict[int, list[datetime]] = defaultdict(list)
        for ts in sorted(rows):
            by_minute[self.minute(ts)].append(ts)
        out: dict[int, list[str]] = {}
        for k, seconds in by_minute.items():
            runs: list[tuple[float, datetime, str]] = []  # (peak, start, group)
            open_runs: dict[str, tuple[float, datetime, datetime]] = {}
            music = 0
            loud: tuple[float, datetime] | None = None
            for ts in seconds:
                r = rows[ts]
                scores = {str(g): float(v) for g, v in (r.classes or {}).items() if _finite(v)}
                if scores.get(SoundGroup.MUSIC.value, 0.0) >= p.music_min:
                    music += 1
                excess = r.loudness_db - median
                if excess >= p.loud_db and (loud is None or excess > loud[0]):
                    loud = (excess, ts)
                for g, v in scores.items():
                    if g in _SKIP_SOUNDS or v < p.sound_min:
                        continue
                    run = open_runs.get(g)
                    if run is not None and ts - run[2] <= timedelta(seconds=1):
                        open_runs[g] = (max(run[0], v), run[1], ts)
                    else:
                        if run is not None:
                            runs.append((run[0], run[1], g))
                        open_runs[g] = (v, ts, ts)
            runs += [(peak, start, g) for g, (peak, start, _) in open_runs.items()]
            strongest = sorted(runs, key=lambda x: (-x[0], x[1], x[2]))[: p.max_sounds]
            parts = []
            if strongest:
                shown = sorted(strongest, key=lambda x: (x[1], x[2]))
                parts.append(
                    "sounds: "
                    + ", ".join(f"{clean_text(g)} {self.clk(s)} {v:.2f}" for v, s, g in shown)
                )
            if loud is not None:
                parts.append(f"loud +{loud[0]:.0f} dB {self.clk(loud[1])}")
            if music >= p.music_min_s:
                parts.append(f"music {music} s")
            if parts:
                out[k] = parts
        return out

    def task(self) -> str:
        n = len(self.i.requests)
        first = (
            f"Answer the editor's {n} request{'s' if n != 1 else ''} first (one idea each, in "
            "their order), then add your own ideas"
            if n
            else "List your ideas"
        )
        return (
            f"Read the whole timeline. {first}, as the system prompt describes. Answer with the "
            "JSON object only."
        )


# --------------------------------------------------------------------------- prompt, request


def ideas_schema() -> dict[str, Any]:
    """Lenient on purpose (no length or count limits): `parse_ideas` repairs and reports."""
    beat = {
        "type": "object",
        "additionalProperties": False,
        "required": ["role", "from", "to", "what", "keep_whole"],
        "properties": {
            "role": {"type": "string", "enum": list(ROLES)},
            "from": {"type": "string", "description": "stream clock, h:mm:ss"},
            "to": {"type": "string", "description": "stream clock, h:mm:ss"},
            "what": {"type": "string"},
            "keep_whole": {
                "type": "boolean",
                "description": "its point happens without words: keep its silence",
            },
        },
    }
    idea = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "request",
            "kind",
            "angle",
            "why",
            "beats",
            "title",
            "hook_text",
            "needs_visual",
            "visual_check",
            "censor",
            "confidence",
        ],
        "properties": {
            "request": {"type": "string", "description": 'the request id (R1, ...) or ""'},
            "kind": {"type": "string", "enum": list(KINDS)},
            "angle": {"type": "string"},
            "why": {"type": "string"},
            "beats": {"type": "array", "items": beat},
            "title": {"type": "string"},
            "hook_text": {"type": "string", "description": "<= 40 characters, or empty"},
            "needs_visual": {"type": "boolean"},
            "visual_check": {"type": "string", "description": 'what the frames must show, or ""'},
            "censor": {"type": "string", "description": "what to blur, or empty"},
            "confidence": {"type": "number"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "ideas"],
        "properties": {
            "summary": {"type": "string"},
            "ideas": {"type": "array", "items": idea},
        },
    }


@dataclass(frozen=True, slots=True)
class IdeasPrompt:
    name: str
    version: str  # "ideas-v1-<sha8>"
    system: str
    user: Template


@cache
def load_ideas_prompt(name: str = PROMPT) -> IdeasPrompt:
    base = resources.files("clipper_judge").joinpath("prompts", name)
    system = base.joinpath("system.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    user = base.joinpath("user.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    digest = hashlib.sha256()
    for part in (system, user, str(IDEAS_FORMAT), str(SCHEMA_VERSION), _canonical(ideas_schema())):
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return IdeasPrompt(name, f"{name}-{digest.hexdigest()[:8]}", system, Template(user))


def build_ideas_request(
    inputs: StreamInputs, params: IdeasParams = DEFAULT_PARAMS, *, prompt: str = PROMPT
) -> JudgeRequest:
    """Pure: the same inputs give the same bytes."""
    template = load_ideas_prompt(prompt)
    doc = _Doc(inputs, params)
    user = template.user.substitute(
        meta=doc.meta(),
        requests=doc.requests(),
        moments=doc.moments(),
        timeline=doc.timeline(),
        task=doc.task(),
        sound_min=f"{params.sound_min:g}",
        loud_db=f"{params.loud_db:g}",
        pause_s=f"{params.pause_mark_s:g}",
    )
    return JudgeRequest(
        prompt_version=template.version,
        schema_version=SCHEMA_VERSION,
        final=True,
        system=template.system,
        user=user,
        schema_name="stream_ideas",
        schema=ideas_schema(),
        max_tokens=MAX_OUTPUT_TOKENS,
    )


def request_sha(request: JudgeRequest) -> str:
    return hashlib.sha256(request.canonical().encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- the answer


@dataclass(frozen=True, slots=True)
class Beat:
    role: str
    raw_from: str
    raw_to: str
    what: str
    start_utc: datetime | None
    end_utc: datetime | None
    keep_whole: bool = False

    @property
    def seconds(self) -> float | None:
        if self.start_utc is None or self.end_utc is None or self.end_utc <= self.start_utc:
            return None
        return (self.end_utc - self.start_utc).total_seconds()

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "from": self.raw_from,
            "to": self.raw_to,
            "what": self.what,
            "keep_whole": self.keep_whole,
            "start_utc": None if self.start_utc is None else self.start_utc.isoformat(),
            "end_utc": None if self.end_utc is None else self.end_utc.isoformat(),
            "seconds": None if self.seconds is None else round(self.seconds, 1),
        }


@dataclass(frozen=True, slots=True)
class Idea:
    n: int
    request: str  # "R1"... or ""
    kind: str
    title: str
    angle: str
    why: str
    hook_text: str
    needs_visual: bool
    visual_check: str
    censor: str
    confidence: float
    beats: tuple[Beat, ...]
    issues: tuple[str, ...]

    @property
    def seconds(self) -> float:
        return sum(b.seconds or 0.0 for b in self.beats)

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "request": self.request,
            "kind": self.kind,
            "title": self.title,
            "angle": self.angle,
            "why": self.why,
            "hook_text": self.hook_text,
            "needs_visual": self.needs_visual,
            "visual_check": self.visual_check,
            "censor": self.censor,
            "confidence": self.confidence,
            "seconds": round(self.seconds, 1),
            "beats": [b.as_dict() for b in self.beats],
            "issues": list(self.issues),
        }


@dataclass(frozen=True, slots=True)
class IdeasResult:
    summary: str
    ideas: tuple[Idea, ...]
    issues: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "ideas": [i.as_dict() for i in self.ideas],
            "issues": list(self.issues),
        }


def parse_ideas(
    raw: str, inputs: StreamInputs, params: IdeasParams = DEFAULT_PARAMS
) -> IdeasResult:
    """The model's JSON text -> ideas with UTC beats; problems become issues, not errors.
    `IdeasError`: not a JSON object."""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise IdeasError(f"the answer is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise IdeasError("the answer must be one JSON object")
    t0 = ensure_utc(inputs.started_at)
    recorded = bridge_jitter(merge(inputs.recorded))
    raw_ideas = data.get("ideas") if isinstance(data.get("ideas"), list) else []
    top_issues = []
    if len(raw_ideas) > params.max_ideas:
        top_issues.append(f"{len(raw_ideas)} ideas: the first {params.max_ideas} kept")
    known = inputs.request_ids()
    ideas = []
    for n, item in enumerate(raw_ideas[: params.max_ideas], start=1):
        if not isinstance(item, dict):
            top_issues.append(f"idea {n} is not an object: left out")
            continue
        issues: list[str] = []
        beats = []
        raw_beats = item.get("beats") if isinstance(item.get("beats"), list) else []
        for j, b in enumerate(raw_beats, start=1):
            if not isinstance(b, dict):
                issues.append(f"beat {j} is not an object: left out")
                continue
            raw_from, raw_to = _str(b.get("from")), _str(b.get("to"))
            a, z = parse_clock(raw_from), parse_clock(raw_to)
            start = None if a is None else t0 + timedelta(seconds=a)
            end = None if z is None else t0 + timedelta(seconds=z)
            if start is None or end is None:
                issues.append(f"beat {j}: unreadable time {raw_from!r}-{raw_to!r}")
            elif end <= start:
                issues.append(f"beat {j}: ends before it starts")
            else:
                missing = sum(h.seconds for h in uncovered(Interval(start, end), recorded))
                if missing > 1.0:
                    issues.append(f"beat {j}: {missing:.0f} s without video")
                if (end - start).total_seconds() > LONG_BEAT_S:
                    issues.append(f"beat {j}: long ({(end - start).total_seconds():.0f} s)")
            role = _str(b.get("role"))
            what, whole = _str(b.get("what")), b.get("keep_whole") is True
            beats.append(Beat(role, raw_from, raw_to, what, start, end, whole))
        if len(beats) > params.max_beats:
            issues.append(f"{len(beats)} beats")
        if not beats:
            issues.append("no beats")
        request = _str(item.get("request"))
        if request and request not in known:
            issues.append(f"unknown request {request!r}")
        idea = Idea(
            n=n,
            request=request,
            kind=_str(item.get("kind")) or "moment",
            title=_str(item.get("title")),
            angle=_str(item.get("angle")),
            why=_str(item.get("why")),
            hook_text=_str(item.get("hook_text")),
            needs_visual=bool(item.get("needs_visual")),
            visual_check=_str(item.get("visual_check")),
            censor=_str(item.get("censor")),
            confidence=_confidence(item.get("confidence")),
            beats=tuple(beats),
            issues=(),
        )
        if idea.seconds > CLIP_MAX_S:
            issues.append(f"{idea.seconds:.0f} s in all: over {CLIP_MAX_S:.0f} s")
        if len(idea.title) > 100:
            issues.append(f"title {len(idea.title)} characters")
        if len(idea.hook_text) > 40:
            issues.append(f"hook text {len(idea.hook_text)} characters")
        ideas.append(_with_issues(idea, issues))
    answered = {i.request for i in ideas}
    missing = [rid for rid in known if rid not in answered]
    if missing:
        top_issues.append("requests without an idea: " + ", ".join(missing))
    return IdeasResult(_str(data.get("summary")), tuple(ideas), tuple(top_issues))


def _with_issues(idea: Idea, issues: Sequence[str]) -> Idea:
    return replace(idea, issues=tuple(issues))


def ideas_markdown(
    result: IdeasResult,
    inputs: StreamInputs,
    meta: Mapping[str, Any],
    previews: Mapping[int, Preview] | None = None,
) -> str:
    t0 = ensure_utc(inputs.started_at)
    previews = previews or {}
    requests = inputs.request_ids()
    lines = [
        f"# Clip ideas: {inputs.channel}, stream {inputs.stream_id} ({t0.date().isoformat()})",
        "",
        f"{meta.get('model')}, prompt {meta.get('prompt_version')}; "
        f"{meta.get('input_tokens')} tokens in ({meta.get('cached_input_tokens')} cached), "
        f"{meta.get('output_tokens')} out; {meta.get('latency_s')} s; "
        f"list price ~${_money(meta.get('cost_usd'))}.",
        "Times are the stream clock: time since the stream went live (about the VOD's timestamp).",
        "",
        "## Summary",
        "",
        result.summary or "(none)",
        "",
    ]
    if result.issues:
        lines += ["Issues: " + "; ".join(result.issues), ""]
    for idea in result.ideas:
        lines.append(
            f"## {idea.n}. {idea.title or '(no title)'}  "
            f"({idea.kind}, confidence {idea.confidence:.2f}, ~{idea.seconds:.0f} s)"
        )
        lines.append("")
        if idea.request:
            r = requests.get(idea.request)
            note = (r.note.strip() or "(no note)") if r is not None else "(unknown request)"
            case = f" (case {r.case_id})" if r is not None else ""
            lines.append(f"- **Your request {idea.request}{case}:** {note}")
        lines.append(f"- **Angle:** {idea.angle}")
        lines.append(f"- **Why:** {idea.why}")
        if idea.hook_text:
            lines.append(f"- **Hook text:** {idea.hook_text}")
        if idea.needs_visual:
            lines.append(f"- **Check the frames:** {idea.visual_check or '(not said)'}")
        if idea.censor:
            lines.append(f"- **Censor:** {idea.censor}")
        if idea.n in previews:
            pv = previews[idea.n]
            cut = (
                f", {pv.planned_s:.0f} s before pauses were cut"
                if pv.seconds < pv.planned_s - 0.5
                else ""
            )
            lines.append(f"- **Preview:** {pv.file} ({pv.seconds:.0f} s{cut})")
        lines.append("- **Beats:**")
        for j, b in enumerate(idea.beats, start=1):
            length = f" ({b.seconds:.0f} s)" if b.seconds is not None else ""
            whole = " [keep whole]" if b.keep_whole else ""
            lines.append(f"  {j}. {b.role} {b.raw_from}{_ARROW}{b.raw_to}{length}{whole}: {b.what}")
        if idea.issues:
            lines.append("- **Issues:** " + "; ".join(idea.issues))
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- reading the DB


async def load_stream(
    session: AsyncSession, stream_id: int, *, bots: BotFilter | None = None
) -> StreamInputs:
    row = (
        await session.execute(
            text(
                "SELECT s.id, s.title, s.category, s.started_at, s.ended_at, s.channel_id,"
                " c.login, c.chat_delay_s FROM streams s JOIN channels c ON c.id = s.channel_id"
                " WHERE s.id = :s"
            ),
            {"s": stream_id},
        )
    ).one_or_none()
    if row is None:
        raise IdeasError(f"no stream {stream_id}")
    params = {"s": stream_id}
    chunks = (
        await session.execute(
            text(
                "SELECT start_utc, end_utc, video_key, audio_key FROM chunks"
                " WHERE stream_id = :s ORDER BY start_utc, seq"
            ),
            params,
        )
    ).all()
    recorded = bridge_jitter(media_ranges(chunks, video=True))
    if not recorded:
        raise IdeasError(f"stream {stream_id} has no video")
    cands = (
        await session.execute(
            text(
                "SELECT moment_from, moment_to, moment_type, chat_delay_s, features FROM candidates"
                " WHERE stream_id = :s AND status = 'emitted' ORDER BY peak_utc, id"
            ),
            params,
        )
    ).all()
    moments = [
        LiveMoment(c.moment_from, c.moment_to, c.moment_type, _viewer_clips(c.features))
        for c in cands
    ]
    if row.chat_delay_s is not None:
        delay = float(row.chat_delay_s)
    elif cands:
        delay = float(statistics.median(c.chat_delay_s for c in cands))
    else:
        delay = 0.0
    sentences = [
        SentenceRow(r.start_utc, r.end_utc, r.text)
        for r in (
            await session.execute(
                text(
                    "SELECT start_utc, end_utc, text FROM transcript_segments"
                    " WHERE stream_id = :s AND NOT provisional ORDER BY start_utc, id"
                ),
                params,
            )
        ).all()
    ]
    shift = timedelta(seconds=delay)
    chat = (
        await session.execute(
            text(
                "SELECT sent_at, msg_id, user_id, user_login, text, emotes, deleted"
                " FROM chat_messages WHERE channel_id = :c AND sent_at >= :a AND sent_at < :b"
                " ORDER BY sent_at, msg_id"
            ),
            {"c": row.channel_id, "a": recorded[0].start + shift, "b": recorded[-1].end + shift},
        )
    ).all()
    audio = (
        await session.execute(
            text(
                "SELECT ts, loudness_db, speech_ratio, classes FROM audio_metrics_1s"
                " WHERE stream_id = :s ORDER BY ts"
            ),
            params,
        )
    ).all()
    return StreamInputs(
        stream_id=row.id,
        channel=row.login,
        title=row.title,
        category=row.category,
        started_at=ensure_utc(row.started_at),
        ended_at=None if row.ended_at is None else ensure_utc(row.ended_at),
        recorded=recorded,
        sentences=sentences,
        chat=chat,
        audio=audio,
        moments=moments,
        chat_delay_s=delay,
        bots=bots if bots is not None else load_bots().for_channel(row.login),
    )


def _viewer_clips(features: Any) -> int:
    if not isinstance(features, Mapping):
        return 0
    explicit = features.get("explicit")
    clips = explicit.get("viewer_clips") if isinstance(explicit, Mapping) else None
    listed = clips.get("clips") if isinstance(clips, Mapping) else None
    return len(listed) if isinstance(listed, list) else 0


# --------------------------------------------------------------------------- previews


@dataclass(frozen=True, slots=True)
class Preview:
    file: str  # relative to the output directory
    seconds: float  # after tightening
    planned_s: float  # the beats as the model gave them


def tighten(
    beat: Interval,
    speech: Iterable[Interval],
    keep: Iterable[Interval],
    params: IdeasParams = DEFAULT_PARAMS,
) -> list[Interval]:
    """The parts of a beat worth showing (pure): its words, padded, and the `keep` spans
    (notable sounds), with pauses up to `max_pause_s` bridged; longer pauses, the lead-in and the
    tail without speech or sound are cut out. Nothing to go by (no word, no sound): the whole
    beat (its point is on screen)."""
    p = params
    before, after = timedelta(seconds=p.pad_before_s), timedelta(seconds=p.pad_after_s)
    spans = []
    for w in speech:
        a, b = max(w.start - before, beat.start), min(w.end + after, beat.end)
        if b > a:
            spans.append(Interval(a, b))
    for k in keep:
        part = k.intersection(beat)
        if part is not None and part.end > part.start:
            spans.append(part)
    if not spans:
        return [beat]
    out: list[Interval] = []
    for s in merge(spans):
        if out and (s.start - out[-1].end).total_seconds() <= p.max_pause_s:
            out[-1] = Interval(out[-1].start, max(out[-1].end, s.end))
        else:
            out.append(s)
    kept = [s for s in out if s.seconds >= p.min_piece_s]
    return kept or out


def notable_seconds(inputs: StreamInputs, params: IdeasParams = DEFAULT_PARAMS) -> list[Interval]:
    """Seconds with a notable sound (a non-speech, non-music class >= sound_min, or loudness
    >= loud_db over the stream's median): kept by `tighten` even without words."""
    rows = [r for r in inputs.audio if _finite(r.loudness_db)]
    if not rows:
        return []
    median = statistics.median(r.loudness_db for r in rows)
    out = []
    for r in rows:
        scores = {str(g): float(v) for g, v in (r.classes or {}).items() if _finite(v)}
        sound = any(v >= params.sound_min for g, v in scores.items() if g not in _SKIP_SOUNDS)
        if sound or r.loudness_db - median >= params.loud_db:
            ts = ensure_utc(r.ts)
            out.append(Interval(ts, ts + timedelta(seconds=1)))
    return merge(out)


async def cut_previews(
    sessions: async_sessionmaker[AsyncSession],
    store: Any,
    result: IdeasResult,
    inputs: StreamInputs,
    out: Path,
    *,
    ffmpeg: str,
    encoder: str,
    force: bool = False,
    tight: bool = True,
    params: IdeasParams = DEFAULT_PARAMS,
) -> dict[int, Preview]:
    """`previews/NN-kind.mp4`: each idea's beats in their order (holes skipped), 540p; with
    `tight`, each beat cut down to its words and notable sounds (`tighten`), unless the model
    marked it `keep_whole`."""
    from clipper_judge.label import Cutter, chunk_videos, pick_encoder, plan_pieces

    work = _ensure_dir(out / "work")
    cutter = Cutter(store, ffmpeg, pick_encoder(ffmpeg, encoder), work)
    sounds = notable_seconds(inputs, params) if tight else []
    margin = timedelta(seconds=5)
    done: dict[int, Preview] = {}
    try:
        for idea in result.ideas:
            path = out / "previews" / f"{idea.n:02d}-{idea.kind}.mp4"
            rel = path.relative_to(out).as_posix()
            ranges: list[Interval] = []
            for b in idea.beats:
                if b.seconds is None:
                    continue
                assert b.start_utc is not None and b.end_utc is not None
                beat = Interval(b.start_utc, b.end_utc)
                if not tight or b.keep_whole:
                    ranges.append(beat)
                    continue
                async with sessions() as session:
                    words = await transcripts.words(
                        session, store, inputs.stream_id, beat.expand(margin, timedelta(0))
                    )
                speech = [
                    Interval(w.start_utc, w.end_utc) for w in words if w.end_utc > w.start_utc
                ]
                ranges += tighten(beat, speech, (s for s in sounds if s.overlaps(beat)), params)
            seconds = sum(r.seconds for r in ranges)
            if path.exists() and not force:
                done[idea.n] = Preview(rel, seconds, idea.seconds)
                continue
            pieces = []
            for r in ranges:
                async with sessions() as session:
                    chunks = await chunk_videos(session, inputs.stream_id, r)
                pieces += plan_pieces(r, chunks)
            if not pieces:
                log.warning("no video for an idea's beats", idea=idea.n)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            cutter.cut(pieces, path)
            done[idea.n] = Preview(rel, seconds, idea.seconds)
            log.info("preview cut", idea=idea.n, file=rel, pieces=len(pieces), seconds=seconds)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return done


# --------------------------------------------------------------------------- editor requests


def load_requests(path: Path, stream_id: int) -> list[EditorRequest]:
    """The editor's requests of one stream from a JSON file: {"requests": [{"case_id",
    "stream_id", "around_utc", "watched": [from, to] | null, "marked": [in, out] | null,
    "note"}]} (UTC ISO times). `IdeasError` when unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data["requests"]
        out = []
        for item in items:
            if int(item["stream_id"]) != stream_id:
                continue
            out.append(
                EditorRequest(
                    case_id=str(item["case_id"]),
                    around_utc=_utc(item["around_utc"]),
                    note=str(item.get("note") or ""),
                    watched=_interval(item.get("watched")),
                    marked=_interval(item.get("marked")),
                )
            )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise IdeasError(f"unreadable requests file {path}: {exc}") from exc
    return out


def _utc(value: Any) -> datetime:
    ts = datetime.fromisoformat(str(value))
    if ts.tzinfo is None:
        raise ValueError(f"time without a zone: {value!r}")
    return ensure_utc(ts)


def _interval(value: Any) -> Interval | None:
    if not value:
        return None
    a, b = (_utc(v) for v in value)
    return Interval(a, b) if b > a else None


# --------------------------------------------------------------------------- the command


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = sub.add_parser(
        "ideas",
        help="read-only trial: clip ideas from a whole stream (one model call)",
        description="Read a whole stream (transcript, chat, sounds, live candidates) and list "
        "post-stream clip ideas: stories, callbacks, running bits (DESIGN 15, 2026-11-26). "
        "Without --model: writes input.md only.",
    )
    p.add_argument("--stream", type=int, required=True, metavar="ID")
    p.add_argument("--out", type=Path, metavar="DIR", help="default data/ideas/<stream>/<prompt>")
    p.add_argument("--prompt", default=PROMPT, metavar="NAME", help=f"prompt dir ({PROMPT})")
    p.add_argument(
        "--requests", type=Path, metavar="FILE",
        help="the editor's requests (JSON, e.g. from a label set's notes)",
    )  # fmt: skip
    p.add_argument(
        "--model", choices=("none", CLAUDE_CLI), default="none",
        help="none (default): no call, reuse response.json if it answers this request",
    )  # fmt: skip
    p.add_argument("--yes", action="store_true", help="allow --model claude-cli (your plan)")
    p.add_argument("--claude-model", metavar="ID", help="another full model id")
    p.add_argument("--effort", choices=EFFORTS)
    p.add_argument("--timeout-s", type=float, default=900.0, metavar="S", help="per call (900)")
    p.add_argument(
        "--budget-usd", type=float, default=5.0, metavar="X",
        help="the CLI's --max-budget-usd for this call, a list-price cap (5)",
    )  # fmt: skip
    p.add_argument("--force", action="store_true", help="call again / cut previews again")
    p.add_argument("--previews", action="store_true", help="cut a rough video per idea")
    p.add_argument("--no-tighten", action="store_true", help="previews: keep the pauses")
    p.add_argument("--encoder", choices=("auto", "h264_nvenc", "libx264"), default="auto")
    p.add_argument("--ffmpeg", default="ffmpeg", metavar="PATH")
    return p


def check_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.model == CLAUDE_CLI and not args.yes:
        parser.error("--model claude-cli calls Claude on your plan (it counts against its limits): "
                     "pass --yes")  # fmt: skip
    if (args.claude_model or args.effort) and args.model != CLAUDE_CLI:
        parser.error("--claude-model/--effort go with --model claude-cli")
    if args.timeout_s <= 0 or args.budget_usd <= 0:
        parser.error("--timeout-s and --budget-usd must be > 0")


def model_settings(args: argparse.Namespace, base: JudgeSettings) -> JudgeSettings:
    update: dict[str, Any] = {
        "model": CLAUDE_CLI,
        "claude_timeout_s": args.timeout_s,
        "claude_max_budget_usd": args.budget_usd,
    }
    if args.claude_model:
        update["claude_model"] = args.claude_model
    if args.effort:
        update["claude_effort"] = args.effort
    return JudgeSettings.model_validate({**base.model_dump(), **update})


async def run_ideas(
    args: argparse.Namespace,
    *,
    sessions: async_sessionmaker[AsyncSession],
    store: Any,
    model: JudgeModel | None,
    out: Any = None,
) -> int:
    out = out or sys.stdout
    prompt = getattr(args, "prompt", None) or PROMPT
    directory = _ensure_dir(args.out or Path("data") / "ideas" / str(args.stream) / prompt)
    async with sessions() as session:
        inputs = await load_stream(session, args.stream)
    requests_file = getattr(args, "requests", None)
    if requests_file is not None:
        inputs = replace(inputs, requests=load_requests(requests_file, args.stream))
    request = build_ideas_request(inputs, prompt=prompt)
    sha = request_sha(request)
    _write(
        directory / "input.md",
        f"<!-- {request.prompt_version}, request sha {sha} -->\n\n# System prompt\n\n"
        f"{request.system}\n\n# User content\n\n{request.user}\n",
    )
    chars = len(request.system) + len(request.user)
    print(
        f"input.md: {chars} characters (~{chars / CHARS_PER_TOKEN / 1000:.0f}k tokens), "
        f"{len(inputs.sentences)} sentences, {len(inputs.chat)} chat messages, "
        f"{len(inputs.moments)} live moments, {len(inputs.requests)} editor requests",
        file=out,
    )
    response_file = directory / "response.json"
    saved = _read_json(response_file)
    answered = isinstance(saved, dict) and isinstance(saved.get("text"), str)
    stale = False
    reusable = (
        answered
        and saved.get("request_sha") == sha
        and (model is None or saved.get("model") == model.name)
    )
    if model is None and answered and not reusable:
        print(
            f"{response_file} answered an earlier version of the document (prompt or data "
            "changed): shown anyway; --model claude-cli --yes asks again",
            file=out,
        )
        reusable = stale = True
    if model is not None and (args.force or not reusable):
        if not await check_model(model):
            return 1
        print(f"calling {model.name} ...", file=out)
        try:
            response = await model.judge(request)
        except ModelError as exc:
            print(f"[error] the call failed: {exc}", file=out)
            return 1
        saved = response_record(response, request, sha, model)
        _write(response_file, json.dumps(saved, ensure_ascii=False, indent=1) + "\n")
    elif not reusable:
        print("no answer for this request yet: run with --model claude-cli --yes", file=out)
        return 0
    elif not stale:
        print(f"reusing {response_file} (same request)", file=out)
    assert isinstance(saved, dict)
    try:
        result = parse_ideas(saved["text"], inputs)
    except IdeasError as exc:
        print(f"[error] {exc} (raw answer in {response_file})", file=out)
        return 1
    previews: dict[int, Preview] = {}
    if args.previews:
        previews = await cut_previews(
            sessions,
            store,
            result,
            inputs,
            directory,
            ffmpeg=args.ffmpeg,
            encoder=args.encoder,
            force=args.force,
            tight=not getattr(args, "no_tighten", False),
        )
    _write(
        directory / "ideas.json",
        json.dumps(
            ideas_document(result, inputs, _meta(saved), previews, prompt=prompt),
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
    )
    _write(directory / "ideas.md", ideas_markdown(result, inputs, _meta(saved), previews))
    kinds = Counter(i.kind for i in result.ideas)
    issues = sum(1 for i in result.ideas if i.issues)
    print(
        f"{len(result.ideas)} ideas ({', '.join(f'{k} {n}' for k, n in sorted(kinds.items()))}), "
        f"{issues} with issues; {len(previews)} previews -> {directory / 'ideas.md'}",
        file=out,
    )
    if result.issues:
        print("issues: " + "; ".join(result.issues), file=out)
    return 0


def ideas_document(
    result: IdeasResult,
    inputs: StreamInputs,
    meta: Mapping[str, Any],
    previews: Mapping[int, Preview],
    *,
    prompt: str,
) -> dict[str, Any]:
    """`ideas.json`: the answer plus what the rating page (`ideas_rate`) shows beside it: the
    stream, the editor's requests and the previews (relative files, seconds after tightening)."""
    return {
        "format": IDEAS_FILE_FORMAT,
        "prompt": prompt,
        "meta": dict(meta),
        "stream": {
            "id": inputs.stream_id,
            "channel": inputs.channel,
            "title": inputs.title,
            "category": inputs.category,
            "started_at": ensure_utc(inputs.started_at).isoformat(),
        },
        "requests": {
            rid: {"case_id": r.case_id, "note": r.note} for rid, r in inputs.request_ids().items()
        },
        "previews": {
            str(n): {"file": pv.file, "seconds": round(pv.seconds, 1), "planned_s": pv.planned_s}
            for n, pv in sorted(previews.items())
        },
        **result.as_dict(),
    }


def response_record(
    response: JudgeResponse, request: JudgeRequest, sha: str, model: JudgeModel
) -> dict[str, Any]:
    return {
        "request_sha": sha,
        "prompt_version": request.prompt_version,
        "model": model.name,
        "options": dict(model.options),
        "at": utc_now().isoformat(),
        "input_tokens": response.usage.input_tokens,
        "cached_input_tokens": response.usage.cached_input_tokens,
        "output_tokens": response.usage.output_tokens,
        "latency_s": round(response.latency_s, 1),
        "cost_usd": response.cost_usd,
        "request_id": response.request_id,
        "extras": dict(response.extras),
        "text": response.text,
    }


def _meta(saved: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "request_sha",
        "prompt_version",
        "model",
        "options",
        "at",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "latency_s",
        "cost_usd",
    )
    return {k: saved.get(k) for k in keys}


async def amain(args: argparse.Namespace) -> int:
    db = Settings(service_name="judge-ideas")
    setup_logging(db, stream=sys.stderr)  # stdout carries only the report
    model = None
    if args.model == CLAUDE_CLI:
        try:
            model = make_model(model_settings(args, JudgeSettings()))
        except (ValueError, OSError) as exc:
            print(f"[error] {exc}", file=sys.stderr)
            return 2
    engine = create_engine(db, connect_args=READ_ONLY)
    try:
        return await run_ideas(
            args, sessions=session_factory(engine), store=ObjectStore(db), model=model
        )
    except IdeasError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- helpers


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    tmp.replace(path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _quote(text_: str) -> str:
    return json.dumps(clean_text(text_), ensure_ascii=False)


def _cut(text_: str, limit: int) -> str:
    return text_ if len(text_) <= limit else text_[: limit - 1].rstrip() + "…"


def _money(value: Any) -> str:
    return f"{value:.2f}" if isinstance(value, int | float) else "?"


def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _confidence(value: Any) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return min(max(float(value), 0.0), 1.0)
    return 0.0


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _finite(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _emotes_only(m: ChatMessageLike) -> bool:
    names = {str(e.get("name")) for e in m.emotes or () if isinstance(e, Mapping)}
    tokens = m.text.split()
    return bool(tokens) and all(t in names for t in tokens)
