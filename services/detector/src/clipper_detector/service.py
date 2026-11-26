"""Live detector service (DESIGN §18.9): one process for all channels it holds a lease on.

Loops (each serialised with the others by one asyncio lock; time comes from `clock`, so tests
step it):
- **live (1 s)**: one query for all held channels, `chat_metrics_1s` from each channel's cursor
  (per-channel ranges) up to the final cutoff `ts < now - final_lag` (10 s: chat-ingest's 5 s
  grace + 1 s flush, plus margin), and no further than the newest row seen + 1 s (chat-ingest
  writes every channel's second in one flush) unless that row is far behind (all quiet, or
  chat-ingest down). Every second up to there is fed to the channel's `ChannelScorer` in ts order
  (no row = 0), with the stream assigned by time from `streams` and the viewer count of the
  latest `viewer_snapshots` row at or before the second (backfilled streams: viewers and
  chat_events unknown). The read also covers the last 30 s before the cursor: a row for a second
  already fed with other values is counted as late (`late_rows`, heartbeat). A closed episode goes
  through stage 2 (raw `chat_messages` + `chat_events` of its windows), the replay dedupe and
  `merge.plan_candidate`, all its writes in one transaction (`writes`).
- **explicit (5 s)**: new raw messages of held channels -> clip mentions and clip links
  (`explicit`); pending signals attach to a candidate within +- 60 s (re-emitting a suppressed
  one), wait while an episode may still claim them, else become candidates of their own.
- **clips (60 s)**: Helix Get Clips per held live channel, only with Twitch credentials.
- **sync (10 s)**: active channels, leases `detect:channel:{id}` (TTL 60 s, renewed every 20 s),
  their streams. A newly held channel replays `[now - 21 min, final]` from `chat_metrics_1s` and
  emits only episodes peaking in the last 15 min that no candidate has decided already: inside a
  candidate's chat span (extended by its folded follow-ups), within +- 60 s of its peak or a
  follow-up's peak (episodes of the replay era), or +- 60 s of its video peak (every draft).
- **sweep (60 s)**: the detector's media requests -> fulfilled (coverage complete, or 15 min
  after the window's end).
- **heartbeat (15 s)**: versions, channels held, lag, emitted/suppressed by reason over 1 h.

State lives in memory only; a channel whose processing failed is rebuilt (replayed) on the next
sync, which the dedupe makes idempotent.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_core import heartbeat, leases
from clipper_core.contracts import CandidateStatus, detect_lease
from clipper_core.logs import get_logger, log_context
from clipper_core.timeline import utc_now
from clipper_detector import SCORER_VERSION, reads, writes
from clipper_detector.explicit import (
    ClipTracker,
    MentionTracker,
    Signal,
    ViewerClip,
    clip_links,
    explicit_features,
    from_vod_later,
    signal_draft,
    total_boost,
)
from clipper_detector.lexicon import ChannelLexicon, Lexicon
from clipper_detector.merge import (
    CandidateDraft,
    Delay,
    FoldIn,
    PriorCandidate,
    plan_candidate,
    resolve_delay,
)
from clipper_detector.params import DetectorParams, detector_version
from clipper_detector.reads import ChannelInfo, StreamInfo
from clipper_detector.scorer import ChannelScorer, Episode, MissingChat, Second, Suspect
from clipper_detector.settings import DetectorSettings
from clipper_detector.stage2 import Stage2Result, evaluate, windows

log = get_logger(__name__)

SERVICE = "detector"
Clock = Callable[[], datetime]
ONE = timedelta(seconds=1)
VIEWER_LOOKBACK = timedelta(seconds=120)  # the latest snapshot before a stream's first second
# An earlier process's peak explains a replayed episode when it lies in the episode's burst
# `[onset - this, end + this]` (replay dedupe, `LiveDetector._decided_by`).
BURST_PAD_S = 10


def final_end(now: datetime, lag_s: float) -> datetime:
    """Exclusive whole-second bound of the final seconds: rows with `ts < now - lag` are final,
    and rows sit on whole seconds, so that is `ts < ceil(now - lag)`."""
    f = now - timedelta(seconds=lag_s)
    whole = f.replace(microsecond=0)
    return whole if whole == f else whole + ONE


def watermark(rows: Iterable[Row], until: datetime, slack_s: float) -> datetime:
    """`until`, or the newest row + 1 s when that is behind but within `slack_s` of it: seconds
    after the newest row may not be written yet (chat-ingest writes a second of every channel in
    one flush). Further behind means all quiet or chat-ingest down: the clock decides."""
    newest = max((r.ts for r in rows), default=None)
    if newest is None:
        return until
    mark = newest + ONE
    if mark < until and (until - mark).total_seconds() <= slack_s:
        return mark
    return until


@dataclass
class LiveStream:
    """Scoring state of the stream a channel is in."""

    info: StreamInfo
    scorer: ChannelScorer
    delay: Delay
    recent: list[PriorCandidate]  # the stream's candidates (cooldown, dedupe)
    viewers: list[tuple[datetime, int]] | None  # None: unknown (backfilled stream)
    viewers_until: datetime  # snapshots before this are loaded
    video_id: str | None = None  # the live VOD's id, learned from Helix clips
    # Peaks (candidate id, chat peak) of the episodes an earlier process decided on this stream
    # (candidates and their follow-ups, when this scorer started): what a replay may re-detect.
    # Each explains at most one replayed episode: a dedupe claims (removes) it.
    unclaimed: list[tuple[int, datetime]] = field(default_factory=list)
    _vi: int = 0

    def viewer_at(self, t: datetime) -> int | None:
        v = self.viewers
        if not v:
            return None
        while self._vi + 1 < len(v) and v[self._vi + 1][0] <= t:
            self._vi += 1
        return v[self._vi][1] if v[self._vi][0] <= t else None

    def add_viewers(self, rows: Iterable[Row], until: datetime) -> None:
        if self.viewers is None:
            return
        last = self.viewers[-1][0] if self.viewers else None
        self.viewers.extend((r.ts, int(r.viewers)) for r in rows if last is None or r.ts > last)
        self.viewers_until = max(self.viewers_until, until)

    def claim(
        self, lo: datetime, hi: datetime, near: datetime, candidate_id: int | None = None
    ) -> int | None:
        """Claim (remove) the unclaimed earlier peak in `[lo, hi]` nearest to `near` (of
        `candidate_id` only, when given). Returns its candidate id, None if there was none."""
        found = [
            e
            for e in self.unclaimed
            if lo <= e[1] <= hi and (candidate_id is None or e[0] == candidate_id)
        ]
        if not found:
            return None
        entry = min(found, key=lambda e: abs(e[1] - near))
        self.unclaimed.remove(entry)
        return entry[0]

    def update_recent(self, prior: PriorCandidate) -> None:
        for i, c in enumerate(self.recent):
            if c.candidate_id == prior.candidate_id:
                self.recent[i] = prior
                return
        self.recent.append(prior)

    def prior_of(self, draft: CandidateDraft, candidate_id: int) -> PriorCandidate:
        d = self.delay.td
        return PriorCandidate(
            candidate_id,
            draft.status,
            draft.chat_peak_utc or draft.peak_utc + d,
            draft.chat_end_utc or draft.moment_to + d,
            draft.score,
            draft.chat_onset_utc,
        )


@dataclass
class ChannelLive:
    """Everything the detector keeps for one held channel."""

    info: ChannelInfo
    lexicon: ChannelLexicon
    mentions: MentionTracker
    clips: ClipTracker
    streams: list[StreamInfo] = field(default_factory=list)
    ready: bool = False  # built (replayed); False: rebuild on the next sync
    cursor: datetime | None = None  # chat_metrics_1s seconds before this are processed
    emit_from: datetime | None = None  # episodes peaking earlier were replayed, not emitted
    replay_until: datetime | None = None  # the end of the last replay (the "replay era")
    raw_cursor: datetime | None = None  # raw chat before this was scanned for signals
    live: LiveStream | None = None
    pending: list[Signal] = field(default_factory=list)
    lease_renewed: datetime | None = None
    fed: dict[datetime, int] = field(default_factory=dict)  # recent seconds: uniques fed
    late_rows: int = 0

    def stream_at(self, t: datetime) -> StreamInfo | None:
        for s in reversed(self.streams):
            if s.contains(t):
                return s
        return None

    def set_streams(self, streams: list[StreamInfo]) -> bool:
        """New stream list; True if the channel must be rebuilt (a stream turned up that
        already runs at the cursor but isn't the one being scored: its row came late)."""
        self.streams = streams
        if self.live is not None:
            for s in streams:
                if s.id == self.live.info.id:
                    self.live.info = s
        if not self.ready or self.cursor is None:
            return False
        current = self.stream_at(self.cursor - ONE)
        if current is None:
            return False
        return self.live is None or self.live.info.id != current.id

    def take_signals(self, near: datetime, within_s: float, delay: Delay) -> list[Signal]:
        within = timedelta(seconds=within_s)
        taken = [s for s in self.pending if abs(s.chat_at(delay.td) - near) <= within]
        self.pending = [s for s in self.pending if s not in taken]
        return taken


@dataclass(frozen=True, slots=True)
class _Outcome:
    """What one episode's transaction did (logged and counted after the commit)."""

    kind: str  # duplicate | folded | written
    candidate_id: int | None
    prior: PriorCandidate | None = None  # for `recent`
    attached: writes.Attached | None = None
    written: writes.Written | None = None
    draft: CandidateDraft | None = None


class LiveDetector:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        settings: DetectorSettings,
        params: DetectorParams,
        lexicon: Lexicon,
        worker_id: str,
        helix: Any | None = None,  # clipper_twitch.helix.HelixClient (or a fake with get_clips)
        clock: Clock = utc_now,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self.params = params
        self.lexicon = lexicon
        self.worker_id = worker_id
        self.helix = helix
        self.clock = clock
        self.version = detector_version(params, lexicon)
        self.channels: dict[int, ChannelLive] = {}
        self._lock = asyncio.Lock()
        self._counts: deque[tuple[datetime, str]] = deque()
        self._started_at = clock()

    @property
    def live_mode(self) -> bool:
        return self.settings.mode in ("live", "all")

    # ------------------------------------------------------------------ lifecycle

    async def run(self, stop: asyncio.Event) -> None:
        st = self.settings
        log.info(
            "detector starting",
            mode=st.mode,
            detector_version=self.version,
            judge_jobs=st.judge_jobs,
            viewer_clips=self.helix is not None,
        )
        if st.mode in ("offline", "all"):
            log.warning(
                "offline pass not built yet (DESIGN §18 build item 5): no detect_offline jobs run"
            )
        loops: list[tuple[float, Callable[[], Awaitable[Any]]]] = []
        if self.live_mode:
            loops += [
                (st.sync_interval_s, self.sync),
                (st.live_interval_s, self.live_tick),
                (st.explicit_interval_s, self.explicit_tick),
                (st.sweep_interval_s, self.sweep),
            ]
            if self.helix is not None:
                loops.append((st.clips_interval_s, self.poll_clips))
        loops.append((st.heartbeat_interval_s, self.beat))
        tasks = [asyncio.create_task(self._every(stop, i, fn)) for i, fn in loops]
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.release_all()
            log.info("detector stopped", **self._last_hour())

    async def release_all(self) -> None:
        try:
            async with self.sessions.begin() as session:
                await session.execute(
                    text("DELETE FROM leases WHERE owner = :o AND resource LIKE 'detect:%'"),
                    {"o": self.worker_id},
                )
        except Exception:
            log.warning("could not release detect leases (they expire)", exc_info=True)
        self.channels.clear()

    @staticmethod
    async def _every(stop: asyncio.Event, interval: float, fn) -> None:
        while not stop.is_set():
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("detector loop failed", loop=getattr(fn, "__name__", "?"))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)

    # ------------------------------------------------------------------ sync: leases, streams

    async def sync(self) -> None:
        async with self._lock:
            await self._sync()

    async def _sync(self) -> None:
        st, p = self.settings, self.params
        now = self.clock()
        ttl = timedelta(seconds=st.lease_ttl_s)
        renew = timedelta(seconds=st.lease_renew_s)
        async with self.sessions() as session:
            active = await reads.active_channels(session)
        async with self.sessions.begin() as session:
            for cid in list(self.channels):
                state = self.channels[cid]
                if cid not in active:
                    await leases.release(session, detect_lease(cid), self.worker_id)
                    del self.channels[cid]
                    log.info("channel no longer active", channel=state.info.login)
                    continue
                if active[cid].login != state.info.login:
                    state.lexicon = self.lexicon.for_channel(active[cid].login)
                state.info = active[cid]
                if state.lease_renewed is not None and now - state.lease_renewed < renew:
                    continue
                if await leases.acquire(session, detect_lease(cid), self.worker_id, ttl):
                    state.lease_renewed = now
                else:
                    del self.channels[cid]
                    log.warning("detect lease lost", channel=state.info.login)
            for cid, info in active.items():
                if cid in self.channels:
                    continue
                if len(self.channels) >= st.max_channels:
                    log.warning("max channels reached", channel=info.login)
                    break
                if not await leases.acquire(session, detect_lease(cid), self.worker_id, ttl):
                    continue  # another detector scores it
                lexicon = self.lexicon.for_channel(info.login)
                self.channels[cid] = ChannelLive(
                    info, lexicon, MentionTracker(p, lexicon), ClipTracker(p), lease_renewed=now
                )
                log.info("channel taken", channel=info.login, channel_id=cid)
        if not self.channels:
            return
        until = final_end(now, p.final_lag_s)
        since = until - timedelta(seconds=p.restart_replay_s + 60)
        async with self.sessions() as session:
            by_channel = await reads.streams(session, list(self.channels), since)
        for state in list(self.channels.values()):
            rebuild = state.set_streams(by_channel.get(state.info.id, []))
            if state.ready and not rebuild:
                continue
            with log_context(channel=state.info.login, channel_id=state.info.id):
                if rebuild:
                    log.info("stream turned up late, replaying the channel")
                try:
                    await self._build(state, now)
                except Exception:
                    state.ready = False
                    log.exception("channel replay failed, retrying on the next sync")

    async def _build(self, state: ChannelLive, now: datetime) -> None:
        """(Re)start scoring: replay `[now - 21 min, final]`; only episodes peaking in the last
        15 min may emit."""
        p = self.params
        until = final_end(now, p.final_lag_s)
        state.ready = False
        state.live = None
        state.fed = {}
        state.emit_from = until - timedelta(seconds=p.restart_emit_s)
        if state.raw_cursor is None:
            state.raw_cursor = state.emit_from
        state.cursor = until - timedelta(seconds=p.restart_replay_s)
        async with self.sessions() as session:
            rows = await reads.metrics(session, {state.info.id: state.cursor}, until)
        until = watermark(rows, until, self.settings.watermark_slack_s)
        state.replay_until = until
        await self._feed(state, rows, until)
        state.ready = True
        log.info(
            "channel scoring",
            stream_id=state.live.info.id if state.live else None,
            replayed_rows=len(rows),
            emit_from=state.emit_from,
        )

    # ------------------------------------------------------------------ live: stage 1

    async def live_tick(self) -> None:
        async with self._lock:
            await self._live_tick()

    async def _live_tick(self) -> None:
        p, st = self.params, self.settings
        until = final_end(self.clock(), p.final_lag_s)
        states = [s for s in self.channels.values() if s.ready and s.cursor and s.cursor < until]
        if not states:
            return
        check = timedelta(seconds=st.late_check_s)
        ranges = {s.info.id: s.cursor - check for s in states}
        lives = [s.live for s in states if s.live is not None and s.live.viewers is not None]
        async with self.sessions() as session:
            rows = await reads.metrics(session, ranges, until)
            vrows = []
            if lives:
                vrows = await reads.viewer_snapshots(
                    session, {lv.info.id: lv.viewers_until for lv in lives}, until
                )
        until = watermark(rows, until, st.watermark_slack_s)
        for lv in lives:
            lv.add_viewers((r for r in vrows if r.stream_id == lv.info.id), until)
        by_channel: dict[int, list[Row]] = {}
        for r in rows:
            by_channel.setdefault(r.channel_id, []).append(r)
        for state in states:
            with log_context(channel=state.info.login, channel_id=state.info.id):
                mine = by_channel.get(state.info.id, [])
                self._check_late(state, mine)
                try:
                    await self._feed(state, mine, until)
                except Exception:
                    state.ready = False
                    log.exception("channel scoring failed, replaying on the next sync")

    def _check_late(self, state: ChannelLive, rows: list[Row]) -> None:
        """Rows of seconds already fed with other values: they came after the final cutoff."""
        assert state.cursor is not None
        late = []
        for r in rows:
            if r.ts >= state.cursor or r.ts not in state.fed:
                continue
            if state.fed[r.ts] != r.unique_chatters:
                late.append(r.ts)
                state.fed[r.ts] = r.unique_chatters  # count each once
        if late:
            state.late_rows += len(late)
            for _ in late:
                self._count("late_row")
            log.warning(
                "late chat_metrics_1s rows: seconds were fed before their row arrived",
                count=len(late),
                first=min(late),
                lag_s=round((self.clock() - min(late)).total_seconds(), 1),
            )

    async def _feed(self, state: ChannelLive, rows: list[Row], until: datetime) -> None:
        assert state.cursor is not None
        by_ts = {r.ts: r for r in rows if r.ts >= state.cursor}
        t = state.cursor
        while t < until:
            row = by_ts.get(t)
            state.fed[t] = row.unique_chatters if row is not None else 0
            await self._second(state, t, row, until)
            t += ONE
        state.cursor = max(state.cursor, until)
        horizon = state.cursor - timedelta(seconds=self.settings.late_check_s)
        state.fed = {ts: v for ts, v in state.fed.items() if ts >= horizon}

    async def _second(
        self, state: ChannelLive, t: datetime, row: Row | None, until: datetime
    ) -> None:
        stream = state.stream_at(t)
        if state.live is not None and (stream is None or stream.id != state.live.info.id):
            await self._close_stream(state)
        if stream is None:
            return
        if state.live is None:
            state.live = await self._open_stream(state, stream, t, until)
        live = state.live
        if row is None:
            second = Second(t, 0, 0, live.viewer_at(t), 0)
        else:
            second = Second(t, row.unique_chatters, row.messages, live.viewer_at(t), row.toggles)
        for event in live.scorer.step(second):
            await self._on_event(state, event)

    async def _open_stream(
        self, state: ChannelLive, stream: StreamInfo, t: datetime, until: datetime
    ) -> LiveStream:
        p = self.params
        async with self.sessions() as session:
            learned = await reads.learned_delays(
                session, state.info.id, stream.started_at, p.learned_delay_count
            )
            delay = resolve_delay(state.info.chat_delay_s, learned, p)
            # The prior (previous stream) only matters from the stream's start; a replay that
            # starts later has 20 min of own history anyway.
            from_start = t - stream.started_at <= timedelta(seconds=p.activity_window_s)
            prior = await reads.prior_baseline(session, stream, p) if from_start else None
            refs = await reads.stream_candidates(session, stream.id)
            viewers: list[tuple[datetime, int]] | None = None
            if stream.live:
                vrows = await reads.viewer_snapshots(
                    session, {stream.id: t - VIEWER_LOOKBACK}, until
                )
                viewers = [(r.ts, int(r.viewers)) for r in vrows]
        log.info(
            "stream scorer started",
            stream_id=stream.id,
            stream_source=stream.source,
            delay_s=delay.seconds,
            delay_source=delay.source,
            prior=None if prior is None else round(prior.activity, 1),
            candidates=len(refs),
        )
        scorer = ChannelScorer(p, started_at=stream.started_at, prior=prior)
        recent = [r.prior for r in refs]
        unclaimed = [
            (c.candidate_id, peak)
            for c in recent
            if c.candidate_id is not None
            for peak in (c.chat_peak, *c.followup_peaks)
        ]
        return LiveStream(stream, scorer, delay, recent, viewers, until, unclaimed=unclaimed)

    async def _close_stream(self, state: ChannelLive) -> None:
        live = state.live
        assert live is not None
        for event in live.scorer.flush():
            await self._on_event(state, event)
        state.live = None
        log.info("stream scorer closed", stream_id=live.info.id)

    async def _on_event(self, state: ChannelLive, event: Any) -> None:
        stream_id = state.live.info.id if state.live else None
        if isinstance(event, Episode):
            await self._decide(state, event)
        elif isinstance(event, Suspect):
            log.debug("suspect", stream_id=stream_id, ts=event.ts, z=round(event.snapshot.z, 2))
        elif isinstance(event, MissingChat):
            log.warning("missing chat", stream_id=stream_id, start=event.start, end=event.end)

    # ------------------------------------------------------------------ live: decisions

    async def _decide(self, state: ChannelLive, episode: Episode) -> None:
        p = self.params
        live = state.live
        assert live is not None and state.emit_from is not None
        if episode.peak < state.emit_from:
            log.debug("replayed episode before the emit window", chat_peak=episode.peak)
            return
        win = windows(episode, p)
        async with self.sessions() as session:
            messages = await reads.messages(session, state.info.id, win.messages)
            events = None
            if live.info.live:  # backfilled streams: events unknown, not "none"
                events = await reads.events(session, state.info.id, win.events)
        signals = state.take_signals(episode.peak, p.signal_attach_s, live.delay)
        result = evaluate(
            episode,
            messages,
            events,
            lexicon=state.lexicon,
            params=p,
            boost=total_boost(signals, p),
        )
        try:
            outcome = await self._commit_episode(state, live, episode, result, signals)
        except Exception:
            state.pending.extend(signals)  # not lost: the replay of this channel re-decides
            raise
        self._after(live, outcome, episode, result)

    def _decided_by(
        self, state: ChannelLive, live: LiveStream, episode: Episode
    ) -> PriorCandidate | None:
        """A candidate that already stands for this episode:
        - its chat span (onset to end, extended by its follow-ups) holds the episode's peak.
          Episodes of this scorer peak after every earlier episode's end, so this never hits a
          new moment; or
        - the episode is a replayed one (peaks before the replay's end) and an *unclaimed* peak
          an earlier process decided (a candidate's or a follow-up's) lies in the episode's own
          burst `[onset - 10 s, end + 10 s]`: a replay with another baseline finds a moment with
          other bounds, and may score a folded follow-up above its candidate.
        Earlier peaks are matched one to one, each to the episode whose burst holds it (a
        dedupe claims it), and peaks before the current episode's burst are dropped as the
        replay passes them (a moment the replay doesn't reproduce must not claim a later one).
        So an episode the earlier process never decided (a stronger burst right after a
        follow-up, the next burst of a fold chain) goes through `plan_candidate` as it would
        have without the restart."""
        peak = episode.peak
        pad = timedelta(seconds=BURST_PAD_S)
        lo, hi = episode.onset - pad, episode.end + pad
        live.unclaimed = [e for e in live.unclaimed if e[1] >= lo]
        covering = [c for c in live.recent if c.covers(peak)]
        if covering:
            dup = min(covering, key=lambda c: abs(c.chat_peak - peak))
            live.claim(lo, hi, peak, dup.candidate_id)
            return dup
        if state.replay_until is None or peak >= state.replay_until:
            return None
        candidate_id = live.claim(lo, hi, peak)
        if candidate_id is None:
            return None
        return next((c for c in live.recent if c.candidate_id == candidate_id), None)

    async def _commit_episode(
        self,
        state: ChannelLive,
        live: LiveStream,
        episode: Episode,
        result: Stage2Result,
        signals: list[Signal],
    ) -> _Outcome:
        """One transaction, under the stream's advisory lock: the stream's candidates fresh
        from the database (another process may have written meanwhile), the dedupe, then the
        fold-in, the candidate (+ media request + judge job) or the signals onto a duplicate."""
        p = self.params
        now = self.clock()
        async with self.sessions.begin() as session:
            await writes.lock_stream(session, live.info.id)
            live.recent = [r.prior for r in await reads.stream_candidates(session, live.info.id)]
            dup = self._decided_by(state, live, episode)
            if dup is not None:
                assert dup.candidate_id is not None
                attached = await self._attach(session, dup.candidate_id, signals, now)
                return _Outcome("duplicate", dup.candidate_id, dup, attached)
            decision = plan_candidate(
                episode,
                result,
                stream_id=live.info.id,
                channel_id=state.info.id,
                delay=live.delay,
                recent=live.recent,
                params=p,
                detector_version=self.version,
                extra_features={"explicit": explicit_features(signals)} if signals else None,
            )
            if isinstance(decision, FoldIn):
                target = decision.into.candidate_id
                assert target is not None
                await writes.add_followup(session, target, decision.followup)
                attached = await self._attach(session, target, signals, now)
                return _Outcome("folded", target, decision.extended, attached)
            draft = decision
            if signals and not draft.emitted:  # a viewer thought it was real (§18.4)
                draft = replace(
                    draft,
                    status=CandidateStatus.EMITTED,
                    features={**draft.features, "reemitted": self._reemitted(signals, now)},
                )
            written = await writes.write_candidate(
                session, draft, judge_jobs=self.settings.judge_jobs, dedupe_s=p.dedupe_s
            )
            attached = None
            if written.duplicate_of is not None:
                attached = await self._attach(session, written.duplicate_of.id, signals, now)
            return _Outcome(
                "written",
                written.candidate_id,
                written.duplicate_of.prior if written.duplicate_of else None,
                attached,
                written,
                draft,
            )

    def _after(
        self, live: LiveStream, out: _Outcome, episode: Episode, result: Stage2Result
    ) -> None:
        """Memory, counts and logs once an episode's transaction committed."""
        prior = out.prior
        if prior is not None and out.attached is not None:
            prior = replace(prior, status=out.attached.status, score=out.attached.score)
        if out.attached is not None:
            self._attached_log(out.attached, live)
        if out.kind == "duplicate":
            assert prior is not None
            live.update_recent(prior)
            self._count("duplicate")
            log.info(
                "episode already decided",
                candidate_id=out.candidate_id,
                stream_id=live.info.id,
                chat_peak=episode.peak,
            )
        elif out.kind == "folded":
            assert prior is not None
            live.update_recent(prior)
            self._count("folded")
            log.info(
                "episode folded into candidate",
                candidate_id=out.candidate_id,
                stream_id=live.info.id,
                chat_peak=episode.peak,
                score=round(result.score, 3),
            )
        else:
            assert out.written is not None and out.draft is not None
            self._written(live, out.draft, out.written, prior)

    @staticmethod
    def _reemitted(signals: list[Signal], now: datetime) -> dict[str, Any]:
        return {"by": sorted({s.kind for s in signals}), "at": now.isoformat()}

    async def _attach(self, session: AsyncSession, candidate_id: int, signals, now: datetime):
        if not signals:
            return None
        return await writes.attach_signals(
            session,
            candidate_id,
            signals,
            params=self.params,
            judge_jobs=self.settings.judge_jobs,
            now=now,
        )

    async def _write(self, state: ChannelLive, draft: CandidateDraft, now: datetime) -> None:
        """A signal-only candidate: one transaction (+ media request + judge job)."""
        live = state.live
        assert live is not None
        async with self.sessions.begin() as session:
            written = await writes.write_candidate(
                session, draft, judge_jobs=self.settings.judge_jobs, dedupe_s=self.params.dedupe_s
            )
        dup = written.duplicate_of.prior if written.duplicate_of else None
        self._written(live, draft, written, dup)

    def _written(
        self,
        live: LiveStream,
        draft: CandidateDraft,
        written: writes.Written,
        dup: PriorCandidate | None,
    ) -> None:
        if written.candidate_id is None:
            self._count("duplicate")
            if dup is not None:
                live.update_recent(dup)
            log.info(
                "candidate already exists",
                candidate_id=dup.candidate_id if dup else None,
                stream_id=live.info.id,
                peak_utc=draft.peak_utc,
                trigger=draft.trigger,
            )
            return
        live.recent.append(live.prior_of(draft, written.candidate_id))
        reason = draft.suppress_reason.value if draft.suppress_reason else None
        self._count("emitted" if draft.emitted else f"suppressed.{reason}")
        log.info(
            "candidate emitted" if draft.emitted else "candidate suppressed",
            candidate_id=written.candidate_id,
            stream_id=live.info.id,
            trigger=draft.trigger,
            reason=reason,
            score=round(draft.score, 3),
            peak_utc=draft.peak_utc,
            chat_peak=draft.chat_peak_utc,
            moment_type=draft.moment_type,
            media_request_id=written.emitted.media_request_id if written.emitted else None,
            judge_job_id=written.emitted.judge_job_id if written.emitted else None,
        )

    def _attached_log(self, attached: writes.Attached, live: LiveStream) -> None:
        if attached.reemitted:
            self._count("reemitted")
        log.info(
            "candidate re-emitted" if attached.reemitted else "signal attached",
            candidate_id=attached.candidate_id,
            stream_id=live.info.id,
            boost=attached.boost,
            score=round(attached.score, 3),
            media_request_id=attached.emitted.media_request_id if attached.emitted else None,
            judge_job_id=attached.emitted.judge_job_id if attached.emitted else None,
        )

    # ------------------------------------------------------------------ explicit signals

    async def explicit_tick(self) -> None:
        async with self._lock:
            await self._explicit_tick()

    async def _explicit_tick(self) -> None:
        p = self.params
        now = self.clock()
        until = final_end(now, p.final_lag_s)
        states = [
            s
            for s in self.channels.values()
            if s.ready and s.raw_cursor is not None and s.raw_cursor < until
        ]
        if states:
            ranges = {s.info.id: s.raw_cursor for s in states if s.raw_cursor is not None}
            async with self.sessions() as session:
                rows = await reads.raw_chat(session, ranges, until)
            by_channel: dict[int, list[Row]] = {}
            for r in rows:
                by_channel.setdefault(r.channel_id, []).append(r)
            for state in states:
                with log_context(channel=state.info.login, channel_id=state.info.id):
                    self._scan(state, by_channel.get(state.info.id, []))
                state.raw_cursor = until
        for state in list(self.channels.values()):
            if state.ready and state.pending:
                with log_context(channel=state.info.login, channel_id=state.info.id):
                    try:
                        await self._resolve_signals(state, now)
                    except Exception:
                        log.exception("explicit signals failed, retrying on the next tick")

    def _scan(self, state: ChannelLive, rows: list[Row]) -> None:
        p = self.params
        assert state.raw_cursor is not None
        for r in rows:
            if r.sent_at < state.raw_cursor:
                continue
            found = [state.mentions.add(r)]
            login = (r.user_login or "").lower()
            poster = r.user_id or (f"login:{login}" if login else None)
            # a link counts as its poster's clip; bots' links (replies to !clip) and deleted
            # messages don't, so it takes two people to make a viewer-clip signal from chat
            if poster is not None and not r.deleted and not state.lexicon.is_bot(login):
                for slug in clip_links(r.text):
                    clip = ViewerClip(slug, poster, r.sent_at, p.clip_link_duration_s, "chat")
                    found.append(state.clips.add(clip))
            for sig in found:
                if sig is not None:
                    self._new_signal(state, sig)

    def _new_signal(self, state: ChannelLive, sig: Signal) -> None:
        state.pending.append(sig)
        self._count(f"signal.{sig.kind}")
        log.info("explicit signal", kind=sig.kind, at=sig.at, video_time=sig.video, **sig.data)

    async def _resolve_signals(self, state: ChannelLive, now: datetime) -> None:
        p = self.params
        live = state.live
        assert state.cursor is not None
        if live is None:  # between streams: keep them a while (a stream row may turn up)
            default = timedelta(seconds=p.default_delay_s)
            max_age = timedelta(seconds=self.settings.signal_max_age_s)
            keep = [s for s in state.pending if state.cursor - s.chat(s.end, default) <= max_age]
            if len(keep) < len(state.pending):
                log.info(
                    "explicit signals outside a stream dropped",
                    count=len(state.pending) - len(keep),
                )
            state.pending = keep
            return
        d = live.delay.td
        within = timedelta(seconds=p.signal_attach_s)
        pending = sorted(state.pending, key=lambda s: s.chat_at(d))
        handled: set[int] = set()
        for i, sig in enumerate(pending):
            if i in handled:
                continue
            chat_at = sig.chat_at(d)
            if not live.info.contains(chat_at):
                handled.add(i)
                log.info("explicit signal outside the stream dropped", kind=sig.kind, at=sig.at)
                continue
            async with self.sessions.begin() as session:
                ref = await reads.nearest_candidate(
                    session, live.info.id, chat_at - d, p.signal_attach_s
                )
                attached = (
                    await self._attach(session, ref.id, [sig], now) if ref is not None else None
                )
            if ref is not None:
                handled.add(i)
                if attached is not None:
                    live.update_recent(
                        replace(ref.prior, status=attached.status, score=attached.score)
                    )
                    self._attached_log(attached, live)
                continue
            if live.scorer.in_episode or state.cursor - chat_at <= within:
                continue  # an episode may still claim it
            group = [
                j
                for j in range(i, len(pending))
                if j not in handled and abs(pending[j].chat_at(d) - chat_at) <= within
            ]
            draft = signal_draft(
                sig,
                stream_id=live.info.id,
                channel_id=state.info.id,
                delay=live.delay,
                params=p,
                detector_version=self.version,
                signals=[pending[j] for j in group],
            )
            await self._write(state, draft, now)
            handled.update(group)
        state.pending = [s for i, s in enumerate(pending) if i not in handled]

    async def poll_clips(self) -> None:
        """Helix Get Clips for every held channel in a live stream (DESIGN §18.4)."""
        if self.helix is None:
            return
        now = self.clock()
        since = now - timedelta(seconds=self.settings.clips_lookback_s)
        targets = [
            (s.info.id, s.info.platform_user_id, s.info.login)
            for s in self.channels.values()
            if s.ready and s.info.platform_user_id and s.live is not None and s.live.info.live
        ]
        limit = asyncio.Semaphore(5)

        async def fetch(cid: int, user_id: str, login: str):
            async with limit:
                try:
                    page = await self.helix.get_clips(
                        user_id, started_at=since, ended_at=now, limit=100
                    )
                except Exception as exc:
                    log.warning("get clips failed", channel=login, error=str(exc))
                    return cid, []
                return cid, page.items

        results = await asyncio.gather(*(fetch(*t) for t in targets))
        async with self._lock:
            for cid, clips in results:
                state = self.channels.get(cid)
                if state is None or state.live is None:
                    continue
                with log_context(channel=state.info.login, channel_id=cid):
                    for c in sorted(clips, key=lambda c: (c.created_at, c.id)):
                        if c.id not in state.clips and self._live_clip(state.live, c):
                            clip = ViewerClip(
                                c.id,
                                c.creator_id or c.creator_name or c.id,
                                c.created_at,
                                float(c.duration),
                                "helix",
                            )
                            sig = state.clips.add(clip)
                            if sig is not None:
                                self._new_signal(state, sig)

    def _live_clip(self, live: LiveStream, c: Any) -> bool:
        """Made from the live stream: not cut from the VOD later (its `vod_offset` far from a
        live clip's place) nor from another VOD (a `video_id` other than the one live clips of
        this stream carry)."""
        duration = float(c.duration)
        if from_vod_later(c.created_at, duration, c.vod_offset, live.info.started_at, self.params):
            log.debug("clip cut from the VOD later skipped", clip_id=c.id, vod_offset=c.vod_offset)
            return False
        if c.video_id:
            if c.vod_offset is not None and live.video_id is None:
                live.video_id = c.video_id  # consistent with the live timeline: ours
            elif live.video_id is not None and c.video_id != live.video_id:
                log.debug("clip of another VOD skipped", clip_id=c.id, video_id=c.video_id)
                return False
        return True

    # ------------------------------------------------------------------ sweep, heartbeat

    async def sweep(self) -> None:
        now = self.clock()
        async with self.sessions.begin() as session:
            done = await writes.sweep_media_requests(
                session, now=now, fulfil_after=timedelta(seconds=self.settings.fulfil_after_s)
            )
        if done:
            log.info("media requests fulfilled", count=done)

    def _count(self, key: str) -> None:
        self._counts.append((self.clock(), key))

    def _last_hour(self) -> dict[str, Any]:
        horizon = self.clock() - timedelta(hours=1)
        while self._counts and self._counts[0][0] < horizon:
            self._counts.popleft()
        c = Counter(k for _, k in self._counts)
        out: dict[str, Any] = {
            k: c.get(k, 0) for k in ("emitted", "reemitted", "folded", "duplicate")
        }
        out["late_rows"] = c.get("late_row", 0)
        for prefix in ("suppressed", "signal"):
            out[prefix] = {
                k.split(".", 1)[1]: n for k, n in sorted(c.items()) if k.startswith(prefix + ".")
            }
        return out

    async def beat(self) -> None:
        now = self.clock()
        channels = []
        for s in self.channels.values():
            lag = None if s.cursor is None else round((now - s.cursor).total_seconds(), 1)
            channels.append(
                {
                    "channel": s.info.login,
                    "stream_id": s.live.info.id if s.live else None,
                    "ready": s.ready,
                    "lag_s": lag,
                    "pending_signals": len(s.pending),
                    "late_rows": s.late_rows,
                }
            )
        lags = [c["lag_s"] for c in channels if c["lag_s"] is not None]
        info = {
            "mode": self.settings.mode,
            "judge_jobs": self.settings.judge_jobs,
            "versions": {
                "detector": self.version,
                "scorer": SCORER_VERSION,
                "params": self.params.digest(),
                "lexicon": self.lexicon.tag,
            },
            "channels_held": len(self.channels),
            "channels": channels,
            "max_lag_s": max(lags) if lags else None,  # includes the final lag (10 s)
            "late_rows": sum(s.late_rows for s in self.channels.values()),
            "last_hour": self._last_hour(),
            "viewer_clips": self.helix is not None,
            "offline": "not built",
        }
        async with self.sessions.begin() as session:
            await heartbeat.beat(
                session,
                worker_id=self.worker_id,
                service=SERVICE,
                started_at=self._started_at,
                info=info,
            )
