"""Deterministic stand-ins for a real LLM (DESIGN §19): the service's default model until the
Claude Code CLI adapter exists (6d), and the models of the tests.

- `FakeJudgeModel` (`CLIPPER_JUDGE_MODEL=fake`): rules on the request text, so the same request
  always gets the same answer. Reject below `min_score` (the detector's score); ask for more
  context in rounds below `context_rounds` (never in the last round); else clip the core moment
  (the resolver fits it to the clip length), titled after the first sentence in the core.
- `ScriptedJudgeModel`: returns scripted outcomes in order and records every request; for tests
  that need a particular answer, an invalid one, or a provider error.

Both report token counts estimated from the text length (4 characters a token), latency 0 and a
request id derived from the request hash: no wall-clock value, so their calls are reproducible.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from clipper_judge.answer import JudgeAnswer
from clipper_judge.model import JudgeResponse, Usage
from clipper_judge.prompt import JudgeRequest

_SCORE = re.compile(r"^detector: .*\bscore (-?\d+(?:\.\d+)?)", re.M)
_CHANNEL = re.compile(r"^channel: (.*)$", re.M)
_NUM = r"([+-]?\d+(?:\.\d+)?)"
_CORE = re.compile(rf"\bcore \[{_NUM}→{_NUM}\]")
_ROUND = re.compile(r"^Round (\d+)\b", re.M)
FAKE_REQUESTS_KEPT = 50
_SENTENCE = re.compile(rf"^S\d+ \[{_NUM}→{_NUM}\] (.+)$", re.M)


def _tokens(text: str) -> int:
    return max(len(text) // 4, 1)


def response_for(request: JudgeRequest, text: str, *, prefix: str) -> JudgeResponse:
    """A fake call's response: usage estimated from the text, a stable request id."""
    digest = hashlib.sha256(request.canonical().encode("utf-8")).hexdigest()[:12]
    return JudgeResponse(
        text=text,
        usage=Usage(
            input_tokens=_tokens(request.user),
            cached_input_tokens=_tokens(request.system),
            output_tokens=_tokens(text),
        ),
        latency_s=0.0,
        request_id=f"{prefix}-{digest}",
        cost_usd=0.0,
    )


class FakeJudgeModel:
    """Rule-based judge, deterministic (see the module docstring)."""

    def __init__(
        self,
        *,
        name: str = "fake",
        min_score: float = 0.0,
        context_rounds: int = 0,
        before_s: float = 300.0,
        after_s: float = 60.0,
    ) -> None:
        self._name = name
        self.min_score = min_score
        self.context_rounds = context_rounds
        self.before_s = before_s
        self.after_s = after_s
        # the latest requests (tests look at them; bounded, it also runs as the service's model)
        self.requests: deque[JudgeRequest] = deque(maxlen=FAKE_REQUESTS_KEPT)

    @property
    def name(self) -> str:
        return self._name

    @property
    def options(self) -> Mapping[str, Any]:
        return {
            "min_score": self.min_score,
            "context_rounds": self.context_rounds,
            "before_s": self.before_s,
            "after_s": self.after_s,
        }

    async def judge(self, request: JudgeRequest) -> JudgeResponse:
        self.requests.append(request)
        return response_for(request, json.dumps(self.answer(request)), prefix=self._name)

    def answer(self, request: JudgeRequest) -> dict[str, Any]:
        user = request.user
        score = _float(_SCORE, user, 0.0)
        round_ = int(m.group(1)) if (m := _ROUND.search(user)) else 0
        if score < self.min_score:
            return {
                "reasons": [f"fake: score {score:.2f} is below {self.min_score:g}"],
                "verdict": "reject",
                "confidence": 0.2,
            }
        if not request.final and round_ < self.context_rounds:
            return {
                "reasons": [f"fake: round {round_} asks for more context"],
                "verdict": "need_context",
                "confidence": 0.5,
                "need_context": {
                    "before_s": self.before_s,
                    "after_s": self.after_s,
                    "why": "the setup before the moment",
                },
                "plan": None,
            }
        core = _CORE.search(user)
        in_s, out_s = (float(core.group(1)), float(core.group(2))) if core else (-10.0, 5.0)
        channel = m.group(1).strip() if (m := _CHANNEL.search(user)) else "stream"
        title = next(
            (
                s.group(3).strip()
                for s in _SENTENCE.finditer(user)
                if float(s.group(2)) > in_s and float(s.group(1)) < out_s
            ),
            f"{channel} clip",
        )
        return {
            "reasons": [f"fake: {title}"],
            "verdict": "clip",
            "confidence": 0.7,
            "need_context": None,
            "plan": {
                "in_s": in_s,
                "out_s": out_s,
                "title": title,
                "hashtags": [re.sub(r"\W", "", channel) or "clip"],
                "caption_style": "bold_pop",
            },
        }


Scripted = str | Mapping[str, Any] | JudgeAnswer | JudgeResponse | BaseException
ScriptItem = Scripted | Callable[[JudgeRequest], Scripted]


class ScriptExhausted(AssertionError):
    """A `ScriptedJudgeModel` was called more often than scripted."""


class ScriptedJudgeModel:
    """Returns the scripted outcomes in order: a JSON text (`str`, sent as is, so invalid JSON
    can be scripted), an answer (`dict` or `JudgeAnswer`, dumped to JSON), a full `JudgeResponse`,
    or an exception to raise (e.g. `ModelError`, `UsageLimitError`); a callable gets the request
    and returns one of these. `requests` records every call; a call past the script raises
    `ScriptExhausted`."""

    def __init__(
        self,
        script: Iterable[ScriptItem] = (),
        *,
        name: str = "scripted",
        options: Mapping[str, Any] | None = None,
    ) -> None:
        self._name = name
        self._options = dict(options or {})
        self._script: deque[ScriptItem] = deque(script)
        self.requests: list[JudgeRequest] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def options(self) -> Mapping[str, Any]:
        return self._options

    def push(self, *items: ScriptItem) -> None:
        self._script.extend(items)

    @property
    def remaining(self) -> int:
        return len(self._script)

    async def judge(self, request: JudgeRequest) -> JudgeResponse:
        self.requests.append(request)
        if not self._script:
            raise ScriptExhausted(f"{self._name}: call {len(self.requests)} was not scripted")
        item = self._script.popleft()
        if callable(item) and not isinstance(item, BaseException):
            item = item(request)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, JudgeResponse):
            return item
        if isinstance(item, JudgeAnswer):
            text = item.model_dump_json()
        elif isinstance(item, str):
            text = item
        else:
            text = json.dumps(dict(item))
        return response_for(request, text, prefix=self._name)


def _float(pattern: re.Pattern[str], text: str, default: float) -> float:
    m = pattern.search(text)
    return float(m.group(1)) if m else default
