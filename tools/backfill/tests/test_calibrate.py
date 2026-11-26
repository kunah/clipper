"""`calibrate`: synthetic audio (lavfi noise, AAC) cut two ways from ONE encode, like Twitch: 10 s
VOD segments and three "live chunks" at odd times. The live chunks sit at a known UTC (the live
PDT clock); the VOD playlist's PDT is 137 ms late. The audio match must find the truth.

The VOD comes as MPEG-TS segments or, like ohnepixel's, as fMP4 (`EXT-X-MAP` init section + `.mp4`
fragments whose `baseMediaDecodeTime` runs on from the VOD start), also with an AAC priming edit
list in the init section."""

import csv
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from sqlalchemy import text

from clipper_backfill.backfill import Backfiller, UsageError
from clipper_backfill.calibrate import _match, calibrate, decode_pcm, locate
from clipper_backfill.chat import StaticEmoteSets
from clipper_backfill.pipeline import MediaError
from clipper_backfill.settings import BackfillSettings
from clipper_core import ffmpeg as ffm
from clipper_core.partitions import ensure_chat_partitions
from clipper_core.storage import keys
from clipper_twitch.gql import Comment, FakeChatReplay, Fragment, VideoInfo
from clipper_twitch.hls import parse_vod_playlist
from clipper_twitch.vod import FakeVodSource

VIDEO, STREAM = "2000000003", "777000333"
CDN = f"https://cdn.test/8173ac3b86530f54c2d4_streamer_{STREAM}_1795000000"
AUDIO_URL = f"{CDN}/audio_only/index-dvr.m3u8"
TRUE_T0 = datetime(2026, 11, 18, 6, 27, 49, 500000, tzinfo=UTC)  # VOD offset 0, live clock
PDT_ERROR_S = 0.137
CHAT_LAG_S = 4.2  # the replay's createdAt - offset band starts here (after TRUE_T0)


def test_locate_finds_a_slice_of_noise():
    rng = np.random.default_rng(3)
    hay = rng.normal(size=20_000).astype(np.float32)
    index, score = locate(hay, hay[12_345:17_000])
    assert index == 12_345 and score == pytest.approx(1.0, abs=1e-6)
    index, score = locate(hay, rng.normal(size=3000).astype(np.float32))
    assert score < 0.2
    with pytest.raises(ValueError):
        locate(hay[:10], hay)


def test_locate_scores_silence_zero_and_never_above_one():
    rng = np.random.default_rng(4)
    noise = rng.normal(size=4000).astype(np.float32)
    hay = np.concatenate([np.zeros(6000, np.float32), noise, np.zeros(3000, np.float32)])
    index, score = locate(hay, noise[1000:3000])
    assert index == 7000 and 0.999 < score <= 1.0
    tiny = np.concatenate([np.full(5000, 1e-12, np.float32), noise])  # ~silent, not exactly 0
    index, score = locate(tiny, noise[:2000])
    assert index == 5000 and score <= 1.0
    assert locate(np.zeros(5000, np.float32), noise[:100]) == (0, 0.0)


def test_calibrate_temp_dirs_are_not_backfill_temp_dirs(tmp_path):
    from clipper_backfill import calibrate as cal_module
    from clipper_backfill.pipeline import TEMP_PREFIX, remove_stale_workdirs

    assert not cal_module.TEMP_PREFIX.startswith(TEMP_PREFIX)
    busy = tmp_path / f"{cal_module.TEMP_PREFIX}x"
    busy.mkdir()
    os.utime(busy, (1, 1))  # looks ancient
    assert remove_stale_workdirs(tmp_path, older_than_s=10) == [] and busy.exists()


async def test_cli_reports_a_value_error_cleanly(monkeypatch, capsys):
    from clipper_backfill import calibrate as cal_module
    from clipper_backfill.cli import _calibrate, parse_args
    from clipper_core.config import Settings

    async def no_t0(*args, **kwargs):
        raise ValueError("no T0 candidate: no PDT, too few chat comments and no created_at")

    monkeypatch.setattr(cal_module, "calibrate", no_t0)
    args = parse_args(["calibrate", VIDEO])
    assert await _calibrate(args, Settings(_env_file=None), backfiller=None) == 2
    err = capsys.readouterr().err
    assert err.startswith(f"[error] {VIDEO}: ValueError: no T0 candidate")


def ffmpeg(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, cwd=cwd)  # fmt: skip


def segment_list(path: Path) -> list[tuple[str, float, float]]:
    with path.open(newline="") as f:
        return [(name, float(a), float(b)) for name, a, b in csv.reader(f)]


