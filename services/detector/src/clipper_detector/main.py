"""detector: chat spikes -> candidates (see clipper_detector.service). DESIGN §18.

    clipper-detector                    # the live service (CLIPPER_DETECTOR_MODE)
    clipper-detector backtest ...       # read-only: stored streams vs viewer clips (backtest_cli)

CLIPPER_DETECTOR_MODE = live | offline | all (offline: build item 5, not built yet). Viewer clips
are polled only when CLIPPER_TWITCH_CLIENT_ID/SECRET are set; everything else runs without them.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Sequence

from clipper_core.config import Settings
from clipper_core.db import create_engine, session_factory
from clipper_core.logs import get_logger, setup_logging
from clipper_core.runtime import run, stop_event_on_signals
from clipper_detector import backtest_cli
from clipper_detector.lexicon import Lexicon
from clipper_detector.params import load_params
from clipper_detector.service import SERVICE, LiveDetector
from clipper_detector.settings import DetectorSettings
from clipper_twitch.helix import HelixClient
from clipper_twitch.settings import TwitchSettings

log = get_logger("clipper_detector")


async def amain() -> int:
    settings = Settings(service_name=SERVICE)
    setup_logging(settings)
    try:
        ds = DetectorSettings()
        params = load_params(ds.params_path)
        lexicon = Lexicon.load(ds.lexicon_path)
    except (ValueError, OSError) as exc:  # ValidationError, LexiconError, TOMLDecodeError
        log.error("invalid detector configuration (CLIPPER_DETECTOR_*)", error=str(exc))
        return 1
    twitch = TwitchSettings()
    helix = HelixClient.from_settings(twitch) if twitch.has_credentials else None
    if helix is None:
        log.info("twitch credentials missing: viewer clips are not polled")
    stop = stop_event_on_signals()
    engine = create_engine(settings)
    try:
        detector = LiveDetector(
            session_factory(engine),
            settings=ds,
            params=params,
            lexicon=lexicon,
            worker_id=settings.worker_id,
            helix=helix,
        )
        await detector.run(stop)
    finally:
        if helix is not None:
            await helix.aclose()
        await engine.dispose()
    return 0


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="clipper-detector",
        description="Detector (DESIGN 18). Without a command: the live service "
        "(CLIPPER_DETECTOR_MODE = live | offline | all).",
    )
    sub = p.add_subparsers(dest="cmd", metavar="command")
    backtest = backtest_cli.add_parser(sub)
    args = p.parse_args(list(argv))
    if args.cmd == "backtest":
        backtest_cli.check_args(backtest, args)
    return args


def main() -> None:
    args = parse_args(sys.argv[1:])
    if args.cmd == "backtest":
        # channel logins and emotes: a cp1252 console or pipe must not crash the report
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(AttributeError, ValueError):
                stream.reconfigure(errors="backslashreplace")
        with contextlib.suppress(KeyboardInterrupt):
            sys.exit(run(lambda: backtest_cli.amain(args)))
        return
    with contextlib.suppress(KeyboardInterrupt):  # Windows Ctrl+C (no signal handlers)
        sys.exit(run(amain))


if __name__ == "__main__":
    main()
