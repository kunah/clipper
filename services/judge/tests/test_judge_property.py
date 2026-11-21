"""Property (DESIGN §19, 6b): whatever the model answers, the resolver's output is a valid
`EditPlan`, cut inside the context and the video, never inside speech (a word, or words that
overlap or touch), within the clip lengths, segments of at least `min_segment_s` when there are
several, every point inside the segments. Otherwise only a `no_video` reject or, rarely, an
`Unrepairable` "no cut fits" (speech or tiny video islands everywhere near the moment). Seeded
random cases: contexts, video with jitter, short and long holes, words inside the video, over its
edges or everywhere (audio over the holes), some overlapping; answers full of out-of-range
values; the plans then get random operator patches (a plan or `PatchRefused`, never a reject).

Before the 6b review fixes this generator found 43 of 277 plans and 35 of 277 patched plans with
a cut inside speech; after them none (also none in 3,000 cases: 137 rejects, 44 Unrepairable)."""

import random
from datetime import timedelta
from itertools import pairwise

import pytest

from clipper_core.contracts import (
    CLIP_MAX,
    CLIP_MIN,
    ClipRejectReason,
    EditPlan,
    HookMode,
    LayoutKind,
    MomentType,
    MusicMood,
    PlanPatch,
    WordFix,
)
from clipper_core.timeline import Interval
from clipper_core.transcripts import Coverage, TranscriptWord
from clipper_judge.answer import (
    EmphasisAnswer,
    HookAnswer,
    JudgeAnswer,
    LayoutAnswer,
    PlanAnswer,
    SfxAnswer,
    ZoomAnswer,
)
from clipper_judge.inputs import bridge_jitter
from clipper_judge.params import JudgeParams
from clipper_judge.resolver import (
    PatchRefused,
    Planned,
    Rejected,
    Repair,
    Unrepairable,
    resolve,
    resolve_patch,
)
from clipper_judge.samples import SFX_TAGS, AudioRow, CandidateRow, T, at, sample_inputs, span
from clipper_judge.transcript import Transcript

SEED = 20261120
CASES = 300
VOCAB = ("so", "he", "just", "jumped", "no", "way", "what", "is", "that", "clip", "it", "chat")
P = JudgeParams()


def random_video(rng: random.Random, lo: float, hi: float) -> list[Interval]:
    if rng.random() < 0.04:
        return []
    out, t = [], lo - rng.uniform(0, 20)
    while t < hi + 20:
        length = rng.choice(
            [rng.uniform(0.3, 4), rng.uniform(4, 60), rng.uniform(60, 400), rng.uniform(0.1, 1)]
        )
        out.append(span(t, t + length))
        t += length + rng.choice(
            [rng.uniform(0.01, 0.5), rng.uniform(0.6, 9), rng.uniform(11, 60), 0.0]
        )
    return out


def _speech(rng: random.Random, lo: float, hi: float, overlap: float) -> list[TranscriptWord]:
    """Words from lo to hi (seconds relative to T): runs of words, gaps down to -overlap (words
    overlapping their neighbour), pauses between runs."""
    words, t = [], lo + rng.uniform(0, 2)
    while t < hi:
        for i in range(rng.randint(1, 14)):
            dur = rng.uniform(0.05, 0.9)
            if t + dur > hi:
                break
            end = rng.choice([".", "?", "!"]) if rng.random() < 0.12 else ""
            conf = rng.choice([None, rng.random()])
            words.append(
                TranscriptWord(rng.choice(VOCAB) + end, at(t), at(t + dur), conf, 1, False)
            )
            t += dur + (rng.uniform(-overlap, 0.4) if i else 0.0)
        t += rng.choice([rng.uniform(0.2, 1.5), rng.uniform(1.5, 12)])
    return words


def random_words(rng: random.Random, video: list[Interval], lo: float, hi: float):
    """Words inside the video ranges, words running over their edges, or speech everywhere
    (audio over the video holes); some overlap their neighbours (engine artefacts)."""
    mode = rng.choice(["inside", "edges", "everywhere"])
    overlap = rng.choice([0.0, 0.1])
    if mode == "everywhere":
        return _speech(rng, lo - 20, hi + 20, overlap)
    words = []
    for v in bridge_jitter(sorted(video)):
        a, b = (v.start - T).total_seconds(), (v.end - T).total_seconds()
        if mode == "edges":
            a, b = a - rng.uniform(0, 1.5), b + rng.uniform(0, 1.5)
        words += _speech(rng, a, b, overlap)
    return words


