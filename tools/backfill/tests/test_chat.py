"""Chat replay mapping and the chat clock (pure).

The main check: real (sanitized) replay pages give the same rows and the same per-second
metrics as live chat-ingest gives for the same messages arriving over IRC (tmi-sent-ts =
createdAt, emotes tag = the fragments' positions), through chat-ingest's own code.
"""

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from clipper_backfill.chat import (
    BAND_MID_S,
    SENT_AT_TOLERANCE_S,
    ChatClock,
    chat_spans,
    clock_for,
    map_comment,
    window_for,
)
from clipper_backfill.t0 import ChatPoint, chat_anchor
from clipper_chat_ingest.mapping import ChannelState
from clipper_chat_ingest.service import ChatIngest
from clipper_core.chat import SecondAggregator, second_of
from clipper_core.timeline import Interval
from clipper_twitch.gql import Comment, Fragment, parse_page
from clipper_twitch.irc import parse

FIXTURES = Path(__file__).parents[3] / "libs" / "twitch" / "tests" / "fixtures"
VIDEO = "2932113307"
CHANNEL_EMOTES = {"OOOO": "7tv", "PogU": "7tv", "WW": "bttv", "LOL": "ffz"}
GLOBAL_EMOTES = {"OOOOO": "7tv", "LUL": "bttv", "PogU": "bttv"}
T = datetime(2026, 11, 18, 6, 27, 53, 700000, tzinfo=UTC)


def fixture_comments() -> list[Comment]:
    comments = []
    for name in ("gql_comments_start.json", "gql_comments_mid.json"):
        body = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        comments += parse_page(body, VIDEO).comments
    return comments


def irc_line(c: Comment) -> str:
    """The PRIVMSG live chat-ingest would have received for this comment."""
    text = c.text
    spans: dict[str, list[str]] = {}
    pos = 0
    for f in c.fragments:
        if f.emote_id:
            spans.setdefault(f.emote_id, []).append(f"{pos}-{pos + len(f.text) - 1}")
        pos += len(f.text)
    tags = {
        "badges": ",".join(f"{k}/{v}" for k, v in c.badges),
        "emotes": "/".join(f"{eid}:{','.join(r)}" for eid, r in spans.items()),
        "id": c.id,
        "tmi-sent-ts": str(round(c.created_at.timestamp() * 1000)),
        "user-id": c.commenter_id,
    }
    raw = ";".join(f"{k}={v}" for k, v in tags.items())
    login = c.commenter_login
    return f"@{raw} :{login}!{login}@{login}.tmi.twitch.tv PRIVMSG #xqc :{text}"


def test_fixture_pages_give_live_rows_and_the_live_aggregator_metrics():
    comments = fixture_comments()
    assert len(comments) == 109 and sum(bool(c.twitch_emotes()) for c in comments) == 4
    anchor = chat_anchor([ChatPoint(c.content_offset_s, c.created_at) for c in comments])
    clock = ChatClock(anchor.t0, "chat_anchor")

    backfill_agg = SecondAggregator()
    backfilled = []
    for c in comments:
        row, fallback = map_comment(
            c,
            channel_id=1,
            stream_id=7,
            clock=clock,
            channel_emotes=CHANNEL_EMOTES,
            global_emotes=GLOBAL_EMOTES,
        )
        assert not fallback  # every createdAt is in the 1 s band
        backfilled.append(row)
        backfill_agg.add_row(row)

    live = ChatIngest(sessions=None, worker_id="test", fetch_emotes=False)
    live.channels = {"xqc": ChannelState(1, "xqc", "71092938", 7, CHANNEL_EMOTES)}
    live.global_emotes = GLOBAL_EMOTES
    for c in comments:
        live.on_message(parse(irc_line(c)), c.created_at + timedelta(seconds=1))

    assert len(live.messages) == len(backfilled)
    for got, want in zip(backfilled, live.messages, strict=True):
        same = ("sent_at", "msg_id", "channel_id", "stream_id", "user_id", "user_login", "text")
        assert [getattr(got, k) for k in same] == [getattr(want, k) for k in same]
        assert (got.badges, got.emotes) == (want.badges, want.emotes)
        assert (got.received_at, got.source, want.source) == (None, "backfill", "live")
        # the replay carries no IRC tags: the 0006 tag columns stay unknown
        assert {got.first_message, got.highlighted, got.reward_id, got.msg_type} == {None}
    assert backfill_agg.pop_all() == live.aggregator.pop_all()

    emotes = {e["name"]: e["source"] for r in backfilled for e in r.emotes or ()}
    assert emotes["xqcL"] == "twitch" and emotes["OOOO"] == "7tv"
    assert emotes["PogU"] == "7tv"  # the channel set wins over the global one


