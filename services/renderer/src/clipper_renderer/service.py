"""The renderer service (DESIGN §20): one `render` job at a time (one process per GPU) and a
heartbeat (`worker_heartbeats`, service `renderer`: encoder, ffmpeg version, renders done /
unrenderable / failed / retried since start, the p50 render time, queued render jobs).

    service = RendererService(sessions, store, settings, worker_id=..., dsn=..., tools=...,
                              encoder="libx264", nvenc_fallback=False)
    await service.run(stop)
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_core import heartbeat
from clipper_core.contracts import JobKind
from clipper_core.jobs import JobRunner
from clipper_core.logs import get_logger
from clipper_core.timeline import utc_now
from clipper_renderer import __version__, reads
from clipper_renderer.handler import RenderHandler, Store
from clipper_renderer.media import Tools
from clipper_renderer.settings import RendererSettings

log = get_logger(__name__)

SERVICE = "renderer"
STOP_TIMEOUT_S = 45.0  # on shutdown, a render in progress gets this long (compose: 60 s grace)


class RendererService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        store: Store,
        settings: RendererSettings,
        *,
        worker_id: str,
        dsn: str,
        tools: Tools,
        encoder: str,
        nvenc_fallback: bool,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self.worker_id = worker_id
        self.tools = tools
        self.started_at = utc_now()
        self.handler = RenderHandler(
            sessions, store, settings, tools=tools, encoder=encoder, nvenc_fallback=nvenc_fallback
        )
        self.runner = JobRunner(
            sessions,
            dsn,
            worker_id,
            {JobKind.RENDER: self.handler},
            poll_interval=settings.poll_interval_s,
        )

    def info(self, queued: int) -> dict[str, Any]:
        st = self.handler.stats
        return {
            "version": __version__,
            "encoder": self.handler.encoder,
            "nvenc_fallback": self.handler.nvenc_fallback,
            "ffmpeg": self.tools.version(),
            "renders": {
                "done": st.done,
                "unrenderable": st.unrenderable,
                "failed": st.failed,
                "retried": st.errors,
            },
            "p50_render_s": st.p50_s,
            "queued": queued,
            "layouts": str(self.settings.layouts_path) if self.settings.layouts_path else None,
            "assets": str(self.settings.assets_dir) if self.settings.assets_dir else None,
        }

    async def beat(self) -> None:
        async with self.sessions.begin() as s:
            queued = await reads.queued_renders(s)
            await heartbeat.beat(
                s,
                worker_id=self.worker_id,
                service=SERVICE,
                started_at=self.started_at,
                info=self.info(queued),
            )

    async def run(self, stop: asyncio.Event) -> int:
        """Until `stop`: 0. The job runner ending on its own (a database error in its claim)
        stops the service with 1 (compose restarts it)."""
        log.info(
            "renderer starting",
            encoder=self.handler.encoder,
            nvenc_fallback=self.handler.nvenc_fallback,
            ffmpeg=self.tools.version(),
            fonts=[f.name for f in self.handler.fonts],
        )
        runner = asyncio.create_task(self.runner.run(stop), name="runner")
        beat = asyncio.create_task(self._beats(stop), name="beat")
        stopper = asyncio.create_task(stop.wait(), name="stop")
        exit_code = 0
        try:
            done, _ = await asyncio.wait([runner, stopper], return_when=asyncio.FIRST_COMPLETED)
            if runner in done and not stop.is_set():
                exc = None if runner.cancelled() else runner.exception()
                log.error("job runner ended unexpectedly", error=str(exc), exc_info=exc)
                exit_code = 1
        finally:
            stop.set()
            _, pending = await asyncio.wait([runner, beat, stopper], timeout=STOP_TIMEOUT_S)
            for task in pending:
                task.cancel()
            await asyncio.gather(runner, beat, stopper, return_exceptions=True)
            log.info("renderer stopped", exit_code=exit_code)
        return exit_code

    async def _beats(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.beat()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("heartbeat failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.settings.heartbeat_interval_s)
