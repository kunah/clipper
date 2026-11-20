"""Restart and handover vs an uninterrupted run, over seeded random fold chains (Postgres).

Each seed is one channel (metrics only: stage 2 passes everything, raids suppress): a base rate,
sometimes busier chat before the first burst (a baseline a late replay sees differently), a burst
A, 0-5 follow-ups 35-60 s apart (weak, or now and then far stronger), sometimes a later separate
moment. One detector process runs all channels uninterrupted; then the same data with the process
stopped and a new one replaying (immediately, or after an outage), and with a handover where both
processes decide for 40 s. The time windows are scaled down (baseline 400 s, replay 460 s, emit
window 350 s) to keep it fast; the property doesn't depend on their sizes.

Every candidate of the uninterrupted run must exist after the restart (chat peak +- 3 s, same
status) or be folded into the candidate it followed; no moment may exist twice (two candidates,
or a candidate and another's follow-up, at one peak). Other differences are allowed only where
the replay saw another baseline: episodes with an onset before the new process's own history is
complete (start - 60 s) that the replay decided, and what follows from them in that channel
(cooldown, budget). They are counted and printed.
"""

import asyncio
import random
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from clipper_detector.lexicon import Lexicon
from clipper_detector.params import DetectorParams
from clipper_detector.service import LiveDetector
from clipper_detector.settings import DetectorSettings
from clipper_detector.synthetic import bump, poisson

pytestmark = pytest.mark.integration

S = datetime(2026, 11, 20, 12, tzinfo=UTC)
P = DetectorParams(
    baseline_window_s=400,
    min_baseline_values=120,
    warmup_stream_s=240,
    prior_until_s=400,
    restart_replay_s=460,
    restart_emit_s=350,
)
TB = 520  # first burst, s after the stream start (a late replay starts after the stream start)
END = TB + 460
SEEDS = range(30)
LEXICON = Lexicon.load()
LAG = timedelta(seconds=P.final_lag_s)
TOL = 3.0  # s: the same moment
# (stop, start): the first process stops, the next one starts (= stop: an immediate restart)
RESTARTS = [(TB + 70, TB + 70), (TB + 150, TB + 150), (TB + 90, TB + 330)]
HANDOVER = TB + 110  # the second process takes over; the first goes on for 40 s


def at(s: float) -> datetime:
    return S + timedelta(seconds=s)


class Clock:
    def __init__(self, s: float) -> None:
        self.now = at(s) + LAG

    def __call__(self) -> datetime:
        return self.now


def scenario(seed: int):
    rng = random.Random(seed)
    base = rng.uniform(3, 6)
    shift = None
    if rng.random() < 0.5:  # busier chat before A
        shift = (TB - rng.randint(100, 250), TB - 15, rng.uniform(2, 6))
    a = TB + rng.randint(-10, 10)
    bursts = [(a, rng.uniform(5, 7))]
    t = a
    for _ in range(rng.randint(0, 5)):
        t += rng.randint(35, 60)
        bursts.append((t, rng.uniform(8, 12) if rng.random() < 0.25 else rng.uniform(2.4, 5)))
    if rng.random() < 0.5:
        bursts.append((TB + rng.randint(340, 400), rng.uniform(4, 8)))
    raids = [c for c, _ in bursts if rng.random() < 0.15]

    def rate(i: int) -> float:
        r = base + (shift[2] if shift and shift[0] <= i < shift[1] else 0.0)
        return r + sum(base * (bump(i, c, 15, f) - 1) for c, f in bursts)

    return rng, rate, raids


async def seed_data(sessions) -> dict[int, int]:
    """One channel + live stream per seed; per-second stats (COPY, no raw chat) and raids."""
    channels: dict[int, int] = {}
    metrics, events = [], []
    async with sessions.begin() as s:
        for seed in SEEDS:
            cid = (
                await s.execute(
                    text("INSERT INTO channels (login) VALUES (:l) RETURNING id"),
                    {"l": f"prop{seed}"},
                )
            ).scalar_one()
            sid = (
                await s.execute(
                    text(
                        "INSERT INTO streams (channel_id, platform_stream_id, started_at)"
                        " VALUES (:c, :p, :t) RETURNING id"
                    ),
                    {"c": cid, "p": f"b{seed}", "t": S},
                )
            ).scalar_one()
            channels[cid] = seed
            rng, rate, raids = scenario(seed)
            for i in range(END + 30):
                u = poisson(rng, rate(i))
                if u:
                    metrics.append((cid, at(i), u, u))
            events += [{"c": cid, "s": sid, "t": at(c - 12)} for c in raids]
        if events:
            await s.execute(
                text(
                    "INSERT INTO chat_events (channel_id, stream_id, sent_at, type, data)"
                    " VALUES (:c, :s, :t, 'raid', '{\"viewerCount\": \"500\"}')"
                ),
                events,
            )
        raw = (await (await s.connection()).get_raw_connection()).driver_connection
        async with raw.cursor() as cur:
            columns = "channel_id, ts, messages, unique_chatters"
            async with cur.copy(f"COPY chat_metrics_1s ({columns}) FROM STDIN") as copy:
                for row in metrics:
                    await copy.write_row(row)
    return channels


