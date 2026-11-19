"""Transcript contract (words files) and the read side: words() with ownership, coverage()."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from clipper_core import transcripts
from clipper_core.contracts import TRANSCRIPTS_NOTIFY_CHANNEL, Word, WordsFile
from clipper_core.storage import keys
from clipper_core.timeline import Interval

T0 = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def iv(a: float, b: float) -> Interval:
    return Interval(at(a), at(b))


# --------------------------------------------------------------------------- contract


def test_notify_channel():
    assert TRANSCRIPTS_NOTIFY_CHANNEL == "clipper_transcripts"


def test_words_file_rejects_naive_t0():
    with pytest.raises(ValidationError, match="naive datetime"):
        WordsFile(chunk_id=1, stream_id=1, seq=0, model="m", t0=datetime(2026, 11, 19, 12))
    with pytest.raises(ValidationError, match="naive datetime"):
        WordsFile.model_validate_json(
            '{"chunk_id": 1, "stream_id": 1, "seq": 0, "model": "m", "t0": "2026-11-19T12:00:00"}'
        )


def test_words_file_json_roundtrip_in_utc():
    cet = timezone(timedelta(hours=1))
    file = WordsFile(
        chunk_id=5,
        stream_id=2,
        seq=3,
        model="fake-asr",
        t0=datetime(2026, 11, 19, 13, 0, 0, 250000, tzinfo=cet),
        words=[
            Word(text="hello", start=0.5, end=0.9, confidence=0.93),
            Word(text="chat", start=1, end=1.4),
        ],
    )
    assert file.t0 == T0 + timedelta(microseconds=250000) and file.t0.tzinfo == UTC
    assert file.version == 1
    again = WordsFile.model_validate_json(file.model_dump_json())
    assert again == file
    assert again.words[1].confidence is None


def test_words_file_reading_rules():
    base = {"chunk_id": 1, "stream_id": 1, "seq": 0, "model": "m", "t0": "2026-11-19T12:00:00Z"}
    # Fields added by a newer writer are ignored; an incompatible version is rejected.
    assert WordsFile.model_validate({**base, "speaker_count": 2}).words == []
    with pytest.raises(ValidationError):
        WordsFile.model_validate({**base, "version": 2})
    with pytest.raises(ValidationError, match="ends before it starts"):
        Word(text="x", start=2.0, end=1.0)
    with pytest.raises(ValidationError):
        Word(text="x", start=-0.1, end=1.0)
    with pytest.raises(ValidationError):
        Word(text="x", start=0, end=1, confidence=1.5)


# --------------------------------------------------------------------------- DB helpers


class FakeStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.fetched: list[str] = []

    def get_bytes(self, key: str) -> bytes:
        self.fetched.append(key)
        return self.blobs[key]


async def make_stream(session, *, ended_at: float | None = None) -> int:
    channel_id = (
        await session.execute(text("INSERT INTO channels (login) VALUES ('test') RETURNING id"))
    ).scalar_one()
    return (
        await session.execute(
            text(
                "INSERT INTO streams (channel_id, platform_stream_id, started_at, ended_at,"
                " status) VALUES (:c, 'stream-1', :t, :e, :status) RETURNING id"
            ),
            {
                "c": channel_id,
                "t": T0,
                "e": None if ended_at is None else at(ended_at),
                "status": "live" if ended_at is None else "ended",
            },
        )
    ).scalar_one()


async def add_chunk(session, stream_id: int, seq: int, a: float, b: float, audio=True) -> int:
    return (
        await session.execute(
            text(
                "INSERT INTO chunks (stream_id, seq, start_utc, end_utc, audio_key) "
                "VALUES (:s, :q, :a, :b, :k) RETURNING id"
            ),
            {
                "s": stream_id,
                "q": seq,
                "a": at(a),
                "b": at(b),
                "k": keys.chunk_audio(stream_id, seq) if audio else None,
            },
        )
    ).scalar_one()


async def add_transcript(
    session,
    store: FakeStore | None,
    stream_id: int,
    seq: int,
    chunk_id: int,
    *,
    owned: tuple[float, float],
    tail_from: float | None = None,
    t0: float | None = None,
    words: list[tuple[str, float]] = (),
) -> None:
    """What the transcriber commits. `words`: (text, start in seconds after T0), 0.3 s long."""
    key = keys.chunk_words(stream_id, seq)
    t0 = owned[0] if t0 is None else t0
    if store is not None:
        file = WordsFile(
            chunk_id=chunk_id,
            stream_id=stream_id,
            seq=seq,
            model="fake",
            t0=at(t0),
            words=[Word(text=w, start=s - t0, end=s - t0 + 0.3, confidence=0.9) for w, s in words],
        )
        store.blobs[key] = file.model_dump_json().encode()
    await session.execute(
        text(
            """
            INSERT INTO chunk_transcripts (chunk_id, model, words_key, segment_count,
                                           owned_from, owned_to, tail_from)
            VALUES (:c, 'fake', :k, 0, :f, :t, :tail)
            """
        ),
        {
            "c": chunk_id,
            "k": key,
            "f": at(owned[0]),
            "t": at(owned[1]),
            "tail": None if tail_from is None else at(tail_from),
        },
    )


def texts(ws: list[transcripts.TranscriptWord]) -> list[str]:
    return [w.text for w in ws]


# --------------------------------------------------------------------------- words()


@pytest.mark.integration
async def test_words_tail_taken_over_by_next_chunk(sessions):
    store = FakeStore()
    async with sessions.begin() as session:
        stream_id = await make_stream(session)
        c0 = await add_chunk(session, stream_id, 0, 0, 60)
        c1 = await add_chunk(session, stream_id, 1, 60, 120)
        # Chunk 0's file still holds its old provisional tail ("running into the cu-") from 55 s
        # on; after the takeover it owns only [0, 55).
        await add_transcript(
            session,
            store,
            stream_id,
            0,
            c0,
            owned=(0, 55),
            words=[("hi", 1), ("chat", 30), ("so", 55), ("running", 55.2), ("cu", 59.5)],
        )
        # Chunk 1 re-transcribed from the old tail point: t0 = 55 = its owned_from, so its first
        # word starts at relative 0.0, and its first words lie before its own start (60).
        await add_transcript(
            session,
            store,
            stream_id,
            1,
            c1,
            owned=(55, 120),
            words=[("so", 55), ("running", 55.2), ("the", 59.5), ("cut", 60.1), ("ok", 90)],
        )

    async with sessions() as session:
        ws = await transcripts.words(session, store, stream_id, iv(0, 120))
    assert texts(ws) == ["hi", "chat", "so", "running", "the", "cut", "ok"]
    assert [w.chunk_id for w in ws] == [c0, c0, c1, c1, c1, c1, c1]  # "so" at 55 from c1 only
    assert not any(w.provisional for w in ws)
    assert ws[3].start_utc == at(55.2) and ws[3].end_utc == at(55.5)
    assert ws[3].start_utc.tzinfo == UTC and ws[3].confidence == 0.9

    # A word belongs to the range of its start; ranges are half-open.
    async with sessions() as session:
        assert texts(await transcripts.words(session, store, stream_id, iv(55.3, 60.1))) == ["the"]
        assert texts(await transcripts.words(session, store, stream_id, iv(55, 55.2))) == ["so"]

    # Only the words files of overlapping owned ranges are loaded.
    store.fetched.clear()
    async with sessions() as session:
        ws = await transcripts.words(session, store, stream_id, iv(30, 50))
    assert texts(ws) == ["chat"]  # interval starts exactly at the word's start
    assert store.fetched == [keys.chunk_words(stream_id, 0)]

    store.fetched.clear()
    async with sessions() as session:
        assert await transcripts.words(session, store, stream_id, iv(200, 300)) == []
        assert await transcripts.words(session, store, stream_id, iv(30, 30)) == []
    assert store.fetched == []


@pytest.mark.integration
async def test_words_provisional_tail_not_taken_over_yet(sessions):
    store = FakeStore()
    async with sessions.begin() as session:
        stream_id = await make_stream(session)
        c0 = await add_chunk(session, stream_id, 0, 0, 60)
        await add_chunk(session, stream_id, 1, 60, 120)  # recorded, not transcribed yet
        await add_transcript(
            session,
            store,
            stream_id,
            0,
            c0,
            owned=(0, 60),
            tail_from=56,
            words=[("so", 40), ("anyway", 56), ("what", 58.9), ("was", 59.8)],
        )

    async with sessions() as session:
        ws = await transcripts.words(session, store, stream_id, iv(0, 120))
    assert [(w.text, w.provisional) for w in ws] == [
        ("so", False),
        ("anyway", True),
        ("what", True),
        ("was", True),
    ]


@pytest.mark.integration
async def test_words_outside_owned_ranges_are_ignored(sessions):
    store = FakeStore()
    async with sessions.begin() as session:
        stream_id = await make_stream(session)
        c0 = await add_chunk(session, stream_id, 0, 0, 60)
        c1 = await add_chunk(session, stream_id, 1, 60, 120)
        await add_transcript(
            session, store, stream_id, 0, c0, owned=(0, 60), words=[("a", 10), ("b", 59.97)]
        )
        # The window started before the owned range (t0 = 50), and an overlap of a few ms between
        # neighbours (timestamp jitter) goes to the later chunk: no word twice.
        await add_transcript(
            session,
            store,
            stream_id,
            1,
            c1,
            owned=(59.95, 120),
            t0=50,
            words=[("old", 52), ("b", 59.96), ("c", 61), ("late", 119.9)],
        )
        # A chunk whose whole range was given to the next one owns nothing.
        c2 = await add_chunk(session, stream_id, 2, 120, 180)
        await add_transcript(session, store, stream_id, 2, c2, owned=(120, 120), words=[("x", 125)])

    async with sessions() as session:
        ws = await transcripts.words(session, store, stream_id, iv(0, 180))
    assert [(w.text, w.chunk_id) for w in ws] == [("a", c0), ("b", c1), ("c", c1), ("late", c1)]
    assert keys.chunk_words(stream_id, 2) not in store.fetched


@pytest.mark.integration
async def test_capped_takeover_loses_no_word_and_leaves_no_hole(sessions):
    """P's tail started at 20 s, but N's window starts at most 30 s before N (at 30 s):
    P keeps [20, 30) as non-provisional, N owns from 30."""
    store = FakeStore()
    async with sessions.begin() as session:
        stream_id = await make_stream(session)
        p = await add_chunk(session, stream_id, 0, 0, 60)
        n = await add_chunk(session, stream_id, 1, 60, 120)
        await add_transcript(
            session,
            store,
            stream_id,
            0,
            p,
            owned=(0, 30),
            words=[("a", 5), ("long", 20), ("rant", 25), ("still", 29.9), ("going", 31.5)],
        )
        await add_transcript(
            session,
            store,
            stream_id,
            1,
            n,
            owned=(30, 120),
            words=[("going", 31.5), ("on", 45), ("done", 61)],
        )

    async with sessions() as session:
        ws = await transcripts.words(session, store, stream_id, iv(0, 120))
        cov = await transcripts.coverage(session, stream_id, iv(0, 120))
    assert [(w.text, w.chunk_id) for w in ws] == [
        ("a", p),
        ("long", p),
        ("rant", p),
        ("still", p),
        ("going", n),
        ("on", n),
        ("done", n),
    ]
    assert cov.complete and cov.transcribed == (iv(0, 120),) and cov.provisional == ()


@pytest.mark.integration
async def test_words_file_of_another_chunk_is_an_error(sessions):
    store = FakeStore()
    async with sessions.begin() as session:
        stream_id = await make_stream(session)
        c0 = await add_chunk(session, stream_id, 0, 0, 60)
        await add_transcript(session, store, stream_id, 0, c0, owned=(0, 60), words=[("a", 1)])
    key = keys.chunk_words(stream_id, 0)
    file = WordsFile.model_validate_json(store.blobs[key])
    store.blobs[key] = file.model_copy(update={"chunk_id": c0 + 1}).model_dump_json().encode()
    async with sessions() as session:
        with pytest.raises(ValueError, match="belongs to chunk"):
            await transcripts.words(session, store, stream_id, iv(0, 60))


# --------------------------------------------------------------------------- coverage()


async def build_stream_for_coverage(sessions, *, ended_at: float | None = None) -> int:
    """0-55 c0 (gave its tail to c1) | 55-120 c1, provisional from 115 | 120-150 ad gap |
    150-210 c2 without audio | 210.2-270 c3 not transcribed | 270-330 c4 | 330.2-390 c5 |
    390-390.4 c6 not transcribed | 390.4-450 c7 | 450- nothing recorded (yet).
    0.2 s jitter holes after c2 and c4."""
    async with sessions.begin() as session:
        stream_id = await make_stream(session, ended_at=ended_at)
        c0 = await add_chunk(session, stream_id, 0, 0, 60)
        c1 = await add_chunk(session, stream_id, 1, 60, 120)
        await add_transcript(session, None, stream_id, 0, c0, owned=(0, 55))
        await add_transcript(session, None, stream_id, 1, c1, owned=(55, 120), tail_from=115)
        await session.execute(
            text(
                "INSERT INTO stream_gaps (stream_id, start_utc, end_utc, reason) "
                "VALUES (:s, :a, :b, 'ad')"
            ),
            {"s": stream_id, "a": at(120), "b": at(150)},
        )
        await add_chunk(session, stream_id, 2, 150, 210, audio=False)
        await add_chunk(session, stream_id, 3, 210.2, 270)
        c4 = await add_chunk(session, stream_id, 4, 270, 330)
        c5 = await add_chunk(session, stream_id, 5, 330.2, 390)
        await add_transcript(session, None, stream_id, 4, c4, owned=(270, 330))
        await add_transcript(session, None, stream_id, 5, c5, owned=(330.2, 390))
        await add_chunk(session, stream_id, 6, 390, 390.4)
        c7 = await add_chunk(session, stream_id, 7, 390.4, 450)
        await add_transcript(session, None, stream_id, 7, c7, owned=(390.4, 450))
    return stream_id


async def cover(sessions, stream_id: int, a: float, b: float, now: float | None = None):
    async with sessions() as session:
        return await transcripts.coverage(
            session, stream_id, iv(a, b), now=None if now is None else at(now)
        )


@pytest.mark.integration
async def test_coverage_of_a_live_stream(sessions):
    stream_id = await build_stream_for_coverage(sessions)

    cov = await cover(sessions, stream_id, -30, 480)
    assert cov.interval == iv(-30, 480)
    assert cov.transcribed == (iv(0, 120), iv(270, 390), iv(390.4, 450))  # hole at 330 bridged
    assert cov.provisional == (iv(115, 120),)
    assert cov.gap == (iv(-30, 0), iv(120, 210))  # before the start; ad + chunk without audio
    # c3 (with the jitter hole before it), and the 0.4 s c6: never bridged over
    assert cov.untranscribed == (iv(210, 270), iv(390, 390.4))
    assert cov.unrecorded == (iv(450, 480),)
    assert cov.pending == (iv(210, 270), iv(390, 390.4), iv(450, 480))
    assert not cov.complete
    assert cov.pending_seconds == pytest.approx(90.4)

    # Judge window inside transcribed media: complete (a provisional tail counts).
    cov = await cover(sessions, stream_id, 30, 120)
    assert cov.complete and cov.transcribed == (iv(30, 120),) and cov.provisional == (iv(115, 120),)

    # Inside c1's prefix (owned from 55, chunk starts at 60): found through the owned range.
    cov = await cover(sessions, stream_id, 56, 59)
    assert cov.complete and cov.transcribed == (iv(56, 59),)

    # A window edge inside a jitter hole is no reason to wait.
    cov = await cover(sessions, stream_id, 330.1, 335)
    assert cov.complete and cov.transcribed == (iv(330.1, 335),)

    # Windows over gaps only, before the stream, or straddling an untranscribed chunk.
    cov = await cover(sessions, stream_id, 125, 205)
    assert cov.complete and cov.gap == (iv(125, 205),) and cov.transcribed == ()
    cov = await cover(sessions, stream_id, -100, -10)
    assert cov.complete and cov.gap == (iv(-100, -10),)
    cov = await cover(sessions, stream_id, 200, 280)
    assert cov.gap == (iv(200, 210),)
    assert cov.untranscribed == (iv(210, 270),) and cov.unrecorded == ()
    assert cov.transcribed == (iv(270, 280),)


@pytest.mark.integration
async def test_coverage_of_an_ended_stream_waits_for_the_recorder_until_grace(sessions):
    stream_id = await build_stream_for_coverage(sessions, ended_at=460)  # last chunk ends 450
    cov = await cover(sessions, stream_id, 440, 500, now=460 + 60)
    assert cov.transcribed == (iv(440, 450),)
    assert cov.unrecorded == (iv(450, 460),)  # the recorder may still upload a last chunk
    assert cov.gap == (iv(460, 500),)

    for after_grace in (460 + 300, 460 + 360):
        cov = await cover(sessions, stream_id, 440, 500, now=after_grace)
        assert cov.complete and cov.gap == (iv(450, 500),)


@pytest.mark.integration
async def test_coverage_of_an_ended_stream_whose_last_chunk_ends_later(sessions):
    stream_id = await build_stream_for_coverage(sessions, ended_at=445)
    cov = await cover(sessions, stream_id, 440, 480, now=446)
    assert cov.complete
    assert cov.transcribed == (iv(440, 450),) and cov.gap == (iv(450, 480),)


@pytest.mark.integration
async def test_coverage_of_an_unknown_stream(sessions):
    with pytest.raises(LookupError):
        await cover(sessions, 12345, 0, 60)


# --------------------------------------------------------------------------- CHECK constraints


@pytest.mark.integration
async def test_owned_range_is_checked_by_the_database(sessions):
    async with sessions.begin() as session:
        stream_id = await make_stream(session)
        chunk_id = await add_chunk(session, stream_id, 0, 0, 60)
    for owned, tail_from in (((30, 20), None), ((0, 60), 60), ((10, 60), 5)):
        with pytest.raises(Exception, match="ck_chunk_transcripts_owned_range"):
            async with sessions.begin() as session:
                await add_transcript(
                    session, None, stream_id, 0, chunk_id, owned=owned, tail_from=tail_from
                )

    # Valid edge states: nothing owned; whole window provisional; owned before the chunk start.
    async with sessions.begin() as session:
        await add_transcript(session, None, stream_id, 0, chunk_id, owned=(10, 10))
        c1 = await add_chunk(session, stream_id, 1, 60, 120)
        await add_transcript(session, None, stream_id, 1, c1, owned=(60, 120), tail_from=60)
        c2 = await add_chunk(session, stream_id, 2, 120, 180)
        await add_transcript(session, None, stream_id, 2, c2, owned=(110, 180))


@pytest.mark.integration
async def test_audio_metric_ranges_are_checked_by_the_database(sessions):
    insert = text(
        "INSERT INTO audio_metrics_1s (stream_id, ts, loudness_db, speech_ratio) "
        "VALUES (:s, :ts, :db, :sr)"
    )
    async with sessions.begin() as session:
        stream_id = await make_stream(session)
        for i, (db, sr) in enumerate(((0.0, 1.0), (-120.0, 0.0), (-23.5, 0.4))):
            await session.execute(insert, {"s": stream_id, "ts": at(i), "db": db, "sr": sr})
    nan, inf = float("nan"), float("inf")
    for db, sr in ((-20, 1.01), (-20, -0.1), (-20, nan), (0.5, 0.5), (nan, 0.5), (-inf, 0.5)):
        with pytest.raises(Exception, match="ck_audio_metrics_1s_ranges"):
            async with sessions.begin() as session:
                await session.execute(insert, {"s": stream_id, "ts": at(10), "db": db, "sr": sr})
