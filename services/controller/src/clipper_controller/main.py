"""controller: one active instance (lease), polls Twitch every 30 s, runs maintenance.

Without CLIPPER_TWITCH_CLIENT_ID/SECRET it still runs maintenance + heartbeats and logs a
reminder; live status starts working once credentials are set (restart needed).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta

from clipper_controller.maintenance import run_maintenance
from clipper_controller.poller import StreamPoller
from clipper_core import heartbeat, leases
from clipper_core.config import Settings
from clipper_core.db import create_engine, session_factory
from clipper_core.logs import get_logger, setup_logging
from clipper_core.runtime import run, stop_event_on_signals
from clipper_core.timeline import utc_now
from clipper_twitch.helix import HelixClient
from clipper_twitch.settings import TwitchSettings

log = get_logger("clipper_controller")

POLL_INTERVAL_S = 30.0
MAINTENANCE_EVERY = 20  # polls (~10 min)
LEASE = "controller"
LEASE_TTL = timedelta(seconds=90)


async def amain() -> None:
    settings = Settings(service_name="controller")
    setup_logging(settings)
    twitch = TwitchSettings()
    stop = stop_event_on_signals()
    engine = create_engine(settings)
    sessions = session_factory(engine)
    helix = HelixClient.from_settings(twitch) if twitch.has_credentials else None
    poller = StreamPoller(sessions, helix) if helix else None
    if poller is None:
        log.warning(
            "twitch credentials missing: live status disabled until "
            "CLIPPER_TWITCH_CLIENT_ID and CLIPPER_TWITCH_CLIENT_SECRET are set"
        )
    started_at = utc_now()
    tick = 0
    last: dict = {}
    try:
        while not stop.is_set():
            try:
                async with sessions.begin() as session:
                    leader = await leases.acquire(session, LEASE, settings.worker_id, LEASE_TTL)
                if leader:
                    if tick % MAINTENANCE_EVERY == 0:
                        await run_maintenance(sessions)
                    if poller:
                        r = await poller.poll_once()
                        last = {"live": r.live, "unknown": r.unknown_logins}
                    tick += 1
                async with sessions.begin() as session:
                    await heartbeat.beat(
                        session,
                        worker_id=settings.worker_id,
                        service="controller",
                        started_at=started_at,
                        info={"leader": leader, "twitch": poller is not None, **last},
                    )
            except Exception:
                log.exception("controller loop failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_S)
    finally:
        async with sessions.begin() as session:
            await leases.release(session, LEASE, settings.worker_id)
        if helix:
            await helix.aclose()
        await engine.dispose()
        log.info("controller stopped")


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):  # Windows Ctrl+C (no signal handlers)
        run(amain)


if __name__ == "__main__":
    main()
