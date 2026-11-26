"""Live detector service against Postgres (DESIGN §18 build item 3 "Done").

Synthetic chat (`ChatSim`, aggregated by the real `SecondAggregator`) is written like chat-ingest
writes it (metrics without stream_id: the detector assigns the stream by time); the service loops
are driven with an injected clock: 1 s live ticks, explicit signals every 5 s, sync every 10 s.
"""

import asyncio
import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from clipper_core.chat import ChatWriter, SecondAggregator
from clipper_core.contracts import (
    DelaySource,
    JobKind,
    Priority,
    SuppressReason,
    dedupe_key,
    detect_lease,
)
from clipper_core.timeline import utc_now
from clipper_detector import reads, writes
from clipper_detector.lexicon import Lexicon
from clipper_detector.params import DetectorParams
from clipper_detector.service import LiveDetector
from clipper_detector.settings import DetectorSettings
from clipper_detector.synthetic import ChatSim, bump
from clipper_twitch.helix import HelixClip, HelixPage

pytestmark = pytest.mark.integration

S = datetime(2026, 11, 20, 12, tzinfo=UTC)  # stream start
C = 660  # burst centre, seconds after S (after the 10 min warm-up)
P = DetectorParams()
LEXICON = Lexicon.load()
ONE = timedelta(seconds=1)
LAG = timedelta(seconds=P.final_lag_s)
DELAY = timedelta(seconds=P.default_delay_s)
EMOTES = frozenset({"KEKW", "LUL", "Pog", "OMEGALUL", "xqcSkip", "peepoHey"})
NORMAL = ("hello there", "what game is this", "nice", "ok", "LUL", "gg", "true", "based", "xqcSkip")
FUNNY = ("KEKW", "KEKW KEKW", "LMAOOO", "OMEGALUL", "KEKW what")
RAID = ("bigraiderRaid bigraiderRaid", "raid hype", "here from the raid", "hello")


def at(s: float) -> datetime:
    return S + timedelta(seconds=s)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def add_channel(
    sessions, login="testchan", *, source="live", ended=None, user_id=None
) -> tuple[int, int]:
    async with sessions.begin() as s:
        cid = (
            await s.execute(
                text("INSERT INTO channels (login, platform_user_id) VALUES (:l, :u) RETURNING id"),
                {"l": login, "u": user_id},
            )
        ).scalar_one()
        sid = (
            await s.execute(
                text(
                    "INSERT INTO streams (channel_id, platform_stream_id, started_at, ended_at,"
                    " status, source) VALUES (:c, :p, :t, :e, :st, :src) RETURNING id"
                ),
                {
                    "c": cid,
                    "p": f"b-{login}",
                    "t": S,
                    "e": ended,
                    "st": "live" if ended is None else "ended",
                    "src": source,
                },
            )
        ).scalar_one()
    return cid, sid


def chat(cid: int, *, seed: int = 0, duration: int = C + 150) -> ChatSim:
    sim = ChatSim(random.Random(seed), S, channel_id=cid, stream_id=None, emote_names=EMOTES)
    sim.chatter(0, duration, 5, NORMAL)
    return sim


def burst(sim: ChatSim, texts, *, center: int = C, factor: float = 4.0):
    """15 s x`factor` raised-cosine burst on top of the 5/s chat, peaking at `center`."""
    return sim.chatter(center - 15, 30, lambda i: 5 * (bump(i, 15, 15, factor) - 1), texts)


def mentions(sim: ChatSim, start: float, users: int = 8, text_: str = "CLIP IT") -> None:
    for k in range(users):
        sim.message(start + k, text_, f"clipper{k}")


async def store_chat(sessions, sim: ChatSim, *, events: bool = True) -> None:
    """As chat-ingest writes it: raw rows, events, per-second stats (stream_id NULL)."""
    agg = SecondAggregator()
    for m in sorted(sim.messages, key=lambda m: m.sent_at):
        agg.add_row(m)
    evs = sim.events if events else []
    for e in evs:
        agg.add_event(e.channel_id, e.stream_id, e.sent_at, e.type)
    async with sessions.begin() as s:
        await ChatWriter().write(s, sim.messages, evs, agg.pop_all(), [])


async def add_viewers(sessions, sid: int, until_s: int, viewers: int = 3000) -> None:
    async with sessions.begin() as s:
        await s.execute(
            text("INSERT INTO viewer_snapshots (stream_id, ts, viewers) VALUES (:s, :t, :v)"),
            [{"s": sid, "t": at(k), "v": viewers} for k in range(0, until_s, 30)],
        )


