"""Resolver repairs (DESIGN §19, 6b): snapping, holes in the video, lengths, never a cut inside a
word, and every repair of §19's list. Times in the comments are seconds relative to T; the sample
transcript's sentences: S5 -24.0..-23.0, S6 -21.0..-18.95, S7 -16.0..-14.65, S8 -12.4..-9.65,
S9 -6.0..-3.95, S10 -2.0..+0.05, S11 +1.0..+3.4, S12 +6.0..+8.4, S13 +12.0..+13.35; video
[-90, -40] (a jitter hole at -70) and [-25, +35]; context [-90, +30]."""

from dataclasses import replace
from datetime import timedelta

import pytest

from clipper_core.contracts import (
    ClipRejectReason,
    EditPlan,
    HookMode,
    MusicMood,
    MusicPlan,
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
from clipper_judge.params import JudgeParams
from clipper_judge.resolver import (
    OPERATOR_MODEL,
    PatchRefused,
    Planned,
    Rejected,
    Repair,
    Unrepairable,
    resolve,
    resolve_patch,
)
from clipper_judge.samples import CandidateRow, T, at, sample_inputs, script_words, span
from clipper_judge.transcript import Transcript

P = JudgeParams()


def answer(in_s: float = -11.5, out_s: float = 4.0, **plan) -> JudgeAnswer:
    plan.setdefault("title", "He jumped off the map")
    return JudgeAnswer(
        verdict="clip",
        reasons=["he jumps off the map"],
        confidence=0.8,
        plan=PlanAnswer(in_s=in_s, out_s=out_s, **plan),
    )


def run(a: JudgeAnswer, inputs=None, params: JudgeParams = P):
    return resolve(a, inputs or sample_inputs(), params, model="fake", prompt_version="v1-test")


def planned(a: JudgeAnswer, inputs=None, params: JudgeParams = P) -> EditPlan:
    res = run(a, inputs, params)
    assert isinstance(res, Planned), res
    return res.plan


def rel(t) -> float:
    return round((t - T).total_seconds(), 3)


def segs(plan: EditPlan) -> list[tuple[float, float]]:
    return [(rel(s.start_utc), rel(s.end_utc)) for s in plan.segments]


def assert_no_cut_inside_a_word(plan: EditPlan, words) -> None:
    for seg in plan.segments:
        for edge in (seg.start_utc, seg.end_utc):
            assert not [w for w in words if w.start_utc < edge < w.end_utc], rel(edge)


def with_video(*ranges: tuple[float, float], **kw):
    return sample_inputs(video=tuple(span(a, b) for a, b in ranges), **kw)


# --------------------------------------------------------------------------- snapping


def test_snap_to_sentence_edges_with_pads():
    plan = planned(answer(-11.5, 4.0))
    # in: S8 starts -12.4 (0.9 away) - 0.15; out: S11 ends 3.4 (0.6 away) + 0.3
    assert segs(plan) == [(-12.55, 3.7)]
    assert plan.repairs == [Repair.SNAP_IN, Repair.SNAP_OUT]
    assert plan.duration == timedelta(seconds=16.25)


def test_in_pad_not_before_the_previous_words_end():
    words = [
        TranscriptWord("first.", at(-3.0), at(-2.0), 0.9, 1, False),
        TranscriptWord("second", at(-1.95), at(-1.5), 0.9, 1, False),
        TranscriptWord("part.", at(-1.45), at(9.0), 0.9, 1, False),
    ]
    plan = planned(answer(-1.5, 9.0), sample_inputs(words=words), JudgeParams(min_clip_s=3))
    assert segs(plan)[0][0] == -2.0  # -1.95 - 0.15 would cut into "first."


def test_out_pad_not_after_the_next_words_start():
    words = [
        TranscriptWord("done.", at(4.0), at(5.0), 0.9, 1, False),
        TranscriptWord("next", at(5.1), at(5.4), 0.9, 1, False),
    ]
    plan = planned(answer(-6.0, 5.2), sample_inputs(words=words))
    assert segs(plan)[0][1] == 5.1  # 5.0 + 0.3 would cut into "next"


def test_in_inside_a_word_snaps_to_a_word_edge():
    # -86.4 lies in S1 (no sentence start within 1.5 s): the nearest word start is "from"
    # (-86.25); its pad stops at the end of "back" (-86.3); out -74 is silence: unsnapped
    plan = planned(answer(-86.4, -74.0))
    assert segs(plan) == [(-86.3, -74.0)]
    assert Repair.SNAP_IN in plan.repairs and Repair.UNSNAPPED_OUT in plan.repairs
    assert_no_cut_inside_a_word(plan, sample_inputs().words)


def test_out_inside_a_word_snaps_to_a_word_end():
    # 6.5 lies in the 2nd "okay" of S12 (no sentence end within 1.5 s): its end 6.65 + 0.3,
    # capped by the next word's start (6.7)
    plan = planned(answer(-6.5, 6.5))
    assert segs(plan) == [(-6.15, 6.7)]
    assert_no_cut_inside_a_word(plan, sample_inputs().words)


def test_unsnapped_in_silence():
    plan = planned(answer(-52.0, -41.0))
    assert segs(plan) == [(-52.0, -41.0)]
    assert Repair.UNSNAPPED_IN in plan.repairs and Repair.UNSNAPPED_OUT in plan.repairs


def test_swapped_in_and_out():
    plan = planned(answer(4.0, -11.5))
    assert segs(plan) == [(-12.55, 3.7)]
    assert Repair.SWAP_IN_OUT in plan.repairs


# --------------------------------------------------------------------------- holes, media


def test_cut_across_a_short_hole_is_split():
    # a 5 s hole [-20.5, -15.5]: S6's "is" and S7's 2nd "no" are cut by it
    inputs = with_video((-90.0, -20.5), (-15.5, 35.0))
    plan = planned(answer(-24.5, 4.0), inputs)
    assert segs(plan) == [(-24.15, -20.65), (-15.35, 3.7)]
    assert Repair.SPLIT_GAP in plan.repairs and Repair.GAP_EDGE_WORD in plan.repairs
    assert_no_cut_inside_a_word(plan, inputs.words)


def test_cut_across_a_long_hole_shrinks_to_the_side_of_t():
    # the ad [-40, -25] is 15 s long: only [-25, ...] is kept, its new in snapped to S5
    plan = planned(answer(-50.0, 4.0))
    assert segs(plan) == [(-24.15, 3.7)]
    assert Repair.SHRINK_GAP in plan.repairs and Repair.SNAP_IN in plan.repairs


def test_short_pieces_next_to_a_hole_are_dropped():
    # a 0.5 s island between 1 s holes; its end moves out of S5's 2nd "wait": 0.35 s left
    inputs = with_video((-90.0, -25.0), (-24.0, -23.5), (-22.5, 35.0))
    plan = planned(answer(-30.0, 4.0), inputs)
    assert segs(plan) == [(-30.0, -25.0), (-22.5, 3.7)]
    assert Repair.DROP_SHORT_SEGMENT in plan.repairs


def test_jitter_hole_is_bridged():
    plan = planned(answer(-80.0, -50.0))  # the 0.3 s hole at -70 lies inside
    assert segs(plan) == [(-80.0, -50.0)]
    assert Repair.MERGE_SEGMENTS in plan.repairs


def test_touching_video_ranges_make_one_segment():
    inputs = with_video((-90.0, -10.0), (-10.0, 35.0))
    plan = planned(answer(-11.5, 4.0), inputs)
    assert segs(plan) == [(-12.55, 3.7)]


def test_cut_outside_the_context_is_clamped():
    plan = planned(answer(-200.0, -75.0))
    assert segs(plan) == [(-90.0, -75.65)]
    assert Repair.CLAMP_CONTEXT in plan.repairs


def test_cut_entirely_outside_the_context_takes_the_core():
    plan = planned(answer(100.0, 140.0))
    assert segs(plan) == [(-12.55, 8.7)]  # core [-11, +9], snapped
    assert Repair.CLAMP_CONTEXT in plan.repairs


def test_cut_inside_a_hole_moves_to_the_video():
    # [-38, -30] lies in the ad: moved to -40 (the nearer range), then grown to S4's start
    plan = planned(answer(-38.0, -30.0))
    assert segs(plan) == [(-60.15, -40.0)]
    assert Repair.MOVE_TO_MEDIA in plan.repairs and Repair.EXTEND_SHORT in plan.repairs
    assert "copyright_music_risk" in plan.flags  # music 0.72 at -60.6..-56.6


def test_edge_in_a_hole_is_clamped_to_the_video():
    plan = planned(answer(-30.0, 4.0))
    assert segs(plan) == [(-24.15, 3.7)]
    assert Repair.CLAMP_VIDEO in plan.repairs


def test_no_video_is_rejected():
    res = run(answer(), with_video())
    assert isinstance(res, Rejected) and res.reason is ClipRejectReason.NO_VIDEO


def test_less_than_three_seconds_of_video_is_rejected():
    res = run(answer(), with_video((-1.0, 1.0)))
    assert isinstance(res, Rejected) and res.reason is ClipRejectReason.NO_VIDEO


def test_short_video_is_kept_below_the_minimum():
    plan = planned(answer(), with_video((-2.5, 2.5)))
    assert 3 <= plan.duration.total_seconds() < 10
    assert Repair.SHORT_MEDIA in plan.repairs


# --------------------------------------------------------------------------- lengths


def test_too_short_is_extended_by_sentences():
    # S9 alone (2.5 s): T lies after the cut, so the out side grows first, sentence by sentence
    plan = planned(answer(-5.9, -4.0))
    assert segs(plan) == [(-6.15, 8.7)]
    assert Repair.EXTEND_SHORT in plan.repairs
    assert 10 <= plan.duration.total_seconds() <= 60


def test_extension_stays_below_the_maximum():
    plan = planned(answer(-5.9, -4.0), params=JudgeParams(min_clip_s=12, max_clip_s=12.5))
    assert 12 <= plan.duration.total_seconds() <= 12.5
    assert_no_cut_inside_a_word(plan, sample_inputs().words)


def continuous_inputs(**kw):
    """Context and video [-200, +30], a 3-word sentence every 5 s."""
    script = tuple((float(s), "one two three.", ()) for s in range(-200, 30, 5))
    context = span(-200.0, 30.0)
    fields = {
        "candidate": CandidateRow(window_from=at(-200.0)),
        "context": context,
        "words": script_words(script),
        "video": (context,),
        "coverage": Coverage(context, (context,), (), (), (), ()),
    }
    return sample_inputs(**{**fields, **kw})


def test_too_long_trims_the_side_farther_from_t():
    inputs = continuous_inputs()
    plan = planned(answer(-150.0, 20.0), inputs)
    ((start, end),) = segs(plan)
    assert end == 21.3  # the near side keeps its snapped edge
    assert -40.0 < start < -38.0
    assert plan.duration.total_seconds() <= 60
    assert Repair.TRIM_LONG in plan.repairs
    assert_no_cut_inside_a_word(plan, inputs.words)


def test_too_long_on_both_sides_trims_both():
    inputs = continuous_inputs()
    plan = planned(answer(-150.0, 20.0), inputs, JudgeParams(min_clip_s=10, max_clip_s=20))
    ((start, end),) = segs(plan)
    assert start < 0 < end and plan.duration.total_seconds() <= 20
    assert abs(abs(start) - end) < 3  # evened out around T


# --------------------------------------------------------------------------- the rest


def test_sfx_repairs():
    cues = [
        SfxAnswer(at_s=-10.0, tag="vine_boom"),
        SfxAnswer(at_s=-9.0, tag="vine_boom"),  # < 2 s after the previous
        SfxAnswer(at_s=-5.0, tag="airhorn"),  # not in the manifest
        SfxAnswer(at_s=3.7, tag="whoosh"),  # at the out point
        SfxAnswer(at_s=10.0, tag="whoosh"),  # after it
        *[SfxAnswer(at_s=float(t), tag="record_scratch") for t in (-8, -6, -4, -2, 0, 2)],
    ]
    plan = planned(answer(sfx=cues))
    assert [rel(c.at_utc) for c in plan.sfx] == [-10.0, -8.0, -6.0, -4.0, -2.0, 0.0]  # max 6
    for code in (Repair.SFX_CLOSE, Repair.SFX_UNKNOWN, Repair.SFX_OUTSIDE, Repair.SFX_CAP):
        assert code in plan.repairs


def test_hashtag_repairs():
    tags = ["#a", "##b", "two words", "", "A", "a", "c", "d", "e", "f"]
    plan = planned(answer(hashtags=tags))
    assert plan.hashtags == ["a", "b", "c", "d", "e"]
    for code in (Repair.HASHTAG_STRIP, Repair.HASHTAG_DROP, Repair.HASHTAG_CAP):
        assert code in plan.repairs


def test_long_hook_text_is_cut_at_a_word():
    text = "This is a very long hook text that goes on and on forever"
    plan = planned(answer(hook=HookAnswer(mode=HookMode.TEXT, text=text)))
    assert plan.hook.mode is HookMode.TEXT
    cut = plan.hook.text
    assert cut and len(cut) <= 40 and text.startswith(cut) and text[len(cut)] == " "
    assert Repair.HOOK_TEXT_TRIM in plan.repairs


@pytest.mark.parametrize("text", [None, "", "   ", "\n\t"])
def test_empty_hook_text_means_no_hook(text):
    plan = planned(answer(hook=HookAnswer(mode=HookMode.TEXT, text=text)))
    assert plan.hook.mode is HookMode.NONE and Repair.HOOK_NONE in plan.repairs


def test_teaser_partly_outside_its_segment_is_clamped():
    # clip [-12.55, 3.7]; -10.5 lies in "and" (-10.65..-10.35): the end moves to its end
    plan = planned(answer(hook=HookAnswer(mode=HookMode.TEASER, from_s=-13.5, to_s=-10.5)))
    assert plan.hook.mode is HookMode.TEASER
    assert (rel(plan.hook.start_utc), rel(plan.hook.end_utc)) == (-12.55, -10.35)
    assert Repair.TEASER_CLAMP in plan.repairs


def test_teaser_longer_than_three_seconds_is_cut():
    plan = planned(answer(hook=HookAnswer(mode=HookMode.TEASER, from_s=-6.0, to_s=0.0)))
    assert (rel(plan.hook.start_utc), rel(plan.hook.end_utc)) == (-6.0, -3.0)


@pytest.mark.parametrize(
    "hook",
    [
        HookAnswer(mode=HookMode.TEASER, from_s=-6.0, to_s=-5.8),  # 0.2 s
        HookAnswer(mode=HookMode.TEASER, from_s=-6.0),  # no end
        HookAnswer(mode=HookMode.TEASER, from_s=20.0, to_s=22.0),  # after the clip
    ],
)
def test_teaser_that_cant_be_repaired_means_no_hook(hook):
    plan = planned(answer(hook=hook))
    assert plan.hook.mode is HookMode.NONE and Repair.HOOK_NONE in plan.repairs


def test_emphasis_resolves_to_real_words():
    marks = [
        EmphasisAnswer(sentence="S9", word="jumped", kind="big"),
        EmphasisAnswer(sentence="S12", word="clip"),  # after the out point (3.7)
        EmphasisAnswer(sentence="S9", word="banana"),  # not in S9
        EmphasisAnswer(sentence="S99", word="map"),  # no such sentence
        EmphasisAnswer(sentence="s11", word="Funniest!"),
        EmphasisAnswer(sentence="10", word="cant"),
    ]
    plan = planned(answer(emphasis=marks))
    assert [(rel(e.start_utc), e.text, e.kind) for e in plan.emphasis] == [
        (-5.3, "jumped", "big"),
        (-1.65, "can't", "highlight"),
        (1.7, "funniest", "highlight"),
    ]
    assert Repair.EMPHASIS_DROP in plan.repairs


def test_emphasis_at_most_one_per_three_words():
    inputs = sample_inputs()
    sentences = Transcript(inputs.words, inputs.context, pause_s=1.0, max_s=30.0).sentences
    marks = [
        EmphasisAnswer(sentence=s.id, word=w.text)
        for s in sentences
        if s.id in ("S8", "S9", "S10", "S11")
        for w in s.words
    ]
    plan = planned(answer(emphasis=marks))
    assert len(plan.emphasis) == 27 // 3  # 27 words in [-12.55, 3.7]
    assert Repair.EMPHASIS_CAP in plan.repairs


def test_zoom_across_a_cut_is_split():
    inputs = with_video((-90.0, -20.5), (-15.5, 35.0))
    zooms = [
        ZoomAnswer(from_s=-22.0, to_s=-10.0, scale=1.3),
        ZoomAnswer(from_s=-21.0, to_s=-15.0),  # 0.35 s on each side of the hole
    ]
    plan = planned(answer(-24.5, 4.0, zooms=zooms), inputs)
    assert [(rel(z.start_utc), rel(z.end_utc)) for z in plan.zooms] == [
        (-22.0, -20.65),
        (-15.35, -10.0),
    ]
    assert Repair.ZOOM_SPLIT in plan.repairs and Repair.ZOOM_DROP in plan.repairs


def test_zoom_scale_and_overlap():
    zooms = [
        ZoomAnswer(from_s=-10.0, to_s=-6.0, scale=2.0),
        ZoomAnswer(from_s=-8.0, to_s=-4.0, scale=1.2, target="face"),
        ZoomAnswer(from_s=2.0, to_s=0.0, scale=0.8),  # reversed
    ]
    plan = planned(answer(zooms=zooms))
    assert [(rel(z.start_utc), rel(z.end_utc), z.scale, z.target) for z in plan.zooms] == [
        (-10.0, -4.0, 1.5, "center"),
        (0.0, 2.0, 1.0, "center"),
    ]
    assert Repair.ZOOM_SCALE in plan.repairs and Repair.ZOOM_MERGE in plan.repairs


@pytest.mark.parametrize("at_s, kept", [(-5.0, True), (3.7, False), (20.0, False)])
def test_thumbnail_outside_the_clip_is_dropped(at_s, kept):
    plan = planned(answer(thumbnail_s=at_s))
    assert (plan.thumbnail_utc is not None) is kept
    assert (Repair.THUMBNAIL_DROP in plan.repairs) is not kept


@pytest.mark.parametrize("title", ["", "   ", "	"])
def test_blank_title_comes_from_the_core(title):
    plan = planned(answer(title=title))
    assert plan.title == "so I went in there and he just"  # S8 overlaps the core [-11, +9]
    assert Repair.TITLE_FROM_TRANSCRIPT in plan.repairs


def test_long_title_is_cut_at_a_word():
    title = "word " * 40
    plan = planned(answer(title=title))
    assert len(plan.title) <= 100 and plan.title.endswith("word")
    assert Repair.TITLE_TRIM in plan.repairs


def test_title_without_transcript_falls_back():
    plan = planned(answer(title=""), sample_inputs(words=()))
    assert plan.title == "xqc clip" and Repair.TITLE_FALLBACK in plan.repairs


def test_title_text_is_cleaned():
    hidden = chr(0x202E) + chr(0x200B) + chr(0xE0001)  # a bidi override, zero-width, a tag
    plan = planned(answer(title="  line one\nline <two> I <3 this" + hidden + "  "))
    assert plan.title == "line one line <two> I <3 this"  # no fence escaping in plan texts
    hook = HookAnswer(mode=HookMode.TEXT, text="I <3 this" + hidden)
    assert planned(answer(hook=hook)).hook.text == "I <3 this"
    res = resolve_patch(plan, PlanPatch(title="xqc <3 jump king"), sample_inputs(), P)
    assert res.plan.title == "xqc <3 jump king"


def test_blocklisted_title_is_flagged_sensitive():
    params = JudgeParams(title_blocklist=("slur",))
    assert "sensitive" in planned(answer(title="What a SLUR!"), params=params).flags
    assert "sensitive" not in planned(answer(title="What a play"), params=params).flags


def test_music_in_the_clip_flags_copyright_risk():
    assert "copyright_music_risk" in planned(answer(-62.0, -50.0)).flags
    assert "copyright_music_risk" not in planned(answer()).flags


def test_partial_context_flag_is_copied():
    plan = planned(answer(), sample_inputs(flags=("partial_context",)))
    assert plan.flags == ["partial_context"]


def test_clamps():
    a = answer(layout=LayoutAnswer(kind="full_crop", focus_x=-0.2))
    plan = planned(a.model_copy(update={"confidence": 1.7}))
    assert plan.confidence == 1.0 and plan.layout.focus_x == 0.0
    assert Repair.CONFIDENCE_CLAMP in plan.repairs and Repair.FOCUS_CLAMP in plan.repairs


def test_clip_without_a_plan_uses_the_core():
    plan = planned(JudgeAnswer(verdict="clip", reasons=["funny"]))
    assert segs(plan) == [(-12.55, 8.7)]
    assert plan.title == "so I went in there and he just"
    assert Repair.DEFAULT_PLAN in plan.repairs


def test_plan_fields():
    a = answer(music_mood=MusicMood.FUNNY, moment_type="hype", caption_style="karaoke")
    plan = planned(a)
    assert plan.music == MusicPlan(mood=MusicMood.FUNNY)
    assert plan.moment_type == "hype" and plan.caption_style == "karaoke"
    assert plan.anchor_utc == T and plan.clip_id == 12 and plan.stream_id == 7
    assert plan.model == "fake" and plan.prompt_version == "v1-test"
    assert plan.reasons == ["he jumps off the map"]
    assert planned(answer()).moment_type == "funny"  # the candidate's


def test_reject_verdict():
    res = run(JudgeAnswer(verdict="reject", reasons=["only a raid"], confidence=0.9))
    assert isinstance(res, Rejected) and res.reason is ClipRejectReason.MODEL
    assert res.reasons == ("only a raid",) and res.confidence == 0.9


# --------------------------------------------------------------------------- patches


def base_plan() -> EditPlan:
    return planned(answer(-11.5, 4.0, music_mood=MusicMood.FUNNY))


def patch(base: EditPlan, inputs=None, **fields):
    return resolve_patch(base, PlanPatch(**fields), inputs or sample_inputs(), P)


def test_patch_word_fixes_only_for_existing_words():
    base = base_plan()
    jumped = at(-5.3)
    res = patch(
        base,
        word_fixes=[
            WordFix(start_utc=jumped + timedelta(microseconds=400), text="JUMPED"),  # rounding
            WordFix(start_utc=jumped, text="again"),  # the same word twice
            WordFix(start_utc=at(-5.2), text="nope"),  # inside a word, not its start
            WordFix(start_utc=at(6.0), text="okay"),  # after the out point
        ],
    )
    assert isinstance(res, Planned)
    assert [(f.start_utc, f.text) for f in res.plan.word_fixes] == [(jumped, "JUMPED")]
    assert Repair.WORD_FIX_DROP in res.plan.repairs
    assert res.plan.model == OPERATOR_MODEL


def test_patch_moves_only_its_edges():
    base = base_plan()
    res = patch(base, out_utc=at(10.2))  # silence: no word within 1.5 s
    assert isinstance(res, Planned)
    assert segs(res.plan) == [(-12.55, 10.2)]
    assert Repair.UNSNAPPED_OUT in res.plan.repairs
    assert Repair.SNAP_IN not in res.plan.repairs  # the base's in point stays as it was


def test_patch_snaps_a_moved_edge():
    res = patch(base_plan(), in_utc=at(-21.5))
    assert segs(res.plan) == [(-21.15, 3.7)]  # S6 starts -21.0


def test_patch_uses_the_contract_lengths():
    inputs = continuous_inputs()
    base = planned(answer(-30.0, 20.0), inputs)
    res = patch(base, inputs, in_utc=at(-120.0))
    assert isinstance(res, Planned)
    assert 120 < res.plan.duration.total_seconds() <= 180
    assert Repair.TRIM_LONG not in res.plan.repairs


def test_patch_fields():
    base = base_plan()
    res = patch(
        base,
        title="New title",
        no_music=True,
        hook={"mode": "text", "text": "watch this"},
        layout={"kind": "blurred_fit"},
    )
    plan = res.plan
    assert plan.title == "New title" and plan.music is None
    assert plan.hook.text == "watch this" and plan.layout.kind == "blurred_fit"
    assert plan.segments == base.segments and plan.reasons == base.reasons


def test_patch_recomputes_derived_flags():
    base = base_plan().model_copy(update={"flags": ["sensitive", "partial_context"]})
    res = patch(base, title="fine title")
    assert res.plan.flags == ["partial_context"]


@pytest.mark.parametrize(
    "video", [(), ((-1.0, 1.0),), ((-12.0, -11.0), (-9.0, -8.0))], ids=["none", "2s", "islands"]
)
def test_a_patch_that_cant_be_cut_is_refused_never_rejected(video):
    res = patch(base_plan(), with_video(*video))
    assert isinstance(res, PatchRefused) and "less than 3 s" in res.error


def test_inputs_replace_keeps_validation():
    with pytest.raises(ValueError):
        replace(sample_inputs(), round=-1)
    assert Interval(at(0), at(1)).seconds == 1


# --------------------------------------------------------------------------- speech, review fixes


def word(text: str, a: float, b: float) -> TranscriptWord:
    return TranscriptWord(text, at(a), at(b), 0.9, 1, False)


def test_overlapping_and_touching_words_are_one_block():
    # "this" overlaps "so", "is" touches "this": no clean cut from -4.0 to -2.0
    words = [
        word("so", -4.0, -3.0),
        word("this", -3.2, -2.4),
        word("is", -2.4, -2.0),
        word("it.", -1.9, -1.5),
        word("next", 0.5, 1.0),
        word("one.", 1.1, 9.0),
    ]
    inputs = sample_inputs(words=words)
    blocks = Transcript(words, inputs.context, pause_s=1.0, max_s=30.0).blocks
    assert [(rel(a), rel(b)) for a, b in blocks] == [
        (-4.0, -2.0),
        (-1.9, -1.5),
        (0.5, 1.0),
        (1.1, 9.0),
    ]
    # -2.2: the nearest word start is "is" (-2.4), a touch point inside the block: its nearer
    # edge (-2.0) instead
    plan = planned(answer(-2.2, 9.0), inputs, JudgeParams(min_clip_s=3))
    assert segs(plan)[0][0] == -2.0
    assert_no_cut_inside_a_word(plan, words)


def test_edges_inside_overlapping_words_never_flip_flop():
    # a chain of overlapping words around the in point: one move to a block edge, no loop
    words = [word(f"w{i}", -10.0 + 0.5 * i, -9.2 + 0.5 * i) for i in range(12)]  # -10.0 .. -3.7
    words.append(word("end.", -1.0, 9.0))
    plan = planned(answer(-7.0, 9.0), sample_inputs(words=words))
    assert segs(plan)[0][0] in (-10.0, -3.7)
    assert_no_cut_inside_a_word(plan, words)


def test_a_tiny_piece_left_by_trimming_is_dropped():
    # trimming 99.3 s to 60 s leaves 0.5 s of the first piece before a 1 s hole: dropped
    inputs = continuous_inputs(words=(), video=(span(-200.0, -40.2), span(-39.2, 20.299)))
    plan = planned(answer(-80.0, 25.0), inputs)
    assert segs(plan) == [(-39.2, 20.299)]
    assert Repair.TRIM_LONG in plan.repairs and Repair.DROP_SHORT_SEGMENT in plan.repairs


def test_a_cut_in_a_small_island_falls_back_to_the_core():
    # the model's cut lands in a 2 s video island; there is plenty of video around T
    inputs = with_video((-90.0, -60.0), (-50.0, -48.0), (-25.0, 35.0))
    plan = planned(answer(-51.0, -47.0), inputs)
    assert segs(plan) == [(-12.55, 8.7)]  # the core [-11, +9], snapped
    assert Repair.CORE_FALLBACK in plan.repairs


def test_no_cut_with_video_near_the_moment_is_unrepairable():
    # only islands < 3 s near the moment, but 3 s of video in all: let the model choose again
    inputs = with_video((-30.0, -28.0), (-20.0, -18.5), (10.0, 11.5))
    res = run(answer(-11.5, 4.0), inputs)
    assert isinstance(res, Unrepairable) and not res.final
    assert "inside one of the video ranges" in res.error
