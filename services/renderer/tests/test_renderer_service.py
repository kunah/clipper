"""The `render` job on Postgres (DESIGN §20 build item 7b): pin + render + upload, the `renders` row
and the `review` job in one transaction, audio_only -> unrenderable without ffmpeg, expired video,
a retry resuming the row, the last attempt -> failed + notify(render_failed), re-rendering from the
pinned copies after the chunk video is gone, the heartbeat, and the real S3 CopyObject path on
SeaweedFS."""

import json
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from clipper_core import jobs
from clipper_core.config import Settings
from clipper_core.contracts import (
    JobKind,
    RenderManifest,
    RenderPayload,
    Word,
    WordsFile,
    dedupe_key,
)
from clipper_core.jobs import JobRunner
from clipper_core.storage import ObjectStore, keys
from clipper_renderer.handler import RenderHandler
from clipper_renderer.media import Tools
from clipper_renderer.render import render_files
from clipper_renderer.service import RendererService
from clipper_renderer.settings import RendererSettings
from clipper_renderer.synthetic import make_chunk

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)


def at(s: float) -> datetime:
    return T0 + timedelta(seconds=s)


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.copies: list[tuple[str, str]] = []

    def exists(self, key: str) -> bool:
        return key in self.objects

    def copy(self, src_key: str, dst_key: str) -> None:
        self.objects[dst_key] = self.objects[src_key]  # KeyError: like NoSuchKey
        self.copies.append((src_key, dst_key))

    def download_file(self, key: str, path) -> None:
        Path(path).write_bytes(self.objects[key])

    def put_file(self, key: str, path, content_type=None) -> None:
        self.objects[key] = Path(path).read_bytes()

    def put_bytes(self, key: str, data: bytes, content_type=None) -> None:
        self.objects[key] = data

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


@pytest.fixture(scope="module")
def tools() -> Tools:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not on PATH")
    return Tools()


@pytest.fixture(scope="module")
def chunk_files(tools, tmp_path_factory):
    d = tmp_path_factory.mktemp("chunks")
    spans = [(1, 0.0, 12.0), (2, 12.2, 11.8), (3, 24.0, 12.0)]  # a 0.2 s jitter hole at 12
    return {
        seq: make_chunk(tools, d / f"{seq:06d}.ts", t0=T0, start_s=s, duration_s=dur)
        for seq, s, dur in spans
    }


class NoTools(Tools):
    """ffmpeg must not run."""

    def run(self, *a, **kw):
        raise AssertionError("ffmpeg ran")

    def probe(self, *a, **kw):
        raise AssertionError("ffprobe ran")


