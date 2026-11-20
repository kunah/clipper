"""Backfill of a mock VOD end to end: FakeVodSource + a mock CDN (httpx.MockTransport) serving
lavfi-made 10 s MPEG-TS segments, the fake GQL (chat replay), fixed emote sets, Postgres and an
in-memory store.

The default VOD: 16 segments (160 s), 8-9 muted, a discontinuity before 12, audio_only with
EXT-X-PROGRAM-DATE-TIME = T0 + offset on every segment (as Twitch serves it). Its chat: 120
comments whose replay anchor is T0 + 3.9 s.
"""

import asyncio
import dataclasses
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from clipper_backfill import rows as bf_rows
from clipper_backfill.backfill import Backfiller, VodOptions
from clipper_backfill.chat import ChatClock, StaticEmoteSets, map_comment
from clipper_backfill.cli import status_lines
from clipper_backfill.pipeline import LeaseLost, MediaError
from clipper_backfill.planner import at
from clipper_backfill.settings import BackfillSettings
from clipper_core.chat import SecondAggregator
from clipper_core.contracts import Priority, backfill_lease
from clipper_core.models import Base
from clipper_core.storage import keys
from clipper_twitch.gql import Comment, FakeChatReplay, Fragment, GqlUnavailable, VideoInfo
from clipper_twitch.helix import HelixClient
from clipper_twitch.vod import FakeVodSource, VodGone, VodRestricted

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]

VIDEO = "2000000001"
STREAM = "777000111"
PATH = f"8173ac3b86530f54c2d4_streamer_{STREAM}_1795000000"
CDN = f"https://cdn.test/{PATH}"
AUDIO_URL = f"{CDN}/audio_only/index-dvr.m3u8"
VIDEO_URL = f"{CDN}/720p30/index-dvr.m3u8"
T0 = datetime(2026, 11, 18, 6, 27, 49, 784000, tzinfo=UTC)  # the PDT T0
CREATED = datetime(2026, 11, 18, 6, 27, 53, tzinfo=UTC)  # GQL createdAt: T0 + 3.216 s
MUTED = frozenset({8, 9})
DISC = frozenset({12})


def make_segments(directory: Path, n: int, *, video: bool = False) -> list[bytes]:
    """n x 10 s MPEG-TS segments of one continuous lavfi stream (timestamps run on)."""
    directory.mkdir(parents=True, exist_ok=True)
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if video:
        args += ["-f", "lavfi", "-i", f"testsrc=size=96x64:rate=10:duration={n * 10}"]
    args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={n * 10}"]
    if video:
        args += ["-c:v", "mpeg2video", "-g", "10"]
    args += ["-c:a", "aac", "-b:a", "32k", "-f", "segment", "-segment_time", "10"]
    args += ["-segment_format", "mpegts", str(directory / "seg%03d.ts")]
    subprocess.run(args, check=True)
    parts = [f.read_bytes() for f in sorted(directory.glob("seg*.ts"))]
    assert len(parts) >= n, len(parts)
    return [*parts[: n - 1], b"".join(parts[n - 1 :])]  # AAC frames overrun 10 s a little


@pytest.fixture(scope="module")
def audio_segments(tmp_path_factory):
    return make_segments(tmp_path_factory.mktemp("audio"), 16)


@pytest.fixture(scope="module")
def video_segments(tmp_path_factory):
    return make_segments(tmp_path_factory.mktemp("video"), 6, video=True)


def vod_playlist(n: int, *, muted=MUTED, disc=DISC, pdt: bool = True) -> str:
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:10",
        "#EXT-X-PLAYLIST-TYPE:EVENT",
        "#EXT-X-MEDIA-SEQUENCE:0",
        f"#EXT-X-TWITCH-TOTAL-SECS:{n * 10:.3f}",
    ]
    for i in range(n):
        if i in disc:
            lines.append("#EXT-X-DISCONTINUITY")
        if pdt:
            stamp = T0 + timedelta(seconds=10 * i)
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{stamp.isoformat(timespec='milliseconds')}")
        lines += ["#EXTINF:10.000,", f"{i}-muted.ts" if i in muted else f"{i}.ts"]
    return "\n".join([*lines, "#EXT-X-ENDLIST", ""])


class MockCdn:
    """Twitch's VOD CDN: playlists + segments of one VOD; `script` answers statuses first."""

    def __init__(self, audio, video=None, *, muted=MUTED, disc=DISC, script=None):
        self.audio, self.video = audio, video or []
        self.playlists = {
            AUDIO_URL: vod_playlist(len(audio), muted=muted, disc=disc),
            VIDEO_URL: vod_playlist(len(self.video), muted=muted, disc=disc, pdt=False),
        }
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        rendition, name = url.removeprefix(CDN + "/").split("/")
        self.calls.append(f"{rendition}/{name}")
        if self.script.get(name):
            return httpx.Response(self.script[name].pop(0))
        if url in self.playlists:
            return httpx.Response(200, text=self.playlists[url])
        index = int(name.split(".")[0].removesuffix("-muted"))
        data = self.audio if rendition == "audio_only" else self.video
        return httpx.Response(200, content=data[index])

    def segment_calls(self) -> list[str]:
        return [c for c in self.calls if c.endswith(".ts")]


class MemoryStore:
    def __init__(self, fail_on=None) -> None:
        self.objects: dict[str, tuple[bytes, str | None]] = {}
        self.fail_on = fail_on  # key whose first upload raises (a crash before the commit)

    def put_file(self, key, path, content_type=None) -> None:
        if key == self.fail_on:
            self.fail_on = None
            raise RuntimeError(f"injected crash uploading {key}")
        self.objects[key] = (Path(path).read_bytes(), content_type)


def video_info(length_s=160) -> VideoInfo:
    return VideoInfo(
        id=VIDEO,
        title="Test stream \U0001f334",
        created_at=CREATED,
        published_at=CREATED,
        length_s=length_s,
        status="RECORDED",
        broadcast_type="ARCHIVE",
        owner_id="4242",
        owner_login="streamer",
        owner_display_name="Streamer",
        game="Just Chatting",
        seek_previews_url=f"{CDN}/storyboards/{VIDEO}-info.json",
    )


def replay(**kwargs) -> FakeChatReplay:
    """Chat whose replay anchor is T0 + 3.9 s (truncated offsets), as on the smoke VOD."""
    comments = []
    for i in range(120):
        true_offset = i * 1.3 + 0.37
        comments.append(
            Comment(
                id=f"c{i}",
                created_at=T0 + timedelta(seconds=3.9 + true_offset),
                content_offset_s=int(true_offset),
                commenter_id=f"u{i % 7}",
                commenter_login=f"user{i % 7}",
                commenter_display_name=None,
                fragments=(Fragment("KEKW"),),
                badges=(),
                user_color=None,
                cursor=None,
            )
        )
    return FakeChatReplay({VIDEO: comments}, videos={VIDEO: video_info()}, **kwargs)


