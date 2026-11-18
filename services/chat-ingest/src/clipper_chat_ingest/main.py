from __future__ import annotations

import contextlib

import httpx

from clipper_chat_ingest.service import ChatIngest
from clipper_core.config import Settings
from clipper_core.db import create_engine, session_factory
from clipper_core.logs import get_logger, setup_logging
from clipper_core.runtime import run, stop_event_on_signals
from clipper_twitch.chat import TwitchChatClient

log = get_logger("clipper_chat_ingest")


async def amain() -> None:
    settings = Settings(service_name="chat-ingest")
    setup_logging(settings)
    stop = stop_event_on_signals()
    engine = create_engine(settings)
    async with httpx.AsyncClient(timeout=20.0) as http:
        service = ChatIngest(session_factory(engine), worker_id=settings.worker_id, http=http)
        log.info("chat-ingest starting", worker=settings.worker_id)
        try:
            await service.run(stop, TwitchChatClient)
        finally:
            await engine.dispose()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):  # Windows Ctrl+C (no signal handlers)
        run(amain)


if __name__ == "__main__":
    main()
