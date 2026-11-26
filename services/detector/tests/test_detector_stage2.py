"""Stage 2 on hand-made episodes: shares, clean uniques, score, moment type, fake-spike rules."""

import json
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from clipper_core.contracts import CandidateStatus, MomentType, SuppressReason
from clipper_detector.lexicon import Category, Lexicon
from clipper_detector.params import DetectorParams
from clipper_detector.stage2 import evaluate, moment_type, windows
from clipper_detector.synthetic import ChatSim, episode

P = DetectorParams()
LEX = Lexicon.load().for_channel("testchan")
T0 = datetime(2026, 11, 20, 12, tzinfo=UTC)
ONSET_S = 100
EP = episode(T0 + timedelta(seconds=ONSET_S))  # peak +4 s, z = (150 - 50) / 7
EMOTES = frozenset(
    {"KEKW", "LUL", "Pog", "PogChamp", "peepoHey", "xqcSkip", "Sadge", "monkaS", "Kappa"}
    | {"pokiShock"}
)
NORMAL = ("hello there", "what game is this", "nice", "ok", "true", "based", "who is that")
R = SuppressReason


def sim(seed: int = 0, *, normal: bool = True) -> ChatSim:
    s = ChatSim(random.Random(seed), T0, emote_names=EMOTES)
    if normal:
        s.chatter(0, 200, 5, NORMAL)
    return s


def burst(s: ChatSim, texts, *, users=None, rate: float = 15, deleted: float = 0.0):
    return s.chatter(ONSET_S, 10, rate, texts, users=users, deleted=deleted)


def run(s: ChatSim, *, events: bool = True, ep=EP, **kw):
    return evaluate(ep, s.messages, s.events if events else None, lexicon=LEX, params=P, **kw)


def test_windows():
    w = windows(EP, P)
    assert w.messages.start == EP.onset - timedelta(seconds=20)
    assert w.messages.end == EP.peak + timedelta(seconds=6)  # through the end of peak + 5 s
    assert w.events.start == EP.onset - timedelta(seconds=60)
    assert w.events.end == w.messages.end


def test_windows_cover_a_late_strongest_second():
    """A plateau: the strongest z comes after peak + 5 s; its whole activity window is read."""
    ep = episode(T0 + timedelta(seconds=ONSET_S))
    late = replace(ep, strongest=replace(ep.strongest, ts=ep.peak + timedelta(seconds=9)))
    w = windows(late, P)
    assert w.messages.end == late.strongest.ts + timedelta(seconds=1)
    assert w.messages.start == ep.onset - timedelta(seconds=20)
    early = replace(
        ep, onset=ep.onset + timedelta(seconds=40), peak=ep.peak + timedelta(seconds=40)
    )
    w = windows(early, P)  # strongest 31 s before the onset (a long episode)
    assert w.messages.start == ep.strongest.ts - timedelta(seconds=9)
    assert w.events.start <= w.messages.start


def test_clean_fraction_uses_the_whole_activity_window():
    """Bots only after peak + 5 s, inside the strongest second's window: they count."""
    ep = episode(T0 + timedelta(seconds=ONSET_S))
    late = replace(ep, strongest=replace(ep.strongest, ts=ep.peak + timedelta(seconds=12)))
    s = sim(normal=False)
    s.chatter(ONSET_S, 30, 10, ("KEKW",))
    bots = ["nightbot", "streamelements", "fossabot", "moobot", "wizebot"]
    s.chatter(ONSET_S + 10, 7, 5, lambda rng: f"bot {rng.randrange(10**6)}", users=bots)
    r = run(s, ep=late)
    assert r.clean_fraction < 0.8


def test_real_reaction_is_emitted_funny():
    s = sim()
    burst(s, ("KEKW", "KEKW KEKW", "LMAOOO", "OMEGALUL", "KEKW what was that"))
    r = run(s)
    assert r.status is CandidateStatus.EMITTED and r.reason is None and r.rules == ()
    assert r.moment_type is MomentType.FUNNY
    assert r.shares.reaction > 0.6 and r.shares.dup < 0.2  # mass KEKW is no copypasta
    assert 0.9 < r.clean_fraction <= 1.0
    assert r.z_clean == pytest.approx((150 * r.clean_fraction - 50) / 7)
    assert r.score == pytest.approx(r.z_clean * (0.6 + 0.8 * 0.5))  # reaction capped at 0.5
    assert r.top_emotes[0][0] == "KEKW"
    features = r.features()
    json.dumps(features)
    assert features["categories"]["funny"] > 0 and features["events"]["known"] is True


