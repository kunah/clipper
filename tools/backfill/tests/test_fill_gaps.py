"""`vod --fill-gaps` (DESIGN §17): an ended stream recorded LIVE, with three live chunks, two
`not_recorded` gap rows (the head and one between the chunks) and an unrecorded tail without a gap
row, and its 200 s VOD as MPEG-TS or fMP4 segments. All audio is cut from ONE AAC encode of pink
noise (as in test_calibrate), so the in-process T0 match against the live chunks finds the true
T0; the VOD playlist's PDT is 137 ms late.

    VOD offset   0 ........ 23.3 ......... 83.3 ......... 121.7 ...... 151.7 ............. 200
    live         head gap   | chunk 0 | chunk 1 |   gap     | chunk 2 |   tail (no gap row)
    VOD segments 0 1 (2)       ...           (8) 9 10 11 (12)  ...   (15) 16 17 [18 muted] 19

Segments (2), (8), (12), (15) cross a live edge: they stay unfilled. Chunks are 20 s here.
"""

import csv
import dataclasses
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from botocore.exceptions import ClientError
from sqlalchemy import text

from clipper_backfill import cli
from clipper_backfill import rows as bf_rows
from clipper_backfill.backfill import Backfiller, FillRefused, VodOptions
from clipper_backfill.chat import StaticEmoteSets, chat_spans
from clipper_backfill.planner import at
from clipper_backfill.settings import BackfillSettings
from clipper_core import chunks as core_chunks
from clipper_core import transcripts
from clipper_core.chat import ChatWriter, MessageRow, SecondAggregator, second_of
from clipper_core.chunks import Gap
from clipper_core.contracts import FILL_SEQ_BASE, Priority, backfill_lease, recording_lease
from clipper_core.models import Base
from clipper_core.partitions import ensure_chat_partitions
from clipper_core.storage import keys
from clipper_core.timeline import Interval, utc_now
from clipper_twitch.gql import (
    Comment,
    CommentPage,
    FakeChatReplay,
    Fragment,
    GqlUnavailable,
    VideoInfo,
)
from clipper_twitch.helix import HelixClient
from clipper_twitch.hls import parse_vod_playlist
from clipper_twitch.settings import TwitchSettings
from clipper_twitch.vod import FakeVodSource

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]

VIDEO, STREAM = "2000000004", "777000444"
CDN = f"https://cdn.test/8173ac3b86530f54c2d4_streamer_{STREAM}_1795000000"
AUDIO_URL = f"{CDN}/audio_only/index-dvr.m3u8"
TRUE_T0 = datetime(2026, 11, 18, 6, 27, 49, 500000, tzinfo=UTC)  # VOD offset 0, live clock
PDT_ERROR_S = 0.137
CHAT_LAG_S = 4.2  # the replay's createdAt - offset band starts here (after TRUE_T0)
LENGTH_S = 200
LIVE_CUTS = (23.3, 53.3, 83.3, 121.7, 151.7)
LIVE_PIECES = (1, 2, 4)  # the pieces of that cut recorded live: chunks 0, 1, 2
MUTED = frozenset({18})
STARTED = TRUE_T0 - timedelta(seconds=3)  # the controller's started_at (before the VOD's T0)
ENDED = TRUE_T0 + timedelta(seconds=400)  # set late, like stream 803's
LIVE_CHAT_IN_GAP = range(95, 98)  # chat-ingest logged these seconds inside the media gap


# --------------------------------------------------------------------------- media fixtures


def ffmpeg(*args, cwd: Path | None = None) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, cwd=cwd)  # fmt: skip


def segment_list(path: Path) -> list[tuple[str, float, float]]:
    with path.open(newline="") as f:
        return [(name, float(a), float(b)) for name, a, b in csv.reader(f)]


@dataclass(frozen=True)
class FakeVod:
    segments: list[tuple[bytes, float, float]]  # (bytes, VOD offset, duration)
    init: bytes | None = None  # fMP4
    pdt_error_s: float | None = PDT_ERROR_S  # None: no EXT-X-PROGRAM-DATE-TIME at all

    def playlist(self) -> str:
        ext = "mp4" if self.init else "ts"
        lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:10", "#EXT-X-PLAYLIST-TYPE:EVENT"]
        if self.init:
            lines.append('#EXT-X-MAP:URI="init-0.mp4"')
        for i, (_, offset, duration) in enumerate(self.segments):
            if self.pdt_error_s is not None:
                pdt = TRUE_T0 + timedelta(seconds=self.pdt_error_s + offset)
                lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{pdt.isoformat(timespec='milliseconds')}")
            name = f"{i}-muted.{ext}" if i in MUTED else f"{i}.{ext}"
            lines += [f"#EXTINF:{duration:.6f},", name]
        return "\n".join([*lines, "#EXT-X-ENDLIST", ""])

    def parsed(self):
        return parse_vod_playlist(self.playlist(), AUDIO_URL).segments


def _merge_short_tail(parts: list[tuple[bytes, float, float]]) -> list[tuple[bytes, float, float]]:
    """A last piece of a few AAC frames goes into the one before (as Twitch has no such piece)."""
    if len(parts) > 1 and parts[-1][2] < 1.0:
        (a, off, dur), (b, _, extra) = parts[-2], parts[-1]
        parts = [*parts[:-2], (a + b, off, dur + extra)]
    return parts


@pytest.fixture(scope="module")
def master(tmp_path_factory) -> Path:
    """ONE AAC encode (200 s of pink noise, 48 kHz) as MPEG-TS: every cut below comes from it."""
    path = tmp_path_factory.mktemp("fill") / "master.ts"
    ffmpeg("-f", "lavfi", "-i", f"anoisesrc=d={LENGTH_S}:c=pink:r=48000:a=0.4:seed=7",
           "-c:a", "aac", "-b:a", "64k", "-f", "mpegts", str(path))  # fmt: skip
    return path


@pytest.fixture(scope="module")
def origin(master) -> float:
    """The encode's first timestamp: offsets below are relative to it."""
    d = master.parent
    ffmpeg("-i", str(master), "-c", "copy", "-f", "segment", "-segment_time", "1000",
           "-segment_list", str(d / "one.csv"), "-segment_list_type", "csv",
           "-segment_format", "mpegts", str(d / "one%03d.ts"))  # fmt: skip
    return segment_list(d / "one.csv")[0][1]