class World:
    def __init__(self, sessions, settings, store, tmp_path: Path) -> None:
        self.sessions = sessions
        self.db = settings
        self.store = store
        self.tmp = tmp_path
        self.prefix = f"t{uuid.uuid4().hex[:8]}"

    async def setup(self, chunk_files, *, mode="full", video=True, words=True):
        async with self.sessions.begin() as s:
            self.channel_id = (
                await s.execute(
                    text(
                        "INSERT INTO channels (login, recording_mode) VALUES ('synth', :m)"
                        " RETURNING id"
                    ),
                    {"m": mode},
                )
            ).scalar_one()
            self.stream_id = (
                await s.execute(
                    text(
                        "INSERT INTO streams (channel_id, platform_stream_id, category,"
                        " started_at) VALUES (:c, :p, 'Just Chatting', :t) RETURNING id"
                    ),
                    {"c": self.channel_id, "p": self.prefix, "t": at(-60)},
                )
            ).scalar_one()
            self.chunks = {}
            for seq, c in chunk_files.items():
                vkey = f"{self.prefix}/" + keys.chunk_video(self.stream_id, seq) if video else None
                akey = f"{self.prefix}/" + keys.chunk_audio(self.stream_id, seq)
                if vkey:
                    self.store.put_file(vkey, c.path)
                cid = (
                    await s.execute(
                        text(
                            "INSERT INTO chunks (stream_id, seq, start_utc, end_utc, video_key,"
                            " audio_key) VALUES (:s, :q, :a, :b, :v, :k) RETURNING id"
                        ),
                        {"s": self.stream_id, "q": seq, "a": c.start_utc, "b": c.end_utc,
                         "v": vkey, "k": akey},
                    )
                ).scalar_one()  # fmt: skip
                self.chunks[seq] = (cid, vkey, c)
            if words:  # "watch this" at 9.5 s, as the transcriber stores it
                cid, _, c = self.chunks[1]
                wkey = f"{self.prefix}/" + keys.chunk_words(self.stream_id, 1)
                t0 = c.start_utc
                wf = WordsFile(
                    chunk_id=cid,
                    stream_id=self.stream_id,
                    seq=1,
                    model="fake",
                    t0=t0,
                    words=[
                        Word(text="watch", start=(at(9.5) - t0).total_seconds(),
                             end=(at(9.8) - t0).total_seconds()),
                        Word(text="this!", start=(at(9.9) - t0).total_seconds(),
                             end=(at(10.3) - t0).total_seconds()),
                    ],
                )  # fmt: skip
                self.store.put_bytes(wkey, wf.model_dump_json().encode())
                await s.execute(
                    text(
                        "INSERT INTO chunk_transcripts (chunk_id, model, words_key,"
                        " segment_count, owned_from, owned_to) VALUES (:c, 'fake', :k, 1, :a, :b)"
                    ),
                    {"c": cid, "k": wkey, "a": c.start_utc, "b": c.end_utc},
                )
        return self

    async def plan(self, segments=((8.0, 16.0),), **kw) -> int:
        async with self.sessions.begin() as s:
            cand = (
                await s.execute(
                    text(
                        "INSERT INTO candidates (stream_id, channel_id, source, status, trigger,"
                        " peak_utc, chat_delay_s, delay_source, moment_from, moment_to,"
                        " window_from, window_to, score, detector_version) VALUES (:s, :c,"
                        " 'live', 'emitted', 'chat', :p, 8, 'default', :p, :p, :a, :b, 5, 'v')"
                        " RETURNING id"
                    ),
                    {"s": self.stream_id, "c": self.channel_id, "p": at(10), "a": at(0),
                     "b": at(30)},
                )
            ).scalar_one()  # fmt: skip
            self.clip_id = (
                await s.execute(
                    text(
                        "INSERT INTO clips (candidate_id, stream_id, channel_id, status,"
                        " anchor_utc, context_from, context_to, priority, waiting_since,"
                        " plan_version) VALUES (:cand, :s, :c, 'planned', :p, :a, :b, 80, now(),"
                        " 1) RETURNING id"
                    ),
                    {"cand": cand, "s": self.stream_id, "c": self.channel_id, "p": at(10),
                     "a": at(0), "b": at(30)},
                )
            ).scalar_one()  # fmt: skip
            plan = {
                "clip_id": self.clip_id,
                "stream_id": self.stream_id,
                "anchor_utc": at(10).isoformat(),
                "segments": [
                    {"start_utc": at(a).isoformat(), "end_utc": at(b).isoformat()}
                    for a, b in segments
                ],
                "title": "a synthetic clip",
                "confidence": 0.9,
                "model": "fake",
                "prompt_version": "v1",
            } | kw
            plan_id = (
                await s.execute(
                    text(
                        "INSERT INTO clip_plans (clip_id, version, round, verdict, plan, model,"
                        " prompt_version) VALUES (:c, 1, 0, 'clip', CAST(:plan AS jsonb), 'fake',"
                        " 'v1') RETURNING id"
                    ),
                    {"c": self.clip_id, "plan": json.dumps(plan)},
                )
            ).scalar_one()
            self.job_id = await jobs.enqueue(
                s,
                JobKind.RENDER,
                payload=RenderPayload(plan_id=plan_id, profile="test_270x480").model_dump(),
                priority=100,
                ref_id=plan_id,
                dedupe_key=dedupe_key(JobKind.RENDER, plan_id),
                max_attempts=3,
            )
        self.plan_id = plan_id
        return plan_id

    def handler(self, tools, renderer=render_files, **kw) -> RenderHandler:
        rs = RendererSettings(_env_file=None, temp_dir=self.tmp, **kw)
        return RenderHandler(
            self.sessions,
            self.store,
            rs,
            tools=tools,
            encoder="libx264",
            nvenc_fallback=False,
            renderer=renderer,
        )

    async def run(self, handler) -> bool:
        runner = JobRunner(
            self.sessions, self.db.database_dsn, "renderer-test", {"render": handler}
        )
        return await runner.run_once()

    async def rows(self, sql: str, **params):
        async with self.sessions() as s:
            return (await s.execute(text(sql), params)).all()

    async def render_row(self):
        (row,) = await self.rows("SELECT * FROM renders")
        return row

    async def job(self, kind: str):
        return await self.rows("SELECT * FROM jobs WHERE kind = :k", k=kind)


@pytest.fixture
async def world(sessions, settings, tmp_path):
    return World(sessions, settings, MemoryStore(), tmp_path)