async def add_chunks(sessions, sid: int, from_s: int, to_s: int) -> list[int]:
    """60 s chunks with audio and their queued transcription jobs (priority LIVE)."""
    ids = []
    async with sessions.begin() as s:
        for seq, start in enumerate(range(from_s, to_s, 60)):
            cid = (
                await s.execute(
                    text(
                        "INSERT INTO chunks (stream_id, seq, start_utc, end_utc, audio_key)"
                        " VALUES (:s, :q, :a, :b, 'k') RETURNING id"
                    ),
                    {"s": sid, "q": seq, "a": at(start), "b": at(start + 60)},
                )
            ).scalar_one()
            await s.execute(
                text(
                    "INSERT INTO jobs (kind, ref_id, dedupe_key, priority)"
                    " VALUES ('transcribe', :c, :k, :p)"
                ),
                {"c": cid, "k": dedupe_key(JobKind.TRANSCRIBE, cid), "p": int(Priority.LIVE)},
            )
            ids.append(cid)
    return ids


def detector(
    sessions, clock, *, worker="det-1", judge_jobs=True, params=P, helix=None, **settings
) -> LiveDetector:
    return LiveDetector(
        sessions,
        settings=DetectorSettings(
            _env_file=None, judge_jobs=judge_jobs, lease_renew_s=0, **settings
        ),
        params=params,
        lexicon=LEXICON,
        worker_id=worker,
        helix=helix,
        clock=clock,
    )


async def drive(
    det: LiveDetector, clock: Clock, until: datetime, on_tick=None, *, sync: bool = True
) -> None:
    """The service loops with the injected clock."""
    while clock.now < until:
        clock.now += ONE
        n = int(clock.now.timestamp())
        if sync and n % 10 == 0:
            await det.sync()
        await det.live_tick()
        if n % 5 == 0:
            await det.explicit_tick()
        if on_tick is not None:
            await on_tick()


async def fetch(sessions, sql: str, **params):
    async with sessions() as s:
        return (await s.execute(text(sql), params)).all()


async def counts(sessions) -> tuple[int, int, int]:
    (row,) = await fetch(
        sessions,
        "SELECT (SELECT count(*) FROM candidates) AS c, (SELECT count(*) FROM media_requests) AS m,"
        " (SELECT count(*) FROM jobs WHERE kind = 'judge') AS j",
    )
    return row.c, row.m, row.j


async def start(sessions, clock_s: float, **kw) -> tuple[LiveDetector, Clock]:
    """A detector whose clock is at `clock_s` (+ the final lag): it holds and replays the
    channel."""
    clock = Clock(at(clock_s) + LAG)
    det = detector(sessions, clock, **kw)
    await det.sync()
    return det, clock


# --------------------------------------------------------------------------- tests


async def test_burst_gives_candidate_media_request_and_judge_job_in_one_transaction(sessions):
    cid, sid = await add_channel(sessions)
    sim = chat(cid)
    burst(sim, FUNNY)
    await store_chat(sessions, sim)
    await add_viewers(sessions, sid, C + 150)
    chunk_ids = await add_chunks(sessions, sid, C - 240, C + 120)
    det, clock = await start(sessions, C - 60)
    assert det.channels[cid].ready and det.channels[cid].live.info.id == sid
    assert await counts(sessions) == (0, 0, 0)

    seen: list[datetime] = []

    async def watch():
        if not seen and clock.now > at(C) and (await counts(sessions))[0]:
            seen.append(clock.now)

    await drive(det, clock, at(C + 60) + LAG, watch)
    (cand,) = await fetch(sessions, "SELECT *, xmin::text AS x FROM candidates")
    assert cand.stream_id == sid and cand.channel_id == cid  # stream assigned by time
    assert cand.status == "emitted" and cand.suppress_reason is None and cand.trigger == "chat"
    assert abs((cand.chat_peak_utc - at(C)).total_seconds()) <= 2
    assert cand.peak_utc == cand.chat_peak_utc - DELAY  # default delay 8 s
    assert cand.moment_type == "funny" and cand.detector_version == det.version
    assert cand.features["viewers"] == 3000 and cand.features["events"]["known"] is True

    (mr,) = await fetch(sessions, "SELECT *, xmin::text AS x FROM media_requests")
    assert cand.media_request_id == mr.id and mr.requested_by == "detector"
    assert (mr.from_utc, mr.to_utc, mr.priority) == (cand.window_from, cand.window_to, 80)
    (job,) = await fetch(sessions, "SELECT *, xmin::text AS x FROM jobs WHERE kind = 'judge'")
    assert job.ref_id == cand.id and job.priority == Priority.TRIGGER
    assert job.dedupe_key == dedupe_key(JobKind.JUDGE, cand.id)
    assert job.payload == {"candidate_id": cand.id}
    bumped = await fetch(
        sessions,
        "SELECT j.ref_id, j.priority, j.xmin::text AS x, c.start_utc, c.end_utc FROM jobs j"
        " JOIN chunks c ON c.id = j.ref_id WHERE j.kind = 'transcribe' ORDER BY c.start_utc",
    )
    inside = [b for b in bumped if b.start_utc < cand.window_to and b.end_utc > cand.window_from]
    assert 0 < len(inside) < len(chunk_ids)
    assert all(b.priority == 80 for b in inside)
    assert all(b.priority == Priority.LIVE for b in bumped if b not in inside)
    # one transaction: every row written or updated has the same xmin
    assert {cand.x, mr.x, job.x} | {b.x for b in inside} == {cand.x}

    # decision latency: the closing second is end + close_after_s; it is final 9 s later
    decided = datetime.fromisoformat(cand.features["chat"]["end"]) + timedelta(
        seconds=P.close_after_s
    )
    final_at = decided + LAG + ONE
    assert seen and (seen[0] - final_at).total_seconds() <= 10

    # sweep: open until transcribed; then fulfilled
    await det.sweep()
    assert (await fetch(sessions, "SELECT fulfilled_at FROM media_requests"))[0][0] is None
    async with sessions.begin() as s:
        for chunk in await fetch(sessions, "SELECT id, start_utc, end_utc FROM chunks"):
            await s.execute(
                text(
                    "INSERT INTO chunk_transcripts (chunk_id, model, words_key, segment_count,"
                    " owned_from, owned_to) VALUES (:c, 'fake', 'w', 0, :a, :b)"
                ),
                {"c": chunk.id, "a": chunk.start_utc, "b": chunk.end_utc},
            )
    await det.sweep()
    assert (await fetch(sessions, "SELECT fulfilled_at FROM media_requests"))[0][0] is not None

    await det.beat()
    (hb,) = await fetch(sessions, "SELECT service, info FROM worker_heartbeats")
    assert hb.service == "detector" and hb.info["channels_held"] == 1
    assert hb.info["last_hour"]["emitted"] == 1 and hb.info["versions"]["detector"] == det.version
    assert hb.info["max_lag_s"] <= P.final_lag_s + 1