@pytest.fixture(scope="module")
def ts_vod(master, origin) -> FakeVod:
    d = master.parent / "ts"
    d.mkdir()
    ffmpeg("-i", str(master), "-c", "copy", "-f", "segment", "-segment_time", "10",
           "-segment_list", str(d / "vod.csv"), "-segment_list_type", "csv",
           "-segment_format", "mpegts", str(d / "vod%03d.ts"))  # fmt: skip
    parts = [((d / n).read_bytes(), a - origin, b - a) for n, a, b in segment_list(d / "vod.csv")]
    return FakeVod(_merge_short_tail(parts))


@pytest.fixture(scope="module")
def fmp4_vod(master) -> FakeVod:
    """The same encode as an fMP4 VOD (ffmpeg's HLS muxer, `baseMediaDecodeTime` running on)."""
    d = master.parent / "fmp4"
    d.mkdir()
    # relative names in `d`: the HLS muxer writes the init section relative to the cwd
    ffmpeg("-i", str(master), "-c", "copy", "-bsf:a", "aac_adtstoasc", "-f", "hls",
           "-hls_segment_type", "fmp4", "-hls_time", "10", "-hls_list_size", "0",
           "-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename", "%03d.m4s",
           "index.m3u8", cwd=d)  # fmt: skip
    vod = parse_vod_playlist((d / "index.m3u8").read_text())
    parts = [((d / s.uri).read_bytes(), s.offset_s, s.duration) for s in vod.segments]
    return FakeVod(_merge_short_tail(parts), init=(d / "init.mp4").read_bytes())


@pytest.fixture(params=["ts", "fmp4"])
def any_vod(request, ts_vod, fmp4_vod) -> FakeVod:
    return ts_vod if request.param == "ts" else fmp4_vod


@pytest.fixture(scope="module")
def live_audio(master, origin) -> list[tuple[bytes, float, float]]:
    """The live chunks: (ADTS .aac as the recorder stores it, offset from TRUE_T0, duration)."""
    d = master.parent / "live"
    d.mkdir()
    ffmpeg("-i", str(master), "-c", "copy", "-f", "segment",
           "-segment_times", ",".join(map(str, LIVE_CUTS)),
           "-segment_list", str(d / "live.csv"), "-segment_list_type", "csv",
           "-segment_format", "mpegts", str(d / "live%03d.ts"))  # fmt: skip
    pieces = segment_list(d / "live.csv")
    out = []
    for i in LIVE_PIECES:
        name, a, b = pieces[i]
        aac = d / f"{name}.aac"
        ffmpeg("-i", str(d / name), "-map", "0:a:0", "-c", "copy", "-f", "adts", str(aac))
        out.append((aac.read_bytes(), a - origin, b - a))
    return out


@pytest.fixture(scope="module")
def other_audio(master) -> bytes:
    """40 s of other noise: a live recording that matches nothing in the VOD."""
    path = master.parent / "other.aac"
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=d=40:c=white:r=48000:a=0.4:seed=99",
           "-c:a", "aac", "-b:a", "64k", "-f", "adts", str(path))  # fmt: skip
    return path.read_bytes()


# --------------------------------------------------------------------------- fakes


class MemoryStore:
    def __init__(self, failing: frozenset[str] = frozenset()) -> None:
        self.objects: dict[str, bytes] = {}
        self.reads: list[str] = []
        self.failing = failing  # keys whose reads fail like S3 does (NoSuchKey)

    def put_file(self, key, path, content_type=None) -> None:
        self.objects[key] = Path(path).read_bytes()

    def get_bytes(self, key: str) -> bytes:
        self.reads.append(key)
        if key in self.failing:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": key}}, "GetObject")
        return self.objects[key]


def comments() -> list[Comment]:
    """One comment every 0.7 s over the whole VOD (createdAt = the IRC server time)."""
    out = []
    for i in range(int(LENGTH_S / 0.7)):
        true_offset = i * 0.7 + 0.3
        out.append(
            Comment(
                id=f"k{i:04d}",
                created_at=TRUE_T0 + timedelta(seconds=CHAT_LAG_S + true_offset),
                content_offset_s=int(true_offset),
                commenter_id=f"u{i % 9}",
                commenter_login=f"user{i % 9}",
                commenter_display_name=None,
                fragments=(Fragment(f"msg {i} KEKW"),),
                badges=(),
                user_color=None,
                cursor=None,
            )
        )
    return out


def video_info() -> VideoInfo:
    return VideoInfo(
        id=VIDEO,
        title="t",
        created_at=TRUE_T0 + timedelta(seconds=3),
        published_at=None,
        length_s=LENGTH_S,
        status="RECORDED",
        broadcast_type="ARCHIVE",
        owner_id="4242",
        owner_login="streamer",
        owner_display_name="Streamer",
        game=None,
        seek_previews_url=f"{CDN}/storyboards/{VIDEO}-info.json",
    )


def replay(page_size=40) -> FakeChatReplay:
    return FakeChatReplay({VIDEO: comments()}, videos={VIDEO: video_info()}, page_size=page_size)


