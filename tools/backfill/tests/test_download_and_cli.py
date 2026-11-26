"""Segment downloads (retries, lost segments, concurrency) and CLI argument parsing."""

import argparse
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from clipper_backfill.backfill import UsageError, _pick_rendition, parse_video_ref
from clipper_backfill.cli import options, parse_args, parse_offset
from clipper_backfill.download import Downloader, DownloadError
from clipper_backfill.pipeline import MediaRunner
from clipper_backfill.planner import plan_units
from clipper_twitch.hls import parse_vod_playlist

BASE = "https://cdn.test/abc_streamer_777_1795000000/audio_only/"


def segments(n):
    text = (
        "#EXTM3U\n" + "".join(f"#EXTINF:10.000,\n{i}.ts\n" for i in range(n)) + "#EXT-X-ENDLIST\n"
    )
    return parse_vod_playlist(text, BASE + "index-dvr.m3u8").segments


class Cdn:
    def __init__(self, script=None, delay=0.0):
        self.script = dict(script or {})  # path -> list of statuses to answer first
        self.calls: list[str] = []
        self.delay = delay
        self.in_flight = self.max_in_flight = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(name)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            statuses = self.script.get(name)
            if statuses:
                status = statuses.pop(0)
                if status == "timeout":
                    raise httpx.ReadTimeout("slow", request=request)
                headers = {"Retry-After": "0"} if status == 429 else {}
                return httpx.Response(status, headers=headers)
            return httpx.Response(200, content=name.encode())
        finally:
            self.in_flight -= 1


def downloader(cdn, **kwargs):
    http = httpx.AsyncClient(transport=httpx.MockTransport(cdn.handler))
    waits: list[float] = []

    async def sleep(s):
        waits.append(s)

    return Downloader(http, sleep=sleep, **kwargs), waits


async def test_retries_transient_errors_then_succeeds():
    cdn = Cdn({"0.ts": [500, "timeout", 429]})
    dl, waits = downloader(cdn, attempts=6, backoff_s=1.0)
    got = await dl.segments(segments(2))
    assert got == {0: b"0.ts", 1: b"1.ts"}
    assert cdn.calls.count("0.ts") == 4
    assert waits == [1.0, 2.0, 0.0]  # backoff doubles; Retry-After wins
    assert dl.bytes == 8 and dl.requests == 5


async def test_404_twice_is_a_lost_segment_and_the_rest_still_arrives():
    cdn = Cdn({"1.ts": [404, 404]})
    dl, _ = downloader(cdn)
    assert await dl.segments(segments(3)) == {0: b"0.ts", 1: None, 2: b"2.ts"}
    assert cdn.calls.count("1.ts") == 2


async def test_404_once_is_a_hiccup():
    cdn = Cdn({"1.ts": [404]})
    dl, _ = downloader(cdn)
    assert (await dl.segments(segments(2)))[1] == b"1.ts"


async def test_forbidden_or_exhausted_retries_raise():
    dl, _ = downloader(Cdn({"0.ts": [403]}))
    with pytest.raises(DownloadError, match="HTTP 403 after 1 attempts"):
        await dl.segments(segments(1))
    dl, _ = downloader(Cdn({"0.ts": [503] * 3}), attempts=3)
    with pytest.raises(DownloadError, match="HTTP 503 after 3 attempts"):
        await dl.segments(segments(2))
    dl, _ = downloader(Cdn({"index-dvr.m3u8": [404, 404]}))
    with pytest.raises(DownloadError, match="playlist not found"):
        await dl.text(BASE + "index-dvr.m3u8")


async def test_at_most_four_requests_at_a_time():
    cdn = Cdn(delay=0.01)
    dl, _ = downloader(cdn, concurrency=4)
    await dl.segments(segments(12))
    assert cdn.max_in_flight == 4
    with pytest.raises(ValueError):
        downloader(cdn, concurrency=5)


async def test_init_section_is_cached():
    cdn = Cdn()
    dl, _ = downloader(cdn)
    assert await dl.init_section(BASE + "init-0.mp4") == b"init-0.mp4"
    assert await dl.init_section(BASE + "init-0.mp4") == b"init-0.mp4"
    assert cdn.calls == ["init-0.mp4"]


async def test_a_lost_init_section_is_cached_too():
    cdn = Cdn(script={"init-1.mp4": [404, 404]})
    dl, waits = downloader(cdn)
    assert await dl.init_section(BASE + "init-1.mp4") is None  # a 404 is asked twice
    assert await dl.init_section(BASE + "init-1.mp4") is None  # then remembered: no backoff
    assert cdn.calls == ["init-1.mp4", "init-1.mp4"] and len(waits) == 1
    assert await dl.init_section(BASE + "init-2.mp4") == b"init-2.mp4"  # another URI: asked