CHANNEL_EMOTES = {"KEKW": "7tv", "OMEGALUL": "bttv"}
GLOBAL_EMOTES = {"Clap": "7tv", "KEKW": "ffz"}


def backfiller(
    sessions,
    cdn,
    store,
    *,
    vods=None,
    gql=None,
    after_commit=None,
    owner="run-1",
    stop=None,
    after_chat_commit=None,
    chat_batch_pages=20,
    emotes=None,
):
    return Backfiller(
        sessions=sessions,
        store=store,
        vod_source=vods
        or FakeVodSource(
            {VIDEO: {"audio_only": AUDIO_URL, "720p30": VIDEO_URL, "best": VIDEO_URL}}
        ),
        gql=gql or replay(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(cdn.handler)),
        settings=BackfillSettings(
            _env_file=None, t0_chat_pages=3, retry_backoff_s=0, chat_batch_pages=chat_batch_pages
        ),
        owner=owner,
        after_commit=after_commit,
        stop=stop,
        emotes=emotes or StaticEmoteSets(GLOBAL_EMOTES, {"4242": CHANNEL_EMOTES}),
        after_chat_commit=after_chat_commit,
    )


async def rows(sessions, sql, **params):
    async with sessions() as s:
        return (await s.execute(text(sql), params)).all()


async def snapshot(sessions) -> dict:
    """Everything the backfill writes, without ids and creation times."""
    queries = {
        "channels": "SELECT login, platform_user_id, display_name, recording_mode, active "
        "FROM channels",
        "streams": "SELECT platform_stream_id, title, category, started_at, ended_at, status, "
        "source FROM streams",
        "vods": "SELECT platform_video_id, title, platform_created_at, duration_s, t0_utc, "
        "t0_method, t0_candidates, rendition, media_status, media_done_s, chat_status, flags, "
        "chat_done_s, chat_metrics_to FROM vods",
        "chunks": "SELECT seq, start_utc, end_utc, video_key, audio_key, video_bytes, "
        "audio_bytes, source FROM chunks ORDER BY seq",
        "gaps": "SELECT start_utc, end_utc, reason FROM stream_gaps ORDER BY start_utc",
        "jobs": "SELECT c.seq, j.kind, j.priority, j.status, j.payload = jsonb_build_object("
        "'chunk_id', c.id) AS payload_ok FROM jobs j JOIN chunks c ON c.id = j.ref_id "
        "ORDER BY c.seq",
        "leases": "SELECT resource FROM leases",
        "messages": "SELECT sent_at, msg_id, channel_id, stream_id, received_at, user_id, "
        "user_login, text, emotes, badges, deleted, source FROM chat_messages "
        "ORDER BY sent_at, msg_id",
        "metrics": "SELECT channel_id, ts, stream_id, messages, unique_chatters, emote_counts, "
        "event_counts FROM chat_metrics_1s ORDER BY ts",
        "events": "SELECT * FROM chat_events",
    }
    return {name: [tuple(r) for r in await rows(sessions, sql)] for name, sql in queries.items()}


async def truncate(sessions) -> None:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with sessions.begin() as s:
        await s.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


def iv(a, b):
    return (at(T0, a), at(T0, b))


# --------------------------------------------------------------------------- the main path


async def test_vod_becomes_contiguous_chunks_muted_gaps_and_backfill_jobs(sessions, audio_segments):
    cdn, store = MockCdn(audio_segments), MemoryStore()
    out = await backfiller(sessions, cdn, store).vod(f"https://www.twitch.tv/videos/{VIDEO}")

    assert out.status == "done", out
    snap = await snapshot(sessions)
    assert snap["channels"] == [("streamer", "4242", "Streamer", "off", False)]  # inactive
    assert snap["streams"] == [
        (STREAM, "Test stream \U0001f334", "Just Chatting", T0, at(T0, 160), "ended", "backfill")
    ]
    ((vid, title, created, duration, t0, method, cands, rendition, mstatus, done, chat, flags),) = (
        v[:12] for v in snap["vods"]
    )
    assert (vid, created, duration, t0, method) == (VIDEO, CREATED, 160.0, T0, "pdt")
    assert title == "Test stream 🌴"
    assert (rendition, mstatus, done, chat) == ("audio_only", "done", 160.0, "done")
    assert len(snap["messages"]) == 120 and snap["events"] == []
    assert cands["pdt"] == T0.isoformat() and cands["gql_created_at"] == CREATED.isoformat()
    assert abs(cands["chat_minus_pdt_s"] - 3.9) < 0.5 and cands["created_at_minus_t0_s"] == 3.216
    assert cands["stream_ids"] == {"preview_path": STREAM, "playlist_path": STREAM}
    assert flags == {"muted_s": 20.0, "discontinuities": 1}

    sid = (await rows(sessions, "SELECT id FROM streams"))[0].id
    chunks = snap["chunks"]
    assert [(c[0], c[1], c[2]) for c in chunks] == [
        (0, *iv(0, 60)),
        (1, *iv(60, 80)),  # ends before the muted run
        (2, *iv(100, 120)),  # ends before the discontinuity
        (3, *iv(120, 160)),
    ]
    assert all(c[3] is None and c[4] == keys.chunk_audio(sid, c[0]) for c in chunks)
    assert all(c[6] > 0 and c[7] == "backfill" for c in chunks)
    assert snap["gaps"] == [(*iv(80, 100), "muted")]
    assert snap["jobs"] == [
        (seq, "transcribe", Priority.BACKFILL, "queued", True) for seq in range(4)
    ]
    assert snap["leases"] == []
    assert set(store.objects) == {keys.chunk_audio(sid, seq) for seq in range(4)}
    assert all(data[:2] == b"\xff\xf1" and ct == "audio/aac" for data, ct in store.objects.values())

    # muted segments are never fetched; every other one exactly once
    assert sorted(cdn.segment_calls()) == sorted(
        f"audio_only/{i}.ts" for i in range(16) if i not in MUTED
    )
    assert any("committed: 4 chunks" in line for line in out.lines)

    # a re-run finds the rows and has nothing to do
    again = await backfiller(sessions, MockCdn(audio_segments), MemoryStore()).vod(VIDEO)
    assert again.status == "done" and again.message == "media complete already; chat done"
    assert "chat: complete already" in again.lines
    assert await snapshot(sessions) == snap


