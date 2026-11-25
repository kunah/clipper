"""Choosing and checking the model adapter (`CLIPPER_JUDGE_MODEL`), shared by the service
(`main`) and `clipper-judge eval` (`evaluate`)."""

from __future__ import annotations

from dataclasses import dataclass

from clipper_core.logs import get_logger
from clipper_judge.claude_cli import ClaudeAuthError, ClaudeCliModel
from clipper_judge.fakes import FakeJudgeModel
from clipper_judge.model import JudgeModel, PermanentModelError
from clipper_judge.prompt import load_prompt
from clipper_judge.settings import CLAUDE_CLI, FAKE, MODELS, JudgeSettings

log = get_logger(__name__)


def settings_for_model(settings: JudgeSettings) -> JudgeSettings:
    """The CLI adapter runs one call at a time (one process, the usage reading stays current):
    `concurrency` 1, with a warning when CLIPPER_JUDGE_CONCURRENCY asked for more."""
    if settings.model == CLAUDE_CLI and settings.concurrency != 1:
        if "concurrency" in settings.model_fields_set:
            log.warning(
                "the Claude Code CLI adapter runs one call at a time: concurrency set to 1",
                configured=settings.concurrency,
            )
        return settings.model_copy(update={"concurrency": 1})
    return settings


def make_model(settings: JudgeSettings) -> JudgeModel:
    if settings.model == FAKE:
        return FakeJudgeModel()
    if settings.model == CLAUDE_CLI:
        return ClaudeCliModel.from_settings(settings)
    raise ValueError(f"unknown judge model {settings.model!r}: one of {', '.join(MODELS)}")


@dataclass(frozen=True)
class Shadow:
    """A shadow run (`settings.shadow`): its adapter and prompt directory."""

    label: str  # model[@effort]/prompt
    model: JudgeModel
    prompt: str


def make_shadows(settings: JudgeSettings) -> list[Shadow]:
    """One adapter per `CLIPPER_JUDGE_SHADOW` entry, of the main adapter's kind (the CLI: another
    model id and/or effort, same token and config dir). `OSError`/`ValueError`: a prompt
    directory that doesn't exist."""
    out = []
    for spec in settings.shadow:
        model_id, effort, prompt = spec.resolved(settings)
        load_prompt(prompt)
        if settings.model == CLAUDE_CLI:
            model: JudgeModel = ClaudeCliModel.from_settings(
                settings.model_copy(update={"claude_model": model_id, "claude_effort": effort})
            )
        else:
            model = FakeJudgeModel(name=model_id)
        out.append(Shadow(spec.label(settings), model, prompt))
    return out


async def check_model(model: JudgeModel) -> bool:
    """The startup check of an adapter that has one (the CLI's `claude auth status`). False:
    the reason is logged, don't judge."""
    if not isinstance(model, ClaudeCliModel):
        return True
    try:
        await model.check_auth()
    except (ClaudeAuthError, PermanentModelError) as exc:
        log.error("claude auth check failed: no judging", error=str(exc))
        return False
    return True
