"""The `render` job (DESIGN §20 "Per job"): one plan -> one `renders` row -> outputs + a `review`
job.

1. Load the plan (`clip_plans`, a `clip` verdict) and the job's `renders` row (created on the
   first attempt; a retry resumes it; a `done`/`unrenderable` row means there is nothing to do).
2. The stream's chunks around the plan. No chunk with video under a range (audio_only) ->
   `unrenderable`/`no_video`, without running ffmpeg.
3. Pin: S3 server-side copy of every chunk video overlapping [in - 30 s, out + 30 s] to
   `clips/{clip}/pinned/{seq}.ts` (existing copies are kept). A chunk the plan needs whose video
   is gone and was never pinned -> `unrenderable`/`video_expired`; one only in the margin is
   skipped with a warning.
4. Download the pinned copies the plan uses, the words of [in, out) (`transcripts.words`), the
   layouts and the usable assets; `render.render_files` in a worker thread.
5. Upload the outputs; one transaction: the row `done` (keys, sha256, measurements) + the
   `review` job (`review:{render}`).
An error retries the job (the row keeps `rendering` and the error); the job's last attempt, or an
error retrying can't fix, marks the row `failed` with a `notify(render_failed)` job, together.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from botocore.exceptions import ClientError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_core import transcripts
from clipper_core.contracts import (
    EditPlan,
    RenderPayload,
    RenderStatus,
    UnrenderableReason,
)
from clipper_core.contracts import (
    Verdict as PlanVerdict,
)
from clipper_core.jobs import ClaimedJob, PermanentJobError
from clipper_core.logs import get_logger, log_context
from clipper_core.storage import keys
from clipper_core.timeline import Interval
from clipper_renderer import __version__, fontset, reads, writes
from clipper_renderer.layout import load_layouts
from clipper_renderer.library import AssetLibrary
from clipper_renderer.media import Tools
from clipper_renderer.profiles import get_profile
from clipper_renderer.render import (
    CAPTIONS,
    CLIP,
    MANIFEST,
    PREVIEW,
    THUMB,
    LocalChunk,
    RenderJob,
    RenderOutput,
    render_files,
)
from clipper_renderer.settings import RendererSettings
from clipper_renderer.timemap import ChunkVideo, NoVideo, build, plan_ranges

log = get_logger(__name__)

CONTENT_TYPES = {
    CLIP: "video/mp4",
    PREVIEW: "video/mp4",
    THUMB: "image/jpeg",
    CAPTIONS: "text/x-ssa; charset=utf-8",
    MANIFEST: "application/json",
}
KEY_COLUMNS = {
    CLIP: "video_key",
    PREVIEW: "preview_key",
    THUMB: "thumb_key",
    CAPTIONS: "captions_key",
    MANIFEST: "manifest_key",
}


class Store(Protocol):
    """The part of `storage.ObjectStore` the renderer uses (tests pass a fake)."""

    def exists(self, key: str) -> bool: ...
    def copy(self, src_key: str, dst_key: str) -> None: ...
    def download_file(self, key: str, path: Path | str) -> None: ...
    def put_file(self, key: str, path: Path | str, content_type: str | None = None) -> None: ...
    def get_bytes(self, key: str) -> bytes: ...


class Unrenderable(Exception):
    def __init__(self, reason: UnrenderableReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


@dataclass
class RenderStats:
    done: int = 0
    unrenderable: int = 0
    failed: int = 0
    errors: int = 0
    render_s: list[float] = field(default_factory=list)  # the last 50 done renders

    def add_time(self, seconds: float) -> None:
        self.render_s = [*self.render_s[-49:], seconds]

    @property
    def p50_s(self) -> float | None:
        if not self.render_s:
            return None
        ordered = sorted(self.render_s)
        return round(ordered[len(ordered) // 2], 2)


Renderer = Callable[[RenderJob, Path, Tools], RenderOutput]


class RenderHandler:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        store: Store,
        settings: RendererSettings,
        *,
        tools: Tools,
        encoder: str,
        nvenc_fallback: bool,
        renderer: Renderer = render_files,
    ) -> None:
        self.sessions = sessions
        self.store = store
        self.settings = settings
        self.tools = tools
        self.encoder = encoder
        self.nvenc_fallback = nvenc_fallback
        self.renderer = renderer
        self.fonts = fontset.bundled_fonts(settings.fonts_dir)
        self.library = AssetLibrary(settings.assets_dir, tools, platforms=settings.asset_platforms)
        self.stats = RenderStats()

    async def __call__(self, job: ClaimedJob) -> None:
        try:
            payload = RenderPayload.model_validate(job.payload)
        except ValidationError as exc:
            raise PermanentJobError(f"bad render payload: {exc}") from exc
        async with self.sessions() as s:
            ctx = await reads.plan_context(s, payload.plan_id)
        if ctx is None:
            raise PermanentJobError(f"plan {payload.plan_id} doesn't exist")
        async with self.sessions.begin() as s:
            row = await writes.claim_row(
                s, job_id=job.id, clip_id=ctx.clip_id, plan_id=ctx.plan_id, profile=payload.profile
            )
        if row.status in (RenderStatus.DONE, RenderStatus.UNRENDERABLE):
            log.info("render already finished", render_id=row.id, status=row.status)
            return
        with log_context(render_id=row.id, clip_id=ctx.clip_id, plan_id=ctx.plan_id):
            started = time.monotonic()
            try:
                await self._render(job, row.id, ctx, payload)
            except Unrenderable as exc:
                async with self.sessions.begin() as s:
                    await writes.mark_unrenderable(
                        s, row.id, exc.reason, detail=str(exc), warnings=[]
                    )
                self.stats.unrenderable += 1
                log.warning("unrenderable", reason=exc.reason, detail=str(exc))
            except Exception as exc:
                elapsed = round(time.monotonic() - started, 3)
                final = isinstance(exc, PermanentJobError) or job.attempts >= job.max_attempts
                error = f"{type(exc).__name__}: {exc}"
                async with self.sessions.begin() as s:
                    if final:
                        await writes.mark_failed(
                            s, row.id, clip_id=ctx.clip_id, error=error, render_s=elapsed
                        )
                    else:
                        await writes.set_error(s, row.id, error, elapsed)
                if final:
                    self.stats.failed += 1
                    log.error("render failed, notice queued", attempt=job.attempts, error=error)
                    raise PermanentJobError(error) from exc
                self.stats.errors += 1
                raise

    async def _render(
        self, job: ClaimedJob, render_id: int, ctx: reads.PlanContext, payload: RenderPayload
    ) -> None:
        if ctx.verdict != PlanVerdict.CLIP or ctx.plan is None:
            raise PermanentJobError(f"plan {ctx.plan_id} is not a clip plan ({ctx.verdict})")
        try:
            plan = EditPlan.model_validate(ctx.plan)
            profile = get_profile(payload.profile)
        except (ValidationError, ValueError) as exc:
            raise PermanentJobError(str(exc)) from exc
        started = time.monotonic()
        ranges = plan_ranges(plan)
        margin = timedelta(seconds=self.settings.pin_margin_s)
        span = Interval(
            min(r.interval.start for r in ranges) - margin,
            max(r.interval.end for r in ranges) + margin,
        )
        async with self.sessions() as s:
            chunks = await reads.chunks_overlapping(s, ctx.stream_id, span.start, span.end)
        with_video = [c for c in chunks if c.video_key]
        planned = [
            ChunkVideo(
                c.id, c.seq, c.start_utc, c.end_utc, keys.clip_pinned_video(ctx.clip_id, c.seq)
            )
            for c in with_video
        ]
        try:
            tm = build(ranges, planned)
        except NoVideo as exc:
            raise Unrenderable(UnrenderableReason.NO_VIDEO, str(exc)) from exc
        used = {p.chunk.chunk_id for p in tm.pieces}

        pinned, margin_warnings = await asyncio.to_thread(self._pin, ctx.clip_id, with_video, used)
        async with self.sessions.begin() as s:
            await writes.set_pinned(s, render_id, pinned)

        work = Path(tempfile.mkdtemp(prefix=f"render-{render_id}-", dir=self.settings.temp_dir))
        try:
            local = await asyncio.to_thread(self._download, work, planned, used)
            async with self.sessions() as s:
                words = await transcripts.words(
                    s, self.store, ctx.stream_id, Interval(plan.in_utc, plan.out_utc)
                )
            layouts, layouts_sha = load_layouts(self.settings.layouts_path)
            usable = await asyncio.to_thread(self.library.usable)
            rjob = RenderJob(
                render_id=render_id,
                plan_id=ctx.plan_id,
                plan=plan,
                profile=profile,
                chunks=local,
                words=words,
                encoder=self.encoder if profile.nvenc else "libx264",
                nvenc_fallback=self.nvenc_fallback,
                login=ctx.login,
                category=ctx.category,
                layouts=layouts,
                layouts_sha256=layouts_sha,
                assets=usable,
                normalisation_db=self.library.normalisation_db,
                fonts=self.fonts,
                timeout_s=self.settings.timeout_s,
            )
            out = await asyncio.to_thread(self.renderer, rjob, work, self.tools)
            uploaded = await asyncio.to_thread(self._upload, ctx.clip_id, render_id, out)
            render_s = round(time.monotonic() - started, 3)
            values = {
                **{KEY_COLUMNS[name]: key for name, key in uploaded.items()},
                "layout": out.layout.kind.value,
                "encoder": out.encoder,
                "renderer_version": __version__,
                "ffmpeg_version": self.tools.version(),
                "graph_hash": out.graph_hash,
                "pinned_keys": pinned,
                "video_sha256": out.video_sha256,
                "duration_s": out.duration_s,
                "fps": out.fps,
                "loudness_lufs": out.loudness_lufs,
                "true_peak_db": out.true_peak_db,
                "render_s": render_s,
                "width": out.width,
                "height": out.height,
                "bytes": out.bytes,
                "preview_bytes": out.preview_bytes,
                "warnings": [*margin_warnings, *out.warnings],
            }
            async with self.sessions.begin() as s:
                await writes.mark_done(s, render_id, values)
                await writes.enqueue_review(s, render_id, priority=job.priority)
            self.stats.done += 1
            self.stats.add_time(render_s)
            log.info(
                "render done",
                duration_s=out.duration_s,
                render_s=render_s,
                ffmpeg_s=out.ffmpeg_s,
                encoder=out.encoder,
                layout=out.layout.kind.value,
                loudness_lufs=out.loudness_lufs,
                warnings=len(values["warnings"]),
            )
        finally:
            if not self.settings.keep_work:
                shutil.rmtree(work, ignore_errors=True)

    def _pin(
        self, clip_id: int, chunks: list[reads.ChunkRow], used: set[int]
    ) -> tuple[list[str], list[str]]:
        pinned: list[str] = []
        warnings: list[str] = []
        for c in chunks:
            dst = keys.clip_pinned_video(clip_id, c.seq)
            if self.store.exists(dst):
                pinned.append(dst)
                continue
            try:
                self.store.copy(c.video_key, dst)
            except (ClientError, KeyError) as exc:
                if not _missing(exc):
                    raise
                if c.id in used:
                    raise Unrenderable(
                        UnrenderableReason.VIDEO_EXPIRED,
                        f"chunk {c.id} (seq {c.seq}): {c.video_key} is gone and was never pinned",
                    ) from exc
                warnings.append(f"pin_skipped: chunk {c.id} (margin only) has no video any more")
                continue
            pinned.append(dst)
        return pinned, warnings

    def _download(self, work: Path, planned: list[ChunkVideo], used: set[int]) -> list[LocalChunk]:
        (work / "in").mkdir(parents=True, exist_ok=True)
        out = []
        for c in planned:
            if c.chunk_id not in used:
                continue
            path = work / "in" / f"{c.seq:06d}.ts"
            self.store.download_file(c.key, path)
            out.append(LocalChunk(c, path))
        return out

    def _upload(self, clip_id: int, render_id: int, out: RenderOutput) -> dict[str, str]:
        uploaded = {}
        for name, path in out.files.items():
            key = keys.render_file(clip_id, render_id, name)
            self.store.put_file(key, path, content_type=CONTENT_TYPES[name])
            uploaded[name] = key
        return uploaded


def _missing(exc: Exception) -> bool:
    if isinstance(exc, KeyError):
        return True
    code = exc.response.get("Error", {}).get("Code") if isinstance(exc, ClientError) else None
    return code in ("404", "NoSuchKey", "NotFound")