async def test_a_render_job_pins_renders_uploads_and_queues_review(world, tools, chunk_files):
    await world.setup(chunk_files)
    await world.plan()
    assert await world.run(world.handler(tools))
    row = await world.render_row()
    assert (row.status, row.job_id, row.clip_id, row.plan_id) == (
        "done",
        world.job_id,
        world.clip_id,
        world.plan_id,
    )
    pinned = [keys.clip_pinned_video(world.clip_id, seq) for seq in (1, 2, 3)]
    assert row.pinned_keys == pinned and all(world.store.exists(k) for k in pinned)
    assert [d for _, d in world.store.copies] == pinned  # server-side copies, all three (margin)
    for name, col in (("clip.mp4", "video_key"), ("preview.mp4", "preview_key"),
                      ("thumb.jpg", "thumb_key"), ("captions.ass", "captions_key"),
                      ("render.json", "manifest_key")):  # fmt: skip
        assert getattr(row, col) == keys.render_file(world.clip_id, row.id, name)
        assert world.store.exists(getattr(row, col))
    manifest = RenderManifest.model_validate_json(world.store.get_bytes(row.manifest_key))
    assert manifest.video_sha256 == row.video_sha256 and manifest.graph_hash == row.graph_hash
    assert [s.seq for s in manifest.sources] == [1, 2]
    assert (row.layout, row.encoder, row.width, row.height, row.fps) == (
        "blurred_fit",
        "libx264",
        270,
        480,
        30.0,
    )
    assert row.duration_s == pytest.approx(8.0, abs=0.04)
    assert abs(row.loudness_lufs + 14) <= 1 and row.true_peak_db <= -1
    assert row.bytes > 0 and row.preview_bytes > 0 and row.render_s > 0
    assert row.renderer_version and row.ffmpeg_version.startswith("ffmpeg version")
    assert "THIS!" in world.store.get_bytes(row.captions_key).decode()  # the words, captioned
    (review,) = await world.job("review")
    assert (review.ref_id, review.dedupe_key, review.priority, review.payload) == (
        row.id,
        f"review:{row.id}",
        100,
        {"render_id": row.id},
    )
    (render_job,) = await world.job("render")
    assert render_job.status == "done"


async def test_the_row_and_the_review_job_commit_together(world, tools, chunk_files, monkeypatch):
    from clipper_renderer import writes

    await world.setup(chunk_files, words=False)
    await world.plan()

    async def broken(*a, **kw):
        raise RuntimeError("review enqueue failed")

    monkeypatch.setattr(writes, "enqueue_review", broken)
    await world.run(world.handler(tools))
    row = await world.render_row()
    assert (row.status, row.video_key) == ("rendering", None)  # not done without its job
    assert "review enqueue failed" in row.error
    assert await world.job("review") == []


async def test_audio_only_is_unrenderable_without_ffmpeg(world, chunk_files):
    await world.setup(chunk_files, mode="audio_only", video=False)
    await world.plan()
    assert await world.run(world.handler(NoTools()))
    row = await world.render_row()
    assert (row.status, row.unrenderable_reason) == ("unrenderable", "no_video")
    assert row.finished_at is not None and await world.job("review") == []
    (job,) = await world.job("render")
    assert job.status == "done"  # no retry


async def test_video_gone_before_pinning_is_unrenderable(world, chunk_files):
    await world.setup(chunk_files)
    await world.plan()
    world.store.delete(world.chunks[2][1])  # retention took chunk 2's video
    assert await world.run(world.handler(NoTools()))
    row = await world.render_row()
    assert (row.status, row.unrenderable_reason) == ("unrenderable", "video_expired")
    assert "seq 2" in row.error


async def test_a_retry_resumes_the_row_and_the_last_attempt_fails_with_a_notice(
    world, tools, chunk_files
):
    await world.setup(chunk_files, words=False)
    await world.plan()
    calls = []

    def flaky(job, work, tools_):
        calls.append(job.render_id)
        if len(calls) == 1:
            raise RuntimeError("ffmpeg crashed")
        return render_files(job, work, tools_)

    handler = world.handler(tools, renderer=flaky)
    await world.run(handler)
    first = await world.render_row()
    assert (first.status, first.error) == ("rendering", "RuntimeError: ffmpeg crashed")
    async with world.sessions.begin() as s:  # the retry's backoff, skipped
        await s.execute(text("UPDATE jobs SET run_after = now()"))
    await world.run(handler)
    second = await world.render_row()
    assert (second.id, second.status, second.error) == (first.id, "done", None)
    assert calls == [first.id, first.id]
    assert handler.stats.errors == 1 and handler.stats.done == 1


