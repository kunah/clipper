"""VOD chat replay -> `chat_messages` + `chat_metrics_1s` of a backfilled stream (DESIGN §17).

Same rows and the same per-second stats as live chat (`clipper_core.chat`: `MessageRow`,
`SecondAggregator.add_row`, `ChatWriter`), so the detector reads backfilled chat like live chat.
No `chat_events` (replay has no USERNOTICE/CLEAR*/ROOMSTATE) and no `viewer_snapshots`.

**Mapping** (`map_comment`): `msg_id` = comment id; `sent_at` = `createdAt` (the `tmi-sent-ts`
clock, ms), or the chat clock's `T_chat + offset + 0.5 s` when it is missing or more than 2 s from
that (`vods.flags.chat_time_fallbacks`); the user from `commenter` (a deleted user: kept, not a
chatter, `flags.comments_without_user`); Twitch emotes from the fragments, 7TV/BTTV/FFZ by token
with the channel's *current* sets (removed ones are lost); badges `{setID: version}`.

**Chat clock.** `createdAt = T_chat + contentOffsetSeconds + [0, 1)`: one 1 s band over 23 h,
offsets assumed truncated (§3). `T_chat` = the chat anchor frozen on the first run
(`vods.t0_candidates["chat"]`), else `T0 + 3.9 s` for a PDT T0, else T0. It sits ~4 s after the
media T0 (`T0_pdt`): the replay compensates stream latency (§17 "Time model").

**Watermark** (resume without double counting). Comments arrive in offset order. After a page
whose last comment has offset L, every comment with offset < L is known, and none still to come
can have `sent_at < T_chat + L - 1.5 s`. A batch commit (one transaction, lease checked) writes
- the messages with offset < L (those at L wait for the next batch): `vods.chat_done_s = L`;
- the metrics of the seconds before `M = second(T_chat + L - 3 s)`: `vods.chat_metrics_to = M`.
A resume pages from `R = M - T_chat - 3.5 s` (Twitch starts an offset page a few s earlier still),
counts only comments with `sent_at >= M` and inserts only those with offset >= `chat_done_s`.
So a crash anywhere loses at most one batch, and a re-run writes exactly what one run would have.

**Fill ranges** (`ChatRunner.run_ranges`, `vod --fill-gaps` in a LIVE stream, DESIGN §17): only the
whole seconds inside the fill ranges (`chat_spans`), each paged from `resume_offset(its start)`.
There the watermark is the time `M = vods.chat_metrics_to` alone: a batch commit writes the
messages with `sent_at < M` and the metrics of the seconds before `M` (`M = min(final_before(L),
range end)`), so every fill second before `M` is complete and nothing at or after it is written;
`chat_done_s` only records the offset reached. Seconds that already have a `chat_metrics_1s` row
(live chat-ingest was running) are left to it: neither their metrics nor their messages are
written (`flags.fill_chat_live_seconds`), and metrics are insert-only anyway
(`ChatWriter(keep_existing_metrics=True)`). Messages chat-ingest logged are skipped by the PK as
well (`sent_at = createdAt = tmi-sent-ts`).
"""

from __future__ import annotations

import asyncio
import math
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_backfill import rows
from clipper_backfill.pipeline import LeaseKeeper
from clipper_backfill.planner import at
from clipper_backfill.t0 import CHAT_MINUS_PDT_S
from clipper_core.chat import ChatWriter, MessageRow, SecondAggregator, second_of
from clipper_core.contracts import DataSource, T0Method, VodChatStatus
from clipper_core.logs import get_logger
from clipper_core.timeline import Interval, ensure_utc
from clipper_twitch.emotes import fetch_channel_emotes, fetch_global_emotes, message_emotes
from clipper_twitch.gql import ChatReplay, Comment, GqlUnavailable

log = get_logger(__name__)

BAND_MID_S = 0.5  # createdAt - T_chat - offset is in [0, 1): its middle
SENT_AT_TOLERANCE_S = 2.0  # createdAt farther than this from T_chat + offset + 0.5 s: fallback
FINAL_LAG_S = 3.0  # seconds before T_chat + L - this are complete (1.5 s spare)
FALLBACK_SHARE_FLAG = 0.05  # more fallbacks than this share of the VOD's messages: flag it
GUARD_MARGIN_S = 10.0  # around the VOD's range when looking for live chat (guard 2)
MAX_EMPTY_PAGES = 3  # `--fill-gaps`: empty pages that still have a next page, then give up