@pytest.fixture(scope="module")
def master(tmp_path_factory) -> Path:
    """ONE AAC encode (130 s of pink noise, 48 kHz) as MPEG-TS: every cut below comes from it."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    path = tmp_path_factory.mktemp("calibrate") / "master.ts"
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=d=130:c=pink:r=48000:a=0.4:seed=42",
           "-c:a", "aac", "-b:a", "96k", "-f", "mpegts", str(path))  # fmt: skip
    return path


@pytest.fixture(scope="module")
def media(master):
    """(TS VOD segments, live chunks as ADTS), each as (bytes, offset from the VOD start, s)."""
    d = master.parent
    ffmpeg("-i", str(master), "-c", "copy", "-f", "segment", "-segment_time", "10",
           "-segment_list", str(d / "vod.csv"), "-segment_list_type", "csv",
           "-segment_format", "mpegts", str(d / "vod%03d.ts"))  # fmt: skip
    ffmpeg("-i", str(master), "-c", "copy", "-f", "segment", "-segment_times", "41.3,71.3,101.7",
           "-segment_list", str(d / "live.csv"), "-segment_list_type", "csv",
           "-segment_format", "mpegts", str(d / "live%03d.ts"))  # fmt: skip
    vod = segment_list(d / "vod.csv")
    origin = vod[0][1]
    segments = [((d / name).read_bytes(), a - origin, b - a) for name, a, b in vod]
    chunks = []
    for name, a, b in segment_list(d / "live.csv")[1:]:
        aac = d / f"{name}.aac"
        ffmpeg("-i", str(d / name), "-map", "0:a:0", "-c", "copy", "-f", "adts", str(aac))
        chunks.append((aac.read_bytes(), a - origin, b - a))
    return segments, chunks


@pytest.fixture(scope="module")
def fmp4(master):
    """The same encode as an fMP4 VOD (ffmpeg's HLS muxer, stream copy; the same 10 s cuts as the
    TS segments, `baseMediaDecodeTime` 0 at the VOD start): (init section, [(fragment, offset,
    duration)])."""
    d = master.parent / "fmp4"
    d.mkdir()
    # relative names in `d`: the HLS muxer writes the init section relative to the cwd
    ffmpeg("-i", str(master), "-c", "copy", "-bsf:a", "aac_adtstoasc", "-f", "hls",
           "-hls_segment_type", "fmp4", "-hls_time", "10", "-hls_list_size", "0",
           "-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename", "%03d.m4s",
           "index.m3u8", cwd=d)  # fmt: skip
    vod = parse_vod_playlist((d / "index.m3u8").read_text())
    assert vod.segments and all(s.init_uri == "init.mp4" for s in vod.segments)
    fragments = [((d / s.uri).read_bytes(), s.offset_s, s.duration) for s in vod.segments]
    return (d / "init.mp4").read_bytes(), fragments


def base_media_decode_time(fragment: bytes) -> int:
    """An fMP4 fragment's `tfdt`: its first sample's decode time (track timescale, 48 kHz)."""
    i = fragment.index(b"tfdt")
    size = 8 if fragment[i + 4] else 4  # version 1: 64 bit
    return int.from_bytes(fragment[i + 8 : i + 8 + size], "big")


def with_priming(init: bytes, samples: int = 1024) -> bytes:
    """`init` whose edit list starts the presentation `samples` in (AAC encoder priming), as many
    muxers write it (ffmpeg's HLS muxer writes an empty one: duration 0, media_time 0)."""
    i = init.index(b"elst")
    assert init[i + 4] == 0 and int.from_bytes(init[i + 8 : i + 12], "big") == 1  # v0, 1 entry
    return init[: i + 16] + samples.to_bytes(4, "big") + init[i + 20 :]  # media_time


def playlist(segments, muted=frozenset(), maps=None) -> str:
    """`maps` (fMP4): {segment index: init section name}, an `EXT-X-MAP` before that segment;
    the segments are then named `N.mp4`, as Twitch does."""
    ext = "mp4" if maps else "ts"
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:10", "#EXT-X-PLAYLIST-TYPE:EVENT"]
    for i, (_, offset, duration) in enumerate(segments):
        if maps and i in maps:
            lines.append(f'#EXT-X-MAP:URI="{maps[i]}"')
        pdt = TRUE_T0 + timedelta(seconds=PDT_ERROR_S + offset)
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{pdt.isoformat(timespec='milliseconds')}")
        lines += [f"#EXTINF:{duration:.6f},", f"{i}-muted.{ext}" if i in muted else f"{i}.{ext}"]
    return "\n".join([*lines, "#EXT-X-ENDLIST", ""])


class Store:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key]


