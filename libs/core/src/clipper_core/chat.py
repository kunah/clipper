"""Chat rows, per-second chat stats and their batch writer, shared by live and backfill.

chat-ingest (live IRC) and the backfill tool (VOD chat replay) map their sources to the same
rows and aggregate them with the same `SecondAggregator`, so `chat_metrics_1s` means the same
for both. Each writes only its own streams' rows (one writer per table and source, DESIGN §17).

Per-second stats: buckets are keyed by the message's server time (`tmi-sent-ts` live, the
replay's `createdAt` for backfill) truncated to the second, and flushed once `grace` has passed,
to wait for slightly late messages. Anything arriving after its second was flushed is still
stored raw, just not counted (tracked in `late_dropped`). A second with no row means 0 messages.

Chat filters shared by the detector and the judge (DESIGN §18, §19): `strip_invisible`,
`normalised_text`, `is_command`, `strip_links`, `is_mention`, `user_key`, and the bot list
(`load_bots().for_channel(login)`, from the packaged `bots.toml`).
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from clipper_core.models import ChatEvent, ChatMessage, ChatMetricSecond
from clipper_core.partitions import ensure_chat_partitions
from clipper_core.timeline import utc_now

# --------------------------------------------------------------------------- rows


@dataclass(slots=True)
class MessageRow:
    """One `chat_messages` row."""

    sent_at: datetime
    msg_id: str
    channel_id: int
    stream_id: int | None
    received_at: datetime | None  # None for backfill (we didn't receive it live)
    user_id: str | None
    user_login: str | None
    text: str
    emotes: list[dict[str, Any]] | None
    badges: dict[str, str] | None
    deleted: bool = False
    source: str = "live"


@dataclass(slots=True)
class EventRow:
    """One `chat_events` row (live only: chat replay has no events)."""

    channel_id: int
    stream_id: int | None
    sent_at: datetime
    type: str
    msg_id: str | None = None
    user_login: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MetricRow:
    """One `chat_metrics_1s` row."""

    channel_id: int
    ts: datetime
    stream_id: int | None
    messages: int
    unique_chatters: int
    emote_counts: dict[str, int]
    event_counts: dict[str, int]


# --------------------------------------------------------------------------- per-second stats


@dataclass(slots=True)
class _Bucket:
    stream_id: int | None = None
    messages: int = 0
    chatters: set[str] = field(default_factory=set)
    emotes: Counter[str] = field(default_factory=Counter)
    events: Counter[str] = field(default_factory=Counter)


def second_of(ts: datetime) -> datetime:
    return ts.replace(microsecond=0)


class SecondAggregator:
    def __init__(self, grace: timedelta = timedelta(seconds=5)) -> None:
        self.grace = grace
        self.late_dropped = 0
        self._buckets: dict[tuple[int, datetime], _Bucket] = {}
        self._flushed_until: dict[int, datetime] = {}  # per channel: seconds < this are flushed

    def _bucket(self, channel_id: int, ts: datetime, stream_id: int | None) -> _Bucket | None:
        sec = second_of(ts)
        if sec < self._flushed_until.get(channel_id, datetime.min.replace(tzinfo=sec.tzinfo)):
            self.late_dropped += 1
            return None
        bucket = self._buckets.setdefault((channel_id, sec), _Bucket())
        if stream_id is not None:
            bucket.stream_id = stream_id
        return bucket

    def add_message(
        self,
        channel_id: int,
        stream_id: int | None,
        sent_at: datetime,
        user_key: str | None,
        emotes: Iterable[tuple[str, int]] = (),
    ) -> None:
        bucket = self._bucket(channel_id, sent_at, stream_id)
        if bucket is None:
            return
        bucket.messages += 1
        if user_key:
            bucket.chatters.add(user_key)
        for name, count in emotes:
            bucket.emotes[name] += count

    def add_row(self, row: MessageRow) -> None:
        """Count one stored message: its channel, stream and second; a chatter is its user id
        (else login; none: not a chatter); emotes by name. Live and backfill both count this way,
        so their `chat_metrics_1s` agree for the same messages."""
        self.add_message(
            row.channel_id,
            row.stream_id,
            row.sent_at,
            row.user_id or row.user_login,
            ((e["name"], e["count"]) for e in row.emotes or ()),
        )

    def add_event(
        self, channel_id: int, stream_id: int | None, sent_at: datetime, kind: str
    ) -> None:
        bucket = self._bucket(channel_id, sent_at, stream_id)
        if bucket is not None:
            bucket.events[kind] += 1

    def pop_ready(self, now: datetime) -> list[MetricRow]:
        cutoff = second_of(now - self.grace)
        return self._pop(lambda sec: sec < cutoff, cutoff)

    def pop_all(self) -> list[MetricRow]:
        return self._pop(lambda sec: True, None)

    def _pop(self, ready, cutoff: datetime | None) -> list[MetricRow]:
        rows: list[MetricRow] = []
        for key in sorted(k for k in self._buckets if ready(k[1])):
            channel_id, sec = key
            b = self._buckets.pop(key)
            rows.append(
                MetricRow(
                    channel_id,
                    sec,
                    b.stream_id,
                    b.messages,
                    len(b.chatters),
                    dict(b.emotes),
                    dict(b.events),
                )
            )
        if cutoff is not None:
            for channel_id in {r.channel_id for r in rows} | set(self._flushed_until):
                self._flushed_until[channel_id] = max(
                    self._flushed_until.get(channel_id, cutoff), cutoff
                )
        return rows

    @property
    def pending(self) -> int:
        return len(self._buckets)


# --------------------------------------------------------------------------- chat filters
# Shared by the detector (§18: stage 2, explicit signals) and the judge (§19: its chat summary),
# so both read chat alike. The patterns are the detector's, moved here unchanged.

# Invisible characters chat clients add to get around Twitch's duplicate-message filter
# (Chatterino/7TV append U+E0000), zero-width joiners/spaces, the emoji variation selector.
_INVISIBLE = re.compile("[\U000e0000-\U000e007f\u200b-\u200d\u2060\u034f\ufe0f]")
_SPACES = re.compile(r"\s+")
_COMMAND = re.compile(r"!\w")  # "!enter", "!points"; not "!!!" or "! KEKW"
# on normalised text (lowercase, single spaces)
_MENTION = re.compile(r"\bcli+p+(?:[\s_-]*(?:it|that|this)\b|ped\b)|^cli+p+[!?.]*$|^!clip\b")
_NEGATION = re.compile(r"\b(?:don'?t|dont|do not|never|not|no|stop|can'?t|cant|won'?t)\s*$")
# links with a scheme or www., and bare domains with a path ("youtube.com/clip/abc")
_URL = re.compile(r"\S*(?:https?://|www\.)\S*|\S+\.\w{2,}/\S*", re.IGNORECASE)


def strip_invisible(text: str) -> str:
    """Text without the invisible characters (for tokens, duplicates)."""
    return _INVISIBLE.sub("", text)


def normalised_text(text: str) -> str:
    """For duplicates and commands: no invisible characters, lowercase, single spaces."""
    return _SPACES.sub(" ", strip_invisible(text)).strip().lower()


def is_command(text: str) -> bool:
    """A bot command: `!` + a letter or digit at the start of the normalised text ("!enter",
    "!points"; not "!!!" or "! KEKW")."""
    return _COMMAND.match(normalised_text(text)) is not None


def strip_links(text: str) -> str:
    return _URL.sub(" ", text)


def is_mention(text: str) -> bool:
    """A request to clip: "clip it/that/this" (also "clipit", "clip_it", stretched "CLIIIP IT"),
    "clipped", a message that is only "clip" ("CLIP", "clip?") or starts with `!clip`; not after a
    negation ("don't clip that", "not clipped"); links are removed first, and a bare "clip" in a
    sentence ("nice clip", "watch the clip") is no request."""
    norm = normalised_text(strip_links(text).replace("\u2019", "'"))
    return any(not _NEGATION.search(norm[: m.start()]) for m in _MENTION.finditer(norm))


class ChatUser(Protocol):
    """A chat row's author fields (`MessageRow`, the ORM model, a test stub)."""

    user_id: str | None
    user_login: str | None