@dataclass(frozen=True, slots=True)
class ChatClock:
    """Where a comment's `createdAt` should lie for its VOD offset."""

    t0: datetime  # T_chat: createdAt = t0 + offset + [0, 1)
    source: str  # "chat_anchor" | "t0+3.9s" | the T0 method

    def expected(self, offset_s: int) -> datetime:
        return self.t0 + timedelta(seconds=offset_s + BAND_MID_S)

    def sent_at(self, comment: Comment) -> tuple[datetime, bool]:
        """(sent_at, fell back): `createdAt`, or the expected time when it is missing or off."""
        expected = self.expected(comment.content_offset_s)
        created = comment.created_at
        if created is None or abs((created - expected).total_seconds()) > SENT_AT_TOLERANCE_S:
            return expected, True
        return ensure_utc(created), False

    def final_before(self, offset_s: int) -> datetime:
        """Once every comment with an offset < `offset_s` is counted, the seconds before this
        are complete."""
        return second_of(self.t0 + timedelta(seconds=offset_s - FINAL_LAG_S))

    def resume_offset(self, metrics_to: datetime) -> int:
        """The offset to page from so that every comment with `sent_at >= metrics_to` is read."""
        latest = (metrics_to - self.t0).total_seconds() - BAND_MID_S - SENT_AT_TOLERANCE_S
        return max(0, math.floor(latest) - 1)


class EmoteSets(Protocol):
    """The third-party (7TV/BTTV/FFZ) emote names a replay is matched against: {name: provider}."""

    async def global_emotes(self) -> dict[str, str]: ...

    async def channel_emotes(self, twitch_id: str) -> dict[str, str]: ...

    def failed(self, twitch_id: str | None) -> list[str]:
        """Providers whose global or channel set could not be fetched (their emotes are not
        counted: `vods.flags.emote_sets_failed`)."""
        ...


class HttpEmoteSets:
    """The providers' *current* sets (`clipper_twitch.emotes`), fetched once per process (global)
    and once per channel. A provider that fails counts as empty (logged, like live) and is
    reported by `failed()`."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http
        self._global: dict[str, str] | None = None
        self._channels: dict[str, dict[str, str]] = {}
        self._failed: dict[str | None, list[str]] = {}  # None: the global sets

    async def global_emotes(self) -> dict[str, str]:
        if self._global is None:
            failed: list[str] = []
            self._global = await fetch_global_emotes(self.http, failed=failed)
            self._failed[None] = failed
            log.info("global emotes loaded", count=len(self._global), failed=failed)
        return self._global

    async def channel_emotes(self, twitch_id: str) -> dict[str, str]:
        if twitch_id not in self._channels:
            failed: list[str] = []
            self._channels[twitch_id] = await fetch_channel_emotes(
                self.http, twitch_id, failed=failed
            )
            self._failed[twitch_id] = failed
            log.info(
                "channel emotes loaded",
                twitch_id=twitch_id,
                count=len(self._channels[twitch_id]),
                failed=failed,
            )
        return self._channels[twitch_id]

    def failed(self, twitch_id: str | None) -> list[str]:
        return sorted({*self._failed.get(None, []), *self._failed.get(twitch_id, [])})


@dataclass(slots=True)
class StaticEmoteSets:
    """Fixed sets (tests; or none at all)."""

    global_set: dict[str, str] = field(default_factory=dict)
    channel_sets: dict[str, dict[str, str]] = field(default_factory=dict)
    failed_providers: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    async def global_emotes(self) -> dict[str, str]:
        self.calls.append("global")
        return dict(self.global_set)

    async def channel_emotes(self, twitch_id: str) -> dict[str, str]:
        self.calls.append(twitch_id)
        return dict(self.channel_sets.get(twitch_id, {}))

    def failed(self, twitch_id: str | None) -> list[str]:
        return sorted(self.failed_providers)


def clock_for(t0_utc: datetime, t0_method: str, t0_candidates: Mapping[str, Any]) -> ChatClock:
    chat = t0_candidates.get("chat")
    if chat:
        return ChatClock(ensure_utc(datetime.fromisoformat(chat)), "chat_anchor")
    if t0_method in (T0Method.PDT, T0Method.AUDIO):  # the media clock: chat sits ~3.9 s later
        return ChatClock(t0_utc + timedelta(seconds=CHAT_MINUS_PDT_S), "t0+3.9s")
    return ChatClock(t0_utc, t0_method)


def chat_clock(vod: rows.VodRow) -> ChatClock:
    return clock_for(vod.t0_utc, vod.t0_method, vod.t0_candidates)


def window_for(
    t0_utc: datetime, t0_method: str, t0_candidates: Mapping[str, Any], duration_s: float
) -> Interval:
    """Where a VOD's chat rows (and its media) can lie: guard 2 looks for live chat here."""
    chat_t0 = clock_for(t0_utc, t0_method, t0_candidates).t0
    margin = timedelta(seconds=GUARD_MARGIN_S)
    return Interval(
        min(t0_utc, chat_t0) - margin,
        max(t0_utc, chat_t0) + timedelta(seconds=duration_s) + margin,
    )


