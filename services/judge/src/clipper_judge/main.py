"""judge: candidates -> clips with an edit plan (see clipper_judge.service). DESIGN §19.

    clipper-judge                 # the service (CLIPPER_JUDGE_MODEL = fake | claude-cli)
    clipper-judge eval ...        # read-only: the judge over labelled candidates (evaluate)
    clipper-judge label prepare|serve ...   # a blind labelling set for the eval (label)

CLIPPER_JUDGE_MODEL = fake (deterministic rules) or claude-cli (the Claude Code CLI on the user's
subscription, `clipper_judge.claude_cli`). With claude-cli the service first runs `claude auth
status` with the CLI's own environment and exits (1, no judging) unless it shows a subscription
login (`oauth_token`, or `claude.ai` natively); it runs one job at a time. Nothing enqueues judge
jobs until the detector runs with CLIPPER_DETECTOR_JUDGE_JOBS=1; `render` jobs wait for the
renderer (`clipper-renderer`, §20).
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
from clipper_core.storage import ObjectStore
from clipper_judge import evaluate, label
from clipper_judge.adapters import check_model, make_model, make_shadows, settings_for_model
from clipper_judge.prompt import load_prompt
from clipper_judge.service import SERVICE, JudgeService
from clipper_judge.settings import JudgeSettings

__all__ = ["main", "make_model", "settings_for_model"]

log = get_logger("clipper_judge")


async def amain() -> int:
    settings = Settings(service_name=SERVICE)
    setup_logging(settings)
    try:
        js = settings_for_model(JudgeSettings())
        model = make_model(js)
        load_prompt(js.prompt)
        shadows = make_shadows(js)
    except (ValueError, OSError) as exc:  # ValidationError, unknown model, missing prompt dir
        log.error("invalid judge configuration (CLIPPER_JUDGE_*)", error=str(exc))
        return 1
    if not await check_model(model):
        return 1
    stop = stop_event_on_signals()
    engine = create_engine(settings)
    try:
        service = JudgeService(
            session_factory(engine),
            ObjectStore(settings),
            model,
            js,
            worker_id=settings.worker_id,
            dsn=settings.database_dsn,
            shadows=shadows,
        )
        return await service.run(stop)  # 1: a runner or the waiter died (compose restarts it)
    finally:
        await engine.dispose()


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="clipper-judge",
        description="Judge (DESIGN 19). Without a command: the service (CLIPPER_JUDGE_MODEL).",
    )
    sub = p.add_subparsers(dest="cmd", metavar="command")
    ev = evaluate.add_parser(sub)
    lab = label.add_parser(sub)
    args = p.parse_args(list(argv))
    if args.cmd == "eval":
        evaluate.check_args(ev, args)
    if args.cmd == "label":
        label.check_args(lab, args)
    return args


def main() -> None:
    args = parse_args(sys.argv[1:])
    if args.cmd == "label":
        with contextlib.suppress(KeyboardInterrupt):
            sys.exit(label.main(args))
        return
    if args.cmd == "eval":
        # titles and chat: a cp1252 console or pipe must not crash the report
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(AttributeError, ValueError):
                stream.reconfigure(errors="backslashreplace")
        with contextlib.suppress(KeyboardInterrupt):
            sys.exit(run(lambda: evaluate.amain(args)))
        return
    with contextlib.suppress(KeyboardInterrupt):  # Windows Ctrl+C (no signal handlers)
        sys.exit(run(amain))


if __name__ == "__main__":
    main()