async def test_raid_is_suppressed_without_media_request_or_job(sessions):
    cid, sid = await add_channel(sessions)
    sim = chat(cid, seed=1)
    sim.event(C - 12, "raid", user_login="bigraider", viewerCount="3000")
    burst(sim, RAID)
    await store_chat(sessions, sim)
    await add_viewers(sessions, sid, C + 150)
    det, clock = await start(sessions, C - 60)
    await drive(det, clock, at(C + 60) + LAG)
    (cand,) = await fetch(sessions, "SELECT * FROM candidates")
    assert cand.status == "suppressed" and cand.suppress_reason == SuppressReason.RAID
    assert cand.media_request_id is None and cand.features["events"]["raids"] == 1
    assert await counts(sessions) == (1, 0, 0)


async def test_restart_emits_only_the_last_15_min_and_never_twice(sessions):
    cid, sid = await add_channel(sessions)
    sim = chat(cid, seed=2, duration=C + 300)
    burst(sim, FUNNY)
    await store_chat(sessions, sim)
    await add_viewers(sessions, sid, C + 300)

    # a restart 4 min after the burst with a 2 min emit window: the burst is replay only
    short = DetectorParams(restart_emit_s=120)
    det, _ = await start(sessions, C + 240, worker="det-short", params=short)
    assert det.channels[cid].ready and await counts(sessions) == (0, 0, 0)
    await det.release_all()

    # the default 15 min: the burst missed while the detector was down is emitted, once
    det, _ = await start(sessions, C + 240)
    assert await counts(sessions) == (1, 1, 1)
    # the same worker id restarting (it retakes its lease): replay matches the candidate
    det2, clock2 = await start(sessions, C + 250)
    await drive(det2, clock2, at(C + 270) + LAG)
    assert await counts(sessions) == (1, 1, 1)
    assert det2._last_hour()["duplicate"] == 1


async def test_lease_handover(sessions):
    cid, _sid = await add_channel(sessions)
    sim = chat(cid, seed=3)
    burst(sim, FUNNY)
    await store_chat(sessions, sim)
    a, clock = await start(sessions, C - 60, worker="det-a")
    b = detector(sessions, clock, worker="det-b")
    await b.sync()
    assert cid in a.channels and cid not in b.channels  # a holds the lease
    await drive(a, clock, at(C + 60) + LAG)
    assert await counts(sessions) == (1, 1, 1)

    async with sessions.begin() as s:  # a stalls: its lease runs out
        await s.execute(text("UPDATE leases SET expires_at = now() - interval '1 second'"))
    await b.sync()
    assert b.channels[cid].ready
    async with sessions() as s:
        assert (
            await s.execute(
                text("SELECT owner FROM leases WHERE resource = :r"), {"r": detect_lease(cid)}
            )
        ).scalar_one() == "det-b"
    await a.sync()  # a notices and lets go
    assert cid not in a.channels
    await drive(b, clock, at(C + 90) + LAG)
    assert await counts(sessions) == (1, 1, 1)  # b's replay found a's candidate