def chat_window(vod: rows.VodRow) -> Interval:
    return window_for(vod.t0_utc, vod.t0_method, vod.t0_candidates, vod.duration_s)


def chat_spans(ranges: Sequence[Interval]) -> list[Interval]:
    """The whole seconds inside each fill range, `[ceil(start), floor(end))` (empty ones left
    out): the seconds a `--fill-gaps` run may write chat for."""
    out = []
    for r in ranges:
        start = second_of(r.start)
        if start < r.start:
            start += timedelta(seconds=1)
        end = second_of(r.end)
        if start < end:
            out.append(Interval(start, end))
    return out


def map_comment(
    comment: Comment,
    *,
    channel_id: int,
    stream_id: int,
    clock: ChatClock,
    channel_emotes: Mapping[str, str],
    global_emotes: Mapping[str, str],
) -> tuple[MessageRow, bool]:
    """A replay comment as the `chat_messages` row live chat-ingest would have stored, and
    whether its `sent_at` is the clock's fallback."""
    sent_at, fallback = clock.sent_at(comment)
    text = comment.text
    emotes = message_emotes(text, comment.twitch_emotes(), channel_emotes, global_emotes)
    row = MessageRow(
        sent_at=sent_at,
        msg_id=comment.id,
        channel_id=channel_id,
        stream_id=stream_id,
        received_at=None,
        user_id=comment.commenter_id,
        user_login=comment.commenter_login,
        text=text,
        emotes=emotes or None,
        badges=dict(comment.badges) or None,
        source=str(DataSource.BACKFILL),
    )
    return row, fallback


@dataclass(slots=True)
class ChatResult:
    status: str  # VodChatStatus after the run
    start_offset: int = 0  # paged from
    done_s: int = 0  # vods.chat_done_s after the run
    metrics_to: datetime | None = None
    pages: int = 0
    batches: int = 0
    messages: int = 0  # inserted by this run (new)
    reread: int = 0  # comments before the old watermark, read again (resume)
    metric_rows: int = 0
    fallbacks: int = 0
    without_user: int = 0
    duplicates: int = 0
    late_dropped: int = 0
    live_seconds: int = 0  # `--fill-gaps`: seconds left as they were (they had a metrics row)
    chatters: set[str] = field(default_factory=set)
    emotes: Counter[str] = field(default_factory=Counter)  # by name, new messages
    emote_sources: dict[str, str] = field(default_factory=dict)
    stopped: bool = False


@dataclass(slots=True)
class _Pending:
    offset_s: int
    row: MessageRow
    fallback: bool


