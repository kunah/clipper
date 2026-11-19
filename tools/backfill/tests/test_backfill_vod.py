"""Backfill of a mock VOD end to end: FakeVodSource + a mock CDN (httpx.MockTransport) serving
lavfi-made 10 s MPEG-TS segments, the fake GQL, Postgres and an in-memory store.

The default VOD: 16 segments (160 s), 8-9 muted, a discontinuity before 12, audio_only with
EXT-X-PROGRAM-DATE-TIME = T0 + offset on every segment (as Twitch serves it).
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
from clipper_backfill.cli import status_lines
from clipper_backfill.pipeline import LeaseLost, MediaError
from clipper_backfill.planner import at
from clipper_backfill.settings import BackfillSettings
from clipper_core.contracts import Priority, backfill_lease
from clipper_core.models import Base
from clipper_core.storage import keys
from clipper_twitch.gql import Comment, FakeChatReplay, Fragment, VideoInfo
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


def backfiller(
    sessions, cdn, store, *, vods=None, gql=None, after_commit=None, owner="run-1", stop=None
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
        settings=BackfillSettings(_env_file=None, t0_chat_pages=3, retry_backoff_s=0),
        owner=owner,
        after_commit=after_commit,
        stop=stop,
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
        "t0_method, t0_candidates, rendition, media_status, media_done_s, chat_status, flags "
        "FROM vods",
        "chunks": "SELECT seq, start_utc, end_utc, video_key, audio_key, video_bytes, "
        "audio_bytes, source FROM chunks ORDER BY seq",
        "gaps": "SELECT start_utc, end_utc, reason FROM stream_gaps ORDER BY start_utc",
        "jobs": "SELECT c.seq, j.kind, j.priority, j.status, j.payload = jsonb_build_object("
        "'chunk_id', c.id) AS payload_ok FROM jobs j JOIN chunks c ON c.id = j.ref_id "
        "ORDER BY c.seq",
        "leases": "SELECT resource FROM leases",
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
        snap["vods"]
    )
    assert (vid, created, duration, t0, method) == (VIDEO, CREATED, 160.0, T0, "pdt")
    assert title == "Test stream 🌴"
    assert (rendition, mstatus, done, chat) == ("audio_only", "done", 160.0, "pending")
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
    assert again.status == "done" and again.message == "media complete already"
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