def test_boost_adds_to_the_score():
    s = sim()
    burst(s, ("KEKW",))
    assert run(s, boost=2.0).score == pytest.approx(run(s).score + 2.0)


def test_raid():
    s = sim()
    s.event(ONSET_S - 10, "raid", user_login="bigraider", viewerCount="3000")
    burst(s, ("bigraiderRaid", "raid hype", "here from the raid", "hello from the raid"))
    r = run(s)
    assert r.reason is R.RAID and r.status is CandidateStatus.SUPPRESSED
    assert (r.events.raids, r.events.raid_viewers) == (1, 3000)
    # events unknown (backfill): the event rules are off
    unknown = run(s, events=False)
    assert unknown.events.known is False and R.RAID not in unknown.rules


def test_raid_too_early_or_kept_for_a_strong_reaction():
    s = sim()
    s.event(ONSET_S - 45, "raid", user_login="old", viewerCount="10")  # before onset - 30 s
    burst(s, ("here from the raid", "raid hype", "hello"))
    assert R.RAID not in run(s).rules

    s = sim()
    s.event(ONSET_S - 10, "raid", user_login="bigraider", viewerCount="3000")
    burst(s, ("KEKW", "OMEGALUL", "LMAO"))  # a real moment: z_clean >= 8, reaction >= 0.5
    r = run(s)
    assert r.events.raids == 1 and R.RAID not in r.rules and r.emitted


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (lambda s: s.event(ONSET_S - 5, "submysterygift", **{"mass-gift-count": "5"}), True),
        (lambda s: s.event(ONSET_S - 5, "submysterygift", **{"mass-gift-count": "4"}), False),
        (lambda s: [s.event(ONSET_S - 5 + i, "subgift") for i in range(10)], True),
        (lambda s: [s.event(ONSET_S - 5 + i, "resub") for i in range(9)], False),
        (lambda s: [s.event(ONSET_S - 25 + i, "sub") for i in range(10)], False),  # too early
    ],
)
def test_subs(setup, expected):
    s = sim()
    setup(s)
    burst(s, ("thanks for the gift", "ty", "gifted", "W"))
    assert (R.SUBS in run(s).rules) is expected
    assert R.SUBS not in run(s, events=False).rules