async def test_a_lost_init_section_marks_every_chunk_on_it_lost(tmp_path):
    """The media path with the cached loss: each unit under the init is lost (-> gaps), and
    neither the init nor any fragment is asked for again."""
    text = '#EXTM3U\n#EXT-X-MAP:URI="init-0.mp4"\n'
    text += "".join(f"#EXTINF:10.000,\n{i}.mp4\n" for i in range(12)) + "#EXT-X-ENDLIST\n"
    segs = parse_vod_playlist(text, BASE + "index-dvr.m3u8").segments
    cdn = Cdn(script={"init-0.mp4": [404, 404]})
    dl, _ = downloader(cdn)
    runner = MediaRunner(None, None, dl, None, workdir=tmp_path)  # _build only downloads here
    units = plan_units(segs, chunk_s=60.0).units
    assert len(units) == 2
    for unit in units:
        files = await runner._build(SimpleNamespace(stream_id=1), unit)
        assert files.lost == {s.index for s in unit.segments} and files.paths == []
    assert cdn.calls == ["init-0.mp4", "init-0.mp4"]  # the 404 asked twice, then remembered


@pytest.mark.parametrize(
    ("ref", "video_id"),
    [
        ("2932113307", "2932113307"),
        ("https://www.twitch.tv/videos/2932113307?t=1h2m", "2932113307"),
        ("twitch.tv/videos/2932113307", "2932113307"),
        ("https://www.twitch.tv/xqc/video/2932113307", "2932113307"),
    ],
)
def test_video_refs(ref, video_id):
    assert parse_video_ref(ref) == video_id


def test_bad_video_ref():
    with pytest.raises(UsageError):
        parse_video_ref("https://www.twitch.tv/xqc")


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("90", 90.0), ("1:30", 90.0), ("1:02:03", 3723.0), ("1h2m3s", 3723.0), ("45.5", 45.5)],
)
def test_offsets(text, seconds):
    assert parse_offset(text) == seconds


@pytest.mark.parametrize("text", ["", "abc", "1:2:3:4", "-5", "1d"])
def test_bad_offsets(text):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_offset(text)


def test_vod_arguments():
    args = parse_args(["vod", "1", "2", "--video", "--max-minutes", "5", "--start-offset", "1:00"])
    opts = options(args)
    assert args.refs == ["1", "2"]
    assert (opts.video, opts.max_minutes, opts.start_offset_s) == ("best", 5.0, 60.0)
    assert opts.media and opts.transcribe and opts.chat and not opts.dry_run
    opts = options(parse_args(["vod", "1", "--dry-run", "--no-transcribe", "--video", "720p60"]))
    assert (opts.dry_run, opts.transcribe, opts.video) == (True, False, "720p60")
    assert options(parse_args(["vod", "1", "--no-media", "--no-chat"])).media is False
    with pytest.raises(SystemExit):
        parse_args(["vod", "1", "--max-minutes", "0"])


def test_chat_and_clips_arguments():
    opts = options(parse_args(["vod", "1", "--no-media", "--chat-max-minutes", "5"]))
    assert (opts.media, opts.chat, opts.clips, opts.chat_max_minutes) == (False, True, True, 5.0)
    opts = options(parse_args(["vod", "1", "--max-minutes", "3", "--no-clips", "--no-chat"]))
    assert (opts.max_minutes, opts.chat_max_minutes, opts.chat, opts.clips) == (
        3.0,
        None,
        False,
        False,
    )
    args = parse_args(["clips", "xqc", "2932113307", "--top", "20"])
    assert (args.refs, args.top) == (["xqc", "2932113307"], 20)
    args = parse_args(["calibrate", "https://www.twitch.tv/videos/2932113307"])
    assert (args.ref, args.windows, args.search) == (
        "https://www.twitch.tv/videos/2932113307", 3, 20.0
    )  # fmt: skip


def test_channel_and_status_arguments():
    args = parse_args(["channel", "xqc", "--since", "2026-11-01", "--limit", "2"])
    assert (args.login, args.limit, args.since.isoformat()) == (
        "xqc", 2, "2026-11-01T00:00:00+00:00"
    )  # fmt: skip
    args = parse_args(["channel", "xqc"])
    assert (args.limit, args.since) == (10, None)
    assert parse_args(["status"]).video_ids == []


@pytest.mark.parametrize(
    "extra",
    [["--dry-run"], ["--max-minutes", "5"], ["--no-media"], ["--video"], ["--no-chat"]],
)
def test_channel_lists_only_and_takes_no_run_options(extra, capsys):
    with pytest.raises(SystemExit) as exc:
        parse_args(["channel", "xqc", *extra])
    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize("limit", ["0", "-1", "2.5", "x"])
def test_channel_limit_is_a_positive_whole_number(limit, capsys):
    with pytest.raises(SystemExit) as exc:
        parse_args(["channel", "xqc", "--limit", limit])
    assert exc.value.code == 2
    assert "--limit" in capsys.readouterr().err


def test_rendition_choice():
    renditions = {"audio_only": "a", "720p60": "v7", "1080p60": "v10", "best": "v10"}
    assert _pick_rendition(renditions, None) == "audio_only"
    assert _pick_rendition(renditions, "best") == "1080p60"
    assert _pick_rendition(renditions, "720p60") == "720p60"
    with pytest.raises(UsageError, match="has"):
        _pick_rendition(renditions, "480p30")
    with pytest.raises(UsageError, match="--video"):
        _pick_rendition({"best": "v"}, None)
