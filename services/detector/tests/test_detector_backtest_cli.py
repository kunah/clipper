"""`clipper-detector backtest` against Postgres (DESIGN §18 build item 4).

Synthetic chat (`ChatSim`) is stored as chat-ingest writes it, viewer clips as the backfill tool
writes them. The backtest runs on a read-only engine (every write raises) and every table is
compared before and after; one test runs the live service over the same rows and compares its
candidates with the backtest's drafts (one scorer for live and backtest).
"""

import io
import json
import random
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from clipper_core.chat import ChatWriter, SecondAggregator
from clipper_core.db import create_engine, session_factory
from clipper_core.models import Base
from clipper_detector import SCORER_VERSION, backtest_cli
from clipper_detector.lexicon import Lexicon
from clipper_detector.params import DetectorParams
from clipper_detector.service import LiveDetector
from clipper_detector.settings import DetectorSettings
from clipper_detector.synthetic import ChatSim, bump

pytestmark = pytest.mark.integration

S = datetime(2026, 11, 20, 12, tzinfo=UTC)  # stream start
END = 3600  # the stream ends 1 h after S; chat is stored for its first 40 min only
CHAT_S = 2400
P = DetectorParams()
LAG = timedelta(seconds=P.final_lag_s)
NOW = S + timedelta(days=1)
EMOTES = frozenset({"KEKW", "LUL", "Pog", "OMEGALUL", "xqcSkip", "peepoHey"})
NORMAL = ("hello there", "what game is this", "nice", "ok", "LUL", "gg", "true", "based", "xqcSkip")
FUNNY = ("KEKW", "KEKW KEKW", "LMAOOO", "OMEGALUL", "KEKW what")
RAID = ("bigraiderRaid bigraiderRaid", "raid hype", "here from the raid", "hello")
# chat peaks (s after S): a funny moment, a raid and a real moment 45 s after it (the live write
# dedupe drops that one), a funny moment with a weaker follow-up 45 s later (folded in)
FUNNY_1, RAID_AT, AFTER_RAID, FUNNY_2 = 900, 1500, 1545, 2100


def at(s: float) -> datetime:
    return S + timedelta(seconds=s)


def burst(sim: ChatSim, texts, center: int, factor: float = 4.0) -> None:
    """15 s x`factor` raised-cosine burst on top of the 5/s chat, peaking at `center`."""
    sim.chatter(center - 15, 30, lambda i: 5 * (bump(i, 15, 15, factor) - 1), texts)


async def one(sessions, sql: str, **values):
    async with sessions.begin() as s:
        return (await s.execute(text(sql), values)).scalar()


async def add_channel(sessions, login: str = "testchan") -> int:
    return await one(
        sessions, "INSERT INTO channels (login, platform_user_id) VALUES (:l, :u) RETURNING id",
        l=login, u=f"u-{login}",
    )  # fmt: skip


async def add_stream(
    sessions, cid: int, start: datetime, end: datetime | None, *, source: str = "live"
) -> int:
    return await one(
        sessions,
        "INSERT INTO streams (channel_id, platform_stream_id, started_at, ended_at, status,"
        " source) VALUES (:c, :p, :t, :e, :st, :src) RETURNING id",
        c=cid, p=f"b-{cid}-{start.timestamp():.0f}", t=start, e=end,
        st="live" if end is None else "ended", src=source,
    )  # fmt: skip


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


async def add_clips(sessions, cid: int, sid: int, clips) -> None:
    """(clip id, video start s after S or None, views): 30 s long, slack 40 s (duration + 10,
    as the backfill tool writes for `vod_t0`)."""
    async with sessions.begin() as s:
        await s.execute(
            text(
                """
                INSERT INTO viewer_clips (clip_id, channel_id, stream_id, vod_offset_s,
                    duration_s, start_utc, end_utc, slack_s, time_source, view_count,
                    platform_created_at)
                VALUES (:id, :c, :s, :off, 30, :a, :b, :slack, 'vod_t0', :v, :made)
                """
            ),
            [
                {
                    "id": clip_id,
                    "c": cid,
                    "s": sid,
                    "off": None if start is None else int(start),
                    "a": None if start is None else at(start),
                    "b": None if start is None else at(start + 30),
                    "slack": None if start is None else 40.0,
                    "v": views,
                    "made": at((start or 0) + 60),
                }
                for clip_id, start, views in clips
            ],
        )