class ChatRunner:
    """Pages one VOD's replay into the live chat tables (see the module docstring)."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        gql: ChatReplay,
        lease: LeaseKeeper,
        *,
        batch_pages: int = 20,
        stop: asyncio.Event | None = None,
        after_commit: Callable[[int], Awaitable[Any]] | None = None,  # tests inject crashes
    ) -> None:
        self.sessions = sessions
        self.gql = gql
        self.lease = lease
        self.batch_pages = max(1, batch_pages)
        self.stop = stop
        self.after_commit = after_commit

    async def run(
        self,
        vod: rows.VodRow,
        *,
        channel_emotes: Mapping[str, str],
        global_emotes: Mapping[str, str],
        until_s: float | None = None,
    ) -> ChatResult:
        """From the watermark to the end of the replay, or until a page reaches `until_s`.

        Raises `ReplayUnavailable` (first page: no replay), `GqlError` (give up: a re-run
        resumes), `LeaseLost`.
        """
        clock = chat_clock(vod)
        old_done, old_metrics_to = vod.chat_done_s, vod.chat_metrics_to
        start = 0 if old_metrics_to is None else min(clock.resume_offset(old_metrics_to), old_done)
        state = _State(
            vod=vod,
            done_s=old_done,
            metrics_to=old_metrics_to,
            writer=ChatWriter(keep_foreign_metrics=True),
            aggregator=SecondAggregator(grace=timedelta(0)),
            result=ChatResult(status=vod.chat_status, start_offset=start, done_s=old_done),
        )
        result = state.result
        log.info(
            "chat replay",
            from_offset_s=start,
            done_s=old_done,
            metrics_to=old_metrics_to.isoformat() if old_metrics_to else None,
            clock=clock.source,
            t_chat=clock.t0.isoformat(),
        )
        await self._pages(state, clock, start, until_s, channel_emotes, global_emotes)
        if result.batches:  # (a crashed run leaves it to the next one)
            await self._flag_fallback_share(vod)
        return result

    async def _pages(
        self,
        state: _State,
        clock: ChatClock,
        start: int,
        until_s: float | None,
        channel_emotes: Mapping[str, str],
        global_emotes: Mapping[str, str],
    ) -> ChatResult:
        vod, result = state.vod, state.result
        old_done, old_metrics_to = vod.chat_done_s, vod.chat_metrics_to
        cursor: str | None = None
        recent: set[str] = set()  # ids of the previous page: Twitch should never repeat them
        last: int | None = None
        pages = 0
        while True:
            if self.stop is not None and self.stop.is_set():
                result.stopped = True
                if last is not None:
                    await self._commit(state, last)
                return result
            if cursor is None:
                page = await self.gql.page(vod.platform_video_id, offset_s=start)
            else:
                page = await self.gql.page(vod.platform_video_id, cursor=cursor)
            result.pages += 1
            pages += 1
            ids: set[str] = set()
            for comment in page.comments:
                if comment.id in recent or comment.id in ids:
                    result.duplicates += 1
                    continue
                ids.add(comment.id)
                row, fallback = map_comment(
                    comment,
                    channel_id=vod.channel_id,
                    stream_id=vod.stream_id,
                    clock=clock,
                    channel_emotes=channel_emotes,
                    global_emotes=global_emotes,
                )
                if old_metrics_to is None or row.sent_at >= old_metrics_to:
                    state.aggregator.add_row(row)  # its second is not final yet
                if comment.content_offset_s >= old_done:
                    state.pending.append(_Pending(comment.content_offset_s, row, fallback))
                else:
                    result.reread += 1  # committed by an earlier run
                last = comment.content_offset_s
            recent = ids
            if not page.has_next_page:
                await self._commit(state, last, final=True)
                return result
            reached = until_s is not None and last is not None and last >= until_s
            if last is not None and (reached or pages >= self.batch_pages):
                await self._commit(state, last)
                pages = 0
                if reached:
                    return result
            cursor = page.cursor

    async def run_ranges(
        self,
        vod: rows.VodRow,
        spans: Sequence[Interval],
        *,
        channel_emotes: Mapping[str, str],
        global_emotes: Mapping[str, str],
        budget_s: float | None = None,
    ) -> ChatResult:
        """`--fill-gaps`: the replay over `spans` only (whole seconds, `chat_spans`), from the
        watermark `vods.chat_metrics_to` on (see the module docstring), until every span is done
        or `budget_s` seconds of them are (`--max-minutes`, `--chat-max-minutes`).

        Raises `ReplayUnavailable` (a range's first page: no replay), `GqlError` (give up: a re-run
        resumes), `LeaseLost`.
        """
        clock = chat_clock(vod)
        mark = vod.chat_metrics_to
        todo = [sp for sp in spans if mark is None or sp.end > mark]
        state = _RangeState(
            vod=vod,
            clock=clock,
            mark=mark,
            done_s=vod.chat_done_s,
            writer=ChatWriter(keep_existing_metrics=True),
            result=ChatResult(status=vod.chat_status, done_s=vod.chat_done_s, metrics_to=mark),
        )
        result = state.result
        log.info(
            "chat replay of the fill ranges",
            ranges=len(todo),
            metrics_to=mark.isoformat() if mark else None,
            clock=clock.source,
            t_chat=clock.t0.isoformat(),
        )
        for i, span in enumerate(todo):
            begin = span.start if state.mark is None else max(span.start, state.mark)
            start = clock.resume_offset(begin)
            if i == 0:
                result.start_offset = start
            finished = await self._range_pages(
                state, span, begin, start, channel_emotes, global_emotes, budget_s
            )
            if not finished:
                break
        else:
            await self._ranges_done(state, spans)
        if result.batches:
            await self._flag_fallback_share(vod)
        return result

    async def _range_pages(
        self,
        state: _RangeState,
        span: Interval,
        begin: datetime,
        start: int,
        channel_emotes: Mapping[str, str],
        global_emotes: Mapping[str, str],
        budget_s: float | None,
    ) -> bool:
        """One span from `begin`: True when it is complete, False when stopped or out of
        budget (both committed up to where they got)."""
        vod, clock, result = state.vod, state.clock, state.result
        aggregator = SecondAggregator(grace=timedelta(0))
        state.late_counted = 0
        pending: list[_Pending] = []
        cursor: str | None = None
        recent: set[str] = set()
        last: int | None = None
        pages = empty = 0
        while True:
            if self.stop is not None and self.stop.is_set():
                result.stopped = True
                if last is not None:
                    await self._range_commit(state, aggregator, pending, last, upto=None, span=span)
                return False
            if cursor is None:
                page = await self.gql.page(vod.platform_video_id, offset_s=start)
            else:
                page = await self.gql.page(vod.platform_video_id, cursor=cursor)
            result.pages += 1
            pages += 1
            ids: set[str] = set()
            for comment in page.comments:
                if comment.id in recent or comment.id in ids:
                    result.duplicates += 1
                    continue
                ids.add(comment.id)
                last = comment.content_offset_s
                row, fallback = map_comment(
                    comment,
                    channel_id=vod.channel_id,
                    stream_id=vod.stream_id,
                    clock=clock,
                    channel_emotes=channel_emotes,
                    global_emotes=global_emotes,
                )
                if begin <= row.sent_at < span.end:  # outside: another range's, or live time
                    aggregator.add_row(row)
                    pending.append(_Pending(comment.content_offset_s, row, fallback))
            if not page.has_next_page or (
                last is not None and clock.final_before(last) >= span.end
            ):  # the replay ends, or every second of the span is final
                await self._range_commit(state, aggregator, pending, last, upto=span.end, span=span)
                return True
            if not page.comments:  # a next page but nothing on this one: ask the same again
                empty += 1
                if empty >= MAX_EMPTY_PAGES:
                    raise GqlUnavailable(
                        f"{empty} empty chat pages in a row that say more follow (a re-run resumes)"
                    )
                continue
            empty = 0
            recent = ids
            reach = min(clock.final_before(last), span.end)
            over = (
                budget_s is not None
                and state.spent_s + _seconds_past(reach, state.mark, begin) >= budget_s
            )
            if pages >= self.batch_pages or over:
                await self._range_commit(state, aggregator, pending, last, upto=reach, span=span)
                pages = 0
                if over:
                    return False
            cursor = page.cursor

    async def _range_commit(
        self,
        state: _RangeState,
        aggregator: SecondAggregator,
        pending: list[_Pending],
        last: int | None,
        *,
        upto: datetime | None,
        span: Interval,
    ) -> None:
        """One transaction: the span's messages and metrics before the new watermark `M`
        (`upto`, or what `last` makes final), except the seconds that already have a metrics row,
        and `chat_metrics_to = M`."""
        vod, result = state.vod, state.result
        lo = span.start if state.mark is None else max(span.start, state.mark)
        if upto is None:
            upto = min(state.clock.final_before(last or 0), span.end)
        mark = max(upto, lo)
        batch = [p for p in pending if p.row.sent_at < mark]
        pending[:] = [p for p in pending if p.row.sent_at >= mark]
        metrics = aggregator.pop_ready(mark)
        done_s = max(state.done_s, last or 0)
        late = aggregator.late_dropped - state.late_counted
        async with self.sessions.begin() as s:
            await self.lease.check(s)
            taken = (
                await rows.metric_seconds(s, channel_id=vod.channel_id, window=Interval(lo, mark))
                if lo < mark
                else set()
            )
            batch = [p for p in batch if second_of(p.row.sent_at) not in taken]
            metrics = [m for m in metrics if m.ts not in taken]
            await state.writer.write(s, [p.row for p in batch], [], metrics, [])
            await rows.update_vod(
                s,
                vod.id,
                chat_done_s=done_s,
                chat_metrics_to=mark,
                chat_status=str(VodChatStatus.PENDING),
            )
            fallbacks = sum(p.fallback for p in batch)
            without_user = sum(1 for p in batch if not (p.row.user_id or p.row.user_login))
            for key, amount in (
                ("chat_time_fallbacks", fallbacks),
                ("comments_without_user", without_user),
                ("chat_late_dropped", late),
                ("fill_chat_live_seconds", len(taken)),
            ):
                if amount:
                    await rows.add_to_flag(s, vod.id, key, amount)
        state.spent_s += (mark - lo).total_seconds()
        state.late_counted = aggregator.late_dropped
        state.mark, state.done_s = mark, done_s
        result.status, result.done_s, result.metrics_to = str(VodChatStatus.PENDING), done_s, mark
        result.batches += 1
        result.messages += len(batch)
        result.metric_rows += len(metrics)
        result.fallbacks += fallbacks
        result.without_user += without_user
        result.late_dropped += late
        result.live_seconds += len(taken)
        for p in batch:
            if p.row.user_id or p.row.user_login:
                result.chatters.add(p.row.user_id or p.row.user_login)
            for e in p.row.emotes or ():
                result.emotes[e["name"]] += e["count"]
                result.emote_sources.setdefault(e["name"], e["source"])
        log.info(
            "chat batch saved",
            messages=len(batch),
            metric_seconds=len(metrics),
            live_seconds_kept=len(taken),
            done_s=done_s,
            metrics_to=mark.isoformat(),
            range_end=span.end.isoformat(),
        )
        if self.after_commit is not None:
            await self.after_commit(result.batches)

    async def _ranges_done(self, state: _RangeState, spans: Sequence[Interval]) -> None:
        """Every span is complete: `chat_status = done`."""
        vod, result = state.vod, state.result
        ends = [second_of(at(vod.t0_utc, vod.duration_s))]
        ends += [sp.end for sp in spans[-1:]]
        if state.mark is not None:
            ends.append(state.mark)
        metrics_to = max(ends)
        done_s = max(state.done_s, math.ceil(vod.duration_s))
        async with self.sessions.begin() as s:
            await self.lease.check(s)
            await rows.update_vod(
                s,
                vod.id,
                chat_done_s=done_s,
                chat_metrics_to=metrics_to,
                chat_status=str(VodChatStatus.DONE),
            )
        state.mark, state.done_s = metrics_to, done_s
        result.status, result.done_s, result.metrics_to = (
            str(VodChatStatus.DONE),
            done_s,
            metrics_to,
        )

    async def _flag_fallback_share(self, vod: rows.VodRow) -> None:
        """`flags.chat_time_fallback_share` while more than 5 % of the VOD's committed messages
        got the clock's time instead of `createdAt` (a broken clock or anchor); removed below."""
        async with self.sessions.begin() as s:
            await self.lease.check(s)
            messages = await rows.stream_message_count(s, vod.stream_id)
            fallbacks = await rows.flag_number(s, vod.id, "chat_time_fallbacks")
            share = fallbacks / messages if messages else 0.0
            if share > FALLBACK_SHARE_FLAG:
                await rows.merge_flags(s, vod.id, {"chat_time_fallback_share": round(share, 4)})
                log.warning("many chat time fallbacks", share=round(share, 4), messages=messages)
            else:
                await rows.drop_flags(s, vod.id, "chat_time_fallback_share")

    async def _commit(self, state: _State, last: int | None, *, final: bool = False) -> None:
        """One transaction: messages, final metrics, watermark, flag counters."""
        vod, result = state.vod, state.result
        clock = chat_clock(vod)
        if final:
            batch, state.pending = state.pending, []
            metrics = state.aggregator.pop_all()
            ends = [clock.t0 + timedelta(seconds=math.ceil(vod.duration_s) + FINAL_LAG_S)]
            ends += [vod.t0_utc + timedelta(seconds=vod.duration_s)]
            if metrics:
                ends.append(metrics[-1].ts + timedelta(seconds=1))
            if state.metrics_to is not None:
                ends.append(state.metrics_to)
            metrics_to = second_of(max(ends))
            done_s = max(state.done_s, math.ceil(vod.duration_s), (last or 0) + 1)
            status = str(VodChatStatus.DONE)
        else:
            assert last is not None
            batch = [p for p in state.pending if p.offset_s < last]
            state.pending = [p for p in state.pending if p.offset_s >= last]
            metrics_to = clock.final_before(last)
            if state.metrics_to is not None:
                metrics_to = max(metrics_to, state.metrics_to)
            metrics = state.aggregator.pop_ready(metrics_to)
            done_s = max(state.done_s, last)
            status = str(VodChatStatus.PENDING)
        fallbacks = sum(p.fallback for p in batch)
        without_user = sum(1 for p in batch if not (p.row.user_id or p.row.user_login))
        late = state.aggregator.late_dropped - result.late_dropped
        async with self.sessions.begin() as s:
            await self.lease.check(s)
            await state.writer.write(s, [p.row for p in batch], [], metrics, [])
            await rows.update_vod(
                s, vod.id, chat_done_s=done_s, chat_metrics_to=metrics_to, chat_status=status
            )
            for key, amount in (
                ("chat_time_fallbacks", fallbacks),
                ("comments_without_user", without_user),
                ("chat_late_dropped", late),
            ):
                if amount:
                    await rows.add_to_flag(s, vod.id, key, amount)
        state.done_s, state.metrics_to = done_s, metrics_to
        result.status, result.done_s, result.metrics_to = status, done_s, metrics_to
        result.batches += 1
        result.messages += len(batch)
        result.metric_rows += len(metrics)
        result.fallbacks += fallbacks
        result.without_user += without_user
        result.late_dropped += late
        for p in batch:
            if p.row.user_id or p.row.user_login:
                result.chatters.add(p.row.user_id or p.row.user_login)
            for e in p.row.emotes or ():
                result.emotes[e["name"]] += e["count"]
                result.emote_sources.setdefault(e["name"], e["source"])
        log.info(
            "chat batch saved",
            messages=len(batch),
            metric_seconds=len(metrics),
            done_s=done_s,
            metrics_to=metrics_to.isoformat(),
            final=final,
        )
        if self.after_commit is not None:
            await self.after_commit(result.batches)


