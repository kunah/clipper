"""Stage 2: the raw chat of an episode confirms it and filters fake spikes (DESIGN §18.3).

`evaluate(episode, messages, events, ...)` is pure: the caller reads `chat_messages` over
`windows(episode).messages` and `chat_events` over `windows(episode).events` (rows outside are
ignored) and passes `events=None` when events are *unknown* (backfilled streams, §17): then the
event rules (raid, subs, mode_toggle, timeouts) are off and cheers come from `Cheer100`-style text.
The message window is `[min(onset - 20 s, strongest - 9 s), max(peak + 5 s, strongest) + 1 s)`, so
it always holds the strongest second's whole activity window.

- **Clean uniques**: per second, the distinct users with a message that is no bot's, no
  `!command` and not deleted (A's own unit: a user repeating himself counts once per second, as in
  `unique_chatters`); `clean_fraction` = clean / raw summed over the strongest second's activity
  window; `z_clean = (A * clean_fraction - B') / S` at that second. Emitting needs `z_clean >= 4`
  (episodes opened in warm-up: 6).
- **Shares** (of the messages in `[onset, peak + 5 s]`, the burst): reaction (an emotion emote/word;
  a pattern-only match counts `pattern_weight`), greeting (greetings that aren't reactions: "YO
  WTF" is a reaction), command (`!` + a letter/digit: "!enter", not "!!!"), bot, vote (one
  number/letter or believe/doubt that isn't a lexicon word like W/L/F, nor an emoticon letter
  like the D of ":D"), dup (the most common normalised text among messages with free text: mass
  "KEKW", "Kappa" or "???" floods are reactions, a copypasta is not), deleted, cheer, moderated
  (deleted, by a user timed out/banned in the window, or sent before a `clearchat`).
- **Score** = `z_clean * (0.6 + 0.8 * min(reaction, 0.5)) + boost`.
- **Moment type**: one vote per user and category (max weight); < 5 votes `unknown`; top share
  >= 0.4 its category (greeting/uncategorised -> `mixed`; a tie goes to the first category in
  lexicon order), else `mixed`.
- **Rules** (first match = `suppress_reason`, all matches in `rules`): raid, subs, bits,
  mode_toggle, spam_wave, commands, vote, copypasta, bots, greeting; none matched but z_clean
  below the emit bar: `weak` (`contracts.SuppressReason`).

Decisions beyond the §18 table (DESIGN §18 build item 2): shares and votes count the burst `[onset,
peak + 5 s]` (`shares_before_onset_s` = 0), not `[onset - 20 s, peak]`: with the 20 s of ordinary
chat a 15 s x4 burst of one copypasta reached a dup share of 0.28-0.42 there (median 0.34 over 10
seeds; threshold 0.4), 0.60-0.73 on the burst; the 20 s still serve for events. Vote is checked
before copypasta ("1"/"2" floods are polls); spam_wave = the moderated share >= 0.3 (timeouts don't
mark messages deleted); `weak` is a new suppress reason; a user's repeats across seconds stay clean
(removing them made real mass-emote moments weak).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from clipper_core.chat import is_command, user_key
from clipper_core.chat import normalised_text as normalised_text  # re-exported
from clipper_core.contracts import CandidateStatus, MomentType, SuppressReason
from clipper_core.timeline import Interval
from clipper_detector.lexicon import (
    Category,
    ChannelLexicon,
    MessageMatch,
    to_moment_type,
    tokens_of,
)
from clipper_detector.params import DetectorParams
from clipper_detector.scorer import Episode


class ChatMessageLike(Protocol):
    """A `chat_messages` row (`clipper_core.chat.MessageRow`, the ORM model, a test stub)."""

    sent_at: datetime
    msg_id: str
    user_id: str | None
    user_login: str | None
    text: str
    emotes: list[dict[str, Any]] | None
    deleted: bool


class ChatEventLike(Protocol):
    """A `chat_events` row (`clipper_core.chat.EventRow`, the ORM model, a test stub)."""

    sent_at: datetime
    type: str
    msg_id: str | None
    user_login: str | None
    data: dict[str, Any]


SUB_EVENTS = frozenset({"sub", "resub", "subgift", "anonsubgift"})
MODERATION_EVENTS = frozenset({"timeout", "ban"})
_VOTE = re.compile(r"\d{1,2}|[a-z]|believe|doubt")
_EMOTICON_LETTERS = frozenset("dopx")  # the rest of :D :O :P XD typed apart, not poll answers
_VOTE_TRAILING = ".,!?;)"
# Cheermotes as plain text (backfill has no bits events): "Cheer100", "BibleThump500", ...
_CHEER = re.compile(
    r"(?:cheer|biblethump|cheerwhal|corgo|uni|showlove|party|seemsgood|pride|kappa|frankerz|"
    r"heyguys|dansgame|elegiggle|trihard|kreygasm|4head|swiftrage|notlikethis|failfish|vohiyo|"
    r"pjsalt|mrdestructoid|bday|ripcheer|shamrock|anon)(\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Windows:
    # chat_messages to read: [min(onset - 20 s, strongest - 9 s), max(peak + 5 s, strongest) + 1 s)
    messages: Interval
    events: Interval  # chat_events to read: from onset - 60 s, to the same end


def windows(episode: Episode, params: DetectorParams) -> Windows:
    """Through the end of second `max(peak + after, strongest)`, from `min(onset - before,
    strongest - activity window)`: the strongest second's activity window is always inside."""
    p = params
    strongest = episode.strongest.ts
    end = max(episode.peak + timedelta(seconds=p.stage2_after_peak_s), strongest)
    end += timedelta(seconds=1)
    start = min(
        episode.onset - timedelta(seconds=p.stage2_before_onset_s),
        strongest - timedelta(seconds=p.activity_window_s - 1),
    )
    events_start = min(episode.onset - timedelta(seconds=p.events_before_onset_s), start)
    return Windows(messages=Interval(start, end), events=Interval(events_start, end))