async def test_the_last_attempt_marks_failed_and_queues_a_notice(world, chunk_files):
    await world.setup(chunk_files, words=False)
    await world.plan()

    def broken(job, work, tools_):
        raise RuntimeError("encoder exploded")

    handler = world.handler(NoTools(), renderer=broken)
    for _ in range(3):  # max_attempts = 3
        await world.run(handler)
        async with world.sessions.begin() as s:
            await s.execute(text("UPDATE jobs SET run_after = now()"))
    row = await world.render_row()
    assert (row.status, row.error) == ("failed", "RuntimeError: encoder exploded")
    (notice,) = await world.job("notify")
    assert (notice.ref_id, notice.dedupe_key) == (row.id, f"notify:{row.id}:render_failed")
    assert notice.payload["kind"] == "render_failed"
    assert notice.payload["clip_id"] == world.clip_id and notice.payload["severity"] == "error"
    (job,) = await world.job("render")
    assert (job.status, job.attempts) == ("failed", 3)
    # someone requeues the job: the row goes back to rendering, the job runs again
    async with world.sessions.begin() as s:
        await s.execute(text("UPDATE jobs SET status = 'queued', attempts = 0"))
    await world.run(world.handler(NoTools(), renderer=broken))
    again = await world.render_row()
    assert (again.id, again.status) == (row.id, "rendering")


async def test_re_render_from_pinned_copies_after_the_chunk_video_is_gone(
    world, tools, chunk_files
):
    await world.setup(chunk_files, words=False)
    await world.plan()
    await world.run(world.handler(tools))
    first = await world.render_row()
    for _, vkey, _ in world.chunks.values():
        world.store.delete(vkey)  # the 48 h retention
    async with world.sessions.begin() as s:  # review's re-render of the same plan: a new job
        await jobs.enqueue(
            s,
            JobKind.RENDER,
            payload=RenderPayload(plan_id=world.plan_id, profile="test_270x480").model_dump(),
            ref_id=world.plan_id,
            dedupe_key=f"render:{world.plan_id}:again",
        )
    await world.run(world.handler(tools))
    rows = await world.rows("SELECT id, status, video_sha256 FROM renders ORDER BY id")
    assert [r.status for r in rows] == ["done", "done"]
    assert rows[1].video_sha256 == first.video_sha256  # same plan, same pinned inputs


async def test_a_margin_chunk_without_video_only_warns(world, tools, chunk_files):
    await world.setup(chunk_files, words=False)
    await world.plan(segments=((2.0, 10.0),))  # needs chunk 1 only; 2 is in the 30 s margin
    world.store.delete(world.chunks[3][1])
    await world.run(world.handler(tools))
    row = await world.render_row()
    assert row.status == "done"
    assert any(w.startswith("pin_skipped") for w in row.warnings)
    assert keys.clip_pinned_video(world.clip_id, 3) not in row.pinned_keys


async def test_the_heartbeat(world, tools, sessions, settings):
    rs = RendererSettings(_env_file=None, temp_dir=world.tmp)
    service = RendererService(
        sessions,
        world.store,
        rs,
        worker_id="renderer-1",
        dsn=settings.database_dsn,
        tools=tools,
        encoder="libx264",
        nvenc_fallback=False,
    )
    await service.beat()
    (beat,) = await world.rows("SELECT service, info FROM worker_heartbeats")
    assert beat.service == "renderer"
    assert beat.info["encoder"] == "libx264" and beat.info["queued"] == 0
    assert beat.info["renders"] == {"done": 0, "unrenderable": 0, "failed": 0, "retried": 0}


async def test_on_seaweedfs_with_real_copy_object(sessions, settings, tools, chunk_files, tmp_path):
    store = ObjectStore(Settings(s3_bucket="clipper-test", _env_file=None))
    try:
        store.ensure_bucket(wait_s=3)
    except Exception as exc:
        pytest.skip(f"object storage not reachable ({exc})")
    w = World(sessions, settings, store, tmp_path)
    created: list[str] = []
    try:
        await w.setup(chunk_files)
        created += [v for _, v, _ in w.chunks.values()]
        await w.plan()
        await w.run(w.handler(tools))
        row = await w.render_row()
        created += [*row.pinned_keys, row.video_key, row.preview_key, row.thumb_key,
                    row.captions_key, row.manifest_key]  # fmt: skip
        assert row.status == "done", row.error
        assert store.get_bytes(row.pinned_keys[0]) == chunk_files[1].path.read_bytes()
        assert store.exists(row.video_key)
    finally:
        for key in created:
            if key:
                store.delete(key)
        store.delete(f"{w.prefix}/" + keys.chunk_words(w.stream_id, 1))
