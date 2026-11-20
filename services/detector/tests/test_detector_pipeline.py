"""Scorer -> stage 2 -> merge end to end on synthetic chat (DESIGN §18 build item 2 "Done")."""

import random
import re
from datetime import UTC, datetime, timedelta

import pytest

from clipper_core.contracts import CandidateStatus, MomentType, SuppressReason
from clipper_detector.lexicon import Lexicon
from clipper_detector.merge import resolve_delay
from clipper_detector.params import DetectorParams, detector_version
from clipper_detector.pipeline import run_stream
from clipper_detector.synthetic import ChatSim, bump

P = DetectorParams()
LEXICON = Lexicon.load()
CHAN = LEXICON.for_channel("testchan")
VERSION = detector_version(P, LEXICON)
T0 = datetime(2026, 11, 20, 12, tzinfo=UTC)
CENTER = 1800
EMOTES = frozenset({"KEKW", "LUL", "Pog", "PogChamp", "peepoHey", "xqcSkip", "OMEGALUL"})
NORMAL = ("hello there", "what game is this", "nice", "ok", "LUL", "gg", "true", "based", "xqcSkip")


def chat(seed: int = 0, duration_s: int = 2400) -> ChatSim:
    sim = ChatSim(random.Random(seed), T0, emote_names=EMOTES)
    sim.chatter(0, duration_s, 5, NORMAL)
    return sim


def burst(sim: ChatSim, texts, center: int = CENTER, factor: float = 4.0, **kw):
    """15 s x`factor` raised-cosine burst on top of the 5/s chat, peaking at `center`."""
    return sim.chatter(center - 15, 30, lambda i: 5 * (bump(i, 15, 15, factor) - 1), texts, **kw)


def run(sim: ChatSim, *, events: bool = True, viewers=lambda i: 3000, **kw):
    return run_stream(
        sim.seconds(viewers=viewers),
        sim.messages,
        sim.events if events else None,
        params=P,
        lexicon=CHAN,
        stream_id=1,
        channel_id=1,
        delay=resolve_delay(None, [], P),
        detector_version=VERSION,
        started_at=T0 - timedelta(hours=1),
        **kw,
    )


def off(ts: datetime) -> float:
    return (ts - T0).total_seconds()


@pytest.mark.parametrize("seed", range(3))
def test_noise_gives_nothing(seed):
    out = run(chat(seed, 3600))
    assert out.drafts == [] and out.episodes == [] and out.missing == []


@pytest.mark.parametrize("seed", range(4))
def test_burst_gives_one_candidate(seed):
    sim = chat(seed)
    burst(sim, ("KEKW", "KEKW KEKW", "LMAOOO", "OMEGALUL", "KEKW what"))
    out = run(sim)
    (draft,) = out.drafts
    assert draft.status is CandidateStatus.EMITTED and draft.suppress_reason is None
    assert abs(off(draft.chat_peak_utc) - CENTER) <= 2
    assert draft.peak_utc == draft.chat_peak_utc - timedelta(seconds=8)
    assert draft.moment_type is MomentType.FUNNY
    assert draft.window_from <= draft.moment_from <= draft.peak_utc <= draft.moment_to
    assert draft.moment_to <= draft.window_to
    assert draft.detector_version == VERSION
    assert re.fullmatch(r"d\d+-p[0-9a-f]{8}-l\d+\.[0-9a-f]{6}", VERSION)
    assert draft.features["viewers"] == 3000


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("raid", SuppressReason.RAID),
        ("mass_gift", SuppressReason.SUBS),
        ("copypasta", SuppressReason.COPYPASTA),
        ("enter", SuppressReason.COMMANDS),
        ("votes", SuppressReason.VOTE),
        ("greeting", SuppressReason.GREETING),
    ],
)
def test_fake_spikes_are_suppressed_with_the_right_reason(scenario, reason):
    sim = chat(1)
    if scenario == "raid":
        sim.event(CENTER - 12, "raid", user_login="bigraider", viewerCount="3000")
        texts = ("bigraiderRaid bigraiderRaid", "raid hype", "here from the raid", "hello")
    elif scenario == "mass_gift":
        sim.event(CENTER - 10, "submysterygift", user_login="rich", **{"mass-gift-count": "50"})
        for k in range(50):
            sim.event(CENTER - 10 + k * 0.1, "subgift", user_login="rich")
        texts = ("thanks for the gifted", "ty rich", "rich W", "gifted")
    elif scenario == "copypasta":
        texts = ("this streamer is the best in the world and nobody can tell me otherwise",)
    elif scenario == "enter":
        texts = ("!enter",)
    elif scenario == "votes":
        texts = ("1", "2")
    else:
        texts = ("hi", "hello", "peepoHey", "hey chat", "hiii", "yo")
    burst(sim, texts)
    out = run(sim)
    (draft,) = out.drafts
    assert draft.status is CandidateStatus.SUPPRESSED
    assert draft.suppress_reason is reason
    assert draft.features["rules"][0] == reason.value
    assert abs(off(draft.chat_peak_utc) - CENTER) <= 3


