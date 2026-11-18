"""Finisher against Postgres + real ffmpeg: spool chunk -> storage + chunk/gap/job rows."""

import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from clipper_core import media
from clipper_core.contracts import GapReason, Priority, RecordingMode
from clipper_core.storage import keys
from clipper_core.timeline import Interval
from clipper_recorder.finisher import Finisher
from clipper_recorder.spool import ChunkState, GapState, Spool

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]

T0 = datetime(2026, 11, 18, 20, 0, tzinfo=UTC)


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str | None]] = {}

    def put_file(self, key, path, content_type=None) -> None:
        self.objects[key] = (Path(path).read_bytes(), content_type)


def make_media(path: Path, *, video=True, audio=True, seconds=2, container="ts") -> None:
    """A small MPEG-TS, or fragmented MP4 (init + fragments), like Twitch serves.

    Built-in encoders only, so any ffmpeg build works.
    """
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if video:
        args += ["-f", "lavfi", "-i", f"testsrc=size=160x120:rate=30:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}"]
    if video:
        args += ["-c:v", "mpeg2video" if container == "ts" else "mpeg4"]
    if audio:
        args += ["-c:a", "aac"]
    if container == "ts":
        args += ["-f", "mpegts"]
    else:
        args += ["-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-f", "mp4"]
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([*args, str(path)], check=True)


async def setup_stream(sessions) -> int:
    async with sessions.begin() as s:
        cid = (
            await s.execute(text("INSERT INTO channels (login) VALUES ('dallas') RETURNING id"))
        ).scalar_one()
        return (
            await s.execute(
                text(
                    "INSERT INTO streams (channel_id, platform_stream_id, started_at) "
                    "VALUES (:c, 'b1', :t) RETURNING id"
                ),
                {"c": cid, "t": T0},
            )
        ).scalar_one()


def spooled_chunk(
    spool: Spool, stream_id: int, seq: int, mode=RecordingMode.FULL, container="ts", **kwargs
):
    state = ChunkState(
        stream_id=stream_id,
        channel="dallas",
        seq=seq,
        mode=mode,
        container=container,
        start=T0 + timedelta(seconds=60 * seq),
        end=T0 + timedelta(seconds=60 * seq + 2),
        closed=True,
        segments=1,
    )
    make_media(spool.media_path(state), container=container, **kwargs)
    state = state.model_copy(update={"bytes": spool.media_path(state).stat().st_size})
    spool.save(state)
    return state


async def rows(sessions, sql, **params):
    async with sessions() as s:
        return (await s.execute(text(sql), params)).all()


async def test_full_chunk_is_uploaded_and_committed_with_gap_and_job(sessions, tmp_path):
    stream_id = await setup_stream(sessions)
    spool, store = Spool(tmp_path), MemoryStore()
    state = spooled_chunk(spool, stream_id, 3)
    state = state.model_copy(
        update={"gap_before": GapState(start=T0, end=state.start, reason=GapReason.AD)}
    )
    # the detector asked for this window before the chunk existed
    async with sessions.begin() as s:
        await media.request_media(
            s,
            stream_id=stream_id,
            window=Interval(state.start, state.end),
            priority=Priority.TRIGGER,
            requested_by="test",
        )
    finisher = Finisher(sessions, store, spool)

    chunk_id = await finisher.finish(state)

    video, audio = keys.chunk_video(stream_id, 3), keys.chunk_audio(stream_id, 3)
    assert store.objects[video][1] == "video/mp2t"
    assert store.objects[audio][1] == "audio/aac"
    assert store.objects[audio][0][:2] == b"\xff\xf1"  # ADTS sync word
    (chunk,) = await rows(sessions, "SELECT * FROM chunks")
    assert (chunk.id, chunk.seq, chunk.start_utc, chunk.end_utc) == (
        chunk_id,
        3,
        state.start,
        state.end,
    )
    assert (chunk.video_key, chunk.audio_key, chunk.source) == (video, audio, "live")
    assert chunk.video_bytes == state.bytes and chunk.audio_bytes > 0
    (gap,) = await rows(sessions, "SELECT * FROM stream_gaps")
    assert (gap.start_utc, gap.end_utc, gap.reason) == (T0, state.start, "ad")
    (job,) = await rows(sessions, "SELECT * FROM jobs")
    assert (job.kind, job.ref_id, job.priority) == ("transcribe", chunk_id, Priority.TRIGGER)
    assert job.payload == {"chunk_id": chunk_id}
    assert spool.pending() == []

    # A retry after the commit (crash before the spool was cleaned) changes nothing.
    state = spooled_chunk(spool, stream_id, 3)
    state = state.model_copy(update={"gap_before": GapState(start=T0, end=T0, reason="ad")})
    assert await finisher.finish(state) == chunk_id
    assert len(await rows(sessions, "SELECT * FROM stream_gaps")) == 1
    assert len(await rows(sessions, "SELECT * FROM jobs")) == 1


