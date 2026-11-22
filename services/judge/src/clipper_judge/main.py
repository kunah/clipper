"""judge: candidates -> clips with an edit plan (see clipper_judge.service). DESIGN §19.

CLIPPER_JUDGE_MODEL = fake (deterministic rules, the only adapter until build item 6d adds the
Claude Code CLI). Nothing enqueues judge jobs until the detector runs with
CLIPPER_DETECTOR_JUDGE_JOBS=1; `render` jobs queue until the renderer exists (§20).
"""

from __future__ import annotations

import contextlib
import sys

from clipper_core.config import Settings
from clipper_core.db import create_engine, session_factory
from clipper_core.logs import get_logger, setup_logging
from clipper_core.runtime import run, stop_event_on_signals
from clipper_core.storage import ObjectStore
from clipper_judge.fakes import FakeJudgeModel
from clipper_judge.model import JudgeModel
from clipper_judge.prompt import load_prompt
from clipper_judge.service import SERVICE, JudgeService
from clipper_judge.settings import JudgeSettings

log = get_logger("clipper_judge")

MODELS = ("fake",)


def make_model(settings: JudgeSettings) -> JudgeModel:
    if settings.model == "fake":
        return FakeJudgeModel()
    raise ValueError(f"unknown judge model {settings.model!r}: one of {', '.join(MODELS)}")


async def amain() -> int:
    settings = Settings(service_name=SERVICE)
    setup_logging(settings)
    try:
        js = JudgeSettings()
        model = make_model(js)
        load_prompt(js.prompt)
    except (ValueError, OSError) as exc:  # ValidationError, unknown model, missing prompt dir
        log.error("invalid judge configuration (CLIPPER_JUDGE_*)", error=str(exc))
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
        )
        return await service.run(stop)  # 1: a runner or the waiter died (compose restarts it)
    finally:
        await engine.dispose()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):  # Windows Ctrl+C (no signal handlers)
        sys.exit(run(amain))


if __name__ == "__main__":
    main()