def test_backfill_events_unknown():
    """No chat_events (backfill): event rules off, the raid's copypasta still caught by dup."""
    sim = chat(2)
    sim.event(CENTER - 12, "raid", user_login="bigraider", viewerCount="3000")
    burst(sim, ("raid hype", "here from the raid", "hello raiders"))
    (draft,) = run(sim, events=False, viewers=lambda i: None).drafts
    assert draft.emitted and draft.features["events"]["known"] is False
    assert draft.features["viewer_ratio"] is None and draft.features["viewers"] is None

    sim = chat(2)
    burst(sim, ("bigraiderRaid bigraiderRaid bigraiderRaid",))
    (draft,) = run(sim, events=False, viewers=lambda i: None).drafts
    assert draft.suppress_reason is SuppressReason.COPYPASTA


def test_cooldown_folds_a_weaker_burst_into_the_first():
    sim = chat(3)
    burst(sim, ("KEKW", "OMEGALUL", "LMAO"), center=CENTER, factor=5)
    burst(sim, ("KEKW", "OMEGALUL"), center=CENTER + 45, factor=2.6)
    out = run(sim)
    assert len(out.episodes) == 2
    (draft,) = out.drafts
    (fold,) = out.folded
    assert fold.into.candidate_id == 0 and draft.features["followups"] == [fold.followup]
    assert abs(off(datetime.fromisoformat(fold.followup["peak"])) - CENTER - 45) <= 3


def test_a_burst_every_45_s_stays_one_candidate():
    """Each fold moves the cooldown to the folded burst's close: without that every other
    burst (90 s after the first close) would be emitted."""
    sim = chat(6, 2600)
    burst(sim, ("KEKW", "OMEGALUL", "LMAO"), center=CENTER, factor=6)
    for k in range(1, 6):
        burst(sim, ("KEKW", "OMEGALUL"), center=CENTER + 45 * k, factor=3.2)
    out = run(sim)
    assert len(out.episodes) == 6
    (draft,) = out.drafts
    assert len(out.folded) == 5 and len(draft.features["followups"]) == 5


def test_budget_caps_emitted_candidates_per_hour():
    sim = chat(4, 1200 + 3600)
    centers = [1300 + 300 * k for k in range(11)]  # 11 moments 5 min apart in one hour
    for k, center in enumerate(centers):  # each weaker than the one before
        burst(sim, ("KEKW", "OMEGALUL", "LMAO"), center=center, factor=6.0 - 0.2 * k)
    out = run(sim)
    assert len(out.drafts) == len(centers)
    budget = [d for d in out.drafts if d.suppress_reason is SuppressReason.BUDGET]
    assert len(out.emitted) >= P.budget_per_hour and budget
    for d in budget:  # each was no better than the 8 emitted in the hour before it
        window = [
            e
            for e in out.emitted
            if d.chat_peak_utc - timedelta(hours=1) < e.chat_peak_utc <= d.chat_peak_utc
        ]
        assert len(window) >= P.budget_per_hour and d.score <= min(e.score for e in window)


def test_missing_chat_run_is_reported_and_harmless():
    sim = ChatSim(random.Random(5), T0, emote_names=EMOTES)
    sim.chatter(0, 1800, 5, NORMAL)
    sim.chatter(2700, 1200, 5, NORMAL)  # chat-ingest down for 15 min
    out = run(sim)
    assert out.drafts == [] and len(out.missing) == 1
    assert abs(off(out.missing[0].start) - 1800) <= 2 and abs(off(out.missing[0].end) - 2700) <= 2
