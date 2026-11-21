"""Judge tunables shared by the input builder and the resolver (DESIGN §19).

One frozen object, so the input tells the model the same limits the resolver enforces. The
service's settings (`CLIPPER_JUDGE_*`, build item 6c) map onto it. All values are provisional;
the prompt and clip lengths get tuned on real candidates.
"""

from __future__ import annotations

from dataclasses import dataclass

from clipper_core.contracts import CLIP_MAX, CLIP_MIN, TEASER_MAX


@dataclass(frozen=True, slots=True)
class JudgeParams:
    # clip length the judge aims at (the contract allows CLIP_MIN..CLIP_MAX, 3-180 s)
    min_clip_s: float = 10.0
    max_clip_s: float = 60.0
    # context rounds: a round < max_rounds may ask for more context (0-based rounds, so with 2
    # the model may ask in rounds 0 and 1 and must decide in round 2), within T - max_before_s
    # .. T + max_after_s
    max_rounds: int = 2
    max_before_s: float = 600.0
    max_after_s: float = 120.0
    max_output_tokens: int = 4000

    # ---- input document
    chat_bucket_s: float = 5.0  # chat summary per bucket (aligned on T)
    top_n: int = 3  # top emotes / messages per bucket
    message_chars: int = 80  # a top message is cut to this length
    low_confidence: float = 0.5  # words below it are marked "?"
    sentence_pause_s: float = 1.0  # sentences: the transcriber's rule (. ? !, pauses, length)
    sentence_max_s: float = 30.0
    silence_note_s: float = 5.0  # transcribed stretches without words this long get a line
    sound_min: float = 0.5  # a sound group this strong makes a second notable
    loud_db: float = 6.0  # ... or loudness this far above the context's median
    music_risk: float = 0.6  # music this strong = Content ID risk
    core_pad_s: float = 5.0  # core = [moment_from - pad, moment_to + pad]

    # ---- resolver: cut points
    snap_window_s: float = 1.5  # a sentence (else word) edge this close is snapped to
    in_pad_s: float = 0.15  # in = edge - pad (not before the previous word's end)
    out_pad_s: float = 0.3  # out = edge + pad (not after the next word's start)
    split_gap_max_s: float = 10.0  # a longer hole in the video is not jumped: T's side is kept
    min_segment_s: float = 1.0  # shorter pieces next to a hole are dropped
    # ---- resolver: the rest of the plan
    teaser_min_s: float = 1.0
    hook_text_max: int = 40
    title_max: int = 100
    hashtags_max: int = 5
    words_per_emphasis: int = 3  # at most one emphasised word per this many words
    zoom_max_scale: float = 1.5
    zoom_min_s: float = 0.5
    sfx_min_gap_s: float = 2.0
    sfx_max: int = 6
    reasons_max: int = 10
    title_blocklist: tuple[str, ...] = ()  # whole words (case-insensitive) -> flag `sensitive`

    def __post_init__(self) -> None:
        lo, hi = CLIP_MIN.total_seconds(), CLIP_MAX.total_seconds()
        if not lo <= self.min_clip_s <= self.max_clip_s <= hi:
            raise ValueError(f"need {lo} <= min_clip_s <= max_clip_s <= {hi}")
        if not 0 < self.teaser_min_s <= TEASER_MAX.total_seconds():
            raise ValueError("teaser_min_s must lie in (0, TEASER_MAX]")
        if self.max_rounds < 0 or self.max_before_s < 0 or self.max_after_s < 0:
            raise ValueError("context caps must not be negative")
        if self.hook_text_max > 40 or self.title_max > 100 or self.hashtags_max > 5:
            raise ValueError("hook text, title and hashtag limits can't exceed the EditPlan's")
        if not 1.0 <= self.zoom_max_scale <= 1.6:
            raise ValueError("zoom_max_scale must lie in [1.0, 1.6] (EditPlan)")
        if self.chat_bucket_s <= 0 or self.words_per_emphasis < 1:
            raise ValueError("chat_bucket_s must be > 0 and words_per_emphasis >= 1")


DEFAULT_PARAMS = JudgeParams()