@dataclass(slots=True)
class _RangeState:
    vod: rows.VodRow
    clock: ChatClock
    mark: datetime | None  # fill seconds before it are committed
    done_s: int
    writer: ChatWriter
    result: ChatResult
    spent_s: float = 0.0  # span seconds committed this run (the budget)
    late_counted: int = 0  # of the current span's aggregator, already in flags


def _seconds_past(reach: datetime, mark: datetime | None, begin: datetime) -> float:
    """Span seconds a commit up to `reach` would add (from the watermark, or the span's start)."""
    lo = begin if mark is None else max(begin, mark)
    return max(0.0, (reach - lo).total_seconds())


@dataclass(slots=True)
class _State:
    vod: rows.VodRow
    done_s: int
    metrics_to: datetime | None
    writer: ChatWriter
    aggregator: SecondAggregator
    result: ChatResult
    pending: list[_Pending] = field(default_factory=list)


def result_lines(result: ChatResult, duration_s: float) -> list[str]:
    top = ", ".join(
        f"{name} {n} ({result.emote_sources.get(name, '?')})"
        for name, n in result.emotes.most_common(8)
    )
    lines = [
        f"chat {result.status} at {result.done_s}/{math.ceil(duration_s)} s: {result.messages} "
        f"new messages ({len(result.chatters)} chatters) from {result.pages} pages since offset "
        f"{result.start_offset} s, {result.metric_rows} metric seconds, final before "
        f"{result.metrics_to.isoformat() if result.metrics_to else '-'}"
        + (" (stopped)" if result.stopped else ""),
        f"  emotes: {sum(result.emotes.values())} ({len(result.emotes)} names): {top or '-'}",
    ]
    notes = {
        "read again": result.reread,
        "time fallbacks": result.fallbacks,
        "without user": result.without_user,
        "duplicates": result.duplicates,
        "late (not counted)": result.late_dropped,
        "seconds with live chat kept": result.live_seconds,
    }
    if any(notes.values()):
        lines.append("  " + ", ".join(f"{k}: {v}" for k, v in notes.items() if v))
    return lines
