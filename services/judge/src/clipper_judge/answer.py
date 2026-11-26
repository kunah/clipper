"""`JudgeAnswer`: what the model fills in (judge-internal, DESIGN §19).

Seconds relative to T (the candidate's peak), sentence ids from the input, and the plan's raw
fields. Deterministic code (`clipper_judge.resolver`) turns it into the versioned `EditPlan` of
libs/core, so the prompt and this schema can change without touching the renderer's contract.

The models are lenient where the resolver repairs (long titles, `#` hashtags, unknown SFX tags,
any zoom scale, ...) and strict where it can't (types, enum values, finite numbers): an answer
that doesn't parse gets one retry with the error stated (`AnswerError`). Unknown keys are
ignored; the schema still says `additionalProperties: false` for providers with strict
structured outputs. The last round uses `FinalJudgeAnswer`, whose schema has no `need_context`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from clipper_core.contracts import (
    CaptionStyle,
    HookMode,
    LayoutKind,
    MomentType,
    MusicMood,
    Verdict,
)

ANSWER_SCHEMA_VERSION = 1


class _Answer(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        allow_inf_nan=False,
        json_schema_extra={"additionalProperties": False},
    )


class ContextAsk(_Answer):
    before_s: float = Field(description="how far before T the context should start, seconds")
    after_s: float = Field(description="how far after T the context should end, seconds")
    why: str = Field(default="", description="what you need to see")


class HookAnswer(_Answer):
    mode: HookMode = HookMode.NONE
    text: str | None = Field(default=None, description="mode text: overlay, <= 40 characters")
    from_s: float | None = Field(default=None, description="mode teaser: start (inside the clip)")
    to_s: float | None = Field(default=None, description="mode teaser: end, 1-3 s after from_s")


class EmphasisAnswer(_Answer):
    sentence: str = Field(description="sentence id from the transcript, e.g. S14")
    word: str = Field(description="a word of that sentence, as written there")
    kind: Literal["highlight", "big"] = "highlight"


class ZoomAnswer(_Answer):
    from_s: float
    to_s: float
    scale: float = Field(default=1.2, description="1.1-1.5")
    target: Literal["face", "gameplay", "center"] = "center"


class SfxAnswer(_Answer):
    at_s: float
    tag: str = Field(description="one of the SFX tags listed in the input")


class LayoutAnswer(_Answer):
    kind: LayoutKind = LayoutKind.AUTO
    focus_x: float | None = Field(default=None, description="full_crop: 0 = left, 1 = right")


class PlanAnswer(_Answer):
    in_s: float = Field(description="clip start, seconds relative to T")
    out_s: float = Field(description="clip end, seconds relative to T")
    title: str = Field(default="", description="<= 100 characters, no hashtags")
    hook: HookAnswer = Field(default_factory=HookAnswer)
    hashtags: list[str] = Field(default_factory=list, description="<= 5 bare words, no '#'")
    caption_style: CaptionStyle = CaptionStyle.BOLD_POP
    emphasis: list[EmphasisAnswer] = Field(default_factory=list)
    zooms: list[ZoomAnswer] = Field(default_factory=list)
    sfx: list[SfxAnswer] = Field(default_factory=list)
    music_mood: MusicMood | None = None
    layout: LayoutAnswer = Field(default_factory=LayoutAnswer)
    thumbnail_s: float | None = None
    moment_type: MomentType | None = Field(default=None, description="if not the detector's")


class JudgeAnswer(_Answer):
    """Rounds that may still ask for more context."""

    reasons: list[str] = Field(
        default_factory=list, description="first a one-line summary for the reviewer"
    )
    verdict: Verdict
    confidence: float = Field(default=0.5, description="0-1: would an editor publish it")
    need_context: ContextAsk | None = Field(default=None, description="verdict need_context")
    plan: PlanAnswer | None = Field(default=None, description="verdict clip")


class FinalJudgeAnswer(_Answer):
    """The last round: decide with what there is (no `need_context`)."""

    reasons: list[str] = Field(
        default_factory=list, description="first a one-line summary for the reviewer"
    )
    verdict: Literal["clip", "reject"]
    confidence: float = Field(default=0.5, description="0-1: would an editor publish it")
    plan: PlanAnswer | None = Field(default=None, description="verdict clip")

    def to_answer(self) -> JudgeAnswer:
        return JudgeAnswer(
            reasons=self.reasons,
            verdict=Verdict(self.verdict),
            confidence=self.confidence,
            plan=self.plan,
        )


class AnswerError(ValueError):
    """The model's output isn't a valid answer (invalid JSON or schema): retry once with this
    message, then the clip fails (§19)."""


def answer_model(final: bool) -> type[JudgeAnswer] | type[FinalJudgeAnswer]:
    return FinalJudgeAnswer if final else JudgeAnswer


def answer_schema(final: bool) -> dict[str, Any]:
    """The JSON schema given to the model: pydantic's, from the models above, without titles and
    class docstrings (core enums' docstrings are internal docs: editing them must not change the
    prompt version). Field descriptions stay."""
    schema = answer_model(final).model_json_schema()
    schema.pop("description", None)
    for definition in schema.get("$defs", {}).values():
        definition.pop("description", None)
    return _without_titles(schema)


def _without_titles(node: Any) -> Any:
    if isinstance(node, dict):
        # a "title" key holding a string is pydantic's label; in `properties` it is a field
        return {
            k: _without_titles(v)
            for k, v in node.items()
            if not (k == "title" and isinstance(v, str))
        }
    if isinstance(node, list):
        return [_without_titles(v) for v in node]
    return node


def parse_answer(raw: str | bytes | Mapping[str, Any], *, final: bool) -> JudgeAnswer:
    """The model's JSON text (or an already decoded object) as a `JudgeAnswer`."""
    try:
        data = json.loads(raw) if isinstance(raw, str | bytes) else dict(raw)
    except ValueError as exc:
        raise AnswerError(f"the answer is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AnswerError("the answer must be one JSON object")
    try:
        parsed = answer_model(final).model_validate(data)
    except ValidationError as exc:
        raise AnswerError(f"the answer doesn't match the schema: {exc}") from exc
    return parsed.to_answer() if isinstance(parsed, FinalJudgeAnswer) else parsed