async def test_crash_mid_run_then_rerun_gives_identical_rows(sessions, audio_segments):
    class Crash(Exception):
        pass

    async def crash_after_seq_1(unit):
        if unit.seq == 1:
            raise Crash

    with pytest.raises(Crash):
        await backfiller(
            sessions, MockCdn(audio_segments), MemoryStore(), after_commit=crash_after_seq_1
        ).vod(VIDEO)
    (vod,) = await rows(sessions, "SELECT media_status, media_done_s FROM vods")
    assert tuple(vod) == ("pending", 80.0)
    assert len(await rows(sessions, "SELECT * FROM chunks")) == 2
    assert await rows(sessions, "SELECT * FROM leases") == []  # released on the way out

    # second crash: after uploading chunk 2's audio, before its commit
    sid = (await rows(sessions, "SELECT id FROM streams"))[0].id
    with pytest.raises(RuntimeError, match="injected crash"):
        await backfiller(
            sessions, MockCdn(audio_segments), MemoryStore(fail_on=keys.chunk_audio(sid, 2))
        ).vod(VIDEO)
    assert len(await rows(sessions, "SELECT * FROM chunks")) == 2
    assert len(await rows(sessions, "SELECT * FROM stream_gaps")) == 0  # goes with chunk 2

    cdn = MockCdn(audio_segments)
    out = await backfiller(sessions, cdn, MemoryStore()).vod(VIDEO)
    assert out.status == "done"
    assert sorted(cdn.segment_calls()) == [f"audio_only/{i}.ts" for i in (10, 11, 12, 13, 14, 15)]
    resumed = await snapshot(sessions)

    await truncate(sessions)
    assert (await backfiller(sessions, MockCdn(audio_segments), MemoryStore()).vod(VIDEO)).ok
    assert await snapshot(sessions) == resumed


async def test_dry_run_reads_but_writes_nothing(sessions, audio_segments):
    cdn, store = MockCdn(audio_segments), MemoryStore()
    gql = replay()
    out = await backfiller(sessions, cdn, store, gql=gql).vod(VIDEO, VodOptions(dry_run=True))

    assert out.status == "dry_run"
    report = "\n".join(out.lines)
    assert "T0 2026-11-18T06:27:49.784000+00:00 (pdt)" in report
    assert "plan: 4 chunks (0:02:20) from 0.0 s to 160.0 s of 160.0 s, seq 0..3" in report
    assert "gaps: muted 0:00:20" in report and "14 segments" in report
    snap = await snapshot(sessions)
    assert all(v == [] for v in snap.values()), snap
    assert store.objects == {}
    assert cdn.calls == ["audio_only/index-dvr.m3u8"]  # the playlist, no segment
    assert len(gql.calls) == 3 and gql.video_calls == [VIDEO]  # T0 sample + metadata

    # after a partial run, a dry run shows the resume point
    await backfiller(sessions, MockCdn(audio_segments), MemoryStore()).vod(
        VIDEO, VodOptions(max_minutes=1)
    )
    before = await snapshot(sessions)
    out = await backfiller(sessions, MockCdn(audio_segments), store).vod(
        VIDEO, VodOptions(dry_run=True)
    )
    report = "\n".join(out.lines)
    assert "media pending at 60.0/160.0 s" in report and "seq 1..3" in report
    assert await snapshot(sessions) == before


# --------------------------------------------------------------------------- variants


async def test_max_minutes_runs_resume_with_the_next_seq(sessions, audio_segments):
    outs = []
    for _ in range(3):
        bf = backfiller(sessions, MockCdn(audio_segments), MemoryStore())
        outs.append(await bf.vod(VIDEO, VodOptions(max_minutes=1)))
        async with sessions() as s:
            (status,) = await bf_rows.status_rows(s, [VIDEO])
        if len(outs) == 1:  # what `clipper-backfill status` shows after the first run
            assert (status.media_status, status.media_done_s, status.chunks) == ("pending", 60, 1)
            assert (status.transcribed, status.transcribe_pending) == (0, 1)
            assert "60/160s 38%" in status_lines([status])[1]
    assert [o.status for o in outs] == ["partial", "partial", "done"]
    assert (status.chunks, status.gaps, float(status.muted_s)) == (4, 1, 20.0)
    chunks = await rows(sessions, "SELECT seq, start_utc, end_utc FROM chunks ORDER BY seq")
    assert [tuple(c) for c in chunks] == [
        (0, *iv(0, 60)),
        (1, *iv(60, 80)),
        (2, *iv(100, 120)),
        (3, *iv(120, 160)),
    ]
    assert len(await rows(sessions, "SELECT * FROM stream_gaps")) == 1


async def test_start_offset_leaves_a_not_recorded_gap(sessions, audio_segments):
    out = await backfiller(sessions, MockCdn(audio_segments), MemoryStore()).vod(
        VIDEO, VodOptions(start_offset_s=125)
    )
    assert out.status == "done"
    assert [tuple(c) for c in await rows(sessions, "SELECT seq, start_utc FROM chunks")] == [
        (0, at(T0, 120))
    ]
    gaps = await rows(sessions, "SELECT start_utc, end_utc, reason FROM stream_gaps")
    assert [tuple(g) for g in gaps] == [(*iv(0, 120), "not_recorded")]


async def test_no_transcribe_then_a_rerun_queues_the_missing_jobs(sessions, audio_segments):
    bf = backfiller(sessions, MockCdn(audio_segments), MemoryStore())
    assert (await bf.vod(VIDEO, VodOptions(transcribe=False))).ok
    assert len(await rows(sessions, "SELECT * FROM chunks WHERE audio_key IS NOT NULL")) == 4
    assert await rows(sessions, "SELECT * FROM jobs") == []

    out = await backfiller(sessions, MockCdn(audio_segments), MemoryStore()).vod(VIDEO)
    assert "queued 4 transcribe jobs left out by --no-transcribe" in out.lines
    jobs = await rows(sessions, "SELECT priority FROM jobs")
    assert [j.priority for j in jobs] == [Priority.BACKFILL] * 4


async def test_lost_segment_becomes_a_not_recorded_gap(sessions, audio_segments):
    cdn = MockCdn(audio_segments, script={"3.ts": [404, 404]})
    out = await backfiller(sessions, cdn, MemoryStore()).vod(VIDEO)
    assert out.status == "done" and out.media.lost_segments == 1
    chunks = await rows(sessions, "SELECT seq, start_utc, end_utc FROM chunks ORDER BY seq")
    assert [tuple(c) for c in chunks] == [
        (0, *iv(0, 30)),
        (1, *iv(40, 80)),
        (2, *iv(100, 120)),
        (3, *iv(120, 160)),
    ]
    gaps = await rows(sessions, "SELECT start_utc, end_utc, reason FROM stream_gaps ORDER BY 1")
    assert [tuple(g) for g in gaps] == [(*iv(30, 40), "not_recorded"), (*iv(80, 100), "muted")]
    (flags,) = await rows(sessions, "SELECT flags FROM vods")
    assert flags.flags["lost_segments"] == 1