def moments(cid: int, *, seed: int = 0, chat_s: int = CHAT_S) -> ChatSim:
    sim = ChatSim(random.Random(seed), S, channel_id=cid, stream_id=None, emote_names=EMOTES)
    sim.chatter(0, chat_s, 5, NORMAL)
    burst(sim, FUNNY, FUNNY_1)
    sim.event(RAID_AT - 12, "raid", user_login="bigraider", viewerCount="3000")
    burst(sim, RAID, RAID_AT)
    burst(sim, FUNNY, AFTER_RAID)
    burst(sim, FUNNY, FUNNY_2, factor=5)
    burst(sim, ("KEKW", "OMEGALUL"), FUNNY_2 + 45, factor=2.6)
    return sim


# clips in video time (= chat time - 8 s): the moment ~20 s into a 30 s clip
CLIPS = [
    ("funny1", FUNNY_1 - 28, 500),  # hit by the first moment
    ("raid", RAID_AT - 28, 300),  # the raid (suppressed) and the dropped moment after it
    ("quiet", 1200, 200),  # nothing in chat: a miss
    ("late", 3000, 400),  # after the stored chat: outside, not scored
    ("nospan", None, 1000),  # no vod_offset yet
]


async def setup_stream(sessions) -> tuple[int, int]:
    """A live stream (events and viewers known) with the moments, the clips and a previous
    stream (the prior)."""
    cid = await add_channel(sessions)
    await add_stream(sessions, cid, at(-3 * 3600), at(-2 * 3600))  # the previous stream
    sid = await add_stream(sessions, cid, S, at(END))
    old = ChatSim(random.Random(9), at(-3 * 3600), channel_id=cid, stream_id=None)
    old.chatter(0, 1800, 3, NORMAL)  # quieter: the prior doesn't raise the bar
    await store_chat(sessions, old)
    await store_chat(sessions, moments(cid))
    await add_viewers(sessions, sid, END)
    await add_clips(sessions, cid, sid, CLIPS)
    return cid, sid


async def snapshot(sessions) -> dict[str, tuple[int, str]]:
    """Every table's row count and a hash of its rows: an insert, update or delete shows."""
    sql = "SELECT count(*), md5(COALESCE(string_agg(x::text, '|' ORDER BY x::text), '')) FROM {} x"
    async with sessions() as s:
        return {
            t.name: tuple((await s.execute(text(sql.format(t.name)))).one())
            for t in Base.metadata.sorted_tables
        }


def read_only_engine(settings, **kwargs):
    return create_engine(settings, connect_args=backtest_cli.READ_ONLY, **kwargs)


@pytest.fixture
async def read_only(settings, sessions):
    """Sessions on the test database (after `sessions` cleaned it) that can't write."""
    engine = read_only_engine(settings)
    yield session_factory(engine)
    await engine.dispose()


async def backtest(argv: list[str], sessions, **kw) -> tuple[int, str]:
    args = backtest_cli.parse_args(argv)
    cfg = backtest_cli.configure(args, DetectorSettings(_env_file=None))
    out = io.StringIO()
    code = await backtest_cli.run_backtest(args, sessions, cfg, out=out, now=kw.get("now", NOW))
    return code, out.getvalue()


# --------------------------------------------------------------------------- tests


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO candidates (stream_id) VALUES (1)",
        "INSERT INTO leases (resource, owner, expires_at) VALUES ('detect:channel:1', 'x', now())",
        "UPDATE channels SET active = false",
        "DELETE FROM jobs",
    ],
)
async def test_the_read_only_sessions_refuse_every_write(read_only, sql):
    async with read_only() as s:
        with pytest.raises(DBAPIError, match="read-only transaction"):
            await s.execute(text(sql))