def comments() -> list[Comment]:
    out = []
    for i in range(90):
        true_offset = i * 1.4 + 0.3
        out.append(
            Comment(
                id=f"k{i}",
                created_at=TRUE_T0 + timedelta(seconds=CHAT_LAG_S + true_offset),
                content_offset_s=int(true_offset),
                commenter_id="u1",
                commenter_login="user1",
                commenter_display_name=None,
                fragments=(Fragment("hi"),),
                badges=(),
                user_color=None,
                cursor=None,
            )
        )
    return out


def backfiller(
    sessions,
    segments,
    *,
    muted=frozenset(),
    inits=None,
    maps=None,
    calls=None,
    failing=frozenset(),
) -> Backfiller:
    """`inits` (fMP4): {name: init section, None = gone (404)}; `maps` defaults to the first one
    for the whole VOD. `calls` collects the requested file names; `failing` names answer 503."""
    inits = inits or {}
    if inits and maps is None:
        maps = {0: next(iter(inits))}

    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        if calls is not None:
            calls.append(name)
        if name in failing:
            return httpx.Response(503)
        if name == "index-dvr.m3u8":
            return httpx.Response(200, text=playlist(segments, muted, maps))
        if name in inits:
            init = inits[name]
            return httpx.Response(404) if init is None else httpx.Response(200, content=init)
        return httpx.Response(200, content=segments[int(name.split(".")[0].split("-")[0])][0])

    info = VideoInfo(
        id=VIDEO,
        title="t",
        created_at=TRUE_T0 + timedelta(seconds=3),
        published_at=None,
        length_s=130,
        status="RECORDED",
        broadcast_type="ARCHIVE",
        owner_id="4242",
        owner_login="streamer",
        owner_display_name="Streamer",
        game=None,
        seek_previews_url=f"{CDN}/storyboards/{VIDEO}-info.json",
    )
    return Backfiller(
        sessions=sessions,
        store=None,
        vod_source=FakeVodSource({VIDEO: {"audio_only": AUDIO_URL}}),
        gql=FakeChatReplay({VIDEO: comments()}, videos={VIDEO: info}, page_size=30),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        settings=BackfillSettings(_env_file=None, t0_chat_pages=3, retry_backoff_s=0),
        owner="calibrate",
        emotes=StaticEmoteSets(),
    )


async def live_recording(sessions, chunks, store: Store) -> int:
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
                    "INSERT INTO streams (channel_id, platform_stream_id, started_at, status) "
                    "VALUES (:c, :p, :t, 'ended') RETURNING id"
                ),
                {"c": cid, "p": STREAM, "t": TRUE_T0},
            )
        ).scalar_one()
        for seq, (data, offset, duration) in enumerate(chunks):
            key = keys.chunk_audio(sid, seq)
            store.objects[key] = data
            start = TRUE_T0 + timedelta(seconds=offset)
            await s.execute(
                text(
                    "INSERT INTO chunks (stream_id, seq, start_utc, end_utc, audio_key) "
                    "VALUES (:s, :q, :a, :b, :k)"
                ),
                {
                    "s": sid,
                    "q": seq,
                    "a": start,
                    "b": start + timedelta(seconds=duration),
                    "k": key,
                },
            )
        await ensure_chat_partitions(s, TRUE_T0.date(), TRUE_T0.date())  # an old day
        for c in comments()[::3]:  # live chat-ingest logged them: tmi-sent-ts = createdAt
            await s.execute(
                text(
                    "INSERT INTO chat_messages (sent_at, msg_id, channel_id, stream_id, "
                    "received_at, text) VALUES (:t, :m, :c, :s, :t, 'hi')"
                ),
                {"t": c.created_at, "m": c.id, "c": cid, "s": sid},
            )
    return sid


