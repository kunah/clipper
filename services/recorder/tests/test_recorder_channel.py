"""ChannelRecorder against a scripted fake Twitch CDN (no network, no DB)."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from clipper_core.contracts import GapReason, RecordingMode
from clipper_recorder.channel import Assignment, ChannelRecorder
from clipper_recorder.spool import Spool
from clipper_recorder.tracker import SegmentTracker

T0 = datetime(2026, 11, 18, 20, 0, tzinfo=UTC)
PLAYLIST = "https://cdn.test/v1/playlist/x.m3u8"


def at(i: int) -> datetime:
    return T0 + timedelta(seconds=2 * i)


def playlist(indices, ads=(), discontinuities=(), maps=None) -> str:
    """maps: {segment index: init name} -> fMP4 playlist (EXT-X-MAP before that segment)."""
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:6", f"#EXT-X-MEDIA-SEQUENCE:{indices[0]}"]
    ext = "mp4" if maps else "ts"
    for i in indices:
        if i in discontinuities:
            lines.append("#EXT-X-DISCONTINUITY")
        if maps and i in maps:
            lines.append(f'#EXT-X-MAP:URI="https://cdn.test/v1/segment/init-{maps[i]}.mp4"')
        stamp = at(i).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{stamp}")
        lines.append(f"#EXTINF:2.000,{'Amazon|1' if i in ads else 'live'}")
        lines.append(f"https://cdn.test/v1/segment/{i}.{ext}")
    return "\n".join(lines) + "\n"


class FakeCdn:
    """Serves the scripted playlists in order, then stops the recorder."""

    def __init__(self, script: list[str | int], stop: asyncio.Event, missing=()) -> None:
        self.script = list(script)  # playlist text, or an HTTP status to fail with
        self.stop = stop
        self.missing = set(missing)
        self.segment_requests: list[int] = []
        self.init_requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".m3u8"):
            if not self.script:
                self.stop.set()
                return httpx.Response(404)
            step = self.script.pop(0)
            if isinstance(step, int):
                return httpx.Response(step)
            return httpx.Response(200, text=step)
        name = request.url.path.rsplit("/", 1)[1].rsplit(".", 1)[0]
        if name.startswith("init-"):
            self.init_requests.append(name)
            return httpx.Response(200, content=f"<{name}>".encode())
        i = int(name)
        self.segment_requests.append(i)
        if i in self.missing:
            return httpx.Response(404)
        return httpx.Response(200, content=f"<seg{i}>".encode())


class FakeResolver:
    def __init__(self, answers=None) -> None:
        self.answers = list(answers or [])
        self.calls = 0

    async def resolve(self, login, mode):
        self.calls += 1
        return self.answers.pop(0) if self.answers else PLAYLIST


async def record(tmp_path, script, *, tracker=None, missing=(), resolver=None, chunk_seconds=6.0):
    stop = asyncio.Event()
    cdn = FakeCdn(script, stop, missing)
    closed = []
    spool = Spool(tmp_path)
    assignment = Assignment(1, "dallas", RecordingMode.FULL, stream_id=7, started_at=T0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(cdn.handler)) as http:
        rec = ChannelRecorder(
            assignment,
            http=http,
            resolver=resolver or FakeResolver(),
            spool=spool,
            submit=closed.append,
            tracker=tracker or SegmentTracker(media_end=None),
            chunk_seconds=chunk_seconds,
            offline_retry_s=0,
            reload_interval_s=0,
        )
        await asyncio.wait_for(rec.run(stop), timeout=10)
    return closed, spool, cdn


async def test_overlapping_reloads_become_contiguous_chunks(tmp_path):
    script = [playlist([0, 1, 2]), playlist([1, 2, 3, 4]), playlist([3, 4, 5, 6, 7, 8, 9])]
    closed, spool, cdn = await record(tmp_path, script)

    assert cdn.segment_requests == list(range(10))  # every segment downloaded exactly once
    assert [c.seq for c in closed] == [0, 1, 2, 3]
    assert [(c.start, c.end) for c in closed] == [
        (at(0), at(3)),
        (at(3), at(6)),
        (at(6), at(9)),
        (at(9), at(10)),  # partial chunk closed when the recorder stopped
    ]
    assert all(c.closed and c.gap_before is None for c in closed)
    media = spool.media_path(closed[1]).read_bytes()
    assert media == b"<seg3><seg4><seg5>"
    assert closed[1].bytes == len(media) and closed[1].segments == 3
    # state on disk matches what was handed to the finisher
    assert spool.pending() == closed


async def test_ad_break_and_lost_segment_split_chunks_and_record_gaps(tmp_path):
    script = [
        playlist([0, 1, 2], ads={2}, discontinuities={2}),
        playlist([1, 2, 3, 4, 5], ads={2, 3}, discontinuities={2, 4}),
        playlist([4, 5, 6, 7]),
    ]
    closed, _, cdn = await record(tmp_path, script, missing={6}, chunk_seconds=60)

    assert 2 not in cdn.segment_requests and 3 not in cdn.segment_requests  # ads not downloaded
    assert [(c.start, c.end) for c in closed] == [(at(0), at(2)), (at(4), at(6)), (at(7), at(8))]
    assert closed[0].gap_before is None
    ad_gap = closed[1].gap_before
    assert (ad_gap.start, ad_gap.end, ad_gap.reason) == (at(2), at(4), GapReason.AD)
    lost = closed[2].gap_before
    assert (lost.start, lost.end, lost.reason) == (at(6), at(7), GapReason.RECONNECT)


async def test_offline_then_playlist_errors_resolve_again(tmp_path):
    resolver = FakeResolver([None, PLAYLIST, PLAYLIST])  # offline first, then live
    script = [playlist([0, 1]), 502, 502, 502, playlist([5, 6])]
    tracker = SegmentTracker.fresh(at(0))
    closed, _, _ = await record(tmp_path, script, resolver=resolver, tracker=tracker)

    assert resolver.calls == 3  # offline, live, again after 3 failed reloads
    assert [(c.start, c.end) for c in closed] == [(at(0), at(2)), (at(5), at(7))]
    assert closed[0].gap_before is None  # go-live == first segment
    gap = closed[1].gap_before
    assert (gap.start, gap.end, gap.reason) == (at(2), at(5), GapReason.RECONNECT)


async def test_fmp4_chunks_start_with_their_init_section(tmp_path):
    a, b = {0: "a"}, {0: "a", 3: "b"}  # the init section changes at segment 3
    script = [playlist([0, 1], maps=a), playlist([0, 1, 2, 3, 4], maps=b)]
    closed, spool, cdn = await record(tmp_path, script, chunk_seconds=60)

    assert cdn.init_requests == ["init-a", "init-b"]  # fetched once each, not per segment
    assert [(c.start, c.end, c.container) for c in closed] == [
        (at(0), at(3), "mp4"),
        (at(3), at(5), "mp4"),  # new init -> new chunk, but no gap
    ]
    assert closed[1].gap_before is None
    assert spool.media_path(closed[0]).read_bytes() == b"<init-a><seg0><seg1><seg2>"
    assert spool.media_path(closed[1]).read_bytes() == b"<init-b><seg3><seg4>"
    assert closed[0].bytes == len(b"<init-a><seg0><seg1><seg2>") and closed[0].segments == 3


async def test_ended_playlist_closes_the_chunk_and_resolves_again(tmp_path):
    resolver = FakeResolver()
    script = [playlist([0, 1]) + "#EXT-X-ENDLIST\n", playlist([5])]
    closed, _, _ = await record(tmp_path, script, resolver=resolver, chunk_seconds=60)

    assert resolver.calls == 2
    assert [(c.start, c.end) for c in closed] == [(at(0), at(2)), (at(5), at(6))]
    gap = closed[1].gap_before
    assert (gap.start, gap.end, gap.reason) == (at(2), at(5), GapReason.RECONNECT)