@dataclass(frozen=True, slots=True)
class Shares:
    reaction: float = 0.0
    greeting: float = 0.0
    command: float = 0.0
    bot: float = 0.0
    vote: float = 0.0
    dup: float = 0.0
    deleted: float = 0.0
    cheer: float = 0.0
    moderated: float = 0.0


@dataclass(frozen=True, slots=True)
class EventSummary:
    known: bool  # False: no chat_events for this stream (backfill); event rules were off
    raids: int = 0
    raid_viewers: int = 0
    mass_gift_max: int = 0  # largest submysterygift mass-gift-count
    sub_events: int = 0  # sub/resub/subgift/anonsubgift
    bits_total: int = 0
    mode_toggles: int = 0  # roomstate changes
    clearchats: int = 0
    moderations: int = 0  # timeouts + bans


@dataclass(frozen=True, slots=True)
class Stage2Result:
    status: CandidateStatus
    reason: SuppressReason | None
    rules: tuple[SuppressReason, ...]  # every rule that matched, in rule order
    z_clean: float
    clean_fraction: float
    score: float
    moment_type: MomentType
    votes: Mapping[str, float]  # category -> votes (one per user)
    shares: Shares
    events: EventSummary
    messages: int
    chatters: int
    clean_chatters: int
    dup_text: str | None = None
    top_emotes: tuple[tuple[str, int], ...] = ()
    warmup: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def emitted(self) -> bool:
        return self.status is CandidateStatus.EMITTED

    def features(self) -> dict[str, Any]:
        """JSON-ready, for `candidates.features`."""
        return {
            "z_clean": round(self.z_clean, 3),
            "clean_fraction": round(self.clean_fraction, 3),
            "score": round(self.score, 3),
            "shares": {k: round(getattr(self.shares, k), 3) for k in Shares.__slots__},
            "categories": {k: round(v, 2) for k, v in self.votes.items()},
            "rules": [r.value for r in self.rules],
            "events": {k: getattr(self.events, k) for k in EventSummary.__slots__},
            "messages": self.messages,
            "chatters": self.chatters,
            "clean_chatters": self.clean_chatters,
            "dup_text": self.dup_text,
            "top_emotes": [list(e) for e in self.top_emotes],
            **self.extra,
        }


# --------------------------------------------------------------------------- helpers


def _emote_names(m: ChatMessageLike) -> list[str]:
    return [e["name"] for e in m.emotes or () if e.get("name")]


def _is_vote(text: str, match: MessageMatch) -> bool:
    """One token, no lexicon match; only trailing punctuation is dropped (":D" is no "d")."""
    tokens = tokens_of(text)
    if len(tokens) != 1 or match.categories:
        return False
    token = tokens[0].lower().rstrip(_VOTE_TRAILING)
    return _VOTE.fullmatch(token) is not None and token not in _EMOTICON_LETTERS


def _cheer_bits(text: str) -> int:
    return sum(int(m.group(1)) for t in tokens_of(text) if (m := _CHEER.fullmatch(t)))


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def moment_type(votes: Mapping[Category, float], params: DetectorParams) -> MomentType:
    """Dominant category of one-per-user votes (DESIGN §18.2)."""
    total = sum(votes.values())
    if total < params.moment_min_votes or total <= 0:
        return MomentType.UNKNOWN
    order = list(Category)
    top = max(votes, key=lambda c: (votes[c], -order.index(c)))
    if votes[top] / total >= params.moment_top_share:
        return to_moment_type(top, enough_votes=True)
    return MomentType.MIXED


