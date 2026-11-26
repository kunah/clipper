"""Explicit signals (DESIGN §18.4): "clip it" mentions in raw chat and viewer-made clips.

Pure trackers, fed in time order; the live service attaches their `Signal`s to candidates
(boost, features, re-emitting a suppressed one) or turns them into candidates of their own.

- **Mentions**: "clip it/that/this" (also "clipit", "clip_it", stretched "CLIIIP IT"), "clipped",
  a message that is only "clip" ("CLIP", "clip?", "CLIPPP!") or starts with `!clip`; not after a
  negation ("don't clip that", "not clipped"); links (with or without a scheme) are removed
  first, and a bare "clip" in a sentence ("nice clip", "watch the clip") is no request. From a
  user who isn't a bot, not deleted. `>= max(3, 3 % of the clean uniques)` distinct users within
  30 s -> a signal at the first mention - 5 s (chat time); later mentions within
  `signal_attach_s` of that first one don't start another.
- **Viewer clips**: Helix Get Clips (the service polls it when Twitch credentials are set) and
  clip links posted in chat. A clip's moment is `[created_at - duration - 5 s, created_at - 5 s]`
  (video time; live clips have no `vod_offset` for minutes). `>= 2` distinct creators (3 when the
  channel made more than 20 clips in the last hour) whose moment midpoints lie within 90 s of the
  newest clip's -> a signal at the middle of the clips' common part (else their median midpoint).
  A clip link in chat counts as a clip made when it was posted, 30 s long, by its poster (the
  service skips bots and deleted messages, so two people must post). Helix clips cut from the
  VOD later (`vod_offset` far from a live clip's place, `from_vod_later`) aren't live signals.
- **Own candidates** (`signal_draft`): at most `signal_budget_per_hour` per stream; more are
  stored suppressed (`budget`).
"""

from __future__ import annotations

import math
import re
import statistics
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from clipper_core.chat import is_command, normalised_text, user_key
from clipper_core.chat import is_mention as is_mention  # re-exported
from clipper_core.chat import strip_links as strip_links  # re-exported
from clipper_core.contracts import CandidateSource, CandidateStatus, MomentType, SuppressReason
from clipper_detector.lexicon import ChannelLexicon
from clipper_detector.merge import CandidateDraft, Delay, PriorCandidate, candidate_times
from clipper_detector.params import DetectorParams

CLIP_MENTIONS = "clip_mentions"  # `candidates.trigger` values and `features.explicit` keys
VIEWER_CLIPS = "viewer_clips"
KINDS = (CLIP_MENTIONS, VIEWER_CLIPS)

_CLIP_LINK = re.compile(
    r"(?:clips\.twitch\.tv/(?:embed\?clip=)?|(?:www\.|m\.)?twitch\.tv/(?:\w+/)?clip/)"
    r"([A-Za-z0-9_-]{4,})",
    re.IGNORECASE,
)
_NOT_SLUGS = frozenset({"embed", "create"})  # clips.twitch.tv pages, not clips


class RawMessage(Protocol):
    sent_at: datetime
    msg_id: str
    user_id: str | None
    user_login: str | None
    text: str
    deleted: bool


@dataclass(frozen=True, slots=True)
class Signal:
    """An explicit signal. `start`..`end` is its evidence, `at` its moment; chat time for
    mentions, video time for viewer clips (`video`)."""

    kind: str
    start: datetime
    at: datetime
    end: datetime
    video: bool
    data: Mapping[str, Any] = field(default_factory=dict)

    def chat(self, ts: datetime, delay: timedelta) -> datetime:
        return ts + delay if self.video else ts

    def chat_at(self, delay: timedelta) -> datetime:
        return self.chat(self.at, delay)


def boost(kind: str, params: DetectorParams) -> float:
    return params.boost_clip_mentions if kind == CLIP_MENTIONS else params.boost_viewer_clip


