"""Eval cases of `clipper-judge eval` (DESIGN §19, build item 6d): fixture files and labels.

**Fixture file** (JSON, UTF-8): one case or a list of cases, each

    {"format": "clipper-judge-eval-case", "version": 1, "id": "803", "label": {...} | null,
     "meta": {...}, "inputs": {...}}

`inputs` is one judge round's `JudgeInputs` (round 0 for a candidate: its window as the context),
lossless for the judge: `build_request` of a loaded fixture gives the same bytes as of the inputs
it was written from (a test pins it). Times are ISO 8601 UTC; the bot list is reduced to the bots
that write in the chat. `clipper-judge eval --save-fixtures DIR` writes them from the database.

**Labels** (TOML or JSON, by the file's extension): case id (a candidate id for database cases)
-> a verdict or a table, at the top level or under `labels`:

    [labels]
    "803" = "clip"
    "804" = { verdict = "reject", note = "only chat spam" }
    "805" = { verdict = "clip", in_s = -12.0, out_s = 5.5 }   # the ideal cut, seconds from T

A fixture's own `label` is used when the labels file has no entry for its id.

**Drafts** (`clipper-detector backtest --export-drafts`, format `clipper-detector-drafts` v1):
detector drafts that aren't live candidates (looser params, suppressed ones), as eval cases with
ids `d<stream>-<peak epoch s>`; `load_drafts` turns each into a `CandidateInfo` (id 0: no
`candidates` row) that the database loader judges like a candidate.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from clipper_core.chat import ChannelBots, MessageRow
from clipper_core.timeline import Interval, ensure_utc
from clipper_core.transcripts import Coverage, TranscriptWord
from clipper_judge.inputs import JudgeInputs, PriorRound
from clipper_judge.reads import CandidateInfo
from clipper_judge.samples import AudioRow, GapRow

FORMAT = "clipper-judge-eval-case"
VERSION = 1
VERDICTS = ("clip", "reject")


class EvalSetError(ValueError):
    """An unreadable fixture or labels file."""


@dataclass(frozen=True, slots=True)
class Label:
    verdict: Literal["clip", "reject"]
    in_s: float | None = None  # the ideal cut relative to T (optional)
    out_s: float | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "in_s": self.in_s, "out_s": self.out_s, "note": self.note}


@dataclass(frozen=True, slots=True)
class FixtureCase:
    id: str
    inputs: JudgeInputs
    label: Label | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- labels


def parse_label(value: Any, where: str = "label") -> Label:
    if isinstance(value, str):
        value = {"verdict": value}
    if not isinstance(value, Mapping):
        raise EvalSetError(f"{where}: a verdict or a table, not {value!r}")
    verdict = str(value.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        raise EvalSetError(f"{where}: verdict must be one of {', '.join(VERDICTS)}")
    cut = [value.get("in_s"), value.get("out_s")]
    if any(v is not None for v in cut):
        if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in cut):
            raise EvalSetError(f"{where}: in_s and out_s go together, as numbers")
        if not cut[0] < cut[1]:
            raise EvalSetError(f"{where}: in_s must be before out_s")
    return Label(
        verdict=verdict,  # type: ignore[arg-type]
        in_s=None if cut[0] is None else float(cut[0]),
        out_s=None if cut[1] is None else float(cut[1]),
        note=str(value.get("note") or ""),
    )


def load_labels(path: Path) -> dict[str, Label]:
    try:
        raw = path.read_bytes()
        if path.suffix.lower() == ".toml":
            data: Any = tomllib.loads(raw.decode("utf-8-sig"))
        else:
            data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError) as exc:  # TOMLDecodeError and JSONDecodeError are ValueErrors
        raise EvalSetError(f"{path}: {exc}") from exc
    if isinstance(data, Mapping) and isinstance(data.get("labels"), Mapping):
        data = data["labels"]
    if not isinstance(data, Mapping):
        raise EvalSetError(f"{path}: expected a table of case id -> label")
    return {str(k).strip(): parse_label(v, f"{path}: {k}") for k, v in data.items()}


# --------------------------------------------------------------------------- drafts

DRAFTS_FORMAT = "clipper-detector-drafts"
DRAFTS_VERSION = 1
DRAFT_PREFIX = "d"


def is_draft_id(case_id: str) -> bool:
    """`d<stream>-<peak epoch s>` (a detector draft), not a candidate id."""
    return case_id.startswith(DRAFT_PREFIX)


@dataclass(frozen=True, slots=True)
class Draft:
    id: str
    candidate: CandidateInfo  # id 0: no candidates row
    raw: Mapping[str, Any]  # the draft as exported (written back into a label set)


def draft_candidate(d: Mapping[str, Any]) -> CandidateInfo:
    c = {k: d[k] for k in CANDIDATE_FIELDS if k != "id"}
    for name in _CANDIDATE_TIMES:
        c[name] = _dt(c[name])
    c["features"] = dict(c.get("features") or {})
    c["chat_delay_s"] = float(c["chat_delay_s"])
    c["score"] = float(c["score"])
    return CandidateInfo(id=0, **c)


def drafts_doc(drafts: Sequence[Draft], **meta: Any) -> dict[str, Any]:
    return {
        "format": DRAFTS_FORMAT,
        "version": DRAFTS_VERSION,
        **meta,
        "drafts": [dict(d.raw) for d in drafts],
    }


def load_drafts(path: Path) -> list[Draft]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise EvalSetError(f"{path}: {exc}") from exc
    if not isinstance(data, Mapping) or data.get("format") != DRAFTS_FORMAT:
        raise EvalSetError(f"{path}: not a {DRAFTS_FORMAT} file")
    if data.get("version") != DRAFTS_VERSION:
        raise EvalSetError(f"{path}: version {data.get('version')!r}, this judge reads 1")
    out: list[Draft] = []
    seen: set[str] = set()
    for i, d in enumerate(data.get("drafts") or ()):
        try:
            draft_id = str(d["id"])
            if not is_draft_id(draft_id):
                raise EvalSetError(f"draft id {draft_id!r} doesn't start with {DRAFT_PREFIX!r}")
            cand = draft_candidate(d)
        except EvalSetError as exc:
            raise EvalSetError(f"{path}: drafts[{i}]: {exc}") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise EvalSetError(f"{path}: drafts[{i}]: {type(exc).__name__}: {exc}") from exc
        if draft_id in seen:
            raise EvalSetError(f"{path}: draft id {draft_id!r} appears twice")
        seen.add(draft_id)
        out.append(Draft(draft_id, cand, dict(d)))
    return out


# --------------------------------------------------------------------------- inputs <-> JSON


def _t(dt: datetime) -> str:
    return ensure_utc(dt).isoformat()


def _dt(value: Any) -> datetime:
    if not isinstance(value, str):
        raise EvalSetError(f"not a timestamp: {value!r}")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise EvalSetError(f"timestamp without an offset: {value!r}")
    return ensure_utc(dt)


def _iv(iv: Interval) -> list[str]:
    return [_t(iv.start), _t(iv.end)]


def _interval(value: Any) -> Interval:
    return Interval(_dt(value[0]), _dt(value[1]))


def _ivs(values: Iterable[Interval]) -> list[list[str]]:
    return [_iv(v) for v in values]


def _intervals(values: Any) -> tuple[Interval, ...]:
    return tuple(_interval(v) for v in values or ())


CANDIDATE_FIELDS = (
    "id",
    "stream_id",
    "channel_id",
    "source",
    "status",
    "trigger",
    "peak_utc",
    "chat_delay_s",
    "delay_source",
    "moment_from",
    "moment_to",
    "window_from",
    "window_to",
    "score",
    "moment_type",
    "features",
)
_CANDIDATE_TIMES = ("peak_utc", "moment_from", "moment_to", "window_from", "window_to")


def _word(w: Any) -> list[Any]:
    return [
        w.text,
        _t(w.start_utc),
        _t(w.end_utc),
        w.confidence,
        getattr(w, "chunk_id", 0),
        bool(getattr(w, "provisional", False)),
    ]


def inputs_to_json(inputs: JudgeInputs) -> dict[str, Any]:
    c = inputs.candidate
    cand: dict[str, Any] = {}
    for name in CANDIDATE_FIELDS:
        value = getattr(c, name, None)
        if name in ("window_from", "window_to") and value is None:
            value = inputs.context.start if name == "window_from" else inputs.context.end
        cand[name] = _t(value) if name in _CANDIDATE_TIMES else value
    cand["features"] = dict(c.features or {})
    bots = sorted(
        {
            m.user_login.lower()
            for m in inputs.chat
            if m.user_login and inputs.bots.is_bot(m.user_login)
        }
    )
    cov = inputs.coverage
    return {
        "clip_id": inputs.clip_id,
        "channel": inputs.channel,
        "stream_title": inputs.stream_title,
        "stream_category": inputs.stream_category,
        "stream_started_at": _t(inputs.stream_started_at),
        "stream_ended_at": None if inputs.stream_ended_at is None else _t(inputs.stream_ended_at),
        "context": _iv(inputs.context),
        "round": inputs.round,
        "candidate": cand,
        "words_interval": _iv(inputs.words_interval),
        "words": [_word(w) for w in inputs.words],
        "coverage": {
            "interval": _iv(cov.interval),
            "transcribed": _ivs(cov.transcribed),
            "provisional": _ivs(cov.provisional),
            "gap": _ivs(cov.gap),
            "untranscribed": _ivs(cov.untranscribed),
            "unrecorded": _ivs(cov.unrecorded),
        },
        "gaps": [[_t(g.start_utc), _t(g.end_utc), g.reason] for g in inputs.gaps],
        "video": _ivs(inputs.video),
        "chat": [
            {
                "sent_at": _t(m.sent_at),
                "msg_id": m.msg_id,
                "user_id": m.user_id,
                "user_login": m.user_login,
                "text": m.text,
                "emotes": m.emotes,
                "deleted": bool(m.deleted),
            }
            for m in inputs.chat
        ],
        "audio": [
            [_t(a.ts), a.loudness_db, a.speech_ratio, dict(a.classes or {})] for a in inputs.audio
        ],
        "prior_rounds": [
            {"round": p.round, "before_s": p.before_s, "after_s": p.after_s, "why": p.why}
            for p in inputs.prior_rounds
        ],
        "sfx_tags": list(inputs.sfx_tags),
        "bots": bots,
        "flags": list(inputs.flags),
    }


def inputs_from_json(data: Mapping[str, Any]) -> JudgeInputs:
    try:
        c = dict(data["candidate"])
        for name in _CANDIDATE_TIMES:
            c[name] = _dt(c[name])
        c["features"] = dict(c.get("features") or {})
        candidate = CandidateInfo(**{k: c[k] for k in CANDIDATE_FIELDS})
        cov = data["coverage"]
        channel_id = candidate.channel_id
        return JudgeInputs(
            clip_id=int(data.get("clip_id", 0)),
            candidate=candidate,
            channel=str(data["channel"]),
            stream_title=data.get("stream_title"),
            stream_category=data.get("stream_category"),
            stream_started_at=_dt(data["stream_started_at"]),
            stream_ended_at=None
            if data.get("stream_ended_at") is None
            else _dt(data["stream_ended_at"]),
            context=_interval(data["context"]),
            round=int(data.get("round", 0)),
            words=[
                TranscriptWord(
                    text=w[0],
                    start_utc=_dt(w[1]),
                    end_utc=_dt(w[2]),
                    confidence=w[3],
                    chunk_id=int(w[4]),
                    provisional=bool(w[5]),
                )
                for w in data["words"]
            ],
            words_interval=_interval(data["words_interval"]),
            coverage=Coverage(
                interval=_interval(cov["interval"]),
                transcribed=_intervals(cov.get("transcribed")),
                provisional=_intervals(cov.get("provisional")),
                gap=_intervals(cov.get("gap")),
                untranscribed=_intervals(cov.get("untranscribed")),
                unrecorded=_intervals(cov.get("unrecorded")),
            ),
            gaps=tuple(GapRow(_dt(g[0]), _dt(g[1]), str(g[2])) for g in data.get("gaps") or ()),
            video=_intervals(data.get("video")),
            chat=[
                MessageRow(
                    sent_at=_dt(m["sent_at"]),
                    msg_id=str(m["msg_id"]),
                    channel_id=channel_id,
                    stream_id=candidate.stream_id,
                    received_at=None,
                    user_id=m.get("user_id"),
                    user_login=m.get("user_login"),
                    text=str(m["text"]),
                    emotes=m.get("emotes"),
                    badges=None,
                    deleted=bool(m.get("deleted", False)),
                )
                for m in data.get("chat") or ()
            ],
            audio=[
                AudioRow(_dt(a[0]), float(a[1]), float(a[2]), dict(a[3]))
                for a in data.get("audio") or ()
            ],
            prior_rounds=tuple(
                PriorRound(
                    int(p["round"]),
                    before_s=float(p["before_s"]),
                    after_s=float(p["after_s"]),
                    why=str(p.get("why") or ""),
                )
                for p in data.get("prior_rounds") or ()
            ),
            sfx_tags=tuple(data.get("sfx_tags") or ()),
            bots=ChannelBots(frozenset(str(b).lower() for b in data.get("bots") or ())),
            flags=tuple(data.get("flags") or ()),
        )
    except EvalSetError:
        raise
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise EvalSetError(f"invalid inputs: {type(exc).__name__}: {exc}") from exc


def case_to_json(
    case_id: str,
    inputs: JudgeInputs,
    *,
    label: Label | None = None,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "format": FORMAT,
        "version": VERSION,
        "id": case_id,
        "label": None if label is None else label.as_dict(),
        "meta": dict(meta or {}),
        "inputs": inputs_to_json(inputs),
    }


def case_from_json(data: Any, where: str = "case") -> FixtureCase:
    if not isinstance(data, Mapping) or data.get("format") != FORMAT:
        raise EvalSetError(f"{where}: not a {FORMAT} object")
    if data.get("version") != VERSION:
        raise EvalSetError(f"{where}: version {data.get('version')!r}, this judge reads {VERSION}")
    case_id = str(data.get("id") or "").strip()
    if not case_id:
        raise EvalSetError(f"{where}: no id")
    try:
        inputs = inputs_from_json(data["inputs"])
    except (EvalSetError, KeyError) as exc:
        raise EvalSetError(f"{where} {case_id}: {exc}") from exc
    label = data.get("label")
    return FixtureCase(
        id=case_id,
        inputs=inputs,
        label=None if label is None else parse_label(label, f"{where} {case_id}"),
        meta=dict(data.get("meta") or {}),
    )


def fixture_files(paths: Sequence[Path]) -> list[Path]:
    """The given files, and the `*.json` files of the given directories (sorted)."""
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out.extend(sorted(p.glob("*.json")))
        else:
            out.append(p)
    return out


def load_fixtures(paths: Sequence[Path]) -> list[FixtureCase]:
    cases: list[FixtureCase] = []
    seen: set[str] = set()
    for path in fixture_files(paths):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise EvalSetError(f"{path}: {exc}") from exc
        for i, item in enumerate(data if isinstance(data, list) else [data]):
            case = case_from_json(item, f"{path}[{i}]")
            if case.id in seen:
                raise EvalSetError(f"{path}: case id {case.id!r} appears twice")
            seen.add(case.id)
            cases.append(case)
    if not cases:
        raise EvalSetError("no fixture cases found")
    return cases


def write_fixture(path: Path, case: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(case, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