@dataclass(slots=True)
class _Msg:
    m: ChatMessageLike
    second: datetime
    user: str | None
    match: MessageMatch
    text: str  # normalised
    bot: bool
    command: bool
    vote: bool
    cheer: bool
    clean: bool = False


# --------------------------------------------------------------------------- evaluate


def evaluate(
    episode: Episode,
    messages: Iterable[ChatMessageLike],
    events: Iterable[ChatEventLike] | None,
    *,
    lexicon: ChannelLexicon,
    params: DetectorParams,
    boost: float = 0.0,
) -> Stage2Result:
    p = params
    win = windows(episode, p)
    raw = sorted(
        (m for m in messages if win.messages.contains(m.sent_at)),
        key=lambda m: (m.sent_at, m.msg_id),
    )
    evs = (
        None
        if events is None
        else sorted((e for e in events if win.events.contains(e.sent_at)), key=lambda e: e.sent_at)
    )
    summary, bits_msgs, moderated_users, clear_at = _events(episode, evs, p)

    msgs: list[_Msg] = []
    for m in raw:
        match = lexicon.match(m.text, _emote_names(m))
        user = user_key(m)
        text = normalised_text(m.text)
        login = (m.user_login or "").lower()
        cheer = _cheer_bits(m.text) > 0 if evs is None else m.msg_id in bits_msgs
        msg = _Msg(
            m=m,
            second=m.sent_at.replace(microsecond=0),
            user=user,
            match=match,
            text=text,
            bot=lexicon.is_bot(login),
            command=is_command(text),
            vote=_is_vote(m.text, match),
            cheer=cheer,
        )
        msg.clean = user is not None and not (msg.bot or msg.command or m.deleted)
        msgs.append(msg)
    core_from = episode.onset - timedelta(seconds=p.shares_before_onset_s)
    core = [x for x in msgs if x.m.sent_at >= core_from]
    if evs is None:
        summary = EventSummary(known=False, bits_total=sum(_cheer_bits(x.m.text) for x in core))

    n = len(core)
    pw = p.pattern_weight

    def share(values: Iterable[float]) -> float:
        return sum(values) / n if n else 0.0

    dup_counts = Counter(x.text for x in core if x.text and not x.match.textless)
    dup_text, dup_n = dup_counts.most_common(1)[0] if dup_counts else (None, 0)
    shares = Shares(
        reaction=share(x.match.reaction_weight(pw) for x in core),
        greeting=share(0.0 if x.match.reaction else x.match.greeting_weight(pw) for x in core),
        command=share(x.command for x in core),
        bot=share(x.bot for x in core),
        vote=share(x.vote for x in core),
        dup=dup_n / n if n else 0.0,
        deleted=share(x.m.deleted for x in core),
        cheer=share(x.cheer for x in core),
        moderated=share(
            x.m.deleted
            or (x.m.user_login or "").lower() in moderated_users
            or (clear_at is not None and x.m.sent_at <= clear_at)
            for x in core
        ),
    )

    clean_fraction = _clean_fraction(episode, msgs, p)
    s = episode.strongest
    z_clean = (s.activity * clean_fraction - s.baseline) / s.scale
    reaction = min(shares.reaction, p.score_reaction_cap)
    score = z_clean * (p.score_base + p.score_reaction_weight * reaction) + boost

    votes = _votes(core, pw)
    rules = _rules(shares, summary, z_clean, p)
    emit_bar = p.z_clean_emit_warmup if episode.warmup else p.z_clean_emit
    reason = rules[0] if rules else (SuppressReason.WEAK if z_clean < emit_bar else None)
    top_emotes: Counter[str] = Counter()
    for x in core:
        for e in x.m.emotes or ():
            if e.get("name"):
                top_emotes[e["name"]] += _int(e.get("count", 1)) or 1
    return Stage2Result(
        status=CandidateStatus.EMITTED if reason is None else CandidateStatus.SUPPRESSED,
        reason=reason,
        rules=rules,
        z_clean=z_clean,
        clean_fraction=clean_fraction,
        score=score,
        moment_type=moment_type(votes, p),
        votes={c.value: v for c, v in sorted(votes.items(), key=lambda kv: -kv[1])},
        shares=shares,
        events=summary,
        messages=n,
        chatters=len({x.user for x in core if x.user}),
        clean_chatters=len({x.user for x in core if x.clean}),
        dup_text=dup_text,
        top_emotes=tuple(sorted(top_emotes.items(), key=lambda kv: (-kv[1], kv[0]))[:5]),
        warmup=episode.warmup,
        # >= 1000 bits: a feature only (a TTS reaction to a big cheer can be a real moment)
        extra={"bits_big": summary.bits_total >= p.bits_feature_min},
    )


