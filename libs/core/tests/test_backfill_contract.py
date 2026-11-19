"""Backfill contract (DESIGN §17): enums vs. model CHECKs, lease names, ffmpeg errors, chat rows."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, text

from clipper_core import ffmpeg
from clipper_core.chat import ChatWriter, MessageRow, MetricRow
from clipper_core.contracts import (
    GapReason,
    T0Method,
    VodChatStatus,
    VodMediaStatus,
    backfill_lease,
    recording_lease,
)
from clipper_core.models import ViewerClip, Vod


def check_values(table, name: str) -> set[str]:
    full = f"ck_{table.name}_{name}"  # naming convention
    (ck,) = [c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == full]
    return set(re.findall(r"'([^']+)'", str(ck.sqltext)))


def test_vod_status_enums_match_the_check_constraints():
    assert check_values(Vod.__table__, "media_status") == {s.value for s in VodMediaStatus}
    assert check_values(Vod.__table__, "chat_status") == {s.value for s in VodChatStatus}


def test_backfill_contract_values():
    assert GapReason.MUTED == "muted"
    assert {m.value for m in T0Method} == {"pdt", "chat", "helix", "audio"}
    assert backfill_lease("2346188660") == "backfill:vod:2346188660"
    assert backfill_lease("1") != recording_lease(1)
    assert ViewerClip.__table__.primary_key.columns.keys() == ["platform", "clip_id"]


def test_ffmpeg_reports_errors_instead_of_raising(tmp_path: Path):
    error = ffmpeg.extract_audio("no-such-ffmpeg-binary", tmp_path / "in.ts", tmp_path / "a.aac")
    assert error and error.startswith(("FileNotFoundError", "OSError", "PermissionError"))


def test_message_row_allows_no_received_at():
    row = MessageRow(
        sent_at=datetime(2026, 11, 1, tzinfo=UTC),
        msg_id="c1",
        channel_id=1,
        stream_id=2,
        received_at=None,
        user_id=None,
        user_login=None,
        text="KEKW",
        emotes=None,
        badges=None,
        source="backfill",
    )
    assert row.received_at is None and row.source == "backfill"


@pytest.mark.integration
async def test_chat_writer_stores_backfilled_rows_idempotently(sessions):
    day = datetime(2025, 3, 4, 12, 0, tzinfo=UTC)  # an old day: its partition is created
    messages = [
        MessageRow(day + timedelta(seconds=i), f"c{i}", 1, None, None, "u", "u", "hi", None, None)
        for i in range(3)
    ]
    for m in messages:
        m.source = "backfill"
    metrics = [MetricRow(1, day, None, 1, 1, {"KEKW": 2}, {})]
    writer = ChatWriter()
    for _ in range(2):  # a resume writes the same rows again
        async with sessions.begin() as s:
            await writer.write(s, messages, [], metrics, [])
    async with sessions() as s:
        stored = (
            await s.execute(text("SELECT received_at, source FROM chat_messages ORDER BY sent_at"))
        ).all()
        (metric,) = (await s.execute(text("SELECT * FROM chat_metrics_1s"))).all()
    assert [tuple(r) for r in stored] == [(None, "backfill")] * 3
    assert (metric.ts, metric.messages, metric.emote_counts) == (day, 1, {"KEKW": 2})
