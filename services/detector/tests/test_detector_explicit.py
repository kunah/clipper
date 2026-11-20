"""Explicit signals (pure): clip mentions, clip links, viewer-clip clusters, own candidates."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from clipper_core.contracts import CandidateStatus, DelaySource, MomentType, SuppressReason
from clipper_detector.explicit import (
    CLIP_MENTIONS,
    VIEWER_CLIPS,
    ClipTracker,
    MentionTracker,
    ViewerClip,
    clip_links,
    explicit_features,
    from_vod_later,
    is_mention,
    merge_data,
    signal_draft,
    total_boost,
)
from clipper_detector.lexicon import Lexicon
from clipper_detector.merge import Delay, PriorCandidate
from clipper_detector.params import DetectorParams
from clipper_detector.service import final_end
from clipper_detector.settings import DetectorSettings

P = DetectorParams()
CHAN = Lexicon.load().for_channel("testchan")
T0 = datetime(2026, 11, 20, 12, tzinfo=UTC)


@dataclass
class Msg:
    sent_at: datetime
    user_login: str
    text: str
    deleted: bool = False
    msg_id: str = "m"
    user_id: str | None = None


def at(s: float) -> datetime:
    return T0 + timedelta(seconds=s)


MENTIONS = (
    "CLIP IT",
    "clip that",
    "someone clip this",
    "CLIIIP IT",
    "clipit",
    "clip_it",
    "clip-it KEKW",
    "who clipped that",
    "!clip",
    "CLIP",
    "clip?",
    "CLIPPP!",
    "don\u2019t care, clip it",  # the negation is in another clause
)
NOT_MENTIONS = (
    "nice clip",
    "that clip is old",
    "watch the clip",
    "next clip pls",
    "Clip of the week",
    "don't clip that",
    "dont clip this",
    "not clipped",
    "youtube.com/clip/abc",
    "https://clips.twitch.tv/AbcDef-123",
    "clips are fun",
    "eclipse",
    "clipboard",
    "the clipper",
    "#clip",
)


def test_mention_text():
    for text in MENTIONS:
        assert is_mention(text), text
    for text in NOT_MENTIONS:
        assert not is_mention(text), text


def test_clip_links():
    text = "lol https://clips.twitch.tv/FunnySlug-abc123 and twitch.tv/xqc/clip/Other_Slug-9"
    assert clip_links(text) == ["FunnySlug-abc123", "Other_Slug-9"]
    assert clip_links("clips.twitch.tv/embed?clip=EmbedSlug") == ["EmbedSlug"]
    assert clip_links("https://m.twitch.tv/clip/MobileSlug") == ["MobileSlug"]
    assert clip_links("www.twitch.tv/xqc/clip/Slug?filter=clips&range=7d") == ["Slug"]
    for text in ("clips.twitch.tv/embed", "clips.twitch.tv/create", "twitch.tv/xqc/clips"):
        assert clip_links(text) == [], text
    assert clip_links("no link here, just a clip") == []


def test_mentions_need_three_users_and_three_percent():
    tracker = MentionTracker(P, CHAN)
    # 194 chatters + the 6 mentioning users = 200 clean uniques in the window: 3 % = 6 needed
    for i in range(194):
        assert tracker.add(Msg(at(i * 0.1), f"u{i}", "hello")) is None
    signals = [tracker.add(Msg(at(21 + k), f"m{k}", "CLIP IT")) for k in range(6)]
    assert signals[:5] == [None] * 5
    sig = signals[5]
    assert sig is not None and sig.kind == CLIP_MENTIONS and not sig.video
    assert sig.at == at(21) - timedelta(seconds=P.mention_lead_s)
    assert sig.data["users"] == 6 and sig.data["clean_uniques"] == 200
    # more mentions of the same moment don't start another signal
    assert tracker.add(Msg(at(30), "m9", "clip it")) is None


def test_mentions_ignore_bots_deleted_and_repeats():
    tracker = MentionTracker(P, CHAN)
    bot = next(iter(CHAN.bots))
    assert tracker.add(Msg(at(0), bot, "clip it")) is None
    assert tracker.add(Msg(at(1), "a", "clip it", deleted=True)) is None
    assert tracker.add(Msg(at(2), "b", "clip it")) is None
    assert tracker.add(Msg(at(3), "b", "CLIP IT")) is None  # the same user again
    assert tracker.add(Msg(at(4), "c", "clip that")) is None
    sig = tracker.add(Msg(at(5), "d", "!clip"))
    assert sig is not None and sig.data["users"] == 3
    # mentions further apart than the 30 s window don't add up
    t2 = MentionTracker(P, CHAN)
    assert [t2.add(Msg(at(100 + 20 * k), f"x{k}", "clip it")) for k in range(3)] == [None] * 3


def clip(cid: str, creator: str, created_s: float, duration: float = 30.0) -> ViewerClip:
    return ViewerClip(cid, creator, at(created_s), duration, "helix")


def test_viewer_clips_two_creators():
    tracker = ClipTracker(P)
    assert tracker.add(clip("a", "alice", 100)) is None
    assert tracker.add(clip("a", "alice", 100)) is None  # known id
    assert tracker.add(clip("b", "alice", 110)) is None  # same creator
    sig = tracker.add(clip("c", "bob", 112))
    assert sig is not None and sig.kind == VIEWER_CLIPS and sig.video
    # common part of the spans [created - 35, created - 5]: [112 - 35, 100 - 5] = [77, 95]
    assert sig.at == at(86) and sig.start == at(65) and sig.end == at(107)
    assert sig.data["creators"] == 2 and [c["id"] for c in sig.data["clips"]] == ["a", "b", "c"]
    assert "c" in tracker
    # far apart: no cluster
    far = ClipTracker(P)
    assert far.add(clip("x", "alice", 100)) is None
    assert far.add(clip("y", "bob", 300)) is None


def test_busy_channel_needs_three_creators():
    tracker = ClipTracker(P)
    for k in range(P.clip_busy_per_hour + 1):  # a clip every 2 min from one creator
        tracker.add(clip(f"old{k}", "spammer", -3000 + 120 * k))
    assert tracker.add(clip("a", "alice", 100)) is None
    assert tracker.add(clip("b", "bob", 105)) is None
    sig = tracker.add(clip("c", "carol", 108))
    assert sig is not None and sig.data["creators"] == 3


def test_merge_and_boost():
    tracker = MentionTracker(P, CHAN)
    sig = None
    for k in range(3):
        sig = tracker.add(Msg(at(k), f"u{k}", "clip it"))
    t = ClipTracker(P)
    t.add(clip("a", "alice", 10))
    clips = t.add(clip("b", "bob", 12))
    assert total_boost([sig, sig, clips], P) == P.boost_clip_mentions + P.boost_viewer_clip
    feats = explicit_features([sig, clips])
    assert set(feats) == {CLIP_MENTIONS, VIEWER_CLIPS}
    more = merge_data(CLIP_MENTIONS, feats[CLIP_MENTIONS], {**sig.data, "users": 9})
    assert more["users"] == 9 and more["first"] == sig.data["first"]
    union = merge_data(VIEWER_CLIPS, clips.data, {"clips": [clip("z", "zed", 14).as_data()]})
    assert union["creators"] == 3 and len(union["clips"]) == 3


def test_signal_draft_times():
    tracker = MentionTracker(P, CHAN)
    sig = None
    for k in range(3):
        sig = tracker.add(Msg(at(100 + k), f"u{k}", "clip it"))
    delay = Delay(8.0, DelaySource.DEFAULT)
    draft = signal_draft(
        sig, stream_id=1, channel_id=2, delay=delay, params=P, detector_version="d1-x"
    )
    assert draft.status is CandidateStatus.EMITTED and draft.trigger == CLIP_MENTIONS
    assert draft.chat_peak_utc == at(95) and draft.peak_utc == at(87)
    assert draft.window_from == at(87 - 90) and draft.window_to == at(87 + 30)
    assert draft.window_from <= draft.moment_from <= draft.peak_utc <= draft.moment_to
    assert draft.moment_type is MomentType.UNKNOWN and draft.score == P.boost_clip_mentions
    assert draft.features["explicit"][CLIP_MENTIONS]["users"] == 3

    t = ClipTracker(P)
    t.add(clip("a", "alice", 100))
    clips = t.add(clip("b", "bob", 104))
    draft = signal_draft(
        clips, stream_id=1, channel_id=2, delay=delay, params=P, detector_version="d1-x"
    )
    assert draft.chat_peak_utc is None and draft.peak_utc == clips.at  # video time
    assert draft.chat_end_utc == clips.end + timedelta(seconds=8)


def test_signal_only_candidates_have_their_own_cap():
    delay = Delay(8.0, DelaySource.DEFAULT)
    tracker = MentionTracker(P, CHAN)
    sig = None
    for k in range(3):
        sig = tracker.add(Msg(at(3000 + k), f"u{k}", "clip it"))
    own = [
        PriorCandidate(k, CandidateStatus.EMITTED, at(600 * k), at(600 * k + 5), 2.0, CLIP_MENTIONS)
        for k in range(P.signal_budget_per_hour)
    ]
    chat = [PriorCandidate(99, CandidateStatus.EMITTED, at(2900), at(2910), 9.0)]  # not counted
    kw = {"stream_id": 1, "channel_id": 2, "delay": delay, "params": P, "detector_version": "x"}
    draft = signal_draft(sig, recent=own + chat, **kw)
    assert draft.status is CandidateStatus.SUPPRESSED
    assert draft.suppress_reason is SuppressReason.BUDGET
    assert draft.features["budget"]["signal_in_window"] == P.signal_budget_per_hour
    assert signal_draft(sig, recent=own[1:] + chat, **kw).emitted


def test_clips_cut_from_the_vod_later():
    started = at(0)
    # a live clip: made at 1000 s, 30 s long -> vod_offset ~ 970
    assert not from_vod_later(at(1000), 30, None, started, P)
    assert not from_vod_later(at(1000), 30, 975, started, P)
    assert from_vod_later(at(1000), 30, 100, started, P)  # an older moment, clipped from the VOD


def test_settings_validate_lease_timing():
    DetectorSettings(_env_file=None)
    with pytest.raises(ValidationError, match="lease_renew_s"):
        DetectorSettings(_env_file=None, lease_renew_s=60)
    with pytest.raises(ValidationError, match="sync_interval_s"):
        DetectorSettings(_env_file=None, lease_renew_s=30, sync_interval_s=30)


def test_final_end():
    lag = 10
    assert final_end(at(20), lag) == at(10)
    assert final_end(at(20.3), lag) == at(11)  # second 10 is final: 10 < 10.3