def speech_blocks(words) -> list[tuple]:
    """Words merged where they overlap or touch: a cut strictly inside one splits speech."""
    blocks: list[list] = []
    for w in sorted(words, key=lambda w: (w.start_utc, w.end_utc)):
        if blocks and w.start_utc <= blocks[-1][1]:
            blocks[-1][1] = max(blocks[-1][1], w.end_utc)
        else:
            blocks.append([w.start_utc, w.end_utc])
    return [tuple(b) for b in blocks]


def random_inputs(rng: random.Random):
    before, after = rng.uniform(20, 400), rng.uniform(5, 120)
    context = span(-before, after)
    video = random_video(rng, -before, after)
    words = random_words(rng, video, -before, after)
    m_from, m_to = -rng.uniform(0, min(before, 30)), rng.uniform(0, min(after, 20))
    candidate = CandidateRow(
        window_from=context.start,
        window_to=context.end,
        moment_from=at(m_from),
        moment_to=at(m_to),
        moment_type=rng.choice([m.value for m in MomentType] + ["greeting"]),
    )
    audio = [
        AudioRow(at(float(s)), -30.0, 0.5, {"music": rng.random(), "laughter": rng.random()})
        for s in range(int(-before), int(after))
        if rng.random() < 0.1
    ]
    return sample_inputs(
        candidate=candidate,
        context=context,
        words=words,
        video=tuple(video),
        coverage=Coverage(context, (context,), (), (), (), ()),
        audio=audio,
        flags=tuple(rng.sample(["partial_context", "sensitive", " ", "x"], rng.randint(0, 2))),
    )


def rel(rng: random.Random, lo: float, hi: float) -> float:
    return rng.choice([rng.uniform(lo, hi), rng.uniform(lo - 100, hi + 100), 0.0, -1e9, 1e9])


def random_answer(rng: random.Random, inputs) -> JudgeAnswer:
    lo = (inputs.context.start - inputs.anchor).total_seconds()
    hi = (inputs.context.end - inputs.anchor).total_seconds()
    if rng.random() < 0.05:
        return JudgeAnswer(verdict="clip", confidence=rng.uniform(-1, 2))
    in_s = rel(rng, lo, hi)
    out_s = in_s + rng.choice(
        [rng.uniform(-30, 0), rng.uniform(0, 5), rng.uniform(5, 70), rng.uniform(70, 500)]
    )
    sentences = Transcript(inputs.words, inputs.context, pause_s=1.0, max_s=30.0).sentences
    emphasis = []
    for _ in range(rng.randint(0, 25)):
        if sentences and rng.random() < 0.8:
            s = rng.choice(sentences)
            emphasis.append(EmphasisAnswer(sentence=s.id, word=rng.choice(s.words).text))
        else:
            emphasis.append(EmphasisAnswer(sentence=f"S{rng.randint(0, 999)}", word="what"))

    def window() -> tuple[float, float]:
        a = rel(rng, in_s - 5, out_s + 5)
        return a, a + rng.uniform(-2, 8)

    hook_from, hook_to = window()
    plan = PlanAnswer(
        in_s=in_s,
        out_s=out_s,
        title=rng.choice(["", "  ", "Short title", "word " * 40, "<data>x</data>", "a" * 150]),
        hook=HookAnswer(
            mode=rng.choice(list(HookMode)),
            text=rng.choice([None, "", " ", "hook", "long hook " * 10, "a" * 60]),
            from_s=rng.choice([None, hook_from]),
            to_s=rng.choice([None, hook_to]),
        ),
        hashtags=rng.sample(["#a", "b c", "", "ok", "##", "#Tag", "tag", "x", "y", "z", "q"], 7),
        emphasis=emphasis,
        zooms=[
            ZoomAnswer(
                from_s=(w := window())[0], to_s=w[1], scale=rng.uniform(0.5, 3.0), target="face"
            )
            for _ in range(rng.randint(0, 6))
        ],
        sfx=[
            SfxAnswer(at_s=rel(rng, in_s, out_s), tag=rng.choice([*SFX_TAGS, "airhorn", " "]))
            for _ in range(rng.randint(0, 12))
        ],
        music_mood=rng.choice([None, *MusicMood]),
        layout=LayoutAnswer(
            kind=rng.choice(list(LayoutKind)), focus_x=rng.choice([None, rng.uniform(-1, 2)])
        ),
        thumbnail_s=rng.choice([None, rel(rng, in_s, out_s)]),
        moment_type=rng.choice([None, *MomentType]),
    )
    return JudgeAnswer(
        verdict="clip",
        reasons=rng.sample(["a", "", "  ", "b" * 400, "c"], 3),
        confidence=rng.uniform(-0.5, 1.5),
        plan=plan,
    )