def comment(offset_s: int, created_at: datetime | None, *, user: str | None = "u1") -> Comment:
    return Comment(
        id=f"c{offset_s}-{user}",
        created_at=created_at,
        content_offset_s=offset_s,
        commenter_id=user,
        commenter_login=user and f"login_{user}",
        commenter_display_name=None,
        fragments=(Fragment("hi KEKW"),),
        badges=(),
        user_color=None,
        cursor=None,
    )


def test_sent_at_is_created_at_or_the_clock_fallback():
    clock = ChatClock(T, "chat_anchor")
    exact = T + timedelta(seconds=100.731)
    assert clock.sent_at(comment(100, exact)) == (exact, False)
    edge = clock.expected(100) + timedelta(seconds=SENT_AT_TOLERANCE_S)
    assert clock.sent_at(comment(100, edge)) == (edge, False)
    expected = T + timedelta(seconds=100 + BAND_MID_S)
    hour_bug = exact + timedelta(hours=1)
    assert clock.sent_at(comment(100, hour_bug)) == (expected, True)
    assert clock.sent_at(comment(100, None)) == (expected, True)
    assert clock.sent_at(comment(100, edge + timedelta(milliseconds=1)))[1]

    row, _ = map_comment(
        comment(100, exact, user=None),
        channel_id=1,
        stream_id=2,
        clock=clock,
        channel_emotes={"KEKW": "7tv"},
        global_emotes={},
    )
    assert (row.user_id, row.user_login, row.emotes) == (None, None, [
        {"name": "KEKW", "count": 1, "source": "7tv"}
    ])  # fmt: skip
    agg = SecondAggregator()
    agg.add_row(row)
    (metric,) = agg.pop_all()
    assert (metric.messages, metric.unique_chatters) == (1, 0)  # kept, not a chatter


def test_clock_choice():
    t0 = datetime(2026, 11, 18, 6, 27, 49, 784000, tzinfo=UTC)
    chat = "2026-11-18T06:27:53.700000+00:00"
    assert clock_for(t0, "pdt", {"chat": chat}) == ChatClock(
        datetime.fromisoformat(chat), "chat_anchor"
    )
    assert clock_for(t0, "pdt", {}).t0 == t0 + timedelta(seconds=3.9)
    # a --fill-gaps T0 measured on the live audio is the media clock too
    assert clock_for(t0, "audio", {}).t0 == t0 + timedelta(seconds=3.9)
    assert clock_for(t0, "audio", {"chat": chat}).source == "chat_anchor"
    assert clock_for(t0, "helix", {}) == ChatClock(t0, "helix")
    window = window_for(t0, "pdt", {"chat": chat}, 100.0)
    assert window.start == t0 - timedelta(seconds=10)
    assert window.end == datetime.fromisoformat(chat) + timedelta(seconds=110)


def test_watermark_math_never_loses_or_repeats_a_second():
    """For any last offset L: no comment still to come (offset >= L) lies before
    `final_before(L)`, and a resume from `resume_offset(M)` reads every comment at or after M."""
    clock = ChatClock(T, "chat_anchor")
    rng = random.Random(5)
    for _ in range(2000):
        last = rng.randrange(0, 50_000)
        final = clock.final_before(last)
        assert final == second_of(final)
        start = clock.resume_offset(final)
        assert start <= last
        # the extreme sent_at of an accepted comment is expected ± tolerance
        earliest_unseen = clock.expected(last) - timedelta(seconds=SENT_AT_TOLERANCE_S)
        assert earliest_unseen >= final + timedelta(seconds=1)
        latest_before_start = clock.expected(start - 1) + timedelta(seconds=SENT_AT_TOLERANCE_S)
        assert start == 0 or latest_before_start < final


def test_chat_spans_are_the_whole_seconds_inside_each_range():
    a = datetime(2026, 11, 18, 6, 27, 49, 500000, tzinfo=UTC)
    s = timedelta(seconds=1)
    ranges = [
        Interval(a, a + 23.3 * s),  # :49.5 .. :12.8 -> [:50, :12)
        Interval(a + 30.5 * s, a + 31.4 * s),  # no whole second inside
        Interval(a + 40.5 * s, a + 42.5 * s),  # exactly two seconds
    ]
    whole = a.replace(microsecond=0)
    assert chat_spans(ranges) == [
        Interval(whole + s, whole + 23 * s),
        Interval(whole + 41 * s, whole + 43 * s),
    ]
    assert chat_spans([]) == []