def detector(sessions, clock, worker: str) -> LiveDetector:
    return LiveDetector(
        sessions,
        settings=DetectorSettings(_env_file=None, lease_renew_s=0, max_channels=1000),
        params=P,
        lexicon=LEXICON,
        worker_id=worker,
        clock=clock,
    )


async def drive(det: LiveDetector, clock: Clock, until_s: float, step: float = 60) -> None:
    """Live ticks every `step` s of clock (decisions don't depend on the tick size)."""
    until = at(until_s) + LAG
    while clock.now < until:
        clock.now = min(clock.now + timedelta(seconds=step), until)
        await det.live_tick()


async def run(sessions, *, stop=None, start=None, handover=None) -> dict[int, list[dict]]:
    async with sessions.begin() as s:
        await s.execute(
            text("TRUNCATE candidates, media_requests, jobs, leases RESTART IDENTITY CASCADE")
        )
    clock = Clock(TB - 60)
    det = detector(sessions, clock, "first")
    await det.sync()
    if handover is not None:
        await drive(det, clock, handover)
        async with sessions.begin() as s:
            await s.execute(text("UPDATE leases SET expires_at = now() - interval '1 second'"))
        clock2 = Clock(handover)
        det2 = detector(sessions, clock2, "second")
        await det2.sync()
        await asyncio.gather(drive(det, clock, handover + 40), drive(det2, clock2, handover + 40))
        await det.sync()  # notices and lets go
        assert det.channels == {}
        det, clock = det2, clock2
    elif stop is not None:
        await drive(det, clock, stop)
        await det.release_all()
        clock = Clock(start)
        det = detector(sessions, clock, "first")  # a restart: the same worker id
        await det.sync()
    await drive(det, clock, END)
    async with sessions() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT channel_id, id, status, chat_peak_utc, features FROM candidates"
                    " ORDER BY channel_id, chat_peak_utc"
                )
            )
        ).all()

    def rel(iso: str) -> float:
        return (datetime.fromisoformat(iso) - S).total_seconds()

    out: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        f = r.features
        out[r.channel_id].append(
            {
                "id": r.id,
                "status": r.status,
                "peak": (r.chat_peak_utc - S).total_seconds(),
                "onset": rel(f["chat"]["onset"]),
                "follow": [rel(x["peak"]) for x in f.get("followups") or ()],
                "prev": f.get("prev_candidate_id"),
            }
        )
    return out


def near(a: float, b: float) -> bool:
    return abs(a - b) <= TOL


def compare(ref: list[dict], got: list[dict], start: float) -> Counter:
    """Classify one channel's differences (see the module docstring)."""
    c: Counter = Counter()
    for i, g in enumerate(got):  # no moment twice
        c["violation.twice"] += sum(near(g["peak"], h["peak"]) for h in got[i + 1 :])
        c["violation.twice"] += sum(
            any(near(g["peak"], f) for f in h["follow"]) for h in got if h is not g
        )
    events = sorted(
        [("ref", r) for r in ref] + [("got", g) for g in got], key=lambda e: e[1]["peak"]
    )
    by_id = {r["id"]: r for r in ref}
    baseline_diff = False  # once the replay's baseline made a difference, later ones follow
    for side, x in events:
        affected = x["onset"] < start - 60 or baseline_diff
        if side == "ref":
            c["ref"] += 1
            match = [g for g in got if near(g["peak"], x["peak"])]
            if match and match[0]["status"] == x["status"]:
                c["same"] += 1
                continue
            prev = by_id.get(x["prev"])
            folded = [g for g in got if any(near(x["peak"], f) for f in g["follow"])]
            if not match and prev and folded and near(folded[0]["peak"], prev["peak"]):
                c["folded_into_prev"] += 1  # allowed: into the candidate it followed
            else:
                kind = "status" if match else "missing"
                c[f"{'baseline' if affected else 'violation'}.{kind}"] += 1
        elif not any(near(x["peak"], r["peak"]) for r in ref):
            c["baseline.new" if affected else "violation.new"] += 1
        else:
            continue
        baseline_diff = True
    return c


async def test_restarts_and_handover_keep_the_uninterrupted_candidates(sessions):
    channels = await seed_data(sessions)
    ref = await run(sessions)
    cands = [c for v in ref.values() for c in v]
    assert len(cands) >= len(channels)  # every seed has candidates
    assert sum(len(c["follow"]) for c in cands) >= 15  # fold chains
    assert any(c["prev"] for c in cands)  # stronger follow-ups emitted on their own
    assert any(c["status"] == "suppressed" for c in cands)  # raids
    totals: Counter = Counter()
    report = []
    variants = [{"stop": a, "start": b} for a, b in RESTARTS] + [{"handover": HANDOVER}]
    for kw in variants:
        got = await run(sessions, **kw)
        start = kw.get("start", kw.get("handover"))
        run_c = Counter()
        for cid in channels:
            run_c += compare(ref.get(cid, []), got.get(cid, []), start)
        report.append((kw, dict(sorted(run_c.items()))))
        totals += run_c
    print(f"\nrestart property: {len(channels)} seeds, {len(cands)} candidates per run")
    for kw, c in report:
        print(" ", kw, c)
    assert {k: v for k, v in totals.items() if k.startswith("violation")} == {}, report