def _events(
    episode: Episode, evs: Sequence[ChatEventLike] | None, p: DetectorParams
) -> tuple[EventSummary, set[str], set[str], datetime | None]:
    if evs is None:
        return EventSummary(known=False), set(), set(), None
    raid_from = episode.onset - timedelta(seconds=p.raid_lookback_s)
    near_from = episode.onset - timedelta(seconds=p.stage2_before_onset_s)
    toggle_from = episode.onset - timedelta(seconds=p.mode_toggle_lookback_s)
    raids = raid_viewers = mass_max = subs = bits_total = toggles = clears = mods = 0
    bits_msgs: set[str] = set()
    moderated: set[str] = set()
    clear_at: datetime | None = None
    for e in evs:
        data = e.data or {}
        if e.type == "raid" and e.sent_at >= raid_from:
            raids += 1
            raid_viewers += _int(data.get("viewerCount"))
        elif e.type == "roomstate" and e.sent_at >= toggle_from:
            toggles += 1
        if e.sent_at < near_from:
            continue
        if e.type == "submysterygift":
            mass_max = max(mass_max, _int(data.get("mass-gift-count")))
        elif e.type in SUB_EVENTS:
            subs += 1
        elif e.type == "bits":
            bits_total += _int(data.get("bits"))
            if e.msg_id:
                bits_msgs.add(e.msg_id)
        elif e.type == "clearchat":
            clears += 1
            clear_at = e.sent_at if clear_at is None else max(clear_at, e.sent_at)
        elif e.type in MODERATION_EVENTS:
            mods += 1
            if e.user_login:
                moderated.add(e.user_login.lower())
    summary = EventSummary(
        known=True,
        raids=raids,
        raid_viewers=raid_viewers,
        mass_gift_max=mass_max,
        sub_events=subs,
        bits_total=bits_total,
        mode_toggles=toggles,
        clearchats=clears,
        moderations=mods,
    )
    return summary, bits_msgs, moderated, clear_at


def _clean_fraction(episode: Episode, msgs: Sequence[_Msg], p: DetectorParams) -> float:
    """Clean / raw unique chatters, summed per second (the unit of A), over the strongest
    second's activity window (inside the read window; [onset, peak + after] when it holds no
    chat)."""
    raw: dict[datetime, set[str]] = defaultdict(set)
    clean: dict[datetime, set[str]] = defaultdict(set)
    for x in msgs:
        if x.user is None:
            continue
        raw[x.second].add(x.user)
        if x.clean:
            clean[x.second].add(x.user)

    def fraction(lo: datetime, hi: datetime) -> float | None:
        total = sum(len(u) for sec, u in raw.items() if lo <= sec <= hi)
        if total == 0:
            return None
        return sum(len(u) for sec, u in clean.items() if lo <= sec <= hi) / total

    s = episode.strongest.ts
    got = fraction(s - timedelta(seconds=p.activity_window_s - 1), s)
    if got is None:
        got = fraction(episode.onset, episode.peak + timedelta(seconds=p.stage2_after_peak_s))
    return 1.0 if got is None else got


def _votes(msgs: Sequence[_Msg], pattern_weight: float) -> dict[Category, float]:
    best: dict[tuple[str, Category], float] = {}
    for x in msgs:
        if x.user is None or x.bot or x.command or x.m.deleted:
            continue
        for cat in x.match.categories:
            w = x.match.weight(cat, pattern_weight)
            key = (x.user, cat)
            if w > best.get(key, 0.0):
                best[key] = w
    votes: dict[Category, float] = defaultdict(float)
    for (_user, cat), w in best.items():
        votes[cat] += w
    return dict(votes)


def _rules(
    shares: Shares, ev: EventSummary, z_clean: float, p: DetectorParams
) -> tuple[SuppressReason, ...]:
    R = SuppressReason
    keep_raid = z_clean >= p.raid_keep_z_clean and shares.reaction >= p.raid_keep_reaction
    checks = (
        (R.RAID, ev.known and ev.raids > 0 and not keep_raid),
        (
            R.SUBS,
            ev.known
            and (ev.mass_gift_max >= p.subs_mass_gift_min or ev.sub_events >= p.subs_events_min),
        ),
        (R.BITS, shares.cheer >= p.bits_share),
        (R.MODE_TOGGLE, ev.known and ev.mode_toggles > 0),
        (R.SPAM_WAVE, shares.moderated >= p.spam_wave_share),
        (R.COMMANDS, shares.command >= p.commands_share),
        (R.VOTE, shares.vote >= p.vote_share),
        (R.COPYPASTA, shares.dup >= p.copypasta_share),
        (R.BOTS, shares.bot >= p.bots_share),
        (R.GREETING, shares.greeting >= p.greeting_share),
    )
    return tuple(reason for reason, hit in checks if hit)