async def test_backfilled_stream_without_events_and_viewers(sessions):
    cid, sid = await add_channel(sessions, source="backfill", ended=at(3600))
    sim = chat(cid, seed=4)
    sim.event(C - 12, "raid", user_login="bigraider", viewerCount="3000")  # not stored
    burst(sim, FUNNY)
    await store_chat(sessions, sim, events=False)
    det, clock = await start(sessions, C - 60, judge_jobs=False)
    assert det.channels[cid].live.viewers is None
    await drive(det, clock, at(C + 60) + LAG)
    (cand,) = await fetch(sessions, "SELECT * FROM candidates")
    assert cand.status == "emitted" and cand.stream_id == sid
    assert cand.features["events"]["known"] is False and cand.features["viewers"] is None
    assert await counts(sessions) == (1, 1, 0)  # media request, no judge job (JUDGE_JOBS=0)


async def test_clip_it_mentions_make_a_candidate(sessions):
    cid, _sid = await add_channel(sessions)
    sim = chat(cid, seed=5, duration=C + 200)
    mentions(sim, C, users=8)  # no chat spike: 8 extra messages in 5/s chat
    await store_chat(sessions, sim)
    det, clock = await start(sessions, C - 60)
    await drive(det, clock, at(C + 40) + LAG)
    assert await counts(sessions) == (0, 0, 0)  # waits: an episode might still claim it
    await drive(det, clock, at(C + 120) + LAG)
    (cand,) = await fetch(sessions, "SELECT * FROM candidates")
    assert cand.trigger == "clip_mentions" and cand.status == "emitted"
    assert cand.chat_peak_utc == at(C) - timedelta(seconds=P.mention_lead_s)
    assert cand.features["explicit"]["clip_mentions"]["users"] >= 5
    assert cand.score == P.boost_clip_mentions
    assert await counts(sessions) == (1, 1, 1)


async def test_clip_mentions_re_emit_a_suppressed_candidate(sessions):
    cid, _sid = await add_channel(sessions)
    sim = chat(cid, seed=1)
    sim.event(C - 12, "raid", user_login="bigraider", viewerCount="3000")
    burst(sim, RAID)
    mentions(sim, C + 40, users=8)  # after the raid's candidate is decided
    await store_chat(sessions, sim)
    det, clock = await start(sessions, C - 60)
    await drive(det, clock, at(C + 38) + LAG)
    (cand,) = await fetch(sessions, "SELECT * FROM candidates")
    assert cand.status == "suppressed" and await counts(sessions) == (1, 0, 0)

    await drive(det, clock, at(C + 60) + LAG)
    (cand,) = await fetch(sessions, "SELECT *, xmin::text AS x FROM candidates")
    assert cand.status == "emitted" and cand.suppress_reason == SuppressReason.RAID  # audit
    assert cand.features["reemitted"]["by"] == ["clip_mentions"]
    assert "clip_mentions" in cand.features["explicit"]
    (mr,) = await fetch(sessions, "SELECT id, xmin::text AS x FROM media_requests")
    (job,) = await fetch(sessions, "SELECT ref_id, xmin::text AS x FROM jobs WHERE kind='judge'")
    assert cand.media_request_id == mr.id and job.ref_id == cand.id
    assert cand.x == mr.x == job.x  # the UPDATE, media request and judge job: one transaction
    assert det._last_hour()["reemitted"] == 1


class FakeHelix:
    def __init__(self, clips: list[HelixClip]) -> None:
        self.clips = clips
        self.calls: list[tuple[str, datetime, datetime]] = []

    async def get_clips(self, broadcaster_id, *, started_at, ended_at, limit=None):
        self.calls.append((broadcaster_id, started_at, ended_at))
        return HelixPage([c for c in self.clips if started_at <= c.created_at <= ended_at])


def helix_clip(slug: str, creator: str, created: datetime, vod_offset=None) -> HelixClip:
    return HelixClip(
        id=slug,
        url="",
        embed_url="",
        broadcaster_id="42",
        broadcaster_name="testchan",
        creator_id=creator,
        creator_name=creator,
        video_id=None,
        game_id="",
        language="en",
        title="lol",
        view_count=1,
        created_at=created,
        thumbnail_url="",
        duration=30.0,
        vod_offset=vod_offset,
        is_featured=False,
    )