@pytest.mark.integration
async def test_calibrate_finds_the_vod_t0_on_the_live_clock(sessions, media):
    segments, chunks = media
    store = Store()
    sid = await live_recording(sessions, chunks, store)
    before = {t: await count(sessions, t) for t in ("vods", "streams", "chat_messages", "leases")}

    cal = await calibrate(backfiller(sessions, segments), store, VIDEO, windows=3, search_s=15)

    assert cal.stream_id == sid and len(cal.matches) == 2  # the 3rd chunk: past the VOD end
    assert all(m.score > 0.9 for m in cal.matches), cal.matches
    error_ms = abs((cal.t0_audio - TRUE_T0).total_seconds()) * 1000
    assert error_ms < 2, (cal.t0_audio, TRUE_T0)
    diff = {n: (t0 - cal.t0_audio).total_seconds() for n, t0 in cal.candidates.items()}
    assert diff["pdt"] == pytest.approx(PDT_ERROR_S, abs=0.003)
    assert diff["chat (truncated)"] == pytest.approx(CHAT_LAG_S, abs=0.3)
    assert diff["chat (rounded)"] == pytest.approx(CHAT_LAG_S + 0.5, abs=0.3)
    assert diff["gql_created_at"] == pytest.approx(3.0, abs=0.003)
    assert (cal.comments, len(cal.created_minus_sent)) == (88, 29)  # every 3rd one is live
    assert max(abs(x) for x in cal.created_minus_sent) < 1e-6
    assert min(cal.sent_minus_offset) >= CHAT_LAG_S - 0.01
    assert max(cal.sent_minus_offset) < CHAT_LAG_S + 1.0

    report = "\n".join(cal.lines())
    assert "T0_audio = " in report and "2 windows" in report
    assert "pdt" in report and "+0.137" in report
    assert "createdAt - tmi-sent-ts: min +0.000" in report
    assert "1 chunks not usable" in report
    after = {t: await count(sessions, t) for t in before}
    assert after == before  # read-only


@pytest.mark.integration
async def test_calibrate_skips_muted_windows_and_needs_a_live_recording(sessions, media):
    segments, chunks = media
    with pytest.raises(UsageError, match="not recorded live"):
        await calibrate(backfiller(sessions, segments), Store(), VIDEO)
    store = Store()
    await live_recording(sessions, chunks, store)
    bf = backfiller(sessions, segments, muted=frozenset({5}))  # [50, 60): inside both windows
    cal = await calibrate(bf, store, VIDEO, search_s=15)
    assert cal.matches == [] and cal.t0_audio is None
    assert "T0_audio: no usable window" in cal.lines()
    assert (
        "note: 3 chunks not usable (VOD edge, muted or init section change around them)"
        in cal.lines()
    )


# --------------------------------------------------------------------------- fMP4 VODs
# The live chunks at 41.3 s and 71.3 s with search_s=15 search VOD fragments 2-8 and 5-11:
# both windows start mid-VOD, at a fragment with a non-zero baseMediaDecodeTime.
WINDOW_STARTS = (2, 5)


def test_fmp4_windows_decode_from_their_first_fragment(master, fmp4, tmp_path):
    """No padding up to the first fragment's baseMediaDecodeTime, no priming dropped: decoded
    sample 0 of init + fragments[k:] is coded sample `tfdt(k)` of the encode, as for TS. And the
    `.aac` that the recorder (live fMP4 spool) and the media path store from such a file starts at
    the same sample with the same length, so both sides of the match share one origin."""
    init, fragments = fmp4
    truth = decode_pcm("ffmpeg", master, rate=48000)  # every coded sample, from 0
    for k in (0, *WINDOW_STARTS):
        start = base_media_decode_time(fragments[k][0])
        assert start == round(fragments[k][1] * 48000) and (start > 0) == (k > 0)
        for name, section in (("plain", init), ("priming", with_priming(init))):
            path = tmp_path / f"{name}_{k}.mp4"
            path.write_bytes(section + b"".join(f for f, _, _ in fragments[k : k + 3]))
            pcm = decode_pcm("ffmpeg", path, rate=48000)
            # the first AAC frames of a cut lack their predecessor (MDCT overlap): match later on
            index, score = locate(truth, pcm[4096 : 4096 + 3 * 48000])
            assert index - 4096 == start and score > 0.999, (name, index - 4096 - start)
            seconds = sum(d for _, _, d in fragments[k : k + 3])
            assert abs(len(pcm) - seconds * 48000) < 1, name  # nothing padded or dropped

            aac = path.with_suffix(".aac")
            assert ffm.extract_audio("ffmpeg", path, aac) is None
            stored = decode_pcm("ffmpeg", aac, rate=48000)
            assert len(stored) == len(pcm), name
            index, score = locate(pcm, stored[4096 : 4096 + 3 * 48000])
            assert index == 4096 and score > 0.999, (name, index - 4096)