async def test_planted_moment_inside_a_clip_is_a_hit_and_nothing_is_written(sessions, read_only):
    _cid, sid = await setup_stream(sessions)
    before = await snapshot(sessions)
    code, out = await backtest(["--stream", str(sid), "--json"], read_only)
    assert code == 0
    doc = json.loads(out)
    (st,) = doc["streams"]
    assert st["stream_id"] == sid and st["source"] == "live"
    assert st["events_known"] and st["viewers_known"] and st["prior"] is not None
    # chat for the first 40 min of the 1 h stream: hours and k count chat hours
    assert st["hours"] == pytest.approx(CHAT_S / 3600, abs=2 / 3600)
    assert datetime.fromisoformat(st["chat"]["to"]) == at(CHAT_S)
    assert st["clips"] == {
        "total": 5,
        "no_span": 1,
        "truth": 5,  # all clips are ranked, the one without a span too
        "scored": 3,
        "excluded": {"no_span": 1, "outside_chat": 1},
    }
    assert st["warnings"] == [] and st["candidates"]["after_broadcast"] == 0
    # 3 clips of 110 s (30 s + 2 x 40 s slack) in 40 min of chat; "late" lies after it
    assert st["chance"] == pytest.approx(330 / CHAT_S, abs=2e-3)
    assert st["candidates"]["emitted"] == 2  # the two funny moments (the follow-up folded)
    assert st["candidates"]["suppressed"] == {"raid": 1}
    assert st["candidates"]["deduped"] == 1  # the moment 45 s after the raid
    assert st["folded"] == 1
    four = st["at"]["4/h"]
    assert (four["k"], four["considered"], four["hits"], four["hit_rate"]) == (3, 2, 1, 0.5)
    assert (four["truth"], four["truth_hit"], four["truth_views"], four["hit_views"]) == (
        3,
        1,
        1000,
        500,
    )
    assert four["recall"] == 0.5
    assert (four["moments"], four["moments_hit"], four["moment_recall"]) == (3, 1, 0.5)
    assert 18 <= four["delay_median_s"] <= 22  # video peak ~20 s after the clip's start
    assert st["at"]["2/h"]["k"] == 2 and st["at"]["8/h"]["k"] == 6
    # the raid clip is missed; the best suppressed candidate inside it is the deduped moment
    assert four["false_suppressions"] == {"dedupe": 1}
    assert st["suppressed_in_truth"] == {"dedupe": 1, "raid": 1}
    by_clip = {t["clip_id"]: t for t in st["truth_clips"]}
    assert by_clip["funny1"]["best_rank"] is not None and by_clip["quiet"]["best_rank"] is None
    assert by_clip["late"]["excluded"] == "outside_chat"
    (hit,) = [c for c in st["candidate_list"] if c["truth_clip"] == "funny1"]
    assert hit["status"] == "emitted" and hit["moment_type"] == "funny"
    assert doc["total"]["at"]["4/h"]["recall"] == 0.5
    assert doc["detector_version"] == st["detector_version"] == doc["label"]
    assert await snapshot(sessions) == before  # no candidates, leases, jobs, ...

    # the table: one row per stream and a total
    code, table = await backtest(["--stream", str(sid)], read_only)
    lines = table.splitlines()
    assert code == 0 and lines[0].startswith("backtest: 1 stream(s), detector d")
    head, row, total = lines[2:]
    assert head.split()[:4] == ["stream", "channel", "hours", "clips"]
    assert head.split()[-7:-2] == [
        "recall@2/h",
        "recall@4/h",
        "recall@8/h",
        "moments@4/h",
        "delay_s",
    ]
    cells = row.split()
    assert cells[:2] == [str(sid), "testchan"] and cells[3:9] == ["5", "5", "3", "1", "0", "1"]
    assert cells[11:13] == ["0.50", "0.14"]  # hit@k, chance
    assert cells[-7:-3] == ["0.50", "0.50", "0.50", "0.50"] and cells[-2:] == ["dedupe", "1"]
    assert total.split()[0] == "total"
    assert await snapshot(sessions) == before