async def test_viewer_clips_attach_to_the_candidate(sessions):
    cid, _sid = await add_channel(sessions, user_id="42")
    sim = chat(cid, seed=6)
    burst(sim, FUNNY)
    await store_chat(sessions, sim)
    # made 15-20 s after the moment (video time = chat time - 8 s)
    helix = FakeHelix(
        [
            helix_clip("c1", "alice", at(C + 10)),
            helix_clip("c2", "bob", at(C + 14)),
            helix_clip("old", "carol", at(C + 12), vod_offset=60),  # cut from the VOD later
        ]
    )
    det, clock = await start(sessions, C - 60, helix=helix)
    await drive(det, clock, at(C + 60) + LAG)
    (cand,) = await fetch(sessions, "SELECT * FROM candidates")
    score = cand.score
    clock.now = at(C + 30) + LAG
    await det.poll_clips()
    assert helix.calls and helix.calls[0][0] == "42"
    await det.explicit_tick()
    (cand,) = await fetch(sessions, "SELECT * FROM candidates")
    clips = cand.features["explicit"]["viewer_clips"]
    assert clips["creators"] == 2 and [c["id"] for c in clips["clips"]] == ["c1", "c2"]
    assert cand.score == pytest.approx(score + P.boost_viewer_clip)
    assert await counts(sessions) == (1, 1, 1)
    # no credentials: nothing polled, nothing breaks
    plain = detector(sessions, clock, worker="det-plain")
    await plain.poll_clips()