async def test_flags_are_merged_not_overwritten(sessions, audio_segments):
    async def note_after_seq_0(unit):  # someone else (a later 5d step, an operator) adds a flag
        if unit.seq == 0:
            async with sessions.begin() as s:
                await s.execute(text("""UPDATE vods SET flags = flags || '{"note": "x"}'"""))

    cdn = MockCdn(audio_segments, script={"7.ts": [404, 404]})
    await backfiller(sessions, cdn, MemoryStore(), after_commit=note_after_seq_0).vod(VIDEO)
    (vod,) = await rows(sessions, "SELECT flags FROM vods")
    assert vod.flags == {"muted_s": 20.0, "discontinuities": 1, "note": "x", "lost_segments": 1}


# --------------------------------------------------------------------------- failures


async def test_lease_lost_between_commits_commits_nothing_more(sessions, audio_segments):
    async def steal_after_seq_0(unit):
        if unit.seq == 0:
            async with sessions.begin() as s:
                await s.execute(
                    text("UPDATE leases SET owner = 'thief', expires_at = now() + interval '1 h'")
                )

    cdn, store = MockCdn(audio_segments), MemoryStore()
    bf = backfiller(sessions, cdn, store, after_commit=steal_after_seq_0)
    with pytest.raises(LeaseLost):
        await bf.vod(VIDEO)
    assert [c.seq for c in await rows(sessions, "SELECT seq FROM chunks")] == [0]
    (vod,) = await rows(sessions, "SELECT media_done_s FROM vods")
    assert vod.media_done_s == 60.0
    (lease,) = await rows(sessions, "SELECT owner FROM leases")
    assert lease.owner == "thief"  # not released by the run that lost it


async def test_stop_event_ends_the_run_between_units(sessions, audio_segments):
    stop = asyncio.Event()

    async def stop_after_seq_0(unit):
        if unit.seq == 0:
            stop.set()

    bf = backfiller(
        sessions, MockCdn(audio_segments), MemoryStore(), after_commit=stop_after_seq_0, stop=stop
    )
    out = await bf.vod(VIDEO)
    assert out.status == "partial" and out.media.stopped
    assert any("(stopped)" in line for line in out.lines)
    assert [c.seq for c in await rows(sessions, "SELECT seq FROM chunks")] == [0]
    assert await rows(sessions, "SELECT * FROM leases") == []


async def test_audio_only_chunk_without_audio_stops_the_run(sessions, audio_segments, tmp_path):
    broken = [b"not mpeg-ts at all" * 100] * 6 + audio_segments[6:]  # all of chunk 0
    bf = backfiller(sessions, MockCdn(broken), MemoryStore())
    bf.settings = BackfillSettings(_env_file=None, t0_chat_pages=3, temp_dir=tmp_path)
    with pytest.raises(MediaError, match="chunk 0: no audio"):
        await bf.vod(VIDEO)
    assert await rows(sessions, "SELECT * FROM chunks") == []
    (vod,) = await rows(sessions, "SELECT media_status, media_done_s FROM vods")
    assert tuple(vod) == ("pending", 0.0)
    assert await rows(sessions, "SELECT * FROM leases") == []
    assert list(tmp_path.iterdir()) == []  # the run's temp dir is gone


async def test_another_rendition_cut_elsewhere_is_refused(sessions, audio_segments):
    await backfiller(sessions, MockCdn(audio_segments), MemoryStore()).vod(
        VIDEO, VodOptions(max_minutes=1)
    )
    before = await snapshot(sessions)
    cdn = MockCdn(audio_segments)  # a video rendition with 13 s segments: 60 s is no boundary
    cdn.playlists[VIDEO_URL] = "\n".join(
        ["#EXTM3U", "#EXT-X-TARGETDURATION:13"]
        + [f"#EXTINF:13.000,\n{i}.ts" for i in range(12)]
        + ["#EXT-X-ENDLIST", ""]
    )
    out = await backfiller(sessions, cdn, MemoryStore()).vod(VIDEO, VodOptions(video="720p30"))
    assert out.status == "refused" and not out.ok
    assert "committed up to 60.000 s" in out.message and "without --video" in out.message
    assert await snapshot(sessions) == before and cdn.segment_calls() == []
    out = await backfiller(sessions, cdn, MemoryStore()).vod(
        VIDEO, VodOptions(video="720p30", dry_run=True)
    )
    assert any(line.startswith("plan: refused") for line in out.lines)


async def test_restricted_after_done_stays_done_and_a_playable_one_completes(
    sessions, audio_segments
):
    restricted = FakeVodSource({VIDEO: VodRestricted("sub-only now")})
    assert (await backfiller(sessions, MockCdn(audio_segments), MemoryStore()).vod(VIDEO)).ok
    out = await backfiller(sessions, MockCdn([b""]), MemoryStore(), vods=restricted).vod(VIDEO)
    assert out.status == "done" and "now restricted" in out.message
    (vod,) = await rows(sessions, "SELECT media_status FROM vods")
    assert vod.media_status == "done"
    assert [g.reason for g in await rows(sessions, "SELECT reason FROM stream_gaps")] == ["muted"]

    # a VOD restricted on its first run and playable later: its gap covers it all -> done
    await truncate(sessions)
    await backfiller(sessions, MockCdn([b""]), MemoryStore(), vods=restricted).vod(VIDEO)
    cdn = MockCdn(audio_segments)
    out = await backfiller(sessions, cdn, MemoryStore()).vod(VIDEO)
    assert out.status == "done" and cdn.segment_calls() == []
    (vod,) = await rows(sessions, "SELECT media_status, media_done_s FROM vods")
    assert tuple(vod) == ("done", 160.0)


async def test_video_rendition_keeps_video_and_muted_parts_are_video_only(sessions, video_segments):
    cdn, store = MockCdn([b"x"] * 6, video_segments, muted={3}, disc=()), MemoryStore()
    out = await backfiller(sessions, cdn, store).vod(VIDEO, VodOptions(video="best"))

    assert out.status == "done", out
    (vod,) = await rows(sessions, "SELECT rendition, t0_method, t0_utc FROM vods")
    assert tuple(vod) == ("720p30", "pdt", T0)  # T0 from the audio_only playlist
    chunks = await rows(
        sessions, "SELECT seq, start_utc, end_utc, video_key, audio_key FROM chunks ORDER BY seq"
    )
    assert [(c.seq, c.start_utc, c.end_utc) for c in chunks] == [
        (0, *iv(0, 30)),
        (1, *iv(30, 40)),
        (2, *iv(40, 60)),
    ]
    assert all(c.video_key for c in chunks)
    assert [c.audio_key is not None for c in chunks] == [True, False, True]
    gaps = await rows(sessions, "SELECT start_utc, end_utc, reason FROM stream_gaps")
    assert [tuple(g) for g in gaps] == [(*iv(30, 40), "muted")]
    assert len(await rows(sessions, "SELECT * FROM jobs")) == 2
    assert sorted(c for c in cdn.calls if c.startswith("audio_only")) == [
        "audio_only/index-dvr.m3u8"
    ]
    video, ctype = store.objects[next(k for k in store.objects if k.endswith("000000.ts"))]
    assert ctype == "video/mp2t" and video[:1] == b"\x47"  # MPEG-TS sync byte