async def test_audio_only_chunk_uploads_audio_only(sessions, tmp_path):
    stream_id = await setup_stream(sessions)
    spool, store = Spool(tmp_path), MemoryStore()
    state = spooled_chunk(spool, stream_id, 0, mode=RecordingMode.AUDIO_ONLY, video=False)

    await Finisher(sessions, store, spool).finish(state)

    assert list(store.objects) == [keys.chunk_audio(stream_id, 0)]
    (chunk,) = await rows(sessions, "SELECT video_key, audio_key FROM chunks")
    assert chunk.video_key is None and chunk.audio_key
    (job,) = await rows(sessions, "SELECT priority FROM jobs")
    assert job.priority == Priority.LIVE


async def test_chunk_without_audio_keeps_video_and_skips_transcription(sessions, tmp_path):
    stream_id = await setup_stream(sessions)
    spool, store = Spool(tmp_path), MemoryStore()
    state = spooled_chunk(spool, stream_id, 0, audio=False)

    await Finisher(sessions, store, spool).finish(state)

    assert list(store.objects) == [keys.chunk_video(stream_id, 0)]
    assert await rows(sessions, "SELECT * FROM jobs") == []


async def test_crashed_chunk_is_cut_back_to_its_last_complete_segment(sessions, tmp_path):
    stream_id = await setup_stream(sessions)
    spool, store = Spool(tmp_path), MemoryStore()
    state = spooled_chunk(spool, stream_id, 0).model_copy(update={"closed": False})
    spool.save(state)
    good = spool.media_path(state).read_bytes()
    with spool.media_path(state).open("ab") as f:
        f.write(b"half a segment")  # the process died mid-append

    recovered = spool.recover(state)
    assert recovered.closed
    assert spool.media_path(state).read_bytes() == good

    finisher = Finisher(sessions, store, spool)
    finisher.start()
    finisher.submit(recovered)
    assert await finisher.drain(timeout_s=30)
    assert store.objects[keys.chunk_video(stream_id, 0)][0] == good
    assert spool.pending() == []


async def test_fmp4_chunk_is_stored_as_ts(sessions, tmp_path):
    stream_id = await setup_stream(sessions)
    spool, store = Spool(tmp_path), MemoryStore()
    state = spooled_chunk(spool, stream_id, 0, container="mp4")
    assert spool.media_path(state).suffix == ".mp4"

    await Finisher(sessions, store, spool).finish(state)

    video, ctype = store.objects[keys.chunk_video(stream_id, 0)]
    assert ctype == "video/mp2t"
    assert video[0] == 0x47 and video[188] == 0x47  # MPEG-TS packets
    assert store.objects[keys.chunk_audio(stream_id, 0)][0][:2] == b"\xff\xf1"  # ADTS
    (job,) = await rows(sessions, "SELECT kind FROM jobs")
    assert job.kind == "transcribe"
    assert not any(tmp_path.rglob("*.*"))  # .mp4, remuxed .ts, .aac and state all gone


def test_empty_leftover_is_dropped(tmp_path):
    spool = Spool(tmp_path)
    state = ChunkState(stream_id=1, channel="x", seq=0, mode=RecordingMode.FULL, start=T0, end=T0)
    spool.save(state)
    assert spool.recover(state) is None
    assert spool.pending() == []