def user_key(m: ChatUser) -> str | None:
    """A chatter: the user id, else the lowercase login (None: not a chatter)."""
    return m.user_id or (m.user_login.lower() if m.user_login else None)


# --------------------------------------------------------------------------- bots

BOTS_FILE = "bots.toml"  # clipper_core/bots.toml: the packaged list


class BotFilter(Protocol):
    def is_bot(self, login: str | None) -> bool: ...


class NoBots:
    """No bot list (tests, or chat without one)."""

    def is_bot(self, login: str | None) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class ChannelBots:
    """The bots of one channel: the global list plus the channel's own."""

    logins: frozenset[str]

    def is_bot(self, login: str | None) -> bool:
        return bool(login) and login.lower() in self.logins


@dataclass(frozen=True, slots=True)
class BotList:
    """`bots.toml`: chat bots (lowercase logins) whose messages the detector leaves out of clean
    uniques (and counts in its "bots" share) and the judge leaves out of its chat summary.
    `version` = the file's `version`; `digest` = sha256 of the file's text (the detector puts it
    into `detector_version` through the lexicon tag)."""

    version: int
    logins: frozenset[str]
    channels: Mapping[str, frozenset[str]]  # channel login -> its own bots
    digest: str

    def for_channel(self, login: str | None) -> ChannelBots:
        own = self.channels.get(login.lower(), frozenset()) if login else frozenset()
        return ChannelBots(self.logins | own)