# --------------------------------------------------------------------------- skips


async def test_restricted_vod_gets_a_not_recorded_gap_over_its_length(sessions):
    vods = FakeVodSource({VIDEO: VodRestricted("usher 403 vod_manifest_restricted")})
    cdn = MockCdn([b""])
    out = await backfiller(sessions, cdn, MemoryStore(), vods=vods).vod(VIDEO)

    assert out.status == "restricted" and "not_recorded gap" in out.message
    (vod,) = await rows(sessions, "SELECT * FROM vods")
    assert (vod.media_status, vod.media_done_s, vod.duration_s, vod.t0_method) == (
        "restricted", 160.0, 160.0, "chat"
    )  # fmt: skip
    assert abs((vod.t0_utc - (T0 + timedelta(seconds=3.9))).total_seconds()) < 0.5
    assert vod.flags["restricted"] is True and "pdt" not in vod.t0_candidates
    (stream,) = await rows(sessions, "SELECT platform_stream_id, ended_at FROM streams")
    assert stream.platform_stream_id == STREAM  # from the storyboard URL
    assert stream.ended_at == at(vod.t0_utc, 160)
    gaps = await rows(sessions, "SELECT start_utc, end_utc, reason FROM stream_gaps")
    assert [tuple(g) for g in gaps] == [(vod.t0_utc, at(vod.t0_utc, 160), "not_recorded")]
    assert cdn.calls == []

    again = await backfiller(sessions, MockCdn([b""]), MemoryStore(), vods=vods).vod(VIDEO)
    assert again.status == "restricted" and "already restricted" in again.message
    assert len(await rows(sessions, "SELECT * FROM stream_gaps")) == 1