async def test_backtest_equals_the_live_service_on_the_same_rows(sessions, read_only):
    """One scorer: the live service, run over the whole stored stream from its start, writes
    exactly the candidates the backtest's pipeline drafts (all columns, features included),
    and drops the same moment (the write dedupe)."""
    cid, sid = await setup_stream(sessions)
    args = backtest_cli.parse_args(["--stream", str(sid)])
    cfg = backtest_cli.configure(args, DetectorSettings(_env_file=None))
    (replayed,) = await backtest_cli.backtest_stream(read_only, sid, cfg, now=NOW)
    run = replayed.run
    assert len(run.drafts) == 3 and len(run.duplicates) == 1 and len(run.folded) == 1

    class Clock:
        now = S + LAG + timedelta(seconds=1)

        def __call__(self) -> datetime:
            return self.now

    clock = Clock()
    det = LiveDetector(
        sessions,
        settings=DetectorSettings(_env_file=None, judge_jobs=False, lease_renew_s=0),
        params=P,
        lexicon=Lexicon.load(),
        worker_id="det-live",
        clock=clock,
    )
    await det.sync()  # takes the channel at the stream's start
    assert det.channels[cid].live.info.id == sid
    clock.now = at(END) + LAG + timedelta(seconds=60)
    await det.live_tick()  # every second of the stream in one tick
    await det.explicit_tick()  # no clip mentions in this chat: no signals
    async with sessions() as s:
        rows = (
            (await s.execute(text("SELECT * FROM candidates ORDER BY peak_utc"))).mappings().all()
        )
    assert det._last_hour()["duplicate"] == 1 and det._last_hour()["folded"] == 1

    drafts = sorted(run.drafts, key=lambda d: d.peak_utc)
    assert len(rows) == len(drafts) == 3
    for row, draft in zip(rows, drafts, strict=True):
        expected = json.loads(json.dumps(draft.row(), default=str))
        got = {k: row[k] for k in draft.row()}
        assert {k: v for k, v in got.items() if k != "features"} == {
            k: v for k, v in draft.row().items() if k != "features"
        }
        assert got["features"] == expected["features"]
    dropped = run.duplicates[0].draft.chat_peak_utc
    assert all(abs((r["chat_peak_utc"] - dropped).total_seconds()) > 5 for r in rows)


async def test_grid_prints_one_row_per_combination_sorted_by_recall(sessions, read_only):
    _cid, sid = await setup_stream(sessions)
    before = await snapshot(sessions)
    argv = ["--stream", str(sid), "--grid", "floor_activity=400,8", "--grid", "z_clean_emit=4,5"]
    code, out = await backtest(argv, read_only)
    assert code == 0
    lines = out.splitlines()
    assert lines[0].startswith("backtest: 1 stream(s), 4 combinations")
    head, *rows = lines[2:]
    assert head.split()[:3] == ["floor_activity", "z_clean_emit", "streams"]
    assert len(rows) == 4
    # floor 400 > any activity: nothing opens, recall 0 -> last
    assert [r.split()[0] for r in rows] == ["8", "8", "400", "400"]
    assert all(r.split()[-1].startswith(f"d{SCORER_VERSION}-p") for r in rows)
    assert len({r.split()[-1] for r in rows}) == 4  # one detector_version per combination

    code, out = await backtest([*argv, "--json"], read_only)
    doc = json.loads(out)
    assert doc["grid_keys"] == ["floor_activity", "z_clean_emit"]
    first, *_, last = doc["grid"]
    assert first["params"]["floor_activity"] == 8 and first["total"]["at"]["4/h"]["recall"] == 0.5
    assert last["params"]["floor_activity"] == 400 and last["total"]["candidates"]["emitted"] == 0
    assert "candidate_list" not in first["streams"][0]  # details only without --grid
    # every combination is scored on the same clips
    assert {c["total"]["clips"]["scored"] for c in doc["grid"]} == {3}
    assert await snapshot(sessions) == before