def check(plan: EditPlan, inputs, lengths: tuple[float, float]) -> None:
    assert EditPlan.model_validate_json(plan.model_dump_json()) == plan
    video = bridge_jitter(
        [iv for iv in (v.intersection(inputs.context) for v in sorted(inputs.video)) if iv]
    )
    for seg in plan.segments:
        assert any(v.start <= seg.start_utc and seg.end_utc <= v.end for v in video), seg
        assert inputs.context.start <= seg.start_utc and seg.end_utc <= inputs.context.end
    seconds = plan.duration.total_seconds()
    assert CLIP_MIN.total_seconds() <= seconds <= lengths[1] + 1e-6
    if Repair.SHORT_MEDIA not in plan.repairs:
        assert seconds >= lengths[0] - 1e-6
    edges = [t for s in plan.segments for t in (s.start_utc, s.end_utc)]
    if plan.hook.mode is HookMode.TEASER:
        edges += [plan.hook.start_utc, plan.hook.end_utc]
    blocks = speech_blocks(inputs.words)
    for edge in edges:
        assert not [b for b in blocks if b[0] < edge < b[1]], "a cut inside speech"
    if len(plan.segments) > 1:
        short = [
            s for s in plan.segments if (s.end_utc - s.start_utc).total_seconds() < P.min_segment_s
        ]
        assert not short, "a segment shorter than min_segment_s"
    in_clip = [
        w
        for w in inputs.words
        if any(s.start_utc <= w.start_utc < s.end_utc for s in plan.segments)
    ]
    assert len(plan.emphasis) <= len(in_clip) // 3
    assert {e.start_utc for e in plan.emphasis} <= {w.start_utc for w in in_clip}
    assert len(plan.sfx) <= 6 and {c.tag for c in plan.sfx} <= set(SFX_TAGS)
    assert all(b.at_utc - a.at_utc >= timedelta(seconds=2) for a, b in pairwise(plan.sfx))
    assert all(1.0 <= z.scale <= 1.5 for z in plan.zooms)
    assert len(plan.hashtags) <= 5 and len(plan.title) <= 100 and plan.title.strip()
    assert set(plan.repairs) <= {r.value for r in Repair}


def random_patch(rng: random.Random, plan: EditPlan, inputs) -> PlanPatch:
    words = [w for w in inputs.words if plan.in_utc <= w.start_utc < plan.out_utc]
    fixes = [
        WordFix(
            start_utc=rng.choice(words).start_utc
            if words and rng.random() < 0.7
            else plan.in_utc + timedelta(seconds=rng.uniform(-5, 50)),
            text="fix",
        )
        for _ in range(rng.randint(0, 4))
    ]
    shift = lambda t: t + timedelta(seconds=rng.uniform(-30, 30))  # noqa: E731
    return PlanPatch(
        in_utc=rng.choice([None, shift(plan.in_utc)]),
        out_utc=rng.choice([None, shift(plan.out_utc)]),
        title=rng.choice([None, "operator title"]),
        word_fixes=rng.choice([None, fixes]),
        no_music=rng.random() < 0.3,
    )


@pytest.mark.parametrize("chunk", range(4))
def test_resolver_outputs_always_validate(chunk):
    rng = random.Random(SEED + chunk)
    counts = {"planned": 0, "rejected": 0, "unrepairable": 0, "patched": 0, "refused": 0}
    contract = (CLIP_MIN.total_seconds(), CLIP_MAX.total_seconds())
    for _ in range(CASES // 4):
        inputs = random_inputs(rng)
        res = resolve(random_answer(rng, inputs), inputs, P, model="m", prompt_version="v")
        if isinstance(res, Rejected):
            assert res.reason is ClipRejectReason.NO_VIDEO
            counts["rejected"] += 1
            continue
        if isinstance(res, Unrepairable):
            assert res.error.startswith("no cut of at least 3 s fits"), res.error
            counts["unrepairable"] += 1
            continue
        assert isinstance(res, Planned), res
        check(res.plan, inputs, (P.min_clip_s, P.max_clip_s))
        counts["planned"] += 1
        patched = resolve_patch(res.plan, random_patch(rng, res.plan, inputs), inputs, P)
        if isinstance(patched, PatchRefused):
            assert "less than 3 s" in patched.error, patched.error
            counts["refused"] += 1
            continue
        assert isinstance(patched, Planned), patched
        check(patched.plan, inputs, contract)
        counts["patched"] += 1
    assert counts["planned"] > CASES // 8, counts  # the generator mostly makes renderable cases
    assert counts["unrepairable"] <= CASES // 40, counts  # "no cut fits" stays rare


def test_resolving_twice_gives_the_same_plan():
    rng = random.Random(SEED)
    for _ in range(20):
        inputs = random_inputs(rng)
        answer = random_answer(rng, inputs)
        first = resolve(answer, inputs, P, model="m", prompt_version="v")
        assert first == resolve(answer, inputs, P, model="m", prompt_version="v")