async def test_stream_recorded_live_is_skipped(sessions, audio_segments):
    async with sessions.begin() as s:
        cid = (
            await s.execute(
                text(
                    "INSERT INTO channels (login, platform_user_id) VALUES ('streamer', '4242') "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO streams (channel_id, platform_stream_id, started_at, status) "
                "VALUES (:c, :p, :t, 'ended')"
            ),
            {"c": cid, "p": STREAM, "t": T0},
        )
    cdn = MockCdn(audio_segments)
    out = await backfiller(sessions, cdn, MemoryStore()).vod(VIDEO)
    assert out.status == "skipped" and "recorded live" in out.message
    assert await rows(sessions, "SELECT * FROM vods") == []
    assert cdn.segment_calls() == []
    (channel,) = await rows(sessions, "SELECT recording_mode, active FROM channels")
    assert tuple(channel) == ("full", True)  # never changed


async def test_lease_held_by_a_live_run_blocks_an_expired_one_does_not(sessions, audio_segments):
    async with sessions.begin() as s:
        await s.execute(
            text(
                "INSERT INTO leases (resource, owner, expires_at) "
                "VALUES (:r, 'other-run', now() + interval '1 minute')"
            ),
            {"r": backfill_lease(VIDEO)},
        )
    out = await backfiller(sessions, MockCdn(audio_segments), MemoryStore()).vod(VIDEO)
    assert out.status == "busy" and "other-run" in out.message and not out.ok
    assert await rows(sessions, "SELECT * FROM vods") == []

    async with sessions.begin() as s:  # that run was killed: its lease ran out
        await s.execute(text("UPDATE leases SET expires_at = now() - interval '1 s'"))
    out = await backfiller(sessions, MockCdn(audio_segments), MemoryStore()).vod(VIDEO)
    assert out.status == "done"
    assert await rows(sessions, "SELECT * FROM leases") == []


async def test_no_media_writes_rows_only_and_a_later_run_fetches(sessions, audio_segments):
    cdn = MockCdn(audio_segments)
    out = await backfiller(sessions, cdn, MemoryStore()).vod(VIDEO, VodOptions(media=False))
    assert out.status == "rows_only" and cdn.segment_calls() == []
    (vod,) = await rows(sessions, "SELECT media_status, t0_method FROM vods")
    assert tuple(vod) == ("off", "pdt")

    out = await backfiller(sessions, MockCdn(audio_segments), MemoryStore()).vod(VIDEO)
    assert out.status == "done"
    assert len(await rows(sessions, "SELECT * FROM chunks")) == 4


async def test_gone_and_unknown_videos(sessions, audio_segments):
    unknown = FakeChatReplay({}, videos={})
    out = await backfiller(sessions, MockCdn(audio_segments), MemoryStore(), gql=unknown).vod("5")
    assert out.status == "gone" and "not found" in out.message

    await backfiller(sessions, MockCdn(audio_segments), MemoryStore()).vod(
        VIDEO, VodOptions(max_minutes=1)
    )
    vods = FakeVodSource({VIDEO: VodGone("deleted")})
    out = await backfiller(sessions, MockCdn(audio_segments), MemoryStore(), vods=vods).vod(VIDEO)
    assert out.status == "gone" and "data kept" in out.message
    (vod,) = await rows(sessions, "SELECT media_status, media_done_s FROM vods")
    assert tuple(vod) == ("gone", 60.0)
    assert len(await rows(sessions, "SELECT * FROM chunks")) == 1


async def test_highlights_and_live_archives_are_skipped(sessions, audio_segments):
    cases = (("HIGHLIGHT", "RECORDED", "highlight"), ("ARCHIVE", "RECORDING", "live"))
    for kind, status, why in cases:
        info = dataclasses.replace(video_info(), broadcast_type=kind, status=status)
        gql = FakeChatReplay({}, videos={VIDEO: info})
        out = await backfiller(sessions, MockCdn(audio_segments), MemoryStore(), gql=gql).vod(VIDEO)
        assert out.status == "skipped" and why in out.message
    assert await rows(sessions, "SELECT * FROM streams") == []


# --------------------------------------------------------------------------- chat replay


def chat_comments() -> list[Comment]:
    """150 comments over ~135 s: Twitch + third-party emotes, badges, two deleted users, a
    missing createdAt and one an hour off (both fall back to the chat clock)."""
    out = []
    for i in range(150):
        true_offset = i * 0.9 + 0.21
        created = T0 + timedelta(seconds=3.9 + true_offset)
        if i == 40:
            created = None
        elif i == 41:
            created += timedelta(hours=1)
        if i % 10 == 3:
            fragments = (Fragment("Kappa", "25"), Fragment(" KEKW KEKW Clap"))
        elif i % 10 == 5:
            fragments = (Fragment("OMEGALUL hi"),)
        else:
            fragments = (Fragment(f"msg {i} KEKW"),)
        deleted = i in (17, 90)
        out.append(
            Comment(
                id=f"m{i:03d}",
                created_at=created,
                content_offset_s=int(true_offset),
                commenter_id=None if deleted else f"u{i % 11}",
                commenter_login=None if deleted else f"user{i % 11}",
                commenter_display_name=None,
                fragments=fragments,
                badges=(("subscriber", "12"),) if i % 2 else (),
                user_color=None,
                cursor=None,
            )
        )
    return out


def chat_replay(page_size=10, **kwargs) -> FakeChatReplay:
    return FakeChatReplay(
        {VIDEO: chat_comments()}, videos={VIDEO: video_info()}, page_size=page_size, **kwargs
    )


async def reference_metrics(sessions) -> list[tuple]:
    """What one pass of the live aggregator gives for all comments (the VOD's chat clock)."""
    (vod,) = await rows(sessions, "SELECT channel_id, stream_id, t0_candidates FROM vods")
    clock = ChatClock(datetime.fromisoformat(vod.t0_candidates["chat"]), "chat_anchor")
    agg = SecondAggregator()
    for c in chat_comments():
        row, _ = map_comment(
            c,
            channel_id=vod.channel_id,
            stream_id=vod.stream_id,
            clock=clock,
            channel_emotes=CHANNEL_EMOTES,
            global_emotes=GLOBAL_EMOTES,
        )
        agg.add_row(row)
    return [
        (m.channel_id, m.ts, m.stream_id, m.messages, m.unique_chatters, m.emote_counts, {})
        for m in agg.pop_all()
    ]


async def test_chat_replay_becomes_live_rows_and_metrics(sessions, audio_segments):
    bf = backfiller(sessions, MockCdn(audio_segments), MemoryStore(), gql=chat_replay())
    out = await bf.vod(VIDEO, VodOptions(media=False))
    assert out.status == "rows_only" and out.message.endswith("chat done"), out
    assert any(line.startswith("chat done at 160/160 s: 150 new messages") for line in out.lines)
    assert any("KEKW 150 (7tv)" in line for line in out.lines)  # the channel set wins
    assert bf.emotes.calls == ["4242", "global"]

    snap = await snapshot(sessions)
    (vod,) = await rows(sessions, "SELECT * FROM vods")
    assert (vod.chat_status, vod.chat_done_s) == ("done", 160)
    assert vod.chat_metrics_to >= at(T0, 160)
    assert vod.flags == {
        "muted_s": 20.0,
        "discontinuities": 1,
        "chat_time_fallbacks": 2,
        "comments_without_user": 2,
    }
    clock = ChatClock(datetime.fromisoformat(vod.t0_candidates["chat"]), "chat_anchor")
    assert abs((clock.t0 - T0).total_seconds() - 3.9) < 0.5  # the replay anchor

    messages = {m[1]: m for m in snap["messages"]}
    assert len(messages) == 150 and snap["events"] == []
    assert {(m[2], m[3], m[4], m[11]) for m in messages.values()} == {
        (vod.channel_id, vod.stream_id, None, "backfill")
    }
    sent_at, _, _, _, _, uid, login, text_, emotes, badges, deleted, _ = messages["m003"]
    assert (uid, login, text_) == ("u3", "user3", "Kappa KEKW KEKW Clap")
    assert (badges, deleted) == ({"subscriber": "12"}, False)
    assert emotes == [
        {"name": "Kappa", "count": 1, "source": "twitch", "id": "25"},
        {"name": "KEKW", "count": 2, "source": "7tv"},
        {"name": "Clap", "count": 1, "source": "7tv"},
    ]
    assert sent_at == T0 + timedelta(seconds=3.9 + 3 * 0.9 + 0.21)  # createdAt
    assert messages["m005"][8] == [{"name": "OMEGALUL", "count": 1, "source": "bttv"}]
    assert messages["m017"][5:7] == (None, None)  # deleted user: kept, not a chatter
    for i in (40, 41):  # missing / an hour off: T_chat + offset + 0.5 s
        assert messages[f"m{i:03d}"][0] == clock.expected(int(i * 0.9 + 0.21))

    assert snap["metrics"] == await reference_metrics(sessions)
    assert sum(m[3] for m in snap["metrics"]) == 150
    (partition,) = await rows(sessions, "SELECT to_regclass('chat_messages_p20261118') AS p")
    assert partition.p is not None


async def test_chat_crash_and_resume_give_the_rows_of_one_clean_run(sessions, audio_segments):
    class Crash(Exception):
        pass

    def crash_after(n):
        async def hook(batch):
            if batch == n:
                raise Crash

        return hook

    for n in (3, 4):  # crash after the 3rd batch, then after 4 more
        bf = backfiller(
            sessions,
            MockCdn(audio_segments),
            MemoryStore(),
            gql=chat_replay(page_size=7),
            chat_batch_pages=1,
            after_chat_commit=crash_after(n),
        )
        with pytest.raises(Crash):
            await bf.vod(VIDEO, VodOptions(media=False))
        (vod,) = await rows(sessions, "SELECT chat_status, chat_done_s, chat_metrics_to FROM vods")
        assert vod.chat_status == "pending" and vod.chat_done_s > 0
        (late,) = await rows(sessions, "SELECT max(ts) AS ts FROM chat_metrics_1s")
        assert late.ts < vod.chat_metrics_to  # nothing at or after the watermark is written
        assert await rows(sessions, "SELECT * FROM leases") == []

    gql = chat_replay(page_size=7)
    bf = backfiller(sessions, MockCdn(audio_segments), MemoryStore(), gql=gql, chat_batch_pages=1)
    out = await bf.vod(VIDEO, VodOptions(media=False))
    assert out.message.endswith("chat done")
    assert any("read again" in line for line in out.lines)  # resumed just before the watermark
    resumed = await snapshot(sessions)

    await truncate(sessions)  # same page size: the T0 sample (and so the chat clock) is the same
    gql = chat_replay(page_size=7)
    out = await backfiller(sessions, MockCdn(audio_segments), MemoryStore(), gql=gql).vod(
        VIDEO, VodOptions(media=False)
    )
    assert out.message.endswith("chat done")
    assert await snapshot(sessions) == resumed
    assert resumed["metrics"] == await reference_metrics(sessions)


async def test_chat_max_minutes_runs_add_up_to_one_run(sessions, audio_segments):
    statuses, starts = [], []
    for _ in range(8):
        bf = backfiller(
            sessions, MockCdn(audio_segments), MemoryStore(), gql=chat_replay(page_size=8)
        )
        if statuses:  # a dry run in between shows the resume point and writes nothing
            before = await snapshot(sessions)
            dry = await bf.vod(VIDEO, VodOptions(media=False, dry_run=True))
            starts.append(next(line for line in dry.lines if line.startswith("chat:")))
            assert await snapshot(sessions) == before
        out = await bf.vod(VIDEO, VodOptions(media=False, chat_max_minutes=0.5))
        statuses.append(out.status)
        if out.status != "partial":
            break
    assert statuses == ["partial"] * (len(statuses) - 1) + ["rows_only"]
    assert len(statuses) >= 4  # 135 s of chat, 30 s per run
    assert all("would resume at offset" in line for line in starts), starts
    split = await snapshot(sessions)

    await truncate(sessions)
    gql = chat_replay(page_size=8)
    await backfiller(sessions, MockCdn(audio_segments), MemoryStore(), gql=gql).vod(
        VIDEO, VodOptions(media=False)
    )
    assert await snapshot(sessions) == split


async def live_chat_seconds(sessions, seconds: int) -> datetime:
    """The channel, with chat-ingest's rows (offline: no stream) for `seconds` seconds from
    T0 + 50 s, in the VOD's range. Returns the first second."""
    first = at(T0, 50).replace(microsecond=0)
    async with sessions.begin() as s:
        cid = (
            await s.execute(
                text(
                    "INSERT INTO channels (login, platform_user_id) VALUES ('streamer', '4242') "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO chat_metrics_1s (channel_id, ts, stream_id, messages, "
                "unique_chatters) SELECT :c, :t + make_interval(secs => i), NULL, 3, 2 "
                "FROM generate_series(0, :n - 1) AS i"
            ),
            {"c": cid, "t": first, "n": seconds},
        )
    return first


async def test_a_minute_of_live_chat_in_the_vod_range_skips_its_chat(sessions, audio_segments):
    await live_chat_seconds(sessions, 60)  # the default guard: 60 s
    gql = chat_replay()
    out = await backfiller(sessions, MockCdn(audio_segments), MemoryStore(), gql=gql).vod(
        VIDEO, VodOptions(media=False)
    )
    assert out.message.endswith("chat skipped")
    assert any("live chat in the VOD's range (60 seconds" in line for line in out.lines)
    (vod,) = await rows(sessions, "SELECT chat_status, chat_done_s FROM vods")
    assert tuple(vod) == ("skipped", 0)
    assert await rows(sessions, "SELECT * FROM chat_messages") == []
    metrics = await rows(sessions, "SELECT DISTINCT stream_id, messages FROM chat_metrics_1s")
    assert [tuple(m) for m in metrics] == [(None, 3)]
    assert len(gql.calls) == 3  # the T0 sample only


async def test_a_few_stray_live_seconds_are_kept_and_the_rest_is_backfilled(
    sessions, audio_segments
):
    first = await live_chat_seconds(sessions, 2)
    out = await backfiller(sessions, MockCdn(audio_segments), MemoryStore(), gql=chat_replay()).vod(
        VIDEO, VodOptions(media=False)
    )
    assert out.message.endswith("chat done")
    assert any(line.startswith("chat: 2 seconds of live chat") for line in out.lines)
    (vod,) = await rows(sessions, "SELECT stream_id, flags FROM vods")
    assert vod.flags["chat_foreign_seconds"] == 2
    assert len(await rows(sessions, "SELECT * FROM chat_messages")) == 150
    theirs = await rows(
        sessions, "SELECT ts, stream_id, messages FROM chat_metrics_1s WHERE stream_id IS NULL"
    )
    assert [tuple(m) for m in theirs] == [(first, None, 3), (first + timedelta(seconds=1), None, 3)]
    ours = await rows(sessions, "SELECT count(*) AS n FROM chat_metrics_1s WHERE stream_id = :s",
                      s=vod.stream_id)  # fmt: skip
    assert ours[0].n > 100


async def test_no_chat_then_chat_and_an_unavailable_replay(sessions, audio_segments):
    out = await backfiller(sessions, MockCdn(audio_segments), MemoryStore(), gql=chat_replay()).vod(
        VIDEO, VodOptions(media=False, chat=False)
    )
    assert out.message == "--no-media: no media fetched"
    (vod,) = await rows(sessions, "SELECT chat_status FROM vods")
    assert vod.chat_status == "off"
    assert await rows(sessions, "SELECT * FROM chat_messages") == []

    gone = FakeChatReplay({}, videos={VIDEO: video_info()})  # the replay is gone now
    out = await backfiller(sessions, MockCdn(audio_segments), MemoryStore(), gql=gone).vod(
        VIDEO, VodOptions(media=False)
    )
    assert out.message.endswith("chat unavailable")
    (vod,) = await rows(sessions, "SELECT chat_status FROM vods")
    assert vod.chat_status == "unavailable"

    out = await backfiller(sessions, MockCdn(audio_segments), MemoryStore(), gql=chat_replay()).vod(
        VIDEO, VodOptions(media=False)
    )
    assert out.message.endswith("chat done")
    assert len(await rows(sessions, "SELECT * FROM chat_messages")) == 150


async def test_a_replay_error_mid_run_keeps_the_batches_before_it(sessions, audio_segments):
    gql = chat_replay(page_size=7, errors={3 + 6: GqlUnavailable("deadline")})  # 3: T0 sample
    bf = backfiller(sessions, MockCdn(audio_segments), MemoryStore(), gql=gql, chat_batch_pages=2)
    with pytest.raises(GqlUnavailable):
        await bf.vod(VIDEO, VodOptions(media=False))
    (vod,) = await rows(sessions, "SELECT chat_status, chat_done_s FROM vods")
    assert vod.chat_status == "pending" and vod.chat_done_s > 0
    assert await rows(sessions, "SELECT * FROM leases") == []
    out = await backfiller(sessions, MockCdn(audio_segments), MemoryStore(), gql=chat_replay()).vod(
        VIDEO, VodOptions(media=False)
    )
    assert out.message.endswith("chat done")
    assert len(await rows(sessions, "SELECT * FROM chat_messages")) == 150


# --------------------------------------------------------------------------- viewer clips


def helix_fake(clips: list[dict] | None) -> HelixClient:
    """Helix with this VOD (stream STREAM) and `clips` (Get Clips ignores the window here;
    None: it answers something that isn't JSON)."""
    video = {
        "id": VIDEO,
        "stream_id": STREAM,
        "user_id": "4242",
        "user_login": "streamer",
        "user_name": "Streamer",
        "title": "Test stream",
        "description": "",
        "created_at": "2026-11-18T06:27:52Z",
        "published_at": None,
        "url": "",
        "thumbnail_url": "",
        "viewable": "public",
        "view_count": 1,
        "language": "en",
        "type": "archive",
        "duration": "2m40s",
        "muted_segments": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if request.url.path == "/helix/videos":
            return httpx.Response(200, json={"data": [video]})
        if request.url.path == "/helix/clips":
            if clips is None:
                return httpx.Response(200, text="<html>not json</html>")
            return httpx.Response(200, json={"data": clips})
        return httpx.Response(404)

    return HelixClient("cid", "s", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def helix_clip(cid: str, offset: int | None) -> dict:
    return {
        "id": cid,
        "url": "",
        "embed_url": "",
        "broadcaster_id": "4242",
        "broadcaster_name": "Streamer",
        "creator_id": "1",
        "creator_name": "Clipper",
        "video_id": VIDEO,
        "game_id": "1",
        "language": "en",
        "title": "the moment",
        "view_count": 42,
        "created_at": "2026-11-18T06:29:00Z",
        "thumbnail_url": "",
        "duration": 20.0,
        "vod_offset": offset,
        "is_featured": True,
    }


async def test_a_vod_run_attaches_viewer_clips(sessions, audio_segments):
    bf = backfiller(sessions, MockCdn(audio_segments), MemoryStore())
    out = await bf.vod(VIDEO, VodOptions(media=False))
    assert any(line.startswith("clips: skipped: needs Helix credentials") for line in out.lines)
    assert await rows(sessions, "SELECT * FROM viewer_clips") == []

    bf.helix = helix_fake([helix_clip("Moment", 95)])
    out = await bf.vod(VIDEO, VodOptions(media=False))
    assert any(line.startswith(f"clips (video {VIDEO}): 1 of 1 fetched") for line in out.lines)
    (clip,) = await rows(sessions, "SELECT * FROM viewer_clips")
    (stream,) = await rows(sessions, "SELECT id FROM streams")
    assert (clip.stream_id, clip.time_source, clip.start_utc) == (stream.id, "vod_t0", at(T0, 95))
    assert clip.slack_s == 30.0 and clip.is_featured

    out = await bf.vod(VIDEO, VodOptions(media=False, clips=False))
    assert not any(line.startswith("clips") for line in out.lines)


async def test_a_vod_recorded_live_gets_its_clips_only(sessions, audio_segments):
    async with sessions.begin() as s:
        cid = (
            await s.execute(
                text(
                    "INSERT INTO channels (login, platform_user_id) VALUES ('streamer', '4242') "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        live = (
            await s.execute(
                text(
                    "INSERT INTO streams (channel_id, platform_stream_id, started_at, status) "
                    "VALUES (:c, :p, :t, 'ended') RETURNING id"
                ),
                {"c": cid, "p": STREAM, "t": T0},
            )
        ).scalar_one()
    bf = backfiller(sessions, MockCdn(audio_segments), MemoryStore())
    bf.helix = helix_fake([helix_clip("Moment", 95)])
    out = await bf.vod(VIDEO)
    assert out.status == "skipped" and "clips only" in out.message
    (clip,) = await rows(sessions, "SELECT * FROM viewer_clips")
    created = datetime(2026, 11, 18, 6, 27, 52, tzinfo=UTC)  # Helix created_at
    assert (clip.stream_id, clip.time_source) == (live, "helix_created_at")
    assert clip.start_utc == created + timedelta(seconds=95) and clip.slack_s == 35.0
    assert await rows(sessions, "SELECT * FROM vods") == []
    assert await rows(sessions, "SELECT * FROM chat_messages") == []


async def test_failed_emote_sets_and_many_time_fallbacks_are_flagged(sessions, audio_segments):
    def sets(failed):
        return StaticEmoteSets(GLOBAL_EMOTES, {"4242": CHANNEL_EMOTES}, failed_providers=failed)

    comments = chat_comments()
    for i, c in enumerate(comments):
        if i % 10 == 7:  # 15 more an hour off: 17 of 150 fall back
            comments[i] = dataclasses.replace(c, created_at=c.created_at + timedelta(hours=1))
    gql = FakeChatReplay({VIDEO: comments}, videos={VIDEO: video_info()}, page_size=10)
    bf = backfiller(sessions, MockCdn(audio_segments), MemoryStore(), gql=gql, emotes=sets(["ffz"]))
    out = await bf.vod(VIDEO, VodOptions(media=False, chat_max_minutes=1))
    assert out.status == "partial"
    assert any("emote sets of ffz could not be fetched" in line for line in out.lines)
    gql = FakeChatReplay({VIDEO: comments}, videos={VIDEO: video_info()}, page_size=10)
    bf = backfiller(
        sessions, MockCdn(audio_segments), MemoryStore(), gql=gql, emotes=sets(["bttv"])
    )
    out = await bf.vod(VIDEO, VodOptions(media=False))
    assert out.message.endswith("chat done")
    (vod,) = await rows(sessions, "SELECT flags FROM vods")
    assert vod.flags["emote_sets_failed"] == ["bttv", "ffz"]  # every run's failures add up
    assert vod.flags["chat_time_fallbacks"] == 17
    assert vod.flags["chat_time_fallback_share"] == round(17 / 150, 4)

    await truncate(sessions)  # the usual 2 of 150: no share flag, no failed sets
    await backfiller(sessions, MockCdn(audio_segments), MemoryStore(), gql=chat_replay()).vod(
        VIDEO, VodOptions(media=False)
    )
    (vod,) = await rows(sessions, "SELECT flags FROM vods")
    assert vod.flags["chat_time_fallbacks"] == 2
    assert "chat_time_fallback_share" not in vod.flags and "emote_sets_failed" not in vod.flags


async def test_a_clips_failure_never_fails_the_vod_run(sessions, audio_segments):
    bf = backfiller(sessions, MockCdn(audio_segments), MemoryStore())
    bf.helix = helix_fake(None)  # Get Clips gone strange (an error none of ours expects)
    out = await bf.vod(VIDEO, VodOptions(media=False))
    assert out.ok and out.message.endswith("chat done")
    assert any(line.startswith("clips: failed: JSONDecodeError") for line in out.lines)
    (vod,) = await rows(sessions, "SELECT flags FROM vods")
    assert vod.flags["clips_error"].startswith("JSONDecodeError")

    bf.helix = helix_fake([helix_clip("Moment", 95)])  # the next run works: the flag goes
    out = await bf.vod(VIDEO, VodOptions(media=False))
    (vod,) = await rows(sessions, "SELECT flags FROM vods")
    assert "clips_error" not in vod.flags
    assert len(await rows(sessions, "SELECT * FROM viewer_clips")) == 1