async def test_backfilled_streams_partial_chat_muted_and_uncertain(sessions, read_only):
    """`--channel` with `--since`: a backfilled stream whose chat runs to `chat_metrics_to`
    (events and viewers unknown: the raid isn't suppressed), a muted clip and an uncertain
    timeline (added to the slack, a warning); one without viewer clips is listed, not scored;
    an older stream is outside `--since`."""
    cid = await add_channel(sessions, "bfchan")
    sid = await add_stream(sessions, cid, S, at(END), source="backfill")
    other = await add_stream(sessions, cid, at(2 * END), at(3 * END), source="backfill")
    old = await add_stream(sessions, cid, at(-5 * END), at(-4 * END), source="backfill")
    for stream, flags, chat_to in (
        # 40 s of quiet chat after the last row (< 60 s: not missing)
        (sid, {"timeline_uncertain_s": 3.5}, at(CHAT_S + 40)),
        (other, {}, at(3 * END)),
        (old, {}, at(-4 * END)),
    ):
        await one(
            sessions,
            "INSERT INTO vods (channel_id, stream_id, platform_video_id, platform_created_at,"
            " duration_s, t0_utc, t0_method, flags, chat_metrics_to) VALUES (:c, :s, :v, :t,"
            " 3600, :t, 'pdt', CAST(:f AS jsonb), :m) RETURNING id",
            c=cid, s=stream, v=f"v{stream}", t=S, f=json.dumps(flags), m=chat_to,
        )  # fmt: skip
    await store_chat(sessions, moments(cid, seed=3), events=False)
    await add_clips(
        sessions,
        cid,
        sid,
        [("funny1", FUNNY_1 - 28, 500), ("music", 1200, 300), ("tail", CHAT_S - 5, 100)],
    )
    await one(
        sessions,
        "INSERT INTO stream_gaps (stream_id, start_utc, end_utc, reason)"
        " VALUES (:s, :a, :b, 'muted') RETURNING id",
        s=sid, a=at(1180), b=at(1300),
    )  # fmt: skip
    before = await snapshot(sessions)
    since = (S - timedelta(hours=1)).isoformat()
    code, out = await backtest(["--channel", "BFchan", "--since", since, "--json"], read_only)
    assert code == 0
    doc = json.loads(out)
    assert [s["stream_id"] for s in doc["streams"]] == [sid, other]  # old: before --since
    st, skipped = doc["streams"]
    assert not st["events_known"] and not st["viewers_known"]
    assert st["stream"]["broadcast_end_from"] == "vod"  # t0_utc + duration_s
    assert datetime.fromisoformat(st["stream"]["broadcast_end"]) == at(3600)
    assert st["timeline_uncertain_s"] == 3.5 and "timeline uncertain" in st["warnings"][0]
    assert {t["slack_s"] for t in st["truth_clips"]} == {43.5}  # 40 s + 3.5 s
    # quiet seconds up to chat_metrics_to are chat we have: "tail" (chat time up to
    # CHAT_S + 33) is inside
    assert datetime.fromisoformat(st["chat"]["to"]) == at(CHAT_S + 40)
    assert st["clips"]["excluded"] == {"muted": 1} and st["clips"]["scored"] == 2
    assert "raid" not in st["candidates"]["suppressed"]  # events unknown: no raid rule
    assert st["at"]["4/h"]["recall"] == pytest.approx(500 / 600, abs=1e-3)  # JSON: 3 digits
    assert skipped["note"] == "no viewer clips" and "hours" not in skipped
    assert doc["total"]["streams"] == 1
    assert await snapshot(sessions) == before

    code, table = await backtest(["--channel", "bfchan", "--since", since], read_only)
    *_, warning, note = table.splitlines()
    assert warning == (
        f"stream {sid} (bfchan): warning: timeline uncertain:"
        " vods.flags.timeline_uncertain_s = 3.5 s added to every clip's slack"
    )
    assert note == f"stream {other} (bfchan): not scored, no viewer clips"


async def test_coverage_ends_at_the_broadcast_end(sessions, read_only):
    """A live stream without a vods row: the last chunk's end (+ the delay) ends the chat
    coverage; offline chat after it is no coverage and its candidates are left out."""
    _cid, sid = await setup_stream(sessions)
    await one(
        sessions,
        "INSERT INTO chunks (stream_id, seq, start_utc, end_utc, audio_key)"
        " VALUES (:s, 0, :a, :b, 'k') RETURNING id",
        s=sid, a=S, b=at(1700),
    )  # fmt: skip
    code, out = await backtest(["--stream", str(sid), "--json"], read_only)
    assert code == 0
    (st,) = json.loads(out)["streams"]
    assert st["stream"]["broadcast_end_from"] == "chunks"
    assert datetime.fromisoformat(st["chat"]["coverage"][-1][1]) == at(1708)
    assert st["hours"] == pytest.approx(1708 / 3600, abs=2 / 3600)
    # FUNNY_2 (2100) lies after the broadcast: left out; FUNNY_1 is the only one scored
    assert st["candidates"]["after_broadcast"] == 1 and st["candidates"]["emitted"] == 1
    (late,) = [c for c in st["candidate_list"] if c["after_broadcast"]]
    assert late["rank"] is None and late["status"] == "emitted"
    assert st["clips"]["excluded"] == {"no_span": 1, "outside_chat": 1}


