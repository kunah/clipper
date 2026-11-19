"""transcriber: claims `transcribe` jobs and writes transcripts (see clipper_transcriber.service).

One process per GPU. SIGTERM/SIGINT stop it after the job in progress.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys

from pydantic import ValidationError

from clipper_core.config import Settings
from clipper_core.db import create_engine, session_factory
from clipper_core.logs import get_logger, setup_logging
from clipper_core.runtime import run, stop_event_on_signals
from clipper_core.storage import ObjectStore
from clipper_transcriber.service import (
    SERVICE,
    SOUND_TAGGERS,
    SPEECH_ENGINES,
    TranscriberService,
    build_engines,
    engine_device,
)
from clipper_transcriber.settings import TranscriberSettings

log = get_logger("clipper_transcriber")


async def amain() -> int:
    settings = Settings(service_name=SERVICE)
    setup_logging(settings)
    try:
        ts = TranscriberSettings()
    except ValidationError as exc:
        log.error(
            "invalid transcriber settings (CLIPPER_TRANSCRIBER_*)",
            errors=exc.errors(include_url=False, include_context=False),
            engines=sorted(SPEECH_ENGINES),
        )
        return 1
    if shutil.which(ts.ffmpeg) is None:
        log.error("ffmpeg not found, set CLIPPER_TRANSCRIBER_FFMPEG", ffmpeg=ts.ffmpeg)
        return 1
    if ts.engine not in SPEECH_ENGINES or ts.tagger not in SOUND_TAGGERS:
        log.error(
            "unknown engine, set CLIPPER_TRANSCRIBER_ENGINE / CLIPPER_TRANSCRIBER_TAGGER",
            engine=ts.engine,
            tagger=ts.tagger,
            engines=sorted(SPEECH_ENGINES),
            taggers=sorted(SOUND_TAGGERS),
        )
        return 1
    stop = stop_event_on_signals()

    engines = await asyncio.to_thread(build_engines, ts)
    db = create_engine(settings)
    store = ObjectStore(settings)
    await asyncio.to_thread(store.ensure_bucket)
    service = TranscriberService(
        session_factory(db),
        store,
        engines,
        ts,
        worker_id=settings.worker_id,
        dsn=settings.database_dsn,
    )
    log.info(
        "transcriber starting",
        speech=engines.speech.name,
        vad=engines.vad.name,
        tagger=engines.tagger.name if engines.tagger is not None else None,
        device=engine_device(engines),
    )
    try:
        await service.run(stop)
    finally:
        await db.dispose()
        log.info(
            "transcriber stopped", jobs_done=service.stats.done, jobs_failed=service.stats.failed
        )
    return 0


def main() -> None:
    # Windows Ctrl+C has no signal handler: KeyboardInterrupt ends the process mid-job, and the
    # controller requeues that job after 15 min (nothing is committed half-way).
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(run(amain))


if __name__ == "__main__":
    main()
