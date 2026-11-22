"""A small world for the judge service's integration tests (DESIGN §19 build item 6c).

One channel and live stream, 60 s chunks around a candidate's peak T (video + audio keys, each
with a queued `transcribe` job), transcripts written as the transcriber commits them (words
files in an in-memory store, `chunk_transcripts` rows, NOTIFY `clipper_transcripts`), candidates
emitted as the detector emits them (row + TRIGGER media request + `judge:{cand}` job), and the
judge service with an injectable clock and model. Uses the root conftest's throwaway database
(`sessions`, `settings`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from clipper_core import jobs, media
from clipper_core.contracts import (
    TRANSCRIPTS_NOTIFY_CHANNEL,
    JobKind,
    JudgePayload,
    Priority,
    Word,
    WordsFile,
    dedupe_key,
)
from clipper_core.partitions import ensure_chat_partitions
from clipper_core.storage import keys
from clipper_core.timeline import Interval, utc_now
from clipper_judge.model import JudgeModel
from clipper_judge.revisions import Revision
from clipper_judge.samples import SCRIPT, WORD_GAP_S, WORD_S
from clipper_judge.service import JudgeService
from clipper_judge.settings import JudgeSettings


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key]

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.objects[key] = data


class DictRevisions:
    """A `RevisionSource` in memory (review_events doesn't exist yet)."""

    def __init__(self) -> None:
        self.events: dict[int, Revision] = {}

    async def load(self, session, review_event_id: int) -> Revision | None:
        return self.events.get(review_event_id)


def script_words(script=SCRIPT) -> list[tuple[str, float, float]]:
    """(text, start, end) relative to T: the sample transcript of `clipper_judge.samples`."""
    out = []
    for start, sentence, _ in script:
        t = start
        for word in sentence.split():
            out.append((word, round(t, 3), round(t + WORD_S, 3)))
            t += WORD_S + WORD_GAP_S
    return out


@dataclass
class Chunk:
    id: int
    seq: int
    start: float  # relative to T
    end: float


class World:
    """T (the candidate's peak) lies 40 s before the real time when the world is made, and the
    injected clock starts at T + 40 s: deferred jobs (`run_after` from the clock) really wait
    for the database's `now()`, as in production."""

    def __init__(self, sessions, settings, *, recording_mode: str = "full") -> None:
        self.T = utc_now().replace(microsecond=0) - timedelta(seconds=40)
        self.S = self.T - timedelta(minutes=30)  # stream start
        self.sessions = sessions
        self.db_settings = settings
        self.store = MemoryStore()
        self.clock = Clock(self.at(40))  # ~ when the detector emits (episode closed)
        self.recording_mode = recording_mode
        self.chunks: list[Chunk] = []
        self.revisions = DictRevisions()
        self.words = script_words()

    def at(self, rel_s: float) -> datetime:
        return self.T + timedelta(seconds=rel_s)

    async def setup(self, *, video: bool = True, offset: float = 0.0, span=(-480, 180)) -> World:
        async with self.sessions.begin() as s:
            self.channel_id = (
                await s.execute(
                    text(
                        "INSERT INTO channels (login, recording_mode) VALUES ('xqc', :m)"
                        " RETURNING id"
                    ),
                    {"m": self.recording_mode},
                )
            ).scalar_one()
            self.stream_id = (
                await s.execute(
                    text(
                        "INSERT INTO streams (channel_id, platform_stream_id, title, category,"
                        " started_at) VALUES (:c, 'b-1', 'JUMP KING ALL DAY', 'Jump King', :t)"
                        " RETURNING id"
                    ),
                    {"c": self.channel_id, "t": self.S},
                )
            ).scalar_one()
        start = span[0] + offset
        seq = 0
        while start < span[1]:
            await self.add_chunk(seq, start, start + 60, video=video)
            seq += 1
            start += 60
        return self

    async def add_chunk(self, seq: int, a: float, b: float, *, video: bool = True) -> Chunk:
        async with self.sessions.begin() as s:
            chunk_id = (
                await s.execute(
                    text(
                        "INSERT INTO chunks (stream_id, seq, start_utc, end_utc, video_key,"
                        " audio_key) VALUES (:s, :q, :a, :b, :v, :k) RETURNING id"
                    ),
                    {
                        "s": self.stream_id,
                        "q": seq,
                        "a": self.at(a),
                        "b": self.at(b),
                        "v": keys.chunk_video(self.stream_id, seq) if video else None,
                        "k": keys.chunk_audio(self.stream_id, seq),
                    },
                )
            ).scalar_one()
            await jobs.enqueue(
                s,
                JobKind.TRANSCRIBE,
                payload={"chunk_id": chunk_id},
                priority=Priority.LIVE,
                ref_id=chunk_id,
                dedupe_key=dedupe_key(JobKind.TRANSCRIBE, chunk_id),
            )
        chunk = Chunk(chunk_id, seq, a, b)
        self.chunks.append(chunk)
        return chunk

    def chunks_in(self, a: float, b: float) -> list[Chunk]:
        return [c for c in self.chunks if c.start < b and c.end > a]

    async def transcribe(self, chunks: Iterable[Chunk], *, notify: bool = False) -> None:
        """As the transcriber commits: words file, transcript row, job done (+ NOTIFY)."""
        async with self.sessions.begin() as s:
            for c in chunks:
                key = keys.chunk_words(self.stream_id, c.seq)
                file = WordsFile(
                    chunk_id=c.id,
                    stream_id=self.stream_id,
                    seq=c.seq,
                    model="fake",
                    t0=self.at(c.start),
                    words=[
                        Word(text=w, start=a - c.start, end=e - c.start, confidence=0.9)
                        for w, a, e in self.words
                        if c.start <= a < c.end
                    ],
                )
                self.store.objects[key] = file.model_dump_json().encode()
                await s.execute(
                    text(
                        "INSERT INTO chunk_transcripts (chunk_id, model, words_key,"
                        " segment_count, owned_from, owned_to) VALUES (:c, 'fake', :k, 0, :f, :t)"
                    ),
                    {"c": c.id, "k": key, "f": self.at(c.start), "t": self.at(c.end)},
                )
                await s.execute(
                    text(
                        "UPDATE jobs SET status = 'done' WHERE kind = 'transcribe' AND ref_id = :c"
                    ),
                    {"c": c.id},
                )
            if notify:
                await s.execute(
                    text("SELECT pg_notify(:ch, :p)"),
                    {"ch": TRANSCRIPTS_NOTIFY_CHANNEL, "p": str(self.stream_id)},
                )

    async def transcribe_range(self, a: float, b: float, **kw) -> None:
        await self.transcribe([c for c in self.chunks_in(a, b) if not await self._done(c)], **kw)

    async def _done(self, c: Chunk) -> bool:
        rows = await self.fetch("SELECT 1 FROM chunk_transcripts WHERE chunk_id = :c", c=c.id)
        return bool(rows)

    async def candidate(
        self,
        *,
        peak: float = 0.0,
        moment: tuple[float, float] = (-6.0, 4.0),
        window: tuple[float, float] = (-90.0, 30.0),
        score: float = 7.4,
        judge_job: bool = True,
        max_attempts: int = 5,
    ) -> int:
        """As the detector emits: row + TRIGGER media request + `judge:{cand}` job."""
        async with self.sessions.begin() as s:
            cand = (
                await s.execute(
                    text(
                        """
                        INSERT INTO candidates (stream_id, channel_id, source, status, trigger,
                               peak_utc, chat_peak_utc, chat_delay_s, delay_source, moment_from,
                               moment_to, window_from, window_to, score, moment_type,
                               detector_version)
                        VALUES (:s, :c, 'live', 'emitted', 'chat', :peak, :chat_peak, 8.0,
                                'default', :mf, :mt, :wf, :wt, :score, 'funny', 'test')
                        RETURNING id
                        """
                    ),
                    {
                        "s": self.stream_id,
                        "c": self.channel_id,
                        "peak": self.at(peak),
                        "chat_peak": self.at(peak + 8),
                        "mf": self.at(peak + moment[0]),
                        "mt": self.at(peak + moment[1]),
                        "wf": self.at(peak + window[0]),
                        "wt": self.at(peak + window[1]),
                        "score": score,
                    },
                )
            ).scalar_one()
            req = await media.request_media(
                s,
                stream_id=self.stream_id,
                window=Interval(self.at(peak + window[0]), self.at(peak + window[1])),
                priority=Priority.TRIGGER,
                requested_by="detector",
            )
            await s.execute(
                text("UPDATE candidates SET media_request_id = :m WHERE id = :id"),
                {"m": req, "id": cand},
            )
            if judge_job:
                await jobs.enqueue(
                    s,
                    JobKind.JUDGE,
                    payload=JudgePayload(candidate_id=cand).model_dump(),
                    priority=Priority.TRIGGER,
                    ref_id=cand,
                    dedupe_key=dedupe_key(JobKind.JUDGE, cand),
                    max_attempts=max_attempts,
                )
        return cand

    async def chat(self, rows: Iterable[tuple[float, str, str]]) -> None:
        """Chat messages (video time relative to T, user, text) at chat time = video + 8 s."""
        async with self.sessions.begin() as s:
            await ensure_chat_partitions(s, self.S.date(), self.at(180).date())
            for i, (rel, user, msg) in enumerate(rows):
                await s.execute(
                    text(
                        "INSERT INTO chat_messages (sent_at, msg_id, channel_id, stream_id,"
                        " user_id, user_login, text) VALUES (:t, :m, :c, :s, :u, :u, :x)"
                    ),
                    {
                        "t": self.at(rel + 8),
                        "m": f"m{i}",
                        "c": self.channel_id,
                        "s": self.stream_id,
                        "u": user,
                        "x": msg,
                    },
                )

    def settings(self, **kw: Any) -> JudgeSettings:
        kw.setdefault("concurrency", 1)
        return JudgeSettings(_env_file=None, **kw)

    def service(self, model: JudgeModel, **kw: Any) -> JudgeService:
        return JudgeService(
            self.sessions,
            self.store,
            model,
            self.settings(**kw),
            worker_id="judge-test",
            dsn=self.db_settings.database_dsn,
            clock=self.clock,
            revisions=self.revisions,
        )

    async def run_jobs(self, service: JudgeService, limit: int = 20) -> int:
        """Run runnable judge/revise jobs until none is left; returns how many ran."""
        n = 0
        while n < limit and await service.runners[0].run_once():
            n += 1
        return n

    async def make_runnable(self) -> None:
        """Jobs waiting for a retry backoff or a deferral: runnable now."""
        async with self.sessions.begin() as s:
            await s.execute(text("UPDATE jobs SET run_after = now() WHERE status = 'queued'"))

    async def fetch(self, sql: str, **params: Any) -> list[Any]:
        async with self.sessions() as s:
            return list((await s.execute(text(sql), params)).all())

    async def one(self, sql: str, **params: Any) -> Any:
        rows = await self.fetch(sql, **params)
        assert len(rows) == 1, rows
        return rows[0]

    async def clip(self, candidate_id: int) -> Any:
        return await self.one(
            "SELECT *, xmin::text AS x FROM clips WHERE candidate_id = :c", c=candidate_id
        )

    async def plans(self, clip_id: int) -> list[Any]:
        return await self.fetch(
            "SELECT *, xmin::text AS x FROM clip_plans WHERE clip_id = :c ORDER BY version",
            c=clip_id,
        )

    async def jobs(self, kind: str) -> list[Any]:
        return await self.fetch(
            "SELECT *, xmin::text AS x FROM jobs WHERE kind = :k ORDER BY id", k=kind
        )

    async def calls(self) -> list[Any]:
        return await self.fetch("SELECT *, xmin::text AS x FROM judge_calls ORDER BY id")


@pytest.fixture
async def world(sessions, settings) -> World:
    return await World(sessions, settings).setup()


@pytest.fixture
def make_world(sessions, settings):
    """Factory for a world with other media: `await make_world(recording_mode=..., video=...,
    offset=...)`."""

    async def make(*, recording_mode: str = "full", **setup: Any) -> World:
        return await World(sessions, settings, recording_mode=recording_mode).setup(**setup)

    return make
