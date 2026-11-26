"""T0 estimator: PDT > chat anchor > created_at, every candidate kept, disagreements flagged."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clipper_backfill.t0 import (
    CHAT_MINUS_PDT_S,
    ChatPoint,
    PdtPoint,
    chat_anchor,
    estimate_t0,
    pdt_points,
)
from clipper_core.contracts import T0Method
from clipper_twitch.gql import parse_page
from clipper_twitch.hls import parse_vod_playlist

T0 = datetime(2026, 11, 18, 6, 27, 49, 784000, tzinfo=UTC)
TWITCH_FIXTURES = Path(__file__).parents[3] / "libs" / "twitch" / "tests" / "fixtures"


def pdt(n=10, *, step_at=None, step_s=0.0):
    """PDT = T0 + offset on 10 s segments (+ a step from segment `step_at` on)."""
    return [
        PdtPoint(
            10.0 * i, T0 + timedelta(seconds=10 * i + (step_s if step_at and i >= step_at else 0))
        )
        for i in range(n)
    ]


def chat(n=60, *, anchor_s=CHAT_MINUS_PDT_S, start=0):
    """Comments whose offsets are truncated: createdAt = T0 + anchor + true offset."""
    points = []
    for i in range(n):
        true_offset = start + i * 7.3 + 0.5  # the fraction runs over [0, 1)
        points.append(ChatPoint(int(true_offset), T0 + timedelta(seconds=anchor_s + true_offset)))
    return points


def test_pdt_wins_and_every_candidate_is_kept():
    created = T0 + timedelta(seconds=3.216)
    est = estimate_t0(
        pdt=pdt(), chat=chat(), created_at=created, created_at_source="gql_created_at",
        path_epoch=created - timedelta(seconds=6),
    )  # fmt: skip
    assert est.method == T0Method.PDT and est.t0 == T0
    c = est.candidates
    assert c["pdt"] == T0.isoformat() and c["pdt_points"] == 10 and c["pdt_max_dev_s"] == 0
    assert c["gql_created_at"] == created.isoformat() and c["created_at_minus_t0_s"] == 3.216
    assert "path_epoch" in c and c["comments"] == 60
    assert abs(c["chat_minus_pdt_s"] - CHAT_MINUS_PDT_S) < 0.5
    assert est.flags == {}


def test_chat_far_from_pdt_and_created_at_far_off_are_flagged():
    est = estimate_t0(pdt=pdt(), chat=chat(anchor_s=12.0), created_at=T0 + timedelta(seconds=45))
    assert est.method == T0Method.PDT and est.t0 == T0
    assert est.flags["t0_chat_pdt"]["chat_minus_pdt_s"] == pytest.approx(12.0, abs=0.6)
    assert est.flags["t0_conflict"] == {"created_at_minus_t0_s": 45.0}


def test_pdt_steps_are_flagged_not_applied():
    est = estimate_t0(pdt=pdt(step_at=4, step_s=5.0))
    assert est.t0 == T0
    assert est.flags["pdt_steps"] == [{"offset_s": 40.0, "step_s": 5.0}]
    assert est.flags["pdt_step_count"] == 1 and est.candidates["pdt_max_dev_s"] == 5.0


def test_chat_anchor_without_pdt_is_the_median_minus_half_a_second():
    est = estimate_t0(chat=chat(anchor_s=2.0), created_at=T0 + timedelta(seconds=4))
    assert est.method == T0Method.CHAT
    # x = T0 + 2 + frac, frac uniform-ish over [0, 1): median ~ T0 + 2.5 -> T0 + 2.0
    assert abs((est.t0 - (T0 + timedelta(seconds=2.0))).total_seconds()) < 0.2
    assert est.candidates["chat_spread_s"] < 1.0
    assert "t0_conflict" not in est.flags


def test_whole_hour_outliers_are_shifted_back_and_others_dropped():
    points = chat(60)
    points[3] = ChatPoint(points[3].offset_s, points[3].created_at + timedelta(hours=1))
    points[7] = ChatPoint(points[7].offset_s, points[7].created_at - timedelta(hours=2))
    points[9] = ChatPoint(points[9].offset_s, points[9].created_at + timedelta(minutes=5))
    anchor = chat_anchor(points)
    assert anchor.hour_fixed == 2 and anchor.outliers == 1 and anchor.comments == 59
    assert anchor.spread_s < 1.0
    est = estimate_t0(chat=points)
    assert est.flags == {"chat_hour_fixed": 2, "chat_outliers": 1}


def test_too_few_comments_fall_back_to_created_at():
    created = T0 + timedelta(seconds=3)
    est = estimate_t0(chat=chat(19), created_at=created)
    assert (est.method, est.t0) == (T0Method.HELIX, created)
    assert est.candidates["comments_sampled"] == 19 and "chat" not in est.candidates


def test_chat_more_than_30_s_off_created_at_is_the_old_offset_bug():
    created = T0
    est = estimate_t0(chat=chat(anchor_s=95.0), created_at=created)
    assert (est.method, est.t0) == (T0Method.HELIX, created)
    assert est.flags["t0_conflict"]["chat_minus_created_at_s"] == pytest.approx(95.0, abs=0.6)
    assert "chat" in est.candidates  # kept for later analysis


def test_nothing_to_go_on():
    with pytest.raises(ValueError):
        estimate_t0(chat=chat(5))


def test_real_smoke_fixtures_reproduce_the_measured_anchors():
    """xqc 2932113307 (smoke test 2026-11-19): T0_pdt = 06:27:49.784, GQL createdAt 06:27:53,
    the chat anchor ~3.9 s after T0_pdt. The fixture playlist keeps segments 0-3 and 18-muted
    (14 cut out), so its PDT shows a 140 s step: exactly what `pdt_steps` is for."""
    text = (TWITCH_FIXTURES / "vod_audio_only.m3u8").read_text(encoding="utf-8")
    playlist = parse_vod_playlist(text, "https://cdn.test/x/audio_only/index-dvr.m3u8")
    body = json.loads((TWITCH_FIXTURES / "gql_comments_start.json").read_text(encoding="utf-8"))
    comments = parse_page(body, "2932113307").comments
    est = estimate_t0(
        pdt=pdt_points(playlist.segments),
        chat=[ChatPoint(c.content_offset_s, c.created_at) for c in comments],
        created_at=datetime(2026, 11, 18, 6, 27, 53, tzinfo=UTC),
        created_at_source="gql_created_at",
    )
    assert est.method == T0Method.PDT and est.t0 == T0
    assert 3.4 <= est.candidates["chat_minus_pdt_s"] <= 4.4
    assert est.candidates["created_at_minus_t0_s"] == 3.216
    assert est.flags["pdt_steps"] == [{"offset_s": 40.0, "step_s": 140.0}]
    assert "t0_chat_pdt" not in est.flags and "t0_conflict" not in est.flags