def test_bits_live_and_from_text():
    s = sim()
    rows = burst(s, ("nice one", "here take my bits", "cheering"))
    for row in rows[::2]:  # half of the burst cheers
        s.event((row.sent_at - T0).total_seconds(), "bits", msg_id=row.msg_id, bits=100)
    r = run(s)
    assert R.BITS in r.rules and r.events.bits_total >= 100 * (len(rows) // 2)
    assert r.features()["events"]["bits_total"] == r.events.bits_total

    s = sim()
    burst(s, ("Cheer100 nice", "Cheer500", "BibleThump100 lets go"))
    r = run(s, events=False)  # backfill: bits from the text
    assert R.BITS in r.rules and r.events.bits_total >= 1000 and not r.events.known
    assert r.features()["bits_big"] is True


def test_commands_votes_copypasta_greeting():
    s = sim()
    burst(s, ("!enter",))
    r = run(s)
    assert r.reason is R.COMMANDS and r.z_clean < 0  # commands aren't clean uniques
    for texts in [("!!!", "!!!!", "!!!! KEKW", "!"), ("! KEKW", "!!! LETS GO", "!!", "KEKW")]:
        s = sim()
        burst(s, texts)  # exclamation floods are reactions, not commands
        r = run(s)
        assert r.shares.command == 0 and R.COMMANDS not in r.rules, texts

    s = sim()
    burst(s, ("1", "2"))
    assert run(s).reason is R.VOTE

    s = sim()
    burst(s, ("this streamer is the best in the world KEKW and nobody can tell me otherwise",))
    r = run(s)
    assert r.reason is R.COPYPASTA and r.dup_text.startswith("this streamer")

    s = sim()
    burst(s, ("hi", "hello", "peepoHey", "hey chat", "hiii"))
    r = run(s)
    assert r.reason is R.GREETING and r.shares.greeting > 0.4
    assert r.shares.dup < 0.4  # greeting-only messages aren't duplicates


def test_w_l_f_floods_are_reactions_not_votes():
    s = sim()
    burst(s, ("W", "WWW", "L", "F"))
    r = run(s)
    assert r.shares.vote < 0.1 and r.emitted


@pytest.mark.parametrize("texts", [(":D", ":O", "D:"), ("D", "o", "O", "xD", "P"), (":P",)])
def test_emoticons_are_no_votes(texts):
    s = sim()
    burst(s, texts)
    r = run(s)
    assert r.shares.vote == 0 and R.VOTE not in r.rules


def test_votes_with_trailing_punctuation():
    s = sim()
    burst(s, ("1!", "2.", "1", "A", "b"))
    assert run(s).reason is R.VOTE


@pytest.mark.parametrize(
    "texts",
    [
        ("pokiShock",),
        ("Kappa",),
        ("pokiShock pokiShock",),
        ("?",),
        ("???", "????", "?"),
        (":D",),
        ("D", "o", "O"),
        (":O :O", "xD"),
    ],
)
def test_emote_and_punctuation_floods_are_no_copypasta(texts):
    s = sim()
    burst(s, texts)
    r = run(s)
    assert r.shares.dup < 0.2 and R.COPYPASTA not in r.rules, (r.shares, r.dup_text)


def test_greetings_inside_reactions_dont_count():
    s = sim()
    burst(s, ("YO WHAT", "YO WTF", "hey OMEGALUL", "hi KEKW", "YOOOO"))
    r = run(s)
    assert r.shares.greeting < 0.1 and R.GREETING not in r.rules and r.emitted


def test_bots():
    s = sim()
    bots = ["nightbot", "streamelements", "pokemoncommunitygame", "fossabot", "moobot"]
    burst(s, lambda rng: f"a wild thing number {rng.randrange(10**6)} appeared", users=bots)
    r = run(s)
    assert r.reason is R.BOTS and r.shares.bot > 0.3


def test_mode_toggle():
    s = sim()
    s.event(ONSET_S - 5, "roomstate", **{"emote-only": "1"})
    burst(s, ("KEKW", "Pog"))
    assert run(s).reason is R.MODE_TOGGLE
    assert R.MODE_TOGGLE not in run(s, events=False).rules

    s = sim()
    s.event(ONSET_S - 15, "roomstate", **{"emote-only": "0"})  # before onset - 10 s
    burst(s, ("KEKW", "Pog"))
    assert R.MODE_TOGGLE not in run(s).rules


def test_spam_wave_deleted_timeouts_clearchat():
    s = sim()
    burst(s, ("buy followers at example", "free stuff here", "visit my site"), deleted=0.6)
    r = run(s)
    assert r.reason is R.SPAM_WAVE and r.shares.deleted > 0.3

    s = sim()
    spammers = [f"spam{i}" for i in range(40)]
    burst(s, ("buy followers", "free stuff", "visit my site"), users=spammers)
    for i, login in enumerate(spammers):
        s.event(ONSET_S + 5 + i * 0.1, "timeout", user_login=login, duration_s=600)
    r = run(s)
    assert r.reason is R.SPAM_WAVE and r.shares.deleted == 0 and r.events.moderations == 40
    assert R.SPAM_WAVE not in run(s, events=False).rules  # timeouts unknown in backfill

    s = sim()
    burst(s, ("buy followers", "free stuff", "visit my site"))
    s.event(ONSET_S + 9, "clearchat")  # a mod clears the chat: everything before it is gone
    assert run(s).reason is R.SPAM_WAVE


def test_mixed_noise_is_a_weak_spike():
    """Bots, commands and deleted messages, each below its rule: z_clean decides."""
    s = sim()
    bots = ["nightbot", "streamelements", "fossabot", "moobot"]
    burst(s, lambda rng: f"bot line {rng.randrange(10**6)}", users=bots, rate=4)
    burst(s, lambda rng: f"!cmd{rng.randrange(10**6)}", rate=4)
    burst(s, lambda rng: f"spam {rng.randrange(10**6)}", rate=4, deleted=1.0)
    r = run(s)
    assert r.rules == () and r.reason is R.WEAK and r.status is CandidateStatus.SUPPRESSED
    assert r.clean_fraction < 0.6 and r.z_clean < P.z_clean_emit


def test_a_users_repeats_stay_clean():
    """Many users each typing KEKW every second: a real moment, not a weak one."""
    s = sim()
    fans = [f"fan{i}" for i in range(40)]
    burst(s, ("KEKW",), users=fans, rate=40)
    r = run(s)
    assert r.clean_fraction > 0.95 and r.emitted and r.moment_type is MomentType.FUNNY


def test_warmup_needs_z_clean_6():
    ep = episode(T0 + timedelta(seconds=ONSET_S), activity=85)  # z = 5
    s = sim()
    burst(s, ("KEKW", "LMAO", "OMEGALUL"))
    normal = run(s, ep=ep)
    assert 4 <= normal.z_clean < 6 and normal.emitted
    warm = run(s, ep=episode(T0 + timedelta(seconds=ONSET_S), activity=85, warmup=True))
    assert warm.reason is R.WEAK and warm.warmup


def test_empty_window():
    r = run(sim(normal=False))
    assert r.messages == 0 and r.clean_fraction == 1.0 and r.moment_type is MomentType.UNKNOWN
    assert r.shares.reaction == 0 and r.emitted  # nothing to contradict stage 1


def test_rows_outside_the_window_are_ignored():
    s = sim(normal=False)
    s.chatter(ONSET_S - 40, 10, 20, ("!enter",))  # before onset - 20 s
    s.chatter(ONSET_S + 30, 10, 20, ("!enter",))  # after peak + 5 s
    s.event(ONSET_S - 90, "raid", viewerCount="5")  # before the events window
    r = run(s)
    assert r.messages == 0 and r.events.raids == 0


# --------------------------------------------------------------------------- moment type


def votes_of(*messages):
    """`(user, text)` pairs in the burst, no other chat."""
    s = sim(normal=False)
    for i, (user, text) in enumerate(messages):
        s.message(ONSET_S + i * 0.05, text, user)
    return run(s)


def test_moment_type_one_vote_per_user():
    funny = [(f"u{i}", "KEKW") for i in range(6)]
    spam = [("hyper", f"Pog {i}") for i in range(20)]  # one user, many messages: one vote
    r = votes_of(*funny, *spam)
    assert r.votes == {"funny": 6.0, "hype": 1.0}
    assert r.moment_type is MomentType.FUNNY


def test_moment_type_mixed_unknown_and_non_moment_categories():
    mixed = [(f"a{i}", "KEKW") for i in range(3)] + [(f"b{i}", "Pog") for i in range(3)]
    mixed += [(f"c{i}", "monkaS") for i in range(3)]
    assert votes_of(*mixed).moment_type is MomentType.MIXED  # top share 1/3
    assert votes_of(*[(f"u{i}", "KEKW") for i in range(4)]).moment_type is MomentType.UNKNOWN
    greetings = [(f"g{i}", "peepoHey") for i in range(6)] + [("x", "KEKW")]
    assert votes_of(*greetings).moment_type is MomentType.MIXED  # greeting wins -> mixed
    unknown_emotes = [(f"e{i}", "xqcSkip") for i in range(6)]
    r = votes_of(*unknown_emotes)
    assert r.votes == {"uncategorised": 6.0} and r.moment_type is MomentType.MIXED
    patterns = [(f"p{i}", "KEKWait") for i in range(9)]  # 0.5 each
    assert votes_of(*patterns).votes == {"funny": 4.5}
    assert votes_of(*patterns).moment_type is MomentType.UNKNOWN  # 4.5 < 5 votes


def test_moment_type_function():
    C = Category
    assert moment_type({}, P) is MomentType.UNKNOWN
    assert moment_type({C.SAD: 5.0}, P) is MomentType.SAD
    # top share 0.4, a tie: the first category in lexicon order
    assert moment_type({C.SAD: 4.0, C.HYPE: 4.0, C.TENSE: 2.0}, P) is MomentType.HYPE
    assert moment_type({C.SAD: 3.0, C.HYPE: 3.0, C.TENSE: 2.0, C.FUNNY: 2.0}, P) is MomentType.MIXED