async def test_usage_errors_exit_2(sessions, read_only, capsys):
    cid = await add_channel(sessions)
    with pytest.raises(backtest_cli.UsageError, match="unknown stream id"):
        await backtest(["--stream", "999"], read_only)
    with pytest.raises(backtest_cli.UsageError, match="unknown channel"):
        await backtest(["--channel", "nobody"], read_only)
    with pytest.raises(backtest_cli.UsageError, match="no streams"):
        await backtest(["--channel", "testchan"], read_only)
    with pytest.raises(backtest_cli.UsageError, match="not a detector param"):
        backtest_cli.configure(
            backtest_cli.parse_args(["--stream", "1", "--grid", "z_on=3,4"]),
            DetectorSettings(_env_file=None),
        )
    with pytest.raises(SystemExit):
        backtest_cli.parse_args(["--stream", "1", "--since", "2026-11-01"])
    assert "--since/--until go with --channel" in capsys.readouterr().err
    # listed with a note, not scored: no clips; only clips without a span; no chat rows
    no_clips = await add_stream(sessions, cid, S, at(600))
    unspanned = await add_stream(sessions, cid, at(700), at(800))
    no_chat = await add_stream(sessions, cid, at(900), at(1000))
    await add_clips(sessions, cid, unspanned, [("pending", None, 10)])
    await add_clips(sessions, cid, no_chat, [("clip", 950, 10)])
    code, out = await backtest(["--stream", str(no_clips), str(unspanned), str(no_chat)], read_only)
    assert code == 0
    assert out.splitlines()[2:] == [
        f"stream {no_clips} (testchan): not scored, no viewer clips",
        f"stream {unspanned} (testchan): not scored, no spanned clips (no vod_offset yet)",
        f"stream {no_chat} (testchan): not scored, no chat rows",
    ]


