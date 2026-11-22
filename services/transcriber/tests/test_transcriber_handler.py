"""The transcribe job handler against Postgres, real .aac files (ffmpeg) and the fake engines:
rows, words files, metrics, NOTIFY, chunk handover, retries, conflicts, heartbeat."""

from __future__ import annotations

import asyncio
import itertools
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import psycopg
import pytest
from sqlalchemy import text

import clipper_transcriber.handler as handler_mod
from clipper_core import jobs, transcripts
from clipper_core.contracts import (
    FILL_SEQ_BASE,
    TRANSCRIPTS_NOTIFY_CHANNEL,
    JobKind,
    JobStatus,
    Priority,
    TranscribePayload,
    WordsFile,
    dedupe_key,
)
from clipper_core.storage import keys
from clipper_core.timeline import Interval
from clipper_transcriber.audio import SAMPLE_RATE, encode_aac, samples
from clipper_transcriber.engines import RecognizedWord
from clipper_transcriber.fakes import synth_speech, synth_to_aac
from clipper_transcriber.handler import HANDOVER_ATTEMPTS, TranscribeHandler
from clipper_transcriber.service import TranscriberService, build_engines
from clipper_transcriber.settings import TranscriberSettings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
]

T0 = datetime(2026, 11, 19, 12, 0, 0, 400000, tzinfo=UTC)  # chunks start mid-second
TOLERANCE_S = 0.04  # AAC encoder delay (~21 ms) + 10 ms analysis frames of the fake engine
SCRIPT = "w1 w2 w3 . w4 w5 . _1.2 w6 w7 w8 w9 . w10 w11 w12 . w13 w14 . w15 w16 w17 . w18"


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


