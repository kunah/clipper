"""`JudgeAnswer` parsing (DESIGN §19): lenient where the resolver repairs, strict where it can't."""

import json

import pytest

from clipper_core.contracts import HookMode, Verdict
from clipper_judge.answer import AnswerError, FinalJudgeAnswer, JudgeAnswer, parse_answer

CLIP = {
    "reasons": ["he jumps off the map", "chat explodes"],
    "verdict": "clip",
    "confidence": 0.8,
    "plan": {
        "in_s": -12.0,
        "out_s": 4.0,
        "title": "x" * 300,  # too long: the resolver trims, parsing doesn't fail
        "hashtags": ["#xqc", "jump king"],
        "hook": {"mode": "text", "text": "y" * 90},
        "zooms": [{"from_s": -6, "to_s": -3, "scale": 4.0}],
        "sfx": [{"at_s": -4, "tag": "no_such_tag"}],
        "emphasis": [{"sentence": "S9", "word": "jumped", "kind": "big"}],
        "layout": {"kind": "full_crop", "focus_x": 3.0},
        "thumbnail_s": -4,
    },
}


def test_parse_clip():
    a = parse_answer(json.dumps(CLIP), final=False)
    assert a.verdict is Verdict.CLIP and a.plan is not None
    assert a.plan.hook.mode is HookMode.TEXT
    assert a.plan.zooms[0].scale == 4.0
    assert a.reasons[0] == "he jumps off the map"


def test_parse_bytes_and_mapping():
    assert parse_answer(json.dumps(CLIP).encode(), final=False) == parse_answer(CLIP, final=False)


def test_unknown_keys_are_ignored():
    data = {**CLIP, "extra": 1, "plan": {**CLIP["plan"], "speaker": "streamer"}}
    assert parse_answer(data, final=False) == parse_answer(CLIP, final=False)


@pytest.mark.parametrize(
    "raw, message",
    [
        ("{not json", "not valid JSON"),
        ("[1, 2]", "one JSON object"),
        ('{"verdict": "maybe"}', "schema"),
        ('{"verdict": "clip", "confidence": NaN}', "schema"),
        ('{"verdict": "clip", "plan": {"in_s": "soon", "out_s": 3}}', "schema"),
        ('{"verdict": "clip", "plan": {"in_s": 1e999, "out_s": 3}}', "schema"),
        (
            '{"verdict": "clip", "plan": {"in_s": 1, "out_s": 3, "caption_style": "comic"}}',
            "schema",
        ),
    ],
)
def test_invalid_answers(raw, message):
    with pytest.raises(AnswerError, match=message):
        parse_answer(raw, final=False)


def test_need_context():
    a = parse_answer(
        {"verdict": "need_context", "need_context": {"before_s": 300, "after_s": 60, "why": "x"}},
        final=False,
    )
    assert a.verdict is Verdict.NEED_CONTEXT
    assert a.need_context is not None and a.need_context.before_s == 300


def test_final_round_cannot_ask_again():
    with pytest.raises(AnswerError):
        parse_answer({"verdict": "need_context"}, final=True)
    a = parse_answer({"verdict": "reject", "reasons": ["meh"], "need_context": {}}, final=True)
    assert isinstance(a, JudgeAnswer)  # converted
    assert a.verdict is Verdict.REJECT and a.need_context is None


def test_final_model_converts():
    final = FinalJudgeAnswer.model_validate(CLIP)
    assert final.to_answer() == parse_answer(CLIP, final=False)