def backfiller(
    sessions,
    store,
    vod: FakeVod,
    *,
    gql=None,
    calls=None,
    after_commit=None,
    after_chat_commit=None,
    chat_batch_pages=20,
    owner="fill-1",
) -> Backfiller:
    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        if calls is not None:
            calls.append(name)
        if name == "index-dvr.m3u8":
            return httpx.Response(200, text=vod.playlist())
        if name == "init-0.mp4":
            return httpx.Response(200, content=vod.init)
        return httpx.Response(200, content=vod.segments[int(name.split(".")[0].split("-")[0])][0])

    return Backfiller(
        sessions=sessions,
        store=store,
        vod_source=FakeVodSource({VIDEO: {"audio_only": AUDIO_URL}}),
        gql=gql or replay(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        settings=BackfillSettings(
            _env_file=None,
            t0_chat_pages=3,
            retry_backoff_s=0,
            chunk_seconds=20,
            chat_batch_pages=chat_batch_pages,
        ),
        owner=owner,
        after_commit=after_commit,
        after_chat_commit=after_chat_commit,
        emotes=StaticEmoteSets(),
    )


# --------------------------------------------------------------------------- the live stream


@dataclass(frozen=True)
class Live:
    stream_id: int
    channel_id: int
    chunks: list[Interval]  # the live chunks' UTC ranges
    live_seconds: frozenset[datetime]  # seconds with a live chat_metrics_1s row


async def live_stream(
    sessions,
    store: MemoryStore,
    live_audio,
    *,
    status: str = "ended",
    source: str = "live",
    audio_for=None,  # seq -> bytes instead of the true live audio
    shift_s: dict[int, float] | None = None,  # seq -> its start_utc is off by this (a bad clock)
    ended_at: datetime = ENDED,
) -> Live:
    """What controller, recorder and chat-ingest wrote: the stream, chunks 0-2 with their gap rows
    and transcribe jobs (`commit_chunk`, as the recorder), live chat while the media was recorded
    plus a few seconds inside the gap (chat-ingest came back earlier)."""
    async with sessions.begin() as s:
        cid = (
            await s.execute(
                text(
                    "INSERT INTO channels (login, platform_user_id) VALUES ('streamer', '4242') "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        sid = (
            await s.execute(
                text(
                    "INSERT INTO streams (channel_id, platform_stream_id, started_at, ended_at, "
                    "status, source) VALUES (:c, :p, :t, :e, :st, :src) RETURNING id"
                ),
                {
                    "c": cid,
                    "p": STREAM,
                    "t": STARTED,
                    "e": ended_at if status == "ended" else None,
                    "st": status,
                    "src": source,
                },
            )
        ).scalar_one()
        prev_end, intervals = STARTED, []
        for seq, (data, offset, duration) in enumerate(live_audio):
            start = TRUE_T0 + timedelta(seconds=offset + (shift_s or {}).get(seq, 0.0))
            iv = Interval(start, start + timedelta(seconds=duration))
            key = keys.chunk_audio(sid, seq)
            store.objects[key] = (audio_for or {}).get(seq, data)
            hole = (iv.start - prev_end).total_seconds() > 0.5
            await core_chunks.commit_chunk(
                s,
                stream_id=sid,
                seq=seq,
                interval=iv,
                video_key=None,
                audio_key=key,
                video_bytes=None,
                audio_bytes=len(data),
                source="live" if source == "live" else "backfill",
                gaps_before=[Gap(prev_end, iv.start, "not_recorded")] if hole else [],
                default_priority=Priority.LIVE,
            )
            prev_end = iv.end
            intervals.append(iv)
        in_gap = [
            Interval(TRUE_T0 + timedelta(seconds=a), TRUE_T0 + timedelta(seconds=a + 1))
            for a in LIVE_CHAT_IN_GAP
        ]
        logged = [
            c for c in comments() if any(iv.contains(c.created_at) for iv in [*intervals, *in_gap])
        ]
        rows = [
            MessageRow(
                sent_at=c.created_at,
                msg_id=c.id,
                channel_id=cid,
                stream_id=sid,
                received_at=c.created_at + timedelta(seconds=0.2),
                user_id=c.commenter_id,
                user_login=c.commenter_login,
                text=c.text,
                emotes=None,
                badges=None,
            )
            for c in logged
        ]
        agg = SecondAggregator(grace=timedelta(0))
        for r in rows:
            agg.add_row(r)
        metrics = agg.pop_all()
        await ensure_chat_partitions(s, TRUE_T0.date(), TRUE_T0.date())
        await ChatWriter().write(s, rows, [], metrics, [])
    return Live(sid, cid, intervals, frozenset(m.ts for m in metrics))


async def query(sessions, sql, **params):
    async with sessions() as s:
        return (await s.execute(text(sql), params)).all()


LIVE_QUERIES = {
    "channels": "SELECT * FROM channels ORDER BY id",
    "streams": "SELECT * FROM streams ORDER BY id",
    "chunks": "SELECT * FROM chunks WHERE source = 'live' ORDER BY seq",
    "gaps": "SELECT * FROM stream_gaps ORDER BY start_utc",
    "jobs": "SELECT j.* FROM jobs j JOIN chunks c ON c.id = j.ref_id WHERE c.source = 'live' "
    "ORDER BY j.id",
    "messages": "SELECT * FROM chat_messages WHERE source = 'live' ORDER BY sent_at, msg_id",
    "events": "SELECT * FROM chat_events",
}


async def live_rows(sessions, live: Live) -> dict:
    """Every row the live services wrote, with all columns (ids and creation times included)."""
    out = {
        name: [tuple(r) for r in await query(sessions, sql)] for name, sql in LIVE_QUERIES.items()
    }
    metrics = await query(sessions, "SELECT * FROM chat_metrics_1s ORDER BY ts")
    out["metrics"] = [tuple(m) for m in metrics if m.ts in live.live_seconds]
    return out


SNAPSHOT = {
    "vods": "SELECT stream_id, platform_video_id, duration_s, t0_utc, t0_method, t0_candidates, "
    "rendition, media_status, media_done_s, chat_status, chat_done_s, chat_metrics_to, flags "
    "FROM vods",
    "chunks": "SELECT seq, start_utc, end_utc, video_key, audio_key, video_bytes, audio_bytes, "
    "source FROM chunks ORDER BY seq",
    "gaps": "SELECT start_utc, end_utc, reason FROM stream_gaps ORDER BY start_utc",
    "jobs": "SELECT c.seq, j.kind, j.priority, j.status FROM jobs j JOIN chunks c "
    "ON c.id = j.ref_id ORDER BY c.seq",
    "messages": "SELECT sent_at, msg_id, stream_id, received_at, user_id, text, source "
    "FROM chat_messages ORDER BY sent_at, msg_id",
    "metrics": "SELECT ts, stream_id, messages, unique_chatters, emote_counts FROM chat_metrics_1s "
    "ORDER BY ts",
    "leases": "SELECT resource FROM leases",
}


async def snapshot(sessions) -> dict:
    return {n: [tuple(r) for r in await query(sessions, sql)] for n, sql in SNAPSHOT.items()}


async def truncate(sessions) -> None:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with sessions.begin() as s:
        await s.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


def fill_ranges_utc(live: Live, t0: datetime, vod: FakeVod) -> list[Interval]:
    """The ranges the fill may write, worked out by hand: head, the gap, the tail."""
    end = at(t0, vod.segments[-1][1] + vod.segments[-1][2])
    c0, c1, c2 = live.chunks
    return [Interval(t0, c0.start), Interval(c1.end, c2.start), Interval(c2.end, end)]


def inside(vod: FakeVod, ranges: list[Interval], t0: datetime) -> list[int]:
    return [
        i
        for i, (_, off, dur) in enumerate(vod.segments)
        if any(at(t0, off) >= r.start and at(t0, off + dur) <= r.end for r in ranges)
    ]


# --------------------------------------------------------------------------- the main path


async def test_fill_gaps_fills_only_the_unrecorded_ranges(sessions, any_vod, live_audio):
    store = MemoryStore()
    live = await live_stream(sessions, store, live_audio)
    before = await live_rows(sessions, live)
    calls: list[str] = []
    out = await backfiller(sessions, store, any_vod, calls=calls).vod(
        VIDEO, VodOptions(fill_gaps=True)
    )
    assert out.status == "done", out
    assert out.message == "fill media done; chat done"

    # nothing the live services wrote changed, and no gap row was added
    assert await live_rows(sessions, live) == before

    # the vods row: the live stream, T0 measured on the live audio (not started_at, not PDT)
    (vod,) = await query(sessions, "SELECT * FROM vods")
    assert vod.stream_id == live.stream_id and vod.platform_video_id == VIDEO
    assert vod.t0_method == "audio" and vod.t0_utc != STARTED
    assert abs((vod.t0_utc - TRUE_T0).total_seconds()) < 0.002
    t0 = vod.t0_utc
    cands, flags = vod.t0_candidates, vod.flags
    assert cands["audio"] == t0.isoformat() and cands["audio_windows"] == 3
    assert cands["pdt_minus_audio_s"] == pytest.approx(PDT_ERROR_S, abs=0.002)
    assert flags["mode"] == "fill_gaps" and "fill_t0_pdt" not in flags
    assert [m["seq"] for m in flags["fill_audio"]["matches"]] == [0, 1, 2]
    assert flags["fill_audio"]["spread_ms"] < 2 and "fill_t0_spread" not in flags
    assert [r["kind"] for r in flags["fill_ranges"]] == ["head", "gap", "tail"]
    assert [r["gap_row"] for r in flags["fill_ranges"]] == [True, True, False]
    assert (vod.media_status, vod.chat_status, vod.rendition) == ("done", "done", "audio_only")
    vod_end = any_vod.segments[-1][1] + any_vod.segments[-1][2]
    assert vod.media_done_s == pytest.approx(vod_end, abs=1e-6)

    # exactly the segments inside the ranges, muted ones skipped, 20 s chunks, seqs from the base
    ranges = fill_ranges_utc(live, t0, any_vod)
    filled = [i for i in inside(any_vod, ranges, t0) if i not in MUTED]
    assert filled == [0, 1, 9, 10, 11, 16, 17, 19]
    fill = await query(sessions, "SELECT * FROM chunks WHERE source = 'backfill' ORDER BY seq")
    groups = [[0, 1], [9, 10], [11], [16, 17], [19]]
    assert [c.seq for c in fill] == [FILL_SEQ_BASE + i for i in range(5)]
    for c, group in zip(fill, groups, strict=True):
        first, last = any_vod.segments[group[0]], any_vod.segments[group[-1]]
        assert (c.start_utc, c.end_utc) == (at(t0, first[1]), at(t0, last[1] + last[2]))
        assert c.stream_id == live.stream_id and c.video_key is None
        assert c.audio_key == keys.chunk_audio(live.stream_id, c.seq) and c.audio_bytes > 0
        assert store.objects[c.audio_key][:2] == b"\xff\xf1"  # ADTS
        assert not any(c.start_utc < iv.end and iv.start < c.end_utc for iv in live.chunks)
        assert any(r.start <= c.start_utc and c.end_utc <= r.end for r in ranges)
    assert not any(f"18-muted.{'mp4' if any_vod.init else 'ts'}" == n for n in calls)
    jobs = await query(
        sessions,
        "SELECT c.seq, j.priority, j.status FROM jobs j JOIN chunks c ON c.id = j.ref_id "
        "WHERE c.source = 'backfill' ORDER BY c.seq",
    )
    assert [tuple(j) for j in jobs] == [(c.seq, Priority.BACKFILL, "queued") for c in fill]
    assert await query(sessions, "SELECT * FROM leases") == []

    # chat: replay messages only in the ranges' whole seconds that had no live chat
    spans = chat_spans(ranges)
    expected = [
        c
        for c in comments()
        if any(sp.contains(c.created_at) for sp in spans)
        and second_of(c.created_at) not in live.live_seconds
    ]
    assert len(expected) > 100
    ours = await query(
        sessions, "SELECT msg_id, sent_at, stream_id FROM chat_messages WHERE source = 'backfill'"
    )
    assert sorted(m.msg_id for m in ours) == sorted(c.id for c in expected)
    assert {m.stream_id for m in ours} == {live.stream_id}
    metrics = await query(sessions, "SELECT ts, stream_id, messages FROM chat_metrics_1s")
    fill_metrics = {m.ts: m for m in metrics if m.ts not in live.live_seconds}
    agg = SecondAggregator(grace=timedelta(0))
    for c in expected:
        agg.add_message(live.channel_id, live.stream_id, c.created_at, c.commenter_id)
    assert {m.ts: m.messages for m in agg.pop_all()} == {
        ts: m.messages for ts, m in fill_metrics.items()
    }
    assert all(any(sp.start <= ts < sp.end for sp in spans) for ts in fill_metrics)
    in_gap = {second_of(TRUE_T0 + timedelta(seconds=a + 0.5)) for a in LIVE_CHAT_IN_GAP}
    assert vod.flags["fill_chat_live_seconds"] >= 2  # the live seconds inside the gap span
    assert in_gap & live.live_seconds
    assert any("seconds with live chat kept" in line for line in out.lines)
    assert any(line.startswith("clips: skipped: needs Helix") for line in out.lines)

    # `status`: the fill's own chunks, jobs and chat seconds; the recorder's gaps not counted
    async with sessions() as s:
        (row,) = await bf_rows.status_rows(s, [VIDEO])
    assert (row.chunks, row.gaps, float(row.muted_s), row.stream_source) == (5, 0, 0.0, "live")
    assert (row.chat_seconds, row.transcribe_pending) == (len(fill_metrics), 5)
    assert "--fill-gaps of live stream" in "\n".join(cli.status_lines([row]))

    # a re-run has nothing to do; without --fill-gaps the VOD gets its clips only
    snap = await snapshot(sessions)
    again = await backfiller(sessions, store, any_vod).vod(VIDEO, VodOptions(fill_gaps=True))
    assert again.status == "done" and again.message == "fill media complete already; chat done"
    plain = await backfiller(sessions, store, any_vod).vod(VIDEO)
    assert plain.status == "skipped" and "clips only" in plain.message
    assert await snapshot(sessions) == snap


async def test_dry_run_prints_the_plan_and_writes_nothing(sessions, ts_vod, live_audio, capsys):
    store = MemoryStore()
    live = await live_stream(sessions, store, live_audio)
    before = await snapshot(sessions)
    out = await backfiller(sessions, store, ts_vod).vod(
        VIDEO, VodOptions(fill_gaps=True, dry_run=True)
    )
    assert out.status == "dry_run"
    report = "\n".join(out.lines)
    with capsys.disabled():
        print("\n--- dry run of the fake fixture ---")
        cli.print_outcome(out)
    assert f"fill: live stream {live.stream_id} (broadcast {STREAM}" in report
    assert "(audio): 3 of 3 live chunk windows matched" in report
    assert re.search(r"T0_pdt - T0_audio = \+13[5-9]\.\d ms", report), report
    assert "T0 check: trusted" in report
    assert re.search(
        r"  candidates: pdt=2026-11-18T06:27:49.637000\+00:00, pdt_points=20, ", report
    )
    assert "pdt_max_dev_s=0.0" in report and "audio_windows=3" in report
    assert re.search(r"  flags: mode=fill_gaps, .*muted_s=10", report), report
    assert "fill ranges: 3" in report and "tail" in report and "no gap row" in report
    assert (
        f"plan: 5 chunks (0:01:20) from 0.0 s, seq {FILL_SEQ_BASE}..{FILL_SEQ_BASE + 4}" in report
    )
    assert "chat: would replay 3 fill ranges" in report
    assert await snapshot(sessions) == before
    assert set(store.objects) == {keys.chunk_audio(live.stream_id, s) for s in range(3)}


# --------------------------------------------------------------------------- resume


async def test_crashes_mid_media_and_mid_chat_resume_to_one_clean_run(sessions, ts_vod, live_audio):
    class Crash(Exception):
        pass

    async def crash_after_second_chunk(unit):
        if unit.seq == FILL_SEQ_BASE + 1:
            raise Crash

    async def crash_after_batch(n):
        if n == 2:
            raise Crash

    store = MemoryStore()
    await live_stream(sessions, store, live_audio)
    with pytest.raises(Crash):  # the same page size: the T0 chat sample is the same
        await backfiller(
            sessions, store, ts_vod, gql=replay(page_size=15), after_commit=crash_after_second_chunk
        ).vod(VIDEO, VodOptions(fill_gaps=True))
    (vod,) = await query(sessions, "SELECT media_status, media_done_s, chat_status FROM vods")
    assert (vod.media_status, vod.chat_status) == ("pending", "pending")
    assert len(await query(sessions, "SELECT * FROM chunks WHERE source = 'backfill'")) == 2
    assert await query(sessions, "SELECT * FROM leases") == []

    # media completes, then the chat crashes after its 2nd batch (1 page per batch)
    bf = backfiller(
        sessions,
        store,
        ts_vod,
        gql=replay(page_size=15),
        after_chat_commit=crash_after_batch,
        chat_batch_pages=1,
    )
    with pytest.raises(Crash):
        await bf.vod(VIDEO, VodOptions(fill_gaps=True))
    (vod,) = await query(sessions, "SELECT media_status, chat_status, chat_metrics_to FROM vods")
    assert (vod.media_status, vod.chat_status) == ("done", "pending")
    written = await query(
        sessions,
        "SELECT max(sent_at) AS last FROM chat_messages WHERE source = 'backfill'",
    )
    assert written[0].last < vod.chat_metrics_to  # nothing at or after the watermark

    # --max-minutes slices for the chat (0.2 min = 12 s of range seconds per run) up to the end
    for _ in range(30):
        out = await backfiller(sessions, store, ts_vod, gql=replay(page_size=15)).vod(
            VIDEO, VodOptions(fill_gaps=True, chat_max_minutes=0.2)
        )
        if out.message.endswith("chat done"):
            break
    assert out.status == "done"
    resumed = await snapshot(sessions)

    await truncate(sessions)
    store = MemoryStore()
    await live_stream(sessions, store, live_audio)
    out = await backfiller(sessions, store, ts_vod, gql=replay(page_size=15)).vod(
        VIDEO, VodOptions(fill_gaps=True)
    )
    assert out.status == "done"
    assert await snapshot(sessions) == resumed


async def test_max_minutes_fills_in_slices(sessions, ts_vod, live_audio):
    store = MemoryStore()
    await live_stream(sessions, store, live_audio)
    statuses = []
    for _ in range(6):
        out = await backfiller(sessions, store, ts_vod).vod(
            VIDEO, VodOptions(fill_gaps=True, max_minutes=0.4, chat=False)
        )
        statuses.append(out.status)
        if out.status == "done":
            break
    # 24 s per run, a chunk started before it is used up runs to its end:
    # [0, 20) + [90, 110) | [110, 120) + [160, 180) | [190, 200) = the end
    assert statuses == ["partial", "partial", "done"]
    seqs = await query(sessions, "SELECT seq FROM chunks WHERE source = 'backfill' ORDER BY seq")
    assert [s.seq for s in seqs] == [FILL_SEQ_BASE + i for i in range(5)]
    (vod,) = await query(sessions, "SELECT chat_status FROM vods")
    assert vod.chat_status == "off"


# --------------------------------------------------------------------------- T0


async def refused_then(sessions, store, vod, why: str, **opts):
    """A run that must refuse (nothing written), and its dry run that shows why."""
    before = await snapshot(sessions)
    with pytest.raises(FillRefused, match=why):
        await backfiller(sessions, store, vod).vod(VIDEO, VodOptions(fill_gaps=True, **opts))
    dry = await backfiller(sessions, store, vod).vod(
        VIDEO, VodOptions(fill_gaps=True, dry_run=True, **opts)
    )
    assert dry.status == "dry_run"
    assert any(line.startswith("T0 check: a run refuses to write") for line in dry.lines)
    assert any(line.startswith("plan: ") for line in dry.lines)  # still the whole plan
    assert await snapshot(sessions) == before
    return dry


async def test_without_a_live_audio_match_only_an_accepted_pdt_is_written(
    sessions, ts_vod, live_audio, other_audio
):
    store = MemoryStore()
    live = await live_stream(
        sessions, store, live_audio, audio_for=dict.fromkeys(range(3), other_audio)
    )
    dry = await refused_then(sessions, store, ts_vod, "no live audio window matched")
    assert any("no live audio window matched" in line for line in dry.lines)
    with pytest.raises(FillRefused, match="--accept-t0 audio: no live audio window matched"):
        await backfiller(sessions, store, ts_vod).vod(
            VIDEO, VodOptions(fill_gaps=True, accept_t0="audio")
        )

    out = await backfiller(sessions, store, ts_vod).vod(
        VIDEO, VodOptions(fill_gaps=True, accept_t0="pdt")
    )
    assert out.status == "done", out
    (vod,) = await query(sessions, "SELECT t0_utc, t0_method, t0_candidates, flags FROM vods")
    t0_pdt = TRUE_T0 + timedelta(seconds=PDT_ERROR_S)
    assert vod.t0_method == "pdt" and vod.t0_utc == t0_pdt
    assert vod.flags["fill_t0_pdt"]["reason"] == "--accept-t0 pdt"
    assert vod.flags["fill_t0_accepted"]["t0"] == "pdt"
    assert len(vod.flags["fill_audio"]["matches"]) == 3  # every chunk tried once, all weak
    assert all(m["score"] < 0.5 for m in vod.flags["fill_audio"]["matches"])
    assert "audio" not in vod.t0_candidates
    first = await query(sessions, "SELECT start_utc FROM chunks WHERE seq = :q", q=FILL_SEQ_BASE)
    assert first[0].start_utc == t0_pdt  # chunks on the PDT clock, still inside the ranges
    assert live.chunks[0].start > at(t0_pdt, 20)


async def test_matches_that_disagree_need_an_accepted_t0(sessions, ts_vod, live_audio):
    store = MemoryStore()
    await live_stream(sessions, store, live_audio, shift_s={2: 0.1})  # chunk 2's clock: +100 ms
    await refused_then(sessions, store, ts_vod, r"the windows disagree by \d+\.\d ms", chat=False)
    out = await backfiller(sessions, store, ts_vod).vod(
        VIDEO, VodOptions(fill_gaps=True, chat=False, accept_t0="audio")
    )
    assert out.status == "done"
    (vod,) = await query(sessions, "SELECT t0_utc, t0_method, flags FROM vods")
    assert vod.t0_method == "audio"
    assert abs((vod.t0_utc - TRUE_T0).total_seconds()) < 0.002  # the median of 3
    spread = vod.flags["fill_t0_spread"]
    assert 98 < spread["spread_ms"] < 102 and len(spread["t0s"]) == 3
    assert "the windows disagree" in vod.flags["fill_t0_accepted"]["problems"][0]


async def test_a_single_matching_window_is_not_enough(sessions, ts_vod, live_audio, other_audio):
    store = MemoryStore()
    await live_stream(  # chunks 1 and 2 match nothing: only chunk 0's window is good
        sessions, store, live_audio, audio_for={1: other_audio, 2: other_audio}
    )
    await refused_then(sessions, store, ts_vod, r"only 1 matching window\(s\), 2 needed")


async def test_an_audio_t0_far_from_the_pdt_is_not_trusted(sessions, ts_vod, live_audio):
    store = MemoryStore()
    await live_stream(sessions, store, live_audio)
    vod = dataclasses.replace(ts_vod, pdt_error_s=0.8)  # a PDT 0.8 s off
    await refused_then(sessions, store, vod, r"T0_pdt - T0_audio = \+0\.[78]\d\d s \(> 0\.5 s\)")
    out = await backfiller(sessions, store, vod).vod(
        VIDEO, VodOptions(fill_gaps=True, chat=False, accept_t0="pdt")
    )
    assert out.status == "done"
    (row,) = await query(sessions, "SELECT t0_utc, t0_method FROM vods")
    assert row.t0_method == "pdt" and row.t0_utc == TRUE_T0 + timedelta(seconds=0.8)


async def test_without_pdt_never_the_chat_anchor_or_created_at(
    sessions, ts_vod, live_audio, other_audio
):
    store = MemoryStore()
    await live_stream(sessions, store, live_audio)
    vod = dataclasses.replace(ts_vod, pdt_error_s=None)  # no PDT: the estimate is the chat anchor
    await refused_then(sessions, store, vod, "no PDT to check T0_audio against")
    with pytest.raises(FillRefused, match="--accept-t0 pdt: the audio_only playlist has no PDT"):
        await backfiller(sessions, store, vod).vod(
            VIDEO, VodOptions(fill_gaps=True, accept_t0="pdt")
        )
    out = await backfiller(sessions, store, vod).vod(
        VIDEO, VodOptions(fill_gaps=True, chat=False, accept_t0="audio")
    )
    assert out.status == "done"
    (row,) = await query(sessions, "SELECT t0_utc, t0_method FROM vods")
    assert row.t0_method == "audio" and abs((row.t0_utc - TRUE_T0).total_seconds()) < 0.002

    await truncate(sessions)  # no PDT and no match: nothing to use, not even for a dry run
    store = MemoryStore()
    await live_stream(sessions, store, live_audio, audio_for=dict.fromkeys(range(3), other_audio))
    for opts in ({}, {"accept_t0": "pdt"}, {"accept_t0": "audio"}, {"dry_run": True}):
        with pytest.raises(FillRefused, match=r"no T0|no live audio window"):
            await backfiller(sessions, store, vod).vod(VIDEO, VodOptions(fill_gaps=True, **opts))
    assert await query(sessions, "SELECT * FROM vods") == []


@pytest.mark.parametrize("failure", ["undecodable", "storage"])
async def test_a_failed_match_makes_way_for_the_next_chunk_of_the_seam(
    sessions, ts_vod, live_audio, failure
):
    """Chunk 2 is the nearest chunk of two seams (the gap's end, the tail's start). Its match
    fails; the tail's seam goes on to chunk 1, the gap's end has no other chunk."""
    key = None
    if failure == "storage":
        store = MemoryStore()
        live = await live_stream(sessions, store, live_audio)
        key = keys.chunk_audio(live.stream_id, 2)
        store.failing = frozenset({key})
    else:
        store = MemoryStore()
        await live_stream(sessions, store, live_audio, audio_for={2: b"\x00" * 4000})
    out = await backfiller(sessions, store, ts_vod).vod(
        VIDEO, VodOptions(fill_gaps=True, chat=False)
    )
    assert out.status == "done", out
    (vod,) = await query(sessions, "SELECT t0_utc, t0_method, flags FROM vods")
    assert vod.t0_method == "audio" and abs((vod.t0_utc - TRUE_T0).total_seconds()) < 0.002
    audio = vod.flags["fill_audio"]
    assert [m["seq"] for m in audio["matches"]] == [0, 1]  # chunk 2 left no match
    error = "ClientError" if failure == "storage" else "MediaError"
    assert any(n.startswith(f"chunk 2: {error}") for n in audio["notes"]), audio["notes"]
    assert "no good match near live chunks 2" in audio["notes"]
    if key is not None:
        assert store.reads.count(key) == 1  # tried once, though two seams name it


# --------------------------------------------------------------------------- refusals


async def test_refused_until_the_recorder_is_done(sessions, ts_vod, live_audio):
    store = MemoryStore()
    live = await live_stream(sessions, store, live_audio, ended_at=utc_now() - timedelta(minutes=1))
    with pytest.raises(FillRefused, match="less than 5 min ago"):
        await backfiller(sessions, store, ts_vod).vod(VIDEO, VodOptions(fill_gaps=True))
    async with sessions.begin() as s:
        await s.execute(
            text("UPDATE streams SET ended_at = :e"), {"e": utc_now() - timedelta(minutes=6)}
        )
        await s.execute(
            text(
                "INSERT INTO leases (resource, owner, expires_at) "
                "VALUES (:r, 'recorder-1', now() + interval '1 minute')"
            ),
            {"r": recording_lease(live.channel_id)},
        )
    with pytest.raises(FillRefused, match=r"a recorder \(recorder-1\) holds the recording lease"):
        await backfiller(sessions, store, ts_vod).vod(VIDEO, VodOptions(fill_gaps=True))
    assert await query(sessions, "SELECT * FROM vods") == []


async def test_refused_for_a_live_seq_next_to_the_fill_base_or_leftover_fill_chunks(
    sessions, ts_vod, live_audio
):
    store = MemoryStore()
    live = await live_stream(sessions, store, live_audio)
    late = Interval(TRUE_T0 + timedelta(seconds=300), TRUE_T0 + timedelta(seconds=310))
    async with sessions.begin() as s:  # a live chunk at FILL_SEQ_BASE - 1 (after the VOD)
        await s.execute(
            text("INSERT INTO chunks (stream_id, seq, start_utc, end_utc) VALUES (:s, :q, :a, :b)"),
            {"s": live.stream_id, "q": FILL_SEQ_BASE - 1, "a": late.start, "b": late.end},
        )
    with pytest.raises(FillRefused, match=f"live seqs up to {FILL_SEQ_BASE - 1}"):
        await backfiller(sessions, store, ts_vod).vod(VIDEO, VodOptions(fill_gaps=True))

    async with sessions.begin() as s:
        await s.execute(text("DELETE FROM chunks WHERE seq = :q"), {"q": FILL_SEQ_BASE - 1})
    out = await backfiller(sessions, store, ts_vod).vod(
        VIDEO, VodOptions(fill_gaps=True, chat=False)
    )
    assert out.status == "done"
    async with sessions.begin() as s:  # the fill's vods row gone, its chunks left behind
        await s.execute(text("DELETE FROM vods"))
    before = await snapshot(sessions)
    for dry in (False, True):
        with pytest.raises(FillRefused, match=r"has 5 backfill chunks .* but no fill vods row"):
            await backfiller(sessions, store, ts_vod).vod(
                VIDEO, VodOptions(fill_gaps=True, dry_run=dry)
            )
    assert await snapshot(sessions) == before


@pytest.mark.parametrize(
    ("status", "source", "why"),
    [
        ("live", "live", "still live"),
        ("ended", "backfill", "backfilled stream"),
    ],
)
async def test_refused_for_a_live_or_backfilled_stream(
    sessions, ts_vod, live_audio, status, source, why
):
    store = MemoryStore()
    await live_stream(sessions, store, live_audio, status=status, source=source)
    before = await snapshot(sessions)
    for dry in (False, True):
        with pytest.raises(FillRefused, match=why):
            await backfiller(sessions, store, ts_vod).vod(
                VIDEO, VodOptions(fill_gaps=True, dry_run=dry)
            )
    assert await snapshot(sessions) == before


async def test_refused_when_we_did_not_record_it(sessions, ts_vod):
    with pytest.raises(FillRefused, match="not recorded live by us"):
        await backfiller(sessions, MemoryStore(), ts_vod).vod(VIDEO, VodOptions(fill_gaps=True))
    assert await query(sessions, "SELECT * FROM vods") == []


async def test_cli_exits_2_on_a_refusal_and_checks_its_options(monkeypatch, capsys, settings):
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(["vod", "1", "--fill-gaps", "--no-media"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        cli.parse_args(["vod", "1", "--fill-gaps", "--start-offset", "60"])
    assert cli.options(cli.parse_args(["vod", "1", "--fill-gaps", "--dry-run"])).fill_gaps
    opts = cli.options(cli.parse_args(["vod", "1", "--fill-gaps", "--accept-t0", "pdt"]))
    assert opts.accept_t0 == "pdt"
    for bad in (["--accept-t0", "pdt"], ["--fill-gaps", "--accept-t0", "chat"]):
        with pytest.raises(SystemExit):
            cli.parse_args(["vod", "1", *bad])

    class Refusing:
        def __init__(self, **kwargs) -> None:
            pass

        async def vod(self, ref, opts):
            assert opts.fill_gaps and opts.dry_run
            raise FillRefused(
                "stream 9 is still live (the recorder writes it): wait until it ended"
            )

    monkeypatch.setattr(cli, "Backfiller", Refusing)
    monkeypatch.setattr(cli, "BackfillSettings", lambda: BackfillSettings(_env_file=None))
    monkeypatch.setattr(cli, "TwitchSettings", lambda: TwitchSettings(_env_file=None))
    args = cli.parse_args(["vod", VIDEO, "--fill-gaps", "--dry-run"])
    assert await cli._backfill(args, settings, sessions=None) == 2
    assert "[refused] 2000000004: stream 9 is still live" in capsys.readouterr().err


class EmptyPages(FakeChatReplay):
    """Answers an empty page that says more follow at the given calls (0-based; the first 3 are
    the T0 sample), as Twitch might in a hiccup."""

    def __init__(self, empty: set[int], **kwargs) -> None:
        super().__init__({VIDEO: comments()}, videos={VIDEO: video_info()}, **kwargs)
        self.empty = empty

    async def page(self, video_id, *, offset_s=None, cursor=None):
        if len(self.calls) in self.empty:
            self.calls.append((video_id, offset_s, cursor))
            return CommentPage(video_id, (), True, False, None)
        return await super().page(video_id, offset_s=offset_s, cursor=cursor)


CHAT_ROWS = (
    "SELECT msg_id FROM chat_messages WHERE source = 'backfill' ORDER BY msg_id",
    "SELECT ts, messages, unique_chatters FROM chat_metrics_1s ORDER BY ts",
)


async def chat_rows(sessions) -> list:
    return [[tuple(r) for r in await query(sessions, sql)] for sql in CHAT_ROWS]


async def test_an_empty_chat_page_with_a_next_page_does_not_end_a_range(
    sessions, ts_vod, live_audio
):
    store = MemoryStore()
    await live_stream(sessions, store, live_audio)
    out = await backfiller(sessions, store, ts_vod, gql=replay(page_size=15)).vod(
        VIDEO, VodOptions(fill_gaps=True)
    )
    assert out.status == "done"
    clean = await chat_rows(sessions)

    await truncate(sessions)  # an empty page inside the head range and one inside the gap's
    store = MemoryStore()
    await live_stream(sessions, store, live_audio)
    gql = EmptyPages({4, 9}, page_size=15)
    out = await backfiller(sessions, store, ts_vod, gql=gql).vod(VIDEO, VodOptions(fill_gaps=True))
    assert out.status == "done" and gql.calls[5] == gql.calls[4]  # asked the same again
    assert await chat_rows(sessions) == clean

    await truncate(sessions)  # three in a row: give up (a re-run resumes)
    store = MemoryStore()
    await live_stream(sessions, store, live_audio)
    with pytest.raises(GqlUnavailable, match="3 empty chat pages"):
        await backfiller(sessions, store, ts_vod, gql=EmptyPages({4, 5, 6}, page_size=15)).vod(
            VIDEO, VodOptions(fill_gaps=True)
        )
    (vod,) = await query(sessions, "SELECT media_status, chat_status FROM vods")
    assert (vod.media_status, vod.chat_status) == ("done", "pending")
    out = await backfiller(sessions, store, ts_vod, gql=replay(page_size=15)).vod(
        VIDEO, VodOptions(fill_gaps=True)
    )
    assert out.status == "done"
    assert await chat_rows(sessions) == clean


# --------------------------------------------------------------------------- readers


async def test_coverage_reads_filled_ranges_as_media_and_leaves_the_live_parts(
    sessions, ts_vod, live_audio
):
    store = MemoryStore()
    live = await live_stream(sessions, store, live_audio)
    whole = Interval(TRUE_T0, TRUE_T0 + timedelta(seconds=LENGTH_S + 5))  # past the VOD

    async def cover(iv: Interval):
        async with sessions() as s:
            return await transcripts.coverage(s, live.stream_id, iv)

    before = await cover(whole)
    c0, c1, c2 = live.chunks
    assert before.untranscribed == (Interval(c0.start, c1.end), c2)  # the live chunks
    assert before.unrecorded == () and before.transcribed == ()

    await backfiller(sessions, store, ts_vod).vod(VIDEO, VodOptions(fill_gaps=True, chat=False))
    fill = await query(
        sessions, "SELECT id, start_utc, end_utc FROM chunks WHERE source = 'backfill' ORDER BY seq"
    )
    after = await cover(whole)
    assert after.unrecorded == ()  # the unfilled edges of the tail (no gap row) read as gap
    fill_ivs = [Interval(c.start_utc, c.end_utc) for c in fill]
    for iv in fill_ivs:  # filled: media now (not yet transcribed), not gap any more
        assert any(u.start <= iv.start and iv.end <= u.end for u in after.untranscribed)
        assert not any(g.overlaps(iv) for g in after.gap)
    for iv in live.chunks:  # the live parts read as before
        assert any(u.start <= iv.start and iv.end <= u.end for u in after.untranscribed)
    muted = ts_vod.segments[18]
    vod_t0 = (await query(sessions, "SELECT t0_utc FROM vods"))[0].t0_utc
    hole = Interval(at(vod_t0, muted[1]), at(vod_t0, muted[1] + muted[2]))
    assert any(g.start <= hole.start and hole.end <= g.end for g in after.gap)

    # transcribed fill chunks read as transcribed (the transcriber's rows)
    async with sessions.begin() as s:
        for c in fill:
            await s.execute(
                text(
                    "INSERT INTO chunk_transcripts (chunk_id, model, words_key, segment_count, "
                    "owned_from, owned_to) VALUES (:c, 'fake', 'k', 0, :a, :b)"
                ),
                {"c": c.id, "a": c.start_utc, "b": c.end_utc},
            )
    done = await cover(whole)
    for iv in fill_ivs:
        assert any(t.start <= iv.start and iv.end <= t.end for t in done.transcribed)
    assert done.untranscribed == (Interval(c0.start, c1.end), c2)  # the live chunks only
    assert done.unrecorded == ()


def helix_with_clip(offset: int) -> HelixClient:
    video = {
        "id": VIDEO, "stream_id": STREAM, "user_id": "4242", "user_login": "streamer",
        "user_name": "Streamer", "title": "t", "description": "",
        "created_at": "2026-11-18T06:27:52Z", "published_at": None, "url": "",
        "thumbnail_url": "", "viewable": "public", "view_count": 1, "language": "en",
        "type": "archive", "duration": "3m20s", "muted_segments": None,
    }  # fmt: skip
    clip = {
        "id": "Moment", "url": "", "embed_url": "", "broadcaster_id": "4242",
        "broadcaster_name": "Streamer", "creator_id": "1", "creator_name": "Clipper",
        "video_id": VIDEO, "game_id": "1", "language": "en", "title": "the moment",
        "view_count": 42, "created_at": "2026-11-18T06:31:00Z", "thumbnail_url": "",
        "duration": 20.0, "vod_offset": offset, "is_featured": False,
    }  # fmt: skip

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if request.url.path == "/helix/videos":
            return httpx.Response(200, json={"data": [video]})
        if request.url.path == "/helix/clips":
            return httpx.Response(200, json={"data": [clip]})
        return httpx.Response(404)

    return HelixClient("cid", "s", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_the_fill_run_attaches_viewer_clips_on_its_t0(sessions, ts_vod, live_audio):
    store = MemoryStore()
    live = await live_stream(sessions, store, live_audio)
    bf = backfiller(sessions, store, ts_vod)
    bf.helix = helix_with_clip(170)
    out = await bf.vod(VIDEO, VodOptions(fill_gaps=True, chat=False))
    assert out.status == "done"
    (clip,) = await query(sessions, "SELECT * FROM viewer_clips")
    (vod,) = await query(sessions, "SELECT t0_utc FROM vods")
    assert (clip.stream_id, clip.time_source) == (live.stream_id, "vod_t0")
    assert clip.start_utc == vod.t0_utc + timedelta(seconds=170)
    assert (
        await query(sessions, "SELECT * FROM leases WHERE resource = :r", r=backfill_lease(VIDEO))
        == []
    )