@pytest.mark.integration
@pytest.mark.parametrize("priming", [False, True], ids=["plain", "priming-edit-list"])
async def test_calibrate_reads_fmp4_vods(sessions, media, fmp4, priming):
    _, chunks = media
    init, fragments = fmp4
    assert all(base_media_decode_time(fragments[k][0]) > 0 for k in WINDOW_STARTS)
    store = Store()
    await live_recording(sessions, chunks, store)
    calls: list[str] = []
    section = with_priming(init) if priming else init
    bf = backfiller(sessions, fragments, inits={"init-0.mp4": section}, calls=calls)

    cal = await calibrate(bf, store, VIDEO, windows=3, search_s=15)

    assert len(cal.matches) == 2 and all(m.score > 0.9 for m in cal.matches), cal.notes
    error_ms = abs((cal.t0_audio - TRUE_T0).total_seconds()) * 1000
    assert error_ms < 2, (cal.t0_audio, TRUE_T0)
    for m in cal.matches:  # each window on its own: where the chunk really starts in the VOD
        assert abs(m.vod_offset_s - chunks[m.seq][1]) < 0.002, m
    fetched = {int(n.split(".")[0]) for n in calls if n[0].isdigit()}
    assert fetched == set(range(WINDOW_STARTS[0], 12))  # fragments 2-8 and 5-11
    assert calls.count("init-0.mp4") == 1  # cached across the windows
    assert cal.notes == ["1 chunks not usable (VOD edge, muted or init section change around them)"]


@pytest.mark.integration
async def test_calibrate_skips_windows_whose_init_section_is_gone(sessions, media, fmp4):
    _, chunks = media
    _, fragments = fmp4
    store = Store()
    await live_recording(sessions, chunks, store)
    calls: list[str] = []
    bf = backfiller(sessions, fragments, inits={"init-0.mp4": None}, calls=calls)

    cal = await calibrate(bf, store, VIDEO, windows=3, search_s=15)

    assert cal.matches == [] and cal.t0_audio is None
    assert cal.notes[1:] == [
        f"chunk {seq}: MediaError: the VOD's fMP4 init section is gone" for seq in (0, 1)
    ]
    assert not any(n[0].isdigit() for n in calls)  # no fragment fetched without its init
    assert calls.count("init-0.mp4") == 2  # the 404 asked twice, then remembered for window 2
    assert "T0_audio: no usable window" in cal.lines()
    assert len(cal.created_minus_sent) == 29  # the chat part still ran


@pytest.mark.integration
async def test_calibrate_skips_a_window_whose_download_fails(sessions, media):
    segments, chunks = media
    store = Store()
    await live_recording(sessions, chunks, store)
    calls: list[str] = []
    # segment 3 (30-40 s) answers 503 for good: only in chunk 0's window (segments 2-8)
    bf = backfiller(sessions, segments, calls=calls, failing=frozenset({"3.ts"}))

    cal = await calibrate(bf, store, VIDEO, windows=3, search_s=15)

    assert calls.count("3.ts") == bf.settings.segment_attempts
    note = cal.notes[1]
    assert note.startswith("chunk 0: DownloadError: segment ") and "HTTP 503" in note
    assert [m.seq for m in cal.matches] == [1] and cal.matches[0].score > 0.9
    assert abs((cal.t0_audio - TRUE_T0).total_seconds()) < 0.002
    assert len(cal.created_minus_sent) == 29 and cal.sent_minus_offset  # the chat part reports


@pytest.mark.integration
async def test_calibrate_leaves_out_windows_across_an_init_change(sessions, media, fmp4):
    _, chunks = media
    init, fragments = fmp4
    store = Store()
    await live_recording(sessions, chunks, store)
    # a new init section from fragment 10 (100.0 s): inside chunk 1's window [56.3, 116.7]
    inits = {"init-0.mp4": init, "init-1.mp4": init}
    bf = backfiller(sessions, fragments, inits=inits, maps={0: "init-0.mp4", 10: "init-1.mp4"})

    cal = await calibrate(bf, store, VIDEO, windows=3, search_s=15)

    assert [m.seq for m in cal.matches] == [0] and cal.matches[0].score > 0.9
    assert abs((cal.t0_audio - TRUE_T0).total_seconds()) < 0.002
    assert cal.notes == ["2 chunks not usable (VOD edge, muted or init section change around them)"]


async def test_match_refuses_a_window_with_two_init_sections(tmp_path):
    m3u8 = playlist([(b"", i * 10.0, 10.0) for i in range(4)], maps={0: "a.mp4", 2: "b.mp4"})
    segs = parse_vod_playlist(m3u8, AUDIO_URL).segments
    bf = SimpleNamespace(downloader=None, settings=None)  # nothing is downloaded
    with pytest.raises(MediaError, match="init section changes inside its window"):
        await _match(bf, Store(), tmp_path, SimpleNamespace(seq=7), segs)


async def count(sessions, table: str) -> int:
    async with sessions() as s:
        return (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
