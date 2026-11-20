"""`calibrate`: synthetic audio (lavfi noise, AAC) cut two ways from ONE encode, like Twitch: 10 s
VOD segments and three "live chunks" at odd times. The live chunks sit at a known UTC (the live
PDT clock); the VOD playlist's PDT is 137 ms late. The audio match must find the truth."""

import csv
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pytest
from sqlalchemy import text

from clipper_backfill.backfill import Backfiller, UsageError
from clipper_backfill.calibrate import calibrate, locate
from clipper_backfill.chat import StaticEmoteSets
from clipper_backfill.settings import BackfillSettings
from clipper_core.partitions import ensure_chat_partitions
from clipper_core.storage import keys
from clipper_twitch.gql import Comment, FakeChatReplay, Fragment, VideoInfo
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


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True)  # fmt: skip


def segment_list(path: Path) -> list[tuple[str, float, float]]:
    with path.open(newline="") as f:
        return [(name, float(a), float(b)) for name, a, b in csv.reader(f)]


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("calibrate")
    master = d / "master.ts"
    ffmpeg("-f", "lavfi", "-i", "anoisesrc=d=130:c=pink:r=48000:a=0.4:seed=42",
           "-c:a", "aac", "-b:a", "96k", "-f", "mpegts", str(master))  # fmt: skip
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


def playlist(segments, muted=frozenset()) -> str:
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:10", "#EXT-X-PLAYLIST-TYPE:EVENT"]
    for i, (_, offset, duration) in enumerate(segments):
        pdt = TRUE_T0 + timedelta(seconds=PDT_ERROR_S + offset)
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{pdt.isoformat(timespec='milliseconds')}")
        lines += [f"#EXTINF:{duration:.6f},", f"{i}-muted.ts" if i in muted else f"{i}.ts"]
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


def backfiller(sessions, segments, *, muted=frozenset()) -> Backfiller:
    def handler(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        if name == "index-dvr.m3u8":
            return httpx.Response(200, text=playlist(segments, muted))
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
        settings=BackfillSettings(_env_file=None, t0_chat_pages=3),
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
    assert "note: 3 chunks not usable (VOD edge or muted around them)" in cal.lines()


async def count(sessions, table: str) -> int:
    async with sessions() as s:
        return (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