def merge_data(kind: str, old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> dict[str, Any]:
    """One `features.explicit[kind]` entry from two signals of the same kind."""
    if not old:
        return dict(new)
    if kind == CLIP_MENTIONS:
        return {
            "users": max(old.get("users", 0), new.get("users", 0)),
            "clean_uniques": max(old.get("clean_uniques", 0), new.get("clean_uniques", 0)),
            "first": min(old.get("first", new["first"]), new["first"]),
            "last": max(old.get("last", new["last"]), new["last"]),
        }
    clips = {c["id"]: c for c in (*old.get("clips", ()), *new.get("clips", ()))}
    ordered = sorted(clips.values(), key=lambda c: (c["created_at"], c["id"]))
    return {"clips": ordered, "creators": len({c["creator"] for c in ordered})}


def explicit_features(signals: Iterable[Signal]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for s in signals:
        out[s.kind] = merge_data(s.kind, out.get(s.kind), s.data)
    return out


def total_boost(signals: Iterable[Signal], params: DetectorParams) -> float:
    return sum(boost(k, params) for k in {s.kind for s in signals})


def clip_links(text: str) -> list[str]:
    """Clip slugs of the Twitch clip links in a message."""
    slugs = (m.group(1) for m in _CLIP_LINK.finditer(text))
    return list(dict.fromkeys(s for s in slugs if s.lower() not in _NOT_SLUGS))


def from_vod_later(
    created_at: datetime,
    duration_s: float,
    vod_offset: int | None,
    started_at: datetime,
    params: DetectorParams,
) -> bool:
    """A Helix clip with a `vod_offset` far from where a clip made live would sit
    (`created_at - started_at - duration`): cut from the VOD later (or another VOD)."""
    if vod_offset is None:
        return False
    expected = (created_at - started_at).total_seconds() - duration_s
    return abs(vod_offset - expected) > params.clip_vod_tolerance_s


def signal_candidates_in_window(
    recent: Iterable[PriorCandidate], at: datetime, params: DetectorParams
) -> int:
    """Emitted signal-only candidates of the stream in `(at - budget window, at]` (chat time)."""
    window = timedelta(seconds=params.budget_window_s)
    return sum(
        1
        for c in recent
        if c.status is CandidateStatus.EMITTED
        and c.trigger in KINDS
        and at - window < c.chat_peak <= at
    )


# --------------------------------------------------------------------------- mentions


class MentionTracker:
    """Clip mentions of one channel; feed every raw message in `sent_at` order."""

    def __init__(self, params: DetectorParams, lexicon: ChannelLexicon) -> None:
        self.p = params
        self.lexicon = lexicon
        self._clean: deque[tuple[datetime, str]] = deque()
        self._mentions: deque[tuple[datetime, str]] = deque()
        self._blocked_until: datetime | None = None

    def add(self, m: RawMessage) -> Signal | None:
        p = self.p
        ts = m.sent_at
        horizon = ts - timedelta(seconds=p.mention_window_s)
        user = user_key(m)
        bot = self.lexicon.is_bot((m.user_login or "").lower())
        text = normalised_text(m.text)
        if user is not None and not (bot or m.deleted or is_command(text)):
            self._clean.append((ts, user))
        for q in (self._clean, self._mentions):
            while q and q[0][0] <= horizon:
                q.popleft()
        if user is None or bot or m.deleted or not is_mention(m.text):
            return None
        if self._blocked_until is not None and ts < self._blocked_until:
            return None
        self._mentions.append((ts, user))
        users = {u for _, u in self._mentions}
        clean = {u for _, u in self._clean}
        need = max(p.mention_min_users, math.ceil(p.mention_min_share * len(clean)))
        if len(users) < need:
            return None
        first = self._mentions[0][0]
        self._mentions.clear()
        self._blocked_until = first + timedelta(seconds=p.signal_attach_s)
        at = first - timedelta(seconds=p.mention_lead_s)
        return Signal(
            CLIP_MENTIONS,
            start=at,
            at=at,
            end=ts,
            video=False,
            data={
                "users": len(users),
                "clean_uniques": len(clean),
                "first": first.isoformat(),
                "last": ts.isoformat(),
            },
        )


# --------------------------------------------------------------------------- viewer clips


@dataclass(frozen=True, slots=True)
class ViewerClip:
    id: str  # Twitch clip slug
    creator: str
    created_at: datetime  # when it was made (a link: when it was posted)
    duration_s: float
    source: str  # helix | chat

    def span(self, latency_s: float) -> tuple[datetime, datetime]:
        end = self.created_at - timedelta(seconds=latency_s)
        return end - timedelta(seconds=self.duration_s), end

    def mid(self, latency_s: float) -> datetime:
        start, end = self.span(latency_s)
        return start + (end - start) / 2

    def as_data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "creator": self.creator,
            "created_at": self.created_at.isoformat(),
            "duration_s": self.duration_s,
            "source": self.source,
        }


class ClipTracker:
    """Viewer clips of one channel (kept for an hour, by id)."""

    def __init__(self, params: DetectorParams) -> None:
        self.p = params
        self._clips: dict[str, ViewerClip] = {}

    def __contains__(self, clip_id: str) -> bool:
        return clip_id in self._clips

    def add(self, clip: ViewerClip) -> Signal | None:
        p = self.p
        if clip.id in self._clips:
            return None
        self._clips[clip.id] = clip
        newest = max(c.created_at for c in self._clips.values())
        hour = newest - timedelta(hours=1)
        keep = hour - timedelta(seconds=p.clip_cluster_s)
        self._clips = {k: c for k, c in self._clips.items() if c.created_at > keep}
        per_hour = sum(1 for c in self._clips.values() if c.created_at > hour)
        need = p.clip_busy_creators if per_hour > p.clip_busy_per_hour else p.clip_min_creators
        lat = p.clip_latency_s
        mid = clip.mid(lat)
        near = sorted(
            (
                c
                for c in self._clips.values()
                if abs((c.mid(lat) - mid).total_seconds()) <= p.clip_cluster_s
            ),
            key=lambda c: (c.created_at, c.id),
        )
        creators = {c.creator for c in near}
        if len(creators) < need:
            return None
        spans = [c.span(lat) for c in near]
        lo, hi = max(s for s, _ in spans), min(e for _, e in spans)
        if lo < hi:
            at = lo + (hi - lo) / 2
        else:
            mids = sorted(c.mid(lat) for c in near)
            base = mids[0]
            at = base + timedelta(
                seconds=statistics.median((m - base).total_seconds() for m in mids)
            )
        return Signal(
            VIEWER_CLIPS,
            start=min(s for s, _ in spans),
            at=at,
            end=max(e for _, e in spans),
            video=True,
            data={"clips": [c.as_data() for c in near], "creators": len(creators)},
        )


# --------------------------------------------------------------------------- own candidates


def signal_draft(
    signal: Signal,
    *,
    stream_id: int,
    channel_id: int,
    delay: Delay,
    params: DetectorParams,
    detector_version: str,
    signals: Iterable[Signal] = (),
    recent: Iterable[PriorCandidate] = (),
) -> CandidateDraft:
    """A candidate for a signal that no chat episode or candidate claimed (trigger = its kind).
    `signals`: every signal attached to it (default: just this one); `recent`: the stream's
    candidates so far (over `signal_budget_per_hour` signal-only ones: suppressed, `budget`)."""
    d = delay.td
    attached = list(signals) or [signal]
    chat_at = signal.chat_at(d)
    chat_start = min(chat_at, signal.chat(signal.start, d))
    chat_end = max(chat_at, signal.chat(signal.end, d))
    times = candidate_times(chat_at, chat_start, chat_end, delay, params)
    features: dict[str, Any] = {
        "explicit": explicit_features(attached),
        "delay": {"seconds": delay.seconds, "source": delay.source.value},
    }
    in_window = signal_candidates_in_window(recent, chat_at, params)
    over = in_window >= params.signal_budget_per_hour
    if over:
        features["budget"] = {"signal_in_window": in_window}
    return CandidateDraft(
        stream_id=stream_id,
        channel_id=channel_id,
        source=CandidateSource.LIVE,
        status=CandidateStatus.SUPPRESSED if over else CandidateStatus.EMITTED,
        suppress_reason=SuppressReason.BUDGET if over else None,
        trigger=signal.kind,
        peak_utc=times.peak_utc,
        chat_peak_utc=None if signal.video else chat_at,
        chat_delay_s=delay.seconds,
        delay_source=delay.source,
        moment_from=times.moment_from,
        moment_to=times.moment_to,
        window_from=times.window_from,
        window_to=times.window_to,
        score=total_boost(attached, params),
        moment_type=MomentType.UNKNOWN,
        features=features,
        detector_version=detector_version,
        chat_end_utc=chat_end,
        chat_onset_utc=chat_start,
    )