async def test_amain_runs_on_a_read_only_engine(sessions, settings, monkeypatch, capsys):
    _cid, sid = await setup_stream(sessions)
    before = await snapshot(sessions)
    engines = []

    def engine(s, **kwargs):
        engines.append(kwargs)
        return create_engine(settings, **kwargs)

    monkeypatch.setattr(backtest_cli, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(backtest_cli, "Settings", lambda **kwargs: settings)  # the test database
    monkeypatch.setattr(backtest_cli, "DetectorSettings", lambda: DetectorSettings(_env_file=None))
    monkeypatch.setattr(backtest_cli, "create_engine", engine)
    assert await backtest_cli.amain(backtest_cli.parse_args(["--stream", str(sid)])) == 0
    assert engines == [{"connect_args": backtest_cli.READ_ONLY}]
    assert capsys.readouterr().out.splitlines()[2].split()[0] == "stream"
    assert await backtest_cli.amain(backtest_cli.parse_args(["--stream", "999"])) == 2
    assert capsys.readouterr().err.startswith("[error] unknown stream id(s): 999")
    bad = backtest_cli.parse_args(["--stream", str(sid), "--grid", "z_open=1"])
    assert await backtest_cli.amain(bad) == 2
    assert "z_stay <= z_suspect <= z_open" in capsys.readouterr().err
    assert await snapshot(sessions) == before


async def test_a_six_hour_stream_takes_well_under_a_minute(sessions, read_only, record_property):
    """DESIGN §18: < 1 min per stream. 6 h of 3/s chat (65k messages) with a moment every
    30 min, viewers and raids; timed from the reads to the score."""
    cid = await add_channel(sessions, "longchan")
    hours = 6
    sid = await add_stream(sessions, cid, S, at(hours * 3600))
    sim = ChatSim(random.Random(11), S, channel_id=cid, stream_id=None, emote_names=EMOTES)
    sim.chatter(0, hours * 3600, 3, NORMAL)
    centers = list(range(1200, hours * 3600 - 600, 1800))
    for k, c in enumerate(centers):
        if k % 4 == 3:
            sim.event(c - 12, "raid", user_login=f"raider{k}", viewerCount="2000")
            sim.chatter(c - 15, 30, lambda i: 3 * (bump(i, 15, 15, 5) - 1), RAID)
        else:
            sim.chatter(c - 15, 30, lambda i: 3 * (bump(i, 15, 15, 5) - 1), FUNNY)
    await store_chat(sessions, sim)
    await add_viewers(sessions, sid, hours * 3600)
    await add_clips(sessions, cid, sid, [(f"c{k}", c - 28, 200 - k) for k, c in enumerate(centers)])
    started = time.perf_counter()
    code, out = await backtest(["--stream", str(sid), "--json"], read_only)
    elapsed = time.perf_counter() - started
    record_property("backtest_6h_s", round(elapsed, 2))
    assert code == 0
    (st,) = json.loads(out)["streams"]
    assert st["hours"] == pytest.approx(hours, abs=0.01)
    truth = {t["clip_id"]: t for t in st["truth_clips"]}
    assert len(truth) == 10  # the top 10 by views (c0..c9)
    raids = {f"c{k}" for k in range(len(centers)) if k % 4 == 3}
    assert {c for c, t in truth.items() if t["best_rank"] is None} == raids & truth.keys()
    assert st["at"]["8/h"]["false_suppressions"]["raid"] == len(raids & truth.keys())
    assert st["at"]["8/h"]["truth_hit"] == len(truth.keys() - raids)
    # the last moment is at 19200 s: chat runs ~40 min past the last clip
    (warning,) = st["warnings"]
    assert "past the last viewer clip" in warning
    assert elapsed < 60


async def test_export_drafts_writes_every_draft_without_scoring(
    sessions, read_only, tmp_path, capsys
):
    """`--export-drafts`: the base params' drafts (emitted and suppressed) of each stream, the
    same the backtest drafts, also for a stream without viewer clips; nothing is written."""
    cid, sid = await setup_stream(sessions)
    # a later stream with chat but without viewer clips (the backtest can't score it)
    bare = await add_stream(sessions, cid, at(2 * 3600), at(3 * 3600))
    later = ChatSim(
        random.Random(3), at(2 * 3600), channel_id=cid, stream_id=None, emote_names=EMOTES
    )
    later.chatter(0, 1800, 5, NORMAL)
    burst(later, FUNNY, 900, factor=5)
    await store_chat(sessions, later)
    before = await snapshot(sessions)
    path = tmp_path / "out" / "drafts.json"
    args = backtest_cli.parse_args(["--stream", str(sid), "--export-drafts", str(path)])
    cfg = backtest_cli.configure(args, DetectorSettings(_env_file=None))
    out = io.StringIO()
    assert await backtest_cli.export_drafts(args, read_only, cfg, out=out, now=NOW) == 0
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["format"] == "clipper-detector-drafts" and doc["version"] == 1
    assert doc["streams"] == [sid] and doc["params"] == "code defaults"
    (replayed,) = await backtest_cli.backtest_stream(read_only, sid, cfg, now=NOW)
    expected = sorted(replayed.run.drafts, key=lambda d: d.peak_utc)
    got = sorted(doc["drafts"], key=lambda d: d["peak_utc"])
    assert [d["id"] for d in got] == [backtest_cli.draft_id(d) for d in expected]
    assert [d["status"] for d in got] == [str(d.status) for d in expected]
    first = got[0]
    assert first["id"] == f"d{sid}-{int(expected[0].peak_utc.timestamp())}"
    assert datetime.fromisoformat(first["window_from"]) == expected[0].window_from
    assert set(first) >= {"trigger", "moment_from", "moment_to", "chat_delay_s", "features"}
    assert f"stream {sid}: {len(expected)} drafts" in out.getvalue()
    # a stream without viewer clips (the backtest only lists it) still gives its drafts
    args = backtest_cli.parse_args(["--stream", str(bare), "--export-drafts", str(path)])
    out = io.StringIO()
    assert await backtest_cli.export_drafts(args, read_only, cfg, out=out, now=NOW) == 0
    bare_doc = json.loads(path.read_text(encoding="utf-8"))
    assert bare_doc["streams"] == [bare] and bare_doc["drafts"]
    assert all(d["stream_id"] == bare for d in bare_doc["drafts"])
    code, listed = await backtest(["--stream", str(bare)], read_only)
    assert code == 0 and "not scored, no viewer clips" in listed
    assert await snapshot(sessions) == before  # read-only
    with pytest.raises(SystemExit):
        backtest_cli.parse_args(["--stream", "1", "--export-drafts", "x", "--grid", "z_open=3,4"])
    assert "--export-drafts runs the base params only" in capsys.readouterr().err