class MemoryStore:
    """In-memory object store with the ObjectStore methods the transcriber uses."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: list[tuple[str, bytes]] = []  # put_bytes calls, in order

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.objects[key] = bytes(data)
        self.uploads.append((key, bytes(data)))

    def put_file(self, key: str, path, content_type: str | None = None) -> None:
        self.objects[key] = Path(path).read_bytes()

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    def download_file(self, key: str, path) -> None:
        Path(path).write_bytes(self.objects[key])


def transcriber_settings(**kw) -> TranscriberSettings:
    return TranscriberSettings(engine="fake", vad="energy", tagger="fake", _env_file=None, **kw)


@dataclass
class Env:
    sessions: object
    store: MemoryStore
    handler: TranscribeHandler
    stream_id: int
    tmp: Path

    async def add_chunk(
        self,
        seq: int,
        a: float,
        b: float,
        pcm: np.ndarray | None = None,
        *,
        audio_file: Path | None = None,
        priority: int = Priority.LIVE,
    ) -> int:
        """Chunk `seq` = [a, b) seconds after T0 with `pcm` encoded as its .aac, or
        `audio_file` (neither: no audio), and its transcription job (enqueued in the same
        transaction, like the recorder does)."""
        audio_key = None
        if pcm is not None:
            audio_file = self.tmp / f"{self.stream_id}-{seq}.aac"
            encode_aac(pcm, audio_file)
        if audio_file is not None:
            audio_key = keys.chunk_audio(self.stream_id, seq)
            self.store.put_file(audio_key, audio_file)
        async with self.sessions.begin() as s:
            chunk_id = (
                await s.execute(
                    text(
                        "INSERT INTO chunks (stream_id, seq, start_utc, end_utc, audio_key) "
                        "VALUES (:s, :q, :a, :b, :k) RETURNING id"
                    ),
                    {"s": self.stream_id, "q": seq, "a": at(a), "b": at(b), "k": audio_key},
                )
            ).scalar_one()
            await jobs.enqueue(
                s,
                JobKind.TRANSCRIBE,
                payload=TranscribePayload(chunk_id=chunk_id).model_dump(),
                ref_id=chunk_id,
                dedupe_key=dedupe_key(JobKind.TRANSCRIBE, chunk_id),
                priority=priority,
            )
        return chunk_id

    async def add_stream_chunks(self, pcm: np.ndarray, cuts: list[float]) -> list[int]:
        """Contiguous chunks of one stream audio cut at `cuts` (seconds)."""
        bounds = [0.0, *cuts, len(pcm) / SAMPLE_RATE]
        return [
            await self.add_chunk(i, a, b, pcm[samples(a) : samples(b)])
            for i, (a, b) in enumerate(itertools.pairwise(bounds))
        ]

    async def transcript(self, chunk_id: int):
        async with self.sessions() as s:
            return (
                await s.execute(
                    text("SELECT * FROM chunk_transcripts WHERE chunk_id = :c"), {"c": chunk_id}
                )
            ).first()

    async def segments(self, chunk_id: int | None = None):
        where = "chunk_id = :c" if chunk_id is not None else "stream_id = :s"
        async with self.sessions() as s:
            return (
                await s.execute(
                    text(
                        "SELECT chunk_id, start_utc, end_utc, text, language, model, provisional,"
                        " avg_logprob, no_speech_prob, confidence"
                        f" FROM transcript_segments WHERE {where} ORDER BY start_utc"
                    ),
                    {"c": chunk_id, "s": self.stream_id},
                )
            ).all()

    async def metrics(self):
        async with self.sessions() as s:
            return (
                await s.execute(
                    text(
                        "SELECT ts, loudness_db, speech_ratio, classes FROM audio_metrics_1s"
                        " WHERE stream_id = :s ORDER BY ts"
                    ),
                    {"s": self.stream_id},
                )
            ).all()

    async def words(self, a: float = 0.0, b: float = 3600.0):
        async with self.sessions() as s:
            return await transcripts.words(s, self.store, self.stream_id, Interval(at(a), at(b)))

    async def coverage(self, a: float, b: float):
        async with self.sessions() as s:
            return await transcripts.coverage(s, self.stream_id, Interval(at(a), at(b)))

    async def job(self, chunk_id: int):
        async with self.sessions() as s:
            return (
                await s.execute(
                    text("SELECT * FROM jobs WHERE dedupe_key = :k"),
                    {"k": dedupe_key(JobKind.TRANSCRIBE, chunk_id)},
                )
            ).one()

    def runner(self, worker: str = "test-worker") -> jobs.JobRunner:
        return jobs.JobRunner(
            self.sessions, "unused-no-listen", worker, {JobKind.TRANSCRIBE: self.handler}
        )


@pytest.fixture
async def env(sessions, tmp_path) -> Env:
    return await make_env(sessions, tmp_path)


async def make_env(sessions, tmp_path, **settings_kw) -> Env:
    async with sessions.begin() as s:
        channel_id = (
            await s.execute(text("INSERT INTO channels (login) VALUES ('test') RETURNING id"))
        ).scalar_one()
        stream_id = (
            await s.execute(
                text(
                    "INSERT INTO streams (channel_id, platform_stream_id, started_at) "
                    "VALUES (:c, 'stream-1', :t) RETURNING id"
                ),
                {"c": channel_id, "t": T0},
            )
        ).scalar_one()
    settings = transcriber_settings(**settings_kw)
    store = MemoryStore()
    handler = TranscribeHandler(sessions, store, build_engines(settings), settings)
    return Env(sessions, store, handler, stream_id, tmp_path)


def cut_inside(expected: list[RecognizedWord], word: str) -> float:
    w = next(w for w in expected if w.text == word)
    return round((w.start + w.end) / 2, 3)


def assert_words(got: list[transcripts.TranscriptWord], expected: list[RecognizedWord]) -> None:
    """Every expected word exactly once, in order, at its time."""
    assert [w.text for w in got] == [w.text for w in expected]
    for g, e in zip(got, expected, strict=True):
        assert abs((g.start_utc - at(e.start)).total_seconds()) < TOLERANCE_S, g


def sentences(expected: list[RecognizedWord]) -> list[str]:
    out, cur = [], []
    for w in expected:
        cur.append(w.text)
        if w.text.endswith("."):
            out.append(" ".join(cur))
            cur = []
    return [*out, " ".join(cur)] if cur else out


# --------------------------------------------------------------------------- one chunk


async def test_single_chunk_through_the_job_runner(env, settings):
    path = env.tmp / "only.aac"
    pcm = synth_to_aac("w1 w2 w3 . w4 w5 .", path)
    duration = len(pcm) / SAMPLE_RATE
    chunk_id = await env.add_chunk(0, 0.0, duration, audio_file=path)
    _, expected = synth_speech("w1 w2 w3 . w4 w5 .")

    listen = await psycopg.AsyncConnection.connect(settings.database_dsn, autocommit=True)
    async with listen as conn:
        await conn.execute(f"LISTEN {TRANSCRIPTS_NOTIFY_CHANNEL}")
        assert await env.runner().run_once()
        notes = [n async for n in conn.notifies(timeout=5, stop_after=1)]
    assert [n.payload for n in notes] == [str(env.stream_id)]
    job = await env.job(chunk_id)
    assert job.status == JobStatus.DONE and job.attempts == 1
    assert env.handler.stats.done == 1 and env.handler.stats.failed == 0
    assert env.handler.stats.realtime_factor > 1

    row = await env.transcript(chunk_id)
    words_key = keys.chunk_words(env.stream_id, 0)
    assert (row.owned_from, row.owned_to, row.tail_from) == (at(0), at(duration), None)
    assert (row.model, row.language, row.words_key) == ("fake-tones", "en", words_key)
    assert row.segment_count == 2 and row.processing_s > 0

    file = WordsFile.model_validate_json(env.store.objects[words_key])
    assert (file.chunk_id, file.stream_id, file.seq, file.model) == (
        chunk_id,
        env.stream_id,
        0,
        "fake-tones",
    )
    assert file.t0 == at(0) and [w.text for w in file.words] == [w.text for w in expected]
    for got, want in zip(file.words, expected, strict=True):
        assert abs(got.start - want.start) < TOLERANCE_S and got.confidence == 0.9

    segs = await env.segments(chunk_id)
    assert [s.text for s in segs] == ["w1 w2 w3.", "w4 w5."]
    assert all(
        (s.provisional, s.model, s.language, s.avg_logprob) == (False, "fake-tones", "en", -0.1)
        for s in segs
    )
    assert abs((segs[0].start_utc - at(expected[0].start)).total_seconds()) < TOLERANCE_S

    metrics = await env.metrics()
    want_ts = []
    ts = at(0).replace(microsecond=0)
    while ts + timedelta(seconds=0.5) < at(duration):
        if ts + timedelta(seconds=0.5) >= at(0):
            want_ts.append(ts)
        ts += timedelta(seconds=1)
    assert [m.ts for m in metrics] == want_ts
    assert all(-100 <= m.loudness_db <= 0 and 0 <= m.speech_ratio <= 1 for m in metrics)
    assert all("speech" in m.classes for m in metrics)
    assert max(m.speech_ratio for m in metrics) > 0.3

    assert_words(await env.words(), expected)
    cov = await env.coverage(0, duration)
    assert cov.complete and cov.provisional == () and cov.transcribed == (cov.interval,)


async def test_chunk_without_audio_or_missing_chunk_writes_nothing(env):
    chunk_id = await env.add_chunk(0, 0.0, 10.0, None)
    assert await env.handler.transcribe(chunk_id) is None
    assert await env.handler.transcribe(chunk_id + 999) is None
    assert await env.runner().run_once()  # the job of the chunk without audio is just done
    assert (await env.job(chunk_id)).status == JobStatus.DONE
    assert await env.transcript(chunk_id) is None
    assert await env.segments() == [] and await env.metrics() == []
    assert not any(k.endswith(".json") for k in env.store.objects)


async def test_broken_audio_fails_the_job_at_once(env, caplog):
    broken = env.tmp / "broken.aac"
    broken.write_bytes(b"this is not audio " * 200)
    chunk_id = await env.add_chunk(0, 0.0, 5.0, audio_file=broken)
    with pytest.raises(jobs.PermanentJobError, match="can't be decoded"):
        await env.handler.transcribe(chunk_id)
    assert "audio file can't be decoded" in caplog.messages

    assert await env.runner().run_once()
    job = await env.job(chunk_id)
    assert job.status == JobStatus.FAILED and job.attempts == 1 < job.max_attempts
    assert job.last_error.startswith("PermanentJobError: audio streams/")
    assert env.handler.stats.failed == 1 and await env.transcript(chunk_id) is None
    assert "job failed permanently, not retrying" in caplog.messages


# --------------------------------------------------------------------------- handover


async def test_handover_every_word_exactly_once(env):
    pcm, expected = synth_speech(SCRIPT)
    total = len(pcm) / SAMPLE_RATE
    cut1, cut2 = cut_inside(expected, "w5."), cut_inside(expected, "w12.")
    a, b, c = await env.add_stream_chunks(pcm, [cut1, cut2])

    first = await env.handler.transcribe(a)
    ta = await env.transcript(a)
    assert ta.tail_from is not None and ta.owned_to == at(cut1)
    assert (await env.segments(a))[-1].provisional

    second = await env.handler.transcribe(b)
    assert second.plan.takeover and not first.plan.takeover
    ta, tb = await env.transcript(a), await env.transcript(b)
    assert tb.owned_from == first.result.tail_from_utc == ta.owned_to  # tiles
    assert ta.tail_from is None and tb.tail_from is not None  # B's own tail (cut in w12)
    a_segs, b_segs = await env.segments(a), await env.segments(b)
    assert not any(s.provisional for s in a_segs) and ta.segment_count == len(a_segs)
    assert b_segs[0].start_utc < at(cut1) and b_segs[0].text == "w4 w5."
    cov = await env.coverage(0, cut2)
    assert cov.complete and cov.provisional == (Interval(tb.tail_from, tb.owned_to),)

    await env.handler.transcribe(c)
    tb, tc = await env.transcript(b), await env.transcript(c)
    assert tb.owned_to == tc.owned_from and tb.tail_from is None and tc.tail_from is None
    assert_words(await env.words(), expected)
    segs = await env.segments()
    assert [s.text for s in segs] == sentences(expected)
    assert not any(s.provisional for s in segs)
    assert [s.chunk_id for s in segs] == [a, b, b, c, c, c, c]
    cov = await env.coverage(0, total)
    assert cov.complete and cov.provisional == () and cov.transcribed == (cov.interval,)

    # metrics: one row per UTC second, from the chunk holding the second's middle
    metrics = await env.metrics()
    assert [m.ts for m in metrics] == [
        at(0).replace(microsecond=0) + timedelta(seconds=i) for i in range(len(metrics))
    ]
    assert (
        metrics[-1].ts + timedelta(seconds=0.5)
        < at(total)
        <= metrics[-1].ts + timedelta(seconds=1.5)
    )


async def test_out_of_order_next_chunk_first(env):
    pcm, expected = synth_speech(SCRIPT)
    cut1, cut2 = cut_inside(expected, "w5."), cut_inside(expected, "w12.")
    a, b, _ = await env.add_stream_chunks(pcm, [cut1, cut2])

    out_of_order = await env.handler.transcribe(b)
    assert not out_of_order.plan.takeover and out_of_order.plan.keep_tail
    tb = await env.transcript(b)
    assert tb.owned_from == at(cut1) and tb.tail_from is not None

    late = await env.handler.transcribe(a)
    assert not late.plan.keep_tail and late.plan.owned_to == at(cut1)
    ta, tb2 = await env.transcript(a), await env.transcript(b)
    assert ta.owned_to == tb2.owned_from == at(cut1) and ta.tail_from is None
    assert tb2 == tb  # B untouched
    assert not any(s.provisional for s in await env.segments(a))
    cov = await env.coverage(0, cut2)
    assert cov.complete and cov.provisional == (Interval(tb.tail_from, tb.owned_to),)
    # every word is there; only the word cut at A/B may come twice (DESIGN: small glitch)
    got = [w.text.rstrip(".") for w in await env.words()]
    assert set(got) == {w.text.rstrip(".") for w in expected if w.start < cut2}
    assert len(got) - len(set(got)) <= 1


# --------------------------------------------------------------------------- retries


async def snapshot(env: Env, chunk_ids: list[int]) -> dict:
    """Everything a job writes (without timings and ids)."""
    rows = []
    for cid in chunk_ids:
        row = await env.transcript(cid)
        rows.append(
            None
            if row is None
            else (
                row.model,
                row.language,
                row.words_key,
                row.segment_count,
                row.owned_from,
                row.owned_to,
                row.tail_from,
            )
        )
    words = [(w.text, w.start_utc, w.end_utc, w.chunk_id, w.provisional) for w in await env.words()]
    files = {k: v for k, v in env.store.objects.items() if k.endswith(".json")}
    segs = [tuple(s) for s in await env.segments()]
    metrics = [tuple(m) for m in await env.metrics()]
    return {"rows": rows, "words": words, "files": files, "segments": segs, "metrics": metrics}


def without_files(snap: dict) -> dict:
    return {k: v for k, v in snap.items() if k != "files"}


async def test_running_a_job_twice_gives_identical_results(env):
    pcm, expected = synth_speech(SCRIPT)
    cut1, cut2 = cut_inside(expected, "w5."), cut_inside(expected, "w12.")
    a, b, c = await env.add_stream_chunks(pcm, [cut1, cut2])
    await env.handler.transcribe(a)
    first = await env.handler.transcribe(b)
    before = await snapshot(env, [a, b, c])

    again = await env.handler.transcribe(b)
    assert first.plan.takeover and again.plan.retake and not again.plan.takeover
    assert again.plan.window_start == first.plan.window_start
    assert await snapshot(env, [a, b, c]) == before

    # and after C took B's tail over, B's re-run stops at C's owned_from (no tail); its words
    # file gets shorter, everything that counts stays the same
    await env.handler.transcribe(c)
    before = await snapshot(env, [a, b, c])
    rerun = await env.handler.transcribe(b)
    assert rerun.plan.retake and not rerun.plan.keep_tail
    assert rerun.plan.owned_to == (await env.transcript(c)).owned_from
    assert without_files(await snapshot(env, [a, b, c])) == without_files(before)
    assert_words(await env.words(), expected)


async def test_failure_before_commit_leaves_the_database_untouched(env, monkeypatch):
    pcm, expected = synth_speech(SCRIPT)
    cut1 = cut_inside(expected, "w5.")
    a, b = await env.add_stream_chunks(pcm, [cut1])
    runner = env.runner()
    assert await runner.run_once()  # A (the older job)
    before = await snapshot(env, [a, b])
    ta = await env.transcript(a)
    assert ta.tail_from is not None

    async def broken(*args, **kwargs):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(handler_mod, "_write_metrics", broken)
    assert await runner.run_once()  # B fails in its transaction, after the words upload
    job = await env.job(b)
    assert job.status == JobStatus.QUEUED and job.last_error == "RuntimeError: injected failure"
    assert keys.chunk_words(env.stream_id, 1) in env.store.objects  # uploaded before commit
    assert without_files(await snapshot(env, [a, b])) == without_files(before)
    assert await env.transcript(a) == ta  # P's tail intact

    monkeypatch.undo()
    async with env.sessions.begin() as s:  # skip the retry backoff
        await s.execute(text("UPDATE jobs SET run_after = now()"))
    assert await runner.run_once()
    job = await env.job(b)
    assert job.status == JobStatus.DONE and job.attempts == 2
    old_tail = ta.tail_from
    ta, tb = await env.transcript(a), await env.transcript(b)
    assert ta.owned_to == tb.owned_from == old_tail and ta.tail_from is None
    assert_words(await env.words(), expected)
    assert env.handler.stats.done == 2 and env.handler.stats.failed == 1


async def test_rerun_of_the_previous_chunk_keeps_the_handover(env):
    pcm, expected = synth_speech(SCRIPT)
    cut1 = cut_inside(expected, "w5.")
    a, b = await env.add_stream_chunks(pcm, [cut1])
    await env.handler.transcribe(a)
    await env.handler.transcribe(b)
    before = await snapshot(env, [a, b])
    ta = await env.transcript(a)

    rerun = await env.handler.transcribe(a)
    assert not rerun.plan.keep_tail and rerun.plan.owned_to == ta.owned_to
    assert (await env.transcript(a)).owned_to == (await env.transcript(b)).owned_from
    assert without_files(await snapshot(env, [a, b])) == without_files(before)
    assert_words(await env.words(), expected)


# --------------------------------------------------------------------------- edge cases


async def test_capped_takeover(sessions, tmp_path):
    env = await make_env(sessions, tmp_path, takeover_cap_s=2.0)
    script = "w1 w2 . w3 w4 w5 w6 w7 w8 w9 w10 . w11"
    pcm, expected = synth_speech(script)
    w4, w5, w9 = (next(w for w in expected if w.text == t) for t in ("w4", "w5", "w9"))
    cut = round(w9.start + 0.28, 3)  # the long sentence w3 .. w10 runs into the cut
    window_start = cut - 2.0
    assert w4.end + 0.05 < window_start < w5.start - 0.03  # the cap lands between two words
    a, b = await env.add_stream_chunks(pcm, [cut])

    await env.handler.transcribe(a)
    old = await env.transcript(a)
    assert old.tail_from is not None and old.tail_from < at(window_start)

    out = await env.handler.transcribe(b)
    assert out.plan.takeover and out.plan.window_start == at(window_start)
    assert out.plan.prev_capped_from == old.tail_from
    ta, tb = await env.transcript(a), await env.transcript(b)
    assert ta.owned_to == tb.owned_from == at(window_start) and ta.tail_from is None
    a_segs = await env.segments(a)
    assert [(s.text, s.provisional) for s in a_segs] == [("w1 w2.", False), ("w3 w4", False)]
    assert a_segs[1].start_utc >= old.tail_from and ta.segment_count == 2
    assert a_segs[1].model == "fake-tones" and a_segs[1].confidence == 0.9
    b_segs = await env.segments(b)
    assert [s.text for s in b_segs] == ["w5 w6 w7 w8 w9 w10.", "w11"]
    assert b_segs[0].start_utc < at(cut)
    assert_words(await env.words(), expected)
    cov = await env.coverage(0, len(pcm) / SAMPLE_RATE)
    assert cov.complete and cov.provisional == ()


async def test_no_takeover_across_a_hole(env):
    pcm, expected = synth_speech(SCRIPT)
    cut = cut_inside(expected, "w5.")
    total = len(pcm) / SAMPLE_RATE
    a = await env.add_chunk(0, 0.0, cut, pcm[: samples(cut)])
    b = await env.add_chunk(1, cut + 0.8, total, pcm[samples(cut + 0.8) :])  # > CHUNK_JITTER
    await env.handler.transcribe(a)
    ta = await env.transcript(a)
    out = await env.handler.transcribe(b)
    assert not out.plan.takeover and out.plan.window_start == at(cut + 0.8)
    assert await env.transcript(a) == ta and ta.tail_from is not None  # A keeps its tail
    assert (await env.transcript(b)).owned_from == at(cut + 0.8)


async def test_fill_gaps_chunks_hand_over_only_inside_their_range(env):
    """Backfill `--fill-gaps` (DESIGN §17): a live chunk, fill chunks (seqs from FILL_SEQ_BASE)
    starting 0.2 s after it (within the jitter) and one in a later range. Neighbours are read by
    seq: nothing is taken from the live chunk, the usual handover runs inside a range, none
    across ranges."""
    pcm, expected = synth_speech(SCRIPT)
    total = len(pcm) / SAMPLE_RATE
    c1, c2, c3 = (cut_inside(expected, w) for w in ("w5.", "w11", "w15"))
    backfill = Priority.BACKFILL
    live = await env.add_chunk(0, 0.0, c1, pcm[: samples(c1)])
    f0 = await env.add_chunk(
        FILL_SEQ_BASE, c1 + 0.2, c2, pcm[samples(c1 + 0.2) : samples(c2)], priority=backfill
    )
    f1 = await env.add_chunk(
        FILL_SEQ_BASE + 1, c2, c3, pcm[samples(c2) : samples(c3)], priority=backfill
    )
    f2 = await env.add_chunk(
        FILL_SEQ_BASE + 2, c3 + 1.0, total, pcm[samples(c3 + 1.0) :], priority=backfill
    )
    await env.handler.transcribe(live)
    t_live = await env.transcript(live)
    assert t_live.tail_from is not None
    outs = [await env.handler.transcribe(c) for c in (f0, f1, f2)]
    assert not outs[0].plan.takeover and outs[0].plan.window_start == at(c1 + 0.2)
    assert outs[1].plan.takeover  # inside one range: the usual handover
    assert not outs[2].plan.takeover and outs[2].plan.window_start == at(c3 + 1.0)
    assert await env.transcript(live) == t_live  # the live chunk keeps its row, tail and all
    t0, t1 = await env.transcript(f0), await env.transcript(f1)
    assert t0.owned_to == t1.owned_from and t0.tail_from is None
    assert t1.tail_from is not None  # no contiguous next chunk: it keeps its tail


async def test_takeover_across_a_jitter_hole_keeps_times_exact(env):
    pcm, expected = synth_speech(SCRIPT)
    w5 = next(w for w in expected if w.text == "w5.")
    cut = round(w5.end + 0.38, 3)  # after the full stop: "w4 w5." ends within 0.5 s of the cut
    total = len(pcm) / SAMPLE_RATE
    a = await env.add_chunk(0, 0.0, cut, pcm[: samples(cut)])
    # a 300 ms jitter hole (inside the pause, so no word is lost with it)
    b = await env.add_chunk(1, cut + 0.3, total, pcm[samples(cut + 0.3) :])
    await env.handler.transcribe(a)
    out = await env.handler.transcribe(b)
    assert out.plan.takeover  # the window audio is zero-padded over the hole
    ta, tb = await env.transcript(a), await env.transcript(b)
    assert ta.owned_to == tb.owned_from and ta.tail_from is None
    assert_words(await env.words(), expected)  # also every word after the hole on time


async def test_takeover_of_a_tail_inside_a_jitter_overlap(sessions, tmp_path):
    # A wider tolerance than CHUNK_JITTER only to fit a detectable tail sentence (>= 250 ms of
    # speech) into the overlap; the rules are the same for any overlap.
    env = await make_env(sessions, tmp_path, contiguity_tolerance_s=1.0)
    pcm, expected = synth_speech(SCRIPT)
    w6 = next(w for w in expected if w.text == "w6")
    cut = round(w6.start - 0.3, 3)
    total = len(pcm) / SAMPLE_RATE
    # A runs 800 ms into B: its last sentence (w6 w7, cut) starts after B's start
    a = await env.add_chunk(0, 0.0, cut + 0.8, pcm[: samples(cut + 0.8)])
    b = await env.add_chunk(1, cut, total, pcm[samples(cut) :])
    await env.handler.transcribe(a)
    ta = await env.transcript(a)
    assert ta.tail_from > at(cut)
    async with env.sessions.begin() as s:  # a non-provisional segment in [B.start, A.tail)
        await s.execute(
            text(
                "INSERT INTO transcript_segments (stream_id, chunk_id, start_utc, end_utc, text,"
                " model) VALUES (:s, :c, :t, :t, 'stray', 'fake-tones')"
            ),
            {"s": env.stream_id, "c": a, "t": at(cut + 0.01)},
        )

    out = await env.handler.transcribe(b)
    assert out.plan.takeover and out.plan.window_start == at(cut)
    ta = await env.transcript(a)
    assert ta.owned_to == at(cut) and ta.tail_from is None
    a_segs = await env.segments(a)
    assert a_segs and all(seg.start_utc < ta.owned_to for seg in a_segs)
    assert ta.segment_count == len(a_segs)
    assert_words(await env.words(), expected)


async def bump_tail(env: Env, chunk_id: int) -> None:
    """Another worker re-transcribes `chunk_id` meanwhile: its tail moves by 1 ms."""
    async with env.sessions.begin() as s:
        await s.execute(
            text(
                "UPDATE chunk_transcripts SET tail_from = tail_from + interval '1 ms'"
                " WHERE chunk_id = :c"
            ),
            {"c": chunk_id},
        )


async def test_conflict_with_the_previous_chunk_is_planned_again(env, monkeypatch, caplog):
    pcm, expected = synth_speech(SCRIPT)
    cut1 = cut_inside(expected, "w5.")
    a, b = await env.add_stream_chunks(pcm, [cut1])
    runner = env.runner()
    assert await runner.run_once()  # A
    real = handler_mod.process_window
    calls: list[int] = []

    async def meanwhile(*args, **kwargs):
        if not calls:
            calls.append(1)
            await bump_tail(env, a)
        return await real(*args, **kwargs)

    monkeypatch.setattr(handler_mod, "process_window", meanwhile)
    assert await runner.run_once()  # B: conflict, planned again from A's new tail, done
    job = await env.job(b)
    assert job.status == JobStatus.DONE and job.attempts == 1
    assert "handover conflict, planning again" in caplog.messages
    ta, tb = await env.transcript(a), await env.transcript(b)
    assert ta.owned_to == tb.owned_from and ta.tail_from is None
    assert_words(await env.words(), expected)


async def test_conflicts_that_keep_coming_go_back_to_the_job_runner(env, monkeypatch):
    pcm, expected = synth_speech(SCRIPT)
    cut1 = cut_inside(expected, "w5.")
    a, b = await env.add_stream_chunks(pcm, [cut1])
    runner = env.runner()
    assert await runner.run_once()  # A
    real = handler_mod.process_window
    plans: list[int] = []

    async def always(*args, **kwargs):
        plans.append(1)
        await bump_tail(env, a)
        return await real(*args, **kwargs)

    monkeypatch.setattr(handler_mod, "process_window", always)
    assert await runner.run_once()
    assert len(plans) == HANDOVER_ATTEMPTS
    job = await env.job(b)
    assert job.status == JobStatus.QUEUED and job.attempts == 1
    assert job.last_error.startswith("HandoverConflict: chunk")
    assert await env.transcript(b) is None and env.handler.stats.failed == 1
    assert keys.chunk_words(env.stream_id, 1) not in env.store.objects  # nothing uploaded

    monkeypatch.undo()
    async with env.sessions.begin() as s:  # skip the retry backoff
        await s.execute(text("UPDATE jobs SET run_after = now()"))
    assert await runner.run_once()
    job = await env.job(b)
    assert job.status == JobStatus.DONE and job.attempts == 2
    assert_words(await env.words(), expected)


async def test_conflict_with_the_next_chunk_done_meanwhile(env, monkeypatch):
    pcm, expected = synth_speech(SCRIPT)
    cut1 = cut_inside(expected, "w5.")
    a, b = await env.add_stream_chunks(pcm, [cut1])
    real = handler_mod.process_window
    calls: list[int] = []

    async def meanwhile(*args, **kwargs):
        if not calls:  # another worker transcribes B (out of order) while A is processed
            calls.append(1)
            await env.handler.transcribe(b)
        return await real(*args, **kwargs)

    monkeypatch.setattr(handler_mod, "process_window", meanwhile)
    out = await env.handler.transcribe(a)  # planned again: B is done, no tail, ends at B
    assert not out.plan.keep_tail and out.plan.owned_to == at(cut1)
    ta, tb = await env.transcript(a), await env.transcript(b)
    assert ta.owned_to == tb.owned_from and ta.tail_from is None


async def test_stale_duplicate_attempt_never_overwrites_the_words_file(env, monkeypatch):
    """Attempt 1 of B plans while A is untranscribed; meanwhile A is done and a duplicate
    attempt of B (stale requeue) commits a takeover. Attempt 1 then conflicts before uploading,
    plans again (re-take) and writes the same result."""
    pcm, expected = synth_speech(SCRIPT)
    cut1 = cut_inside(expected, "w5.")
    a, b = await env.add_stream_chunks(pcm, [cut1])
    real = handler_mod.process_window
    calls: list[int] = []

    async def meanwhile(*args, **kwargs):
        result = await real(*args, **kwargs)
        if not calls:
            calls.append(1)
            await env.handler.transcribe(a)
            assert (await env.handler.transcribe(b)).plan.takeover
        return result

    monkeypatch.setattr(handler_mod, "process_window", meanwhile)
    out = await env.handler.transcribe(b)
    assert out.plan.retake
    tb = await env.transcript(b)
    key = keys.chunk_words(env.stream_id, 1)
    t0s = [WordsFile.model_validate_json(data).t0 for k, data in env.store.uploads if k == key]
    assert t0s == [tb.owned_from, tb.owned_from]  # the duplicate's and the re-plan's
    assert_words(await env.words(), expected)


async def test_takeover_while_the_next_chunk_is_already_done(env):
    pcm, expected = synth_speech(SCRIPT)
    cut1, cut2 = cut_inside(expected, "w5."), cut_inside(expected, "w12.")
    a, b, c = await env.add_stream_chunks(pcm, [cut1, cut2])
    await env.handler.transcribe(a)
    await env.handler.transcribe(c)  # out of order
    out = await env.handler.transcribe(b)
    assert out.plan.takeover and not out.plan.keep_tail and out.plan.owned_to == at(cut2)
    ta, tb, tc = [await env.transcript(x) for x in (a, b, c)]
    assert ta.owned_to == tb.owned_from and tb.owned_to == tc.owned_from == at(cut2)
    assert ta.tail_from is None and tb.tail_from is None
    assert not any(seg.provisional for seg in await env.segments())
    cov = await env.coverage(0, len(pcm) / SAMPLE_RATE)
    assert cov.complete and cov.provisional == ()
    # every word; only the word cut at B/C may come twice (out of order there)
    got = [w.text.rstrip(".") for w in await env.words()]
    assert set(got) == {w.text.rstrip(".") for w in expected}
    assert len(got) - len(set(got)) <= 1


async def test_reruns_after_a_capped_takeover(sessions, tmp_path):
    env = await make_env(sessions, tmp_path, takeover_cap_s=2.0)
    script = "w1 w2 . w3 w4 w5 w6 w7 w8 w9 w10 . w11"
    pcm, expected = synth_speech(script)
    w9 = next(w for w in expected if w.text == "w9")
    a, b = await env.add_stream_chunks(pcm, [round(w9.start + 0.28, 3)])
    await env.handler.transcribe(a)
    assert (await env.handler.transcribe(b)).plan.prev_capped_from is not None
    before = without_files(await snapshot(env, [a, b]))

    again = await env.handler.transcribe(b)  # N again: re-take, P's rebuilt segments stay
    assert again.plan.retake and not again.plan.takeover
    assert without_files(await snapshot(env, [a, b])) == before
    rerun = await env.handler.transcribe(a)  # P again: up to N's owned_from, no tail
    assert not rerun.plan.keep_tail
    after = without_files(await snapshot(env, [a, b]))
    assert (after["rows"], after["words"], after["metrics"]) == (
        before["rows"],
        before["words"],
        before["metrics"],
    )
    # the same sentences; now with the engine's scores instead of rebuilt from words
    assert [seg[:7] for seg in after["segments"]] == [seg[:7] for seg in before["segments"]]
    assert [s.text for s in await env.segments(a)] == ["w1 w2.", "w3 w4"]
    assert_words(await env.words(), expected)


class FlakyStore(MemoryStore):
    """Downloads of `fail_key` fail at once, of `slow_key` take a while."""

    def __init__(self, objects: dict[str, bytes], fail_key: str, slow_key: str) -> None:
        super().__init__()
        self.objects = objects
        self.fail_key, self.slow_key = fail_key, slow_key
        self.written: list[Path] = []

    def download_file(self, key: str, path) -> None:
        if key == self.fail_key:
            raise OSError("storage unavailable")
        if key == self.slow_key:
            time.sleep(0.3)
        super().download_file(key, path)
        self.written.append(Path(path))


async def test_failed_download_waits_for_the_other_and_cleans_up(env):
    pcm, expected = synth_speech(SCRIPT)
    a, b = await env.add_stream_chunks(pcm, [cut_inside(expected, "w5.")])
    await env.handler.transcribe(a)  # tail: B's window needs A's audio
    store = FlakyStore(
        env.store.objects, keys.chunk_audio(env.stream_id, 0), keys.chunk_audio(env.stream_id, 1)
    )
    env.handler.store = store
    with pytest.raises(OSError, match="storage unavailable"):
        await env.handler.transcribe(b)
    # B's own (slow) download finished before the temp dir was removed, then it was removed
    assert len(store.written) == 1 and not store.written[0].parent.exists()
    assert await env.transcript(b) is None


# --------------------------------------------------------------------------- heartbeat


async def test_heartbeat_reports_models_stats_and_queue_depth(sessions, settings, tmp_path):
    env = await make_env(sessions, tmp_path)
    pcm, _ = synth_speech("w1 w2 .")
    await env.add_chunk(0, 0.0, 2.0, pcm, priority=Priority.TRIGGER)
    await env.add_chunk(1, 2.0, 4.0, None, priority=Priority.LIVE)
    await env.add_chunk(2, 4.0, 6.0, None, priority=Priority.LIVE)
    ts = transcriber_settings()
    service = TranscriberService(
        sessions, env.store, build_engines(ts), ts, worker_id="tr-1", dsn=settings.database_dsn
    )

    async def info():
        async with sessions() as s:
            return (
                await s.execute(
                    text("SELECT service, info FROM worker_heartbeats WHERE worker_id = 'tr-1'")
                )
            ).one()

    await service.beat()
    row = await info()
    assert row.service == "transcriber"
    assert row.info["queue"] == {"80": 1, "10": 2}
    assert row.info["models"] == {
        "speech": "fake-tones",
        "tagger": "fake-loudness",
        "vad": "energy-vad",
    }
    assert row.info["device"] == "cpu" and row.info["realtime_factor"] is None
    assert (row.info["jobs_done"], row.info["jobs_failed"]) == (0, 0)

    assert await service.runner.run_once()  # the TRIGGER job first
    await service.beat()
    row = await info()
    assert row.info["queue"] == {"10": 2} and row.info["jobs_done"] == 1
    assert row.info["realtime_factor"] > 1 and row.info["current_chunk"] is None


async def test_service_runs_jobs_and_stops_gracefully(sessions, settings, tmp_path):
    env = await make_env(sessions, tmp_path)
    ts = transcriber_settings(heartbeat_interval_s=0.2)
    service = TranscriberService(
        sessions,
        env.store,
        build_engines(ts),
        ts,
        worker_id="tr-2",
        dsn=settings.database_dsn,
        poll_interval=60,  # only NOTIFY can wake it up in time
    )
    stop = asyncio.Event()
    task = asyncio.create_task(service.run(stop))
    try:
        await asyncio.sleep(1)  # idle and LISTENing
        pcm, _ = synth_speech("w1 w2 .")
        chunk_id = await env.add_chunk(0, 0.0, 2.0, pcm)
        for _ in range(100):
            if (await env.job(chunk_id)).status == JobStatus.DONE:
                break
            await asyncio.sleep(0.1)
        assert (await env.job(chunk_id)).status == JobStatus.DONE
        assert await env.transcript(chunk_id) is not None
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)
    async with sessions() as s:
        info = (
            await s.execute(text("SELECT info FROM worker_heartbeats WHERE worker_id = 'tr-2'"))
        ).scalar_one()
    assert info["models"]["speech"] == "fake-tones"


async def test_stop_waits_for_the_job_in_progress(sessions, settings, tmp_path, monkeypatch):
    env = await make_env(sessions, tmp_path)
    ts = transcriber_settings()
    service = TranscriberService(
        sessions, env.store, build_engines(ts), ts, worker_id="tr-3", dsn=settings.database_dsn
    )
    started, release = asyncio.Event(), asyncio.Event()
    real = handler_mod.process_window

    async def slow(*args, **kwargs):
        started.set()
        await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(handler_mod, "process_window", slow)
    pcm, _ = synth_speech("w1 w2 .")
    chunk_id = await env.add_chunk(0, 0.0, 2.0, pcm)
    stop = asyncio.Event()
    task = asyncio.create_task(service.run(stop))
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        stop.set()  # SIGTERM while the job runs
        await asyncio.sleep(0.3)
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, timeout=10)
    finally:
        release.set()
        stop.set()
        if not task.done():
            task.cancel()
    assert (await env.job(chunk_id)).status == JobStatus.DONE
    assert await env.transcript(chunk_id) is not None
