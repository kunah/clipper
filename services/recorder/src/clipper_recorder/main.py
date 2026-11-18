"""recorder: records live streams into ~60 s chunks (see clipper_recorder.service)."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys

import httpx

from clipper_core.config import Settings
from clipper_core.db import create_engine, session_factory
from clipper_core.logs import get_logger, setup_logging
from clipper_core.runtime import run, stop_event_on_signals
from clipper_core.storage import ObjectStore
from clipper_recorder.finisher import Finisher
from clipper_recorder.resolver import StreamlinkResolver
from clipper_recorder.service import Recorder
from clipper_recorder.settings import RecorderSettings
from clipper_recorder.spool import Spool

log = get_logger("clipper_recorder")


async def amain() -> int:
    settings = Settings(service_name="recorder")
    setup_logging(settings)
    rs = RecorderSettings()
    if shutil.which(rs.ffmpeg) is None:
        log.error("ffmpeg not found, set CLIPPER_RECORDER_FFMPEG", ffmpeg=rs.ffmpeg)
        return 1
    stop = stop_event_on_signals()
    engine = create_engine(settings)
    store = ObjectStore(settings)
    await asyncio.to_thread(store.ensure_bucket)
    spool = Spool(rs.spool_dir)
    finisher = Finisher(
        session_factory(engine), store, spool, ffmpeg=rs.ffmpeg, workers=rs.finish_workers
    )
    log.info(
        "recorder starting",
        worker=settings.worker_id,
        spool=str(rs.spool_dir.resolve()),
        chunk_seconds=rs.chunk_seconds,
        qualities=rs.quality_list,
    )
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as http:
            recorder = Recorder(
                session_factory(engine),
                settings=rs,
                worker_id=settings.worker_id,
                http=http,
                resolver=StreamlinkResolver(rs.quality_list),
                spool=spool,
                finisher=finisher,
            )
            await recorder.run(stop)
    finally:
        await engine.dispose()
    return 0


def main() -> None:
    # Windows Ctrl+C has no signal handler: the open chunk stays in the spool and is finished
    # on the next start.
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(run(amain))


if __name__ == "__main__":
    main()