async def test_run_loop(sessions):
    """The real loops on a clock offset into the seeded data: the first sync replays and emits
    the burst; stop releases the leases."""
    cid, _sid = await add_channel(sessions)
    sim = chat(cid, seed=7)
    burst(sim, FUNNY)
    await store_chat(sessions, sim)
    offset = utc_now() - (at(C + 60) + LAG)
    det = detector(
        sessions,
        lambda: utc_now() - offset,
        live_interval_s=0.05,
        sync_interval_s=0.2,
        explicit_interval_s=0.1,
        heartbeat_interval_s=0.2,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(det.run(stop))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if (await counts(sessions))[0] and await fetch(sessions, "SELECT 1 FROM worker_heartbeats"):
            break
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert await counts(sessions) == (1, 1, 1)
    assert await fetch(sessions, "SELECT * FROM leases") == []


async def test_previous_stream_gives_prior_and_learned_delay(sessions):
    """Delay = median of the last valid estimates by stream start (no uncertain backfill); the
    prior = the previous stream's median activity and viewers."""
    cid, sid = await add_channel(sessions)
    async with sessions.begin() as s:
        prev = []
        for k, (source, delay, valid) in enumerate(
            [
                ("live", 5.0, True),
                ("live", 6.0, True),
                ("backfill", 30.0, True),
                ("live", 99.0, False),
            ]
        ):
            started = S - timedelta(hours=4 - k)
            prev.append(
                (
                    await s.execute(
                        text(
                            "INSERT INTO streams (channel_id, platform_stream_id, started_at,"
                            " ended_at, status, source) VALUES (:c, :p, :t, :e, 'ended', :src)"
                            " RETURNING id"
                        ),
                        {
                            "c": cid,
                            "p": f"prev{k}",
                            "t": started,
                            "e": started + timedelta(minutes=30),
                            "src": source,
                        },
                    )
                ).scalar_one()
            )
            await s.execute(
                text(
                    "INSERT INTO chat_delay_estimates (channel_id, stream_id, method, delay_s,"
                    " events, strength, valid, detector_version)"
                    " VALUES (:c, :s, 'audio_events', :d, 20, :st, :v, 'd1')"
                ),
                {"c": cid, "s": prev[-1], "d": delay, "st": 2.0 if valid else None, "v": valid},
            )
        await s.execute(
            text(
                "INSERT INTO vods (channel_id, stream_id, platform_video_id, platform_created_at,"
                " duration_s, t0_utc, t0_method, flags)"
                " VALUES (:c, :s, 'v1', :t, 1800, :t, 'pdt', '{\"timeline_uncertain_s\": 3.2}')"
            ),
            {"c": cid, "s": prev[2], "t": S - timedelta(hours=2)},
        )
        last = S - timedelta(hours=1)  # the previous stream: 3 chatters/s for 20 min
        await s.execute(
            text(
                "INSERT INTO chat_metrics_1s (channel_id, ts, stream_id, messages, unique_chatters)"
                " VALUES (:c, :t, :s, 3, 3)"
            ),
            [{"c": cid, "t": last + timedelta(seconds=i), "s": prev[3]} for i in range(1200)],
        )
        await s.execute(
            text("INSERT INTO viewer_snapshots (stream_id, ts, viewers) VALUES (:s, :t, 1000)"),
            {"s": prev[3], "t": last},
        )
    det, _ = await start(sessions, 30)  # the scorer sees the stream from its start
    live = det.channels[cid].live
    assert live.info.id == sid
    assert (live.delay.seconds, live.delay.source) == (5.5, DelaySource.LEARNED)
    prior = live.scorer.prior
    assert prior is not None and prior.activity == pytest.approx(30.0, abs=1)
    assert prior.viewers == 1000

    async with sessions.begin() as s:  # the operator's override wins
        await s.execute(text("UPDATE channels SET chat_delay_s = 3 WHERE id = :c"), {"c": cid})
    det, _ = await start(sessions, 40, worker="det-2")
    assert det.channels == {}  # det-1 still holds the lease
    det, _ = await start(sessions, 40)
    assert det.channels[cid].live.delay.source == DelaySource.OVERRIDE


# --------------------------------------------------------------------------- review fixes


async def test_fold_in_is_written_to_the_candidate(sessions):
    cid, _sid = await add_channel(sessions)
    sim = chat(cid, seed=8, duration=C + 200)
    burst(sim, ("KEKW", "OMEGALUL", "LMAO"), factor=5)
    burst(sim, ("KEKW", "OMEGALUL"), center=C + 45, factor=2.6)
    await store_chat(sessions, sim)
    det, clock = await start(sessions, C - 60)
    await drive(det, clock, at(C + 120) + LAG)
    (cand,) = await fetch(sessions, "SELECT * FROM candidates")
    (fold,) = cand.features["followups"]
    assert abs((datetime.fromisoformat(fold["peak"]) - at(C + 45)).total_seconds()) <= 3
    assert det._last_hour()["folded"] == 1 and await counts(sessions) == (1, 1, 1)
    (prior,) = det.channels[cid].live.recent  # its span now reaches the follow-up's end
    assert prior.followup_peaks == (datetime.fromisoformat(fold["peak"]),)
    assert prior.chat_end == datetime.fromisoformat(fold["end"])


TA = 2000  # the fold chain's first burst


async def test_restart_after_a_fold_chain_emits_nothing_new(sessions):
    """A burst with 5 weaker follow-ups 45 s apart folds into one candidate. A restart replays
    with a shorter, lower baseline (the early extra chat is out of its window): the follow-ups
    score above the candidate, but they lie in its span, so nothing new is emitted."""
    cid, sid = await add_channel(sessions)
    sim = ChatSim(random.Random(11), S, channel_id=cid, stream_id=None, emote_names=EMOTES)
    sim.chatter(0, TA + 1200, 5, NORMAL)
    sim.chatter(0, 1700, 3, NORMAL)  # extra chat early: raises the first run's baseline
    burst(sim, ("KEKW", "OMEGALUL", "LMAO"), center=TA, factor=6)
    for k in range(1, 6):
        burst(sim, ("KEKW", "OMEGALUL"), center=TA + 45 * k, factor=4.5)
    await store_chat(sessions, sim)
    await add_viewers(sessions, sid, TA + 1200)
    await start(sessions, TA + 300)
    (cand,) = await fetch(sessions, "SELECT features FROM candidates")
    assert len(cand.features["followups"]) == 5
    det2, _ = await start(sessions, TA + 1000)
    assert await counts(sessions) == (1, 1, 1)
    assert det2._last_hour()["duplicate"] >= 1 and det2._last_hour()["emitted"] == 0


async def test_stream_row_turning_up_late_rebuilds_the_channel(sessions):
    cid, sid = await add_channel(sessions)
    async with sessions.begin() as s:  # the controller hasn't written the stream yet
        await s.execute(
            text("UPDATE streams SET started_at = started_at + interval '100 days' WHERE id = :i"),
            {"i": sid},
        )
    sim = chat(cid, seed=3)
    burst(sim, FUNNY)
    await store_chat(sessions, sim)
    det, clock = await start(sessions, C - 60)
    assert det.channels[cid].ready and det.channels[cid].live is None
    await drive(det, clock, at(C + 20) + LAG)
    async with sessions.begin() as s:  # now it is there, started at S
        await s.execute(
            text("UPDATE streams SET started_at = :t WHERE id = :i"), {"t": S, "i": sid}
        )
    await drive(det, clock, at(C + 70) + LAG)
    assert det.channels[cid].live.info.id == sid
    assert await counts(sessions) == (1, 1, 1)


async def test_chat_clip_links_need_two_posters(sessions):
    cid, _sid = await add_channel(sessions)
    sim = chat(cid, seed=9)
    bot = next(iter(LEXICON.for_channel("testchan").bots))
    sim.message(C - 200, "https://clips.twitch.tv/SoloOne-aaaa", "solo")  # one poster,
    sim.message(C - 199, "and clips.twitch.tv/SoloTwo-bbbb", "solo")  # two links: no signal
    sim.message(C - 198, "clip made: https://clips.twitch.tv/BotMade-cccc", bot)  # a bot's
    burst(sim, FUNNY)
    sim.message(C + 5, "LOL https://clips.twitch.tv/FanOne-dddd", "fan1")
    sim.message(C + 8, "https://www.twitch.tv/testchan/clip/FanTwo-eeee", "fan2")
    await store_chat(sessions, sim)
    det, clock = await start(sessions, C - 260)
    await drive(det, clock, at(C - 150) + LAG)
    assert "viewer_clips" not in det._last_hour()["signal"]
    await drive(det, clock, at(C + 60) + LAG)
    (cand,) = await fetch(sessions, "SELECT * FROM candidates")
    clips = cand.features["explicit"]["viewer_clips"]
    assert clips["creators"] == 2
    assert {c["id"] for c in clips["clips"]} == {"FanOne-dddd", "FanTwo-eeee"}
    assert {c["creator"] for c in clips["clips"]} == {"login:fan1", "login:fan2"}


async def test_deactivated_channel_releases_its_lease(sessions):
    cid, _sid = await add_channel(sessions)
    det, _ = await start(sessions, C)
    assert await fetch(sessions, "SELECT owner FROM leases") == [("det-1",)]
    async with sessions.begin() as s:
        await s.execute(text("UPDATE channels SET active = false WHERE id = :c"), {"c": cid})
    await det.sync()
    assert det.channels == {} and await fetch(sessions, "SELECT * FROM leases") == []


async def test_late_metric_rows_and_the_watermark(sessions):
    cid, _sid = await add_channel(sessions)
    sim = chat(cid, seed=10, duration=C - 20)  # chat-ingest has written up to C - 20
    await store_chat(sessions, sim)
    det, clock = await start(sessions, C - 60)
    state = det.channels[cid]
    await drive(det, clock, at(C - 17) + LAG)  # the clock says C - 17 is final
    (newest,) = (await fetch(sessions, "SELECT max(ts) FROM chat_metrics_1s"))[0]
    assert state.cursor == newest + ONE  # held at the newest row, not fed as zeros

    # the next seconds arrive late (a slow flush): fed with their values, not counted late
    async with sessions.begin() as s:
        await s.execute(
            text(
                "INSERT INTO chat_metrics_1s (channel_id, ts, messages, unique_chatters)"
                " VALUES (:c, :t, 4, 4)"
            ),
            [{"c": cid, "t": newest + ONE * k} for k in range(1, 4)],
        )
    await drive(det, clock, at(C - 16) + LAG)
    assert state.fed[newest + ONE] == 4 and state.late_rows == 0

    # a row for a second already fed with other values: counted once, shown in the heartbeat
    fed_ts = newest - ONE * 5
    async with sessions.begin() as s:
        await s.execute(
            text(
                "UPDATE chat_metrics_1s SET unique_chatters = unique_chatters + 7"
                " WHERE channel_id = :c AND ts = :t"
            ),
            {"c": cid, "t": fed_ts},
        )
    await drive(det, clock, at(C - 12) + LAG)
    assert state.late_rows == 1
    await det.beat()
    (hb,) = await fetch(sessions, "SELECT info FROM worker_heartbeats")
    assert hb.info["late_rows"] == 1 and hb.info["last_hour"]["late_rows"] == 1

    # chat stops for longer than the slack: the clock decides again (zeros)
    await drive(det, clock, at(C + 30) + LAG)
    assert state.cursor == at(C + 30)


async def test_two_writers_at_once_make_one_candidate(sessions):
    """A handover race: both processes hold the channel in memory and decide the same burst."""
    cid, _sid = await add_channel(sessions)
    sim = chat(cid, seed=12)
    burst(sim, FUNNY)
    await store_chat(sessions, sim)
    a, clock_a = await start(sessions, C - 60, worker="det-a")
    async with sessions.begin() as s:
        await s.execute(text("UPDATE leases SET expires_at = now() - interval '1 second'"))
    b, clock_b = await start(sessions, C - 60, worker="det-b")
    assert cid in a.channels and cid in b.channels
    end = at(C + 60) + LAG
    await asyncio.gather(drive(a, clock_a, end, sync=False), drive(b, clock_b, end, sync=False))
    assert await counts(sessions) == (1, 1, 1)
    assert a._last_hour()["emitted"] + b._last_hour()["emitted"] == 1


async def test_a_retaken_channel_does_not_widen_the_others_reads(sessions, monkeypatch):
    ids = []
    for k in range(3):
        cid, _sid = await add_channel(sessions, login=f"ch{k}")
        await store_chat(sessions, chat(cid, seed=k, duration=C + 200))
        ids.append(cid)
    det, clock = await start(sessions, C)
    await drive(det, clock, at(C + 20) + LAG)
    calls = []
    for name in ("raw_chat", "metrics"):
        orig = getattr(reads, name)

        async def spy(session, ranges, until, _orig=orig, _name=name):
            calls.append((_name, dict(ranges), until))
            return await _orig(session, ranges, until)

        monkeypatch.setattr(reads, name, spy)
    del det.channels[ids[0]]  # lost and retaken: it replays and rescans 15 min of chat
    await drive(det, clock, at(C + 40) + LAG)
    retaken = [c for c in calls if c[0] == "raw_chat" and ids[0] in c[1]]
    assert retaken and max((c[2] - c[1][ids[0]]).total_seconds() for c in retaken) >= 800
    for name, ranges, until in calls:
        for other in ids[1:]:
            if other in ranges:  # the others read only from their own cursors
                span = (until - ranges[other]).total_seconds()
                assert span <= (P.final_lag_s + 40 if name == "metrics" else 10), (name, span)


async def test_signals_survive_a_failed_decision(sessions, monkeypatch):
    """A decision that fails puts its signals back; the channel is replayed on the next sync
    and the episode is decided with them."""
    cid, _sid = await add_channel(sessions)
    sim = chat(cid, seed=13)
    burst(sim, FUNNY)
    mentions(sim, C - 5, users=8)  # pending before the episode closes
    await store_chat(sessions, sim)
    det, clock = await start(sessions, C - 60)
    orig = writes.write_candidate
    failures = []

    async def flaky(*args, **kwargs):
        if not failures:
            failures.append(1)
            raise RuntimeError("database went away")
        return await orig(*args, **kwargs)

    monkeypatch.setattr(writes, "write_candidate", flaky)
    await drive(det, clock, at(C + 80) + LAG)
    assert failures
    (cand,) = await fetch(sessions, "SELECT * FROM candidates")
    assert "clip_mentions" in cand.features["explicit"]
    assert cand.score > P.boost_clip_mentions and await counts(sessions) == (1, 1, 1)


TB = 900  # a chain on a young stream: a restart replays from the stream start


async def outcome(sessions) -> list[tuple]:
    rows = await fetch(
        sessions,
        "SELECT id, status, suppress_reason, chat_peak_utc, round(score::numeric, 2) AS score,"
        " features FROM candidates ORDER BY id",
    )
    return [
        (
            r.id,
            r.status,
            r.suppress_reason,
            (r.chat_peak_utc - S).total_seconds(),
            r.score,
            [f["peak"] for f in r.features.get("followups") or ()],
            r.features.get("prev_candidate_id"),
        )
        for r in rows
    ]


async def run_through(sessions, end: int, cut: int | None = None) -> list[tuple]:
    """One live run from TB - 60 to `end`; with `cut`, the process stops there and a new one
    (replay) goes on."""
    async with sessions.begin() as s:
        await s.execute(
            text("TRUNCATE candidates, media_requests, jobs, leases RESTART IDENTITY CASCADE")
        )
    det, clock = await start(sessions, TB - 60)
    if cut is not None:
        await drive(det, clock, at(cut) + LAG)
        await det.release_all()
        det, clock = await start(sessions, cut)
    await drive(det, clock, at(end) + LAG)
    return await outcome(sessions)


async def test_restart_mid_chain_matches_the_uninterrupted_run(sessions):
    """Restarts between the follow-ups of a fold chain: every earlier peak explains one
    replayed episode, the next bursts fold as they would have."""
    cid, _sid = await add_channel(sessions)
    sim = chat(cid, seed=14, duration=TB + 400)
    burst(sim, ("KEKW", "OMEGALUL", "LMAO"), center=TB, factor=6)
    for k in range(1, 6):
        burst(sim, ("KEKW", "OMEGALUL"), center=TB + 45 * k, factor=4.5)
    await store_chat(sessions, sim)
    ref = await run_through(sessions, TB + 330)
    assert sum(len(c[5]) for c in ref) >= 3  # the chain folds
    for cut in (TB + 70, TB + 110, TB + 150, TB + 200):
        assert await run_through(sessions, TB + 330, cut) == ref, cut


async def test_strong_moment_after_a_follow_up_survives_a_restart(sessions):
    """A, a weak follow-up (folded), then a much stronger burst: emitted with prev_candidate_id
    whether or not the process restarted after the follow-up."""
    cid, _sid = await add_channel(sessions)
    sim = chat(cid, seed=21, duration=TB + 320)
    burst(sim, ("KEKW", "OMEGALUL", "LMAO"), center=TB, factor=6)
    burst(sim, ("KEKW", "OMEGALUL"), center=TB + 45, factor=4.5)
    burst(sim, ("Pog", "POGGERS", "LETS GO", "KEKW"), center=TB + 92, factor=12)
    await store_chat(sessions, sim)
    ref = await run_through(sessions, TB + 250)
    assert [c[1] for c in ref] == ["emitted", "emitted"] and ref[1][6] == ref[0][0]
    assert len(ref[0][5]) == 1  # the weak one folded
    for cut in (TB + 70, TB + 100):
        assert await run_through(sessions, TB + 250, cut) == ref, cut