def parse_bots(text: str) -> BotList:
    """A `bots.toml` text: `version = N`, `logins = [...]`, `[channels.<login>] logins = [...]`."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"bots file is not valid TOML: {exc}") from exc
    if unknown := set(data) - {"version", "logins", "channels"}:
        raise ValueError(f"unknown keys in the bots file: {sorted(unknown)}")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("the bots file needs `version = N` (an integer >= 1)")
    channels: dict[str, frozenset[str]] = {}
    for login, table in (data.get("channels") or {}).items():
        if not isinstance(table, Mapping) or set(table) - {"logins"}:
            raise ValueError(f"`[channels.{login}]` takes only `logins = [...]`")
        channels[login.lower()] = _logins(table.get("logins", []), f"channels.{login}.logins")
    return BotList(
        version=version,
        logins=_logins(data.get("logins", []), "logins"),
        channels=channels,
        digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _logins(value: Any, where: str) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(f"`{where}` must be a list of non-empty strings")
    return frozenset(v.strip().lower() for v in value)


@cache
def _packaged_bots() -> BotList:
    text = resources.files("clipper_core").joinpath(BOTS_FILE).read_text(encoding="utf-8")
    return parse_bots(text.replace("\r\n", "\n"))


def load_bots(path: str | Path | None = None) -> BotList:
    """The packaged bot list (`clipper_core/bots.toml`), or another file in the same format."""
    if path is None:
        return _packaged_bots()
    return parse_bots(Path(path).read_text(encoding="utf-8").replace("\r\n", "\n"))


# --------------------------------------------------------------------------- writer


class ChatWriter:
    """Batch writes of chat_messages / chat_events / chat_metrics_1s.

    Messages: `ON CONFLICT DO NOTHING` (PK `sent_at, msg_id`), so re-writing a batch is safe.
    Metrics: upserted per `(channel_id, ts)`. Events: plain inserts. Creates the chat partitions
    of the days it writes (remembered per writer instance).

    `keep_foreign_metrics=True` (the backfill tool): an existing metrics row of another stream, or
    of no stream (live chat while offline), is left as it is instead of overwritten, so a writer
    never replaces the other source's seconds (one writer per table and source, DESIGN §17).
    """

    def __init__(self, *, keep_foreign_metrics: bool = False) -> None:
        self._partition_days: set[date] = set()
        self.keep_foreign_metrics = keep_foreign_metrics

    async def write(
        self,
        session: AsyncSession,
        messages: list[MessageRow],
        events: list[EventRow],
        metrics: list[MetricRow],
        deleted_msg_ids: list[str],
    ) -> None:
        """Everything in one transaction (the caller commits)."""
        if messages:
            await self._ensure_partitions(session, {m.sent_at.date() for m in messages})
            await session.execute(
                insert(ChatMessage).on_conflict_do_nothing(), [asdict(m) for m in messages]
            )
        if events:
            await session.execute(insert(ChatEvent), [asdict(e) for e in events])
        if metrics:
            stmt = insert(ChatMetricSecond)
            stmt = stmt.on_conflict_do_update(
                index_elements=["channel_id", "ts"],
                set_={
                    "stream_id": stmt.excluded.stream_id,
                    "messages": stmt.excluded.messages,
                    "unique_chatters": stmt.excluded.unique_chatters,
                    "emote_counts": stmt.excluded.emote_counts,
                    "event_counts": stmt.excluded.event_counts,
                },
                where=(
                    ChatMetricSecond.stream_id == stmt.excluded.stream_id
                    if self.keep_foreign_metrics
                    else None
                ),
            )
            await session.execute(stmt, [asdict(m) for m in metrics])
        if deleted_msg_ids:
            # Deletions refer to recent messages; the time bound keeps it to 1-2 partitions.
            newest = min((m.sent_at for m in messages), default=utc_now())
            await session.execute(
                text(
                    "UPDATE chat_messages SET deleted = true "
                    "WHERE msg_id = ANY(:ids) AND sent_at >= :since"
                ),
                {"ids": deleted_msg_ids, "since": newest - timedelta(days=1)},
            )

    async def _ensure_partitions(self, session: AsyncSession, days: set[date]) -> None:
        missing = days - self._partition_days
        if missing:
            await ensure_chat_partitions(session, min(missing), max(missing))
            self._partition_days |= missing
