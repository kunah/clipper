"""Randomised model of the chunk handover (pure, seeded, fast).

`plan_window` plus the handler's commit rules, with interleaved workers, duplicate attempts of a
job (stale requeue), conflicts with re-planning (`HANDOVER_ATTEMPTS`, then a job retry),
failures after the words upload, re-runs, and chunk jitter (holes and overlaps up to
`CHUNK_JITTER`, and real holes). The engine is ideal: a window's words file holds every true
word it can hear in `[window_start, owned_to)`. Checked after every step: the CHECKs, tiling of
contiguous neighbours, and that an upload which isn't committed never changes what readers see;
at the end: every audible word is owned exactly once (with `transcripts.words()`' rules).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from clipper_core.contracts import CHUNK_JITTER
from clipper_transcriber.handler import (
    HANDOVER_ATTEMPTS,
    ChunkState,
    TranscriptState,
    WindowPlan,
    contiguous,
    plan_window,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
SEEDS = 300


def at(seconds: float) -> datetime:
    return T0 + timedelta(microseconds=round(seconds * 1e6))


@dataclass(frozen=True)
class Attempt:
    chunk: int
    plan: WindowPlan
    seen: tuple[TranscriptState | None, ...]
    plans: int  # how many plans this job attempt has made


class Model:
    def __init__(self, seed: int, *, duplicates: bool, jitter: bool) -> None:
        self.rnd = rnd = random.Random(seed)
        self.n = n = rnd.randint(2, 6)
        self.starts: list[datetime] = []
        self.ends: list[datetime] = []
        pos = 0.0
        for i in range(n):
            if i and jitter:
                pos += rnd.choice([-0.45, -0.04, 0.0, 0.0, 0.03, 0.3, 0.5, 0.8])  # 0.8: a hole
            end = pos + rnd.choice([3, 10, 40, 60, 61.5])
            self.starts.append(at(pos))
            self.ends.append(at(end))
            pos = end
        self.words = sorted({at(round(rnd.uniform(0, pos), 3)) for _ in range(200)})
        self.cap = timedelta(seconds=rnd.choice([5, 30]))
        self.duplicates = duplicates
        self.jitter = jitter
        self.rows: dict[int, TranscriptState] = {}
        self.files: dict[int, list[datetime]] = {}  # one key per chunk, overwritten
        self.errors: list[str] = []

    # ------------------------------------------------------------------ state

    def state(self, i: int) -> ChunkState | None:
        if not 0 <= i < self.n:
            return None
        return ChunkState(i, 1, i, self.starts[i], self.ends[i], f"audio/{i}", self.rows.get(i))

    def snapshot(self, i: int) -> tuple[TranscriptState | None, ...]:
        return tuple(self.rows.get(j) for j in (i - 1, i, i + 1))

    def plan(self, i: int, plans: int) -> Attempt:
        p = plan_window(self.state(i), self.state(i - 1), self.state(i + 1), cap=self.cap)
        return Attempt(i, p, self.snapshot(i), plans)

    def hears(self, i: int, w: datetime) -> bool:
        """The window audio: N's own audio, and P's audio only up to N's start."""
        if self.starts[i] <= w < self.ends[i]:
            return True
        return i > 0 and self.starts[i - 1] <= w < min(self.ends[i - 1], self.starts[i])

    def read_words(self) -> list[datetime]:
        """`transcripts.words()`: owned ranges by owned_from, a later range cuts an earlier."""
        order = sorted(
            (j for j, r in self.rows.items() if r.owned_to > r.owned_from),
            key=lambda j: (self.rows[j].owned_from, j),
        )
        got: list[datetime] = []
        for k, j in enumerate(order):
            r = self.rows[j]
            hi = r.owned_to
            if k + 1 < len(order):
                hi = min(hi, self.rows[order[k + 1]].owned_from)
            got += [w for w in self.files[j] if r.owned_from <= w < hi]
        return sorted(got)

    # ------------------------------------------------------------------ steps

    def commit(self, a: Attempt) -> bool:
        """The locked transaction of one attempt. False: conflict or failure (retry later)."""
        i, plan = a.chunk, a.plan
        if self.snapshot(i) != a.seen:
            return False  # HandoverConflict
        view = self.read_words()
        self.files[i] = [
            w for w in self.words if plan.window_start <= w < plan.owned_to and self.hears(i, w)
        ]
        if self.rnd.random() < 0.1:  # the transaction fails after the upload: rolled back
            if i in self.rows and self.read_words() != view:
                self.errors.append(f"uncommitted upload of {i} changed what readers see")
            return False
        tail = None
        if plan.keep_tail and self.rnd.random() < 0.7:
            span = (plan.owned_to - plan.window_start) / timedelta(microseconds=1)
            tail = plan.window_start + timedelta(microseconds=self.rnd.randrange(int(span) + 1))
            if not plan.window_start <= tail < plan.owned_to:
                tail = None
        self.rows[i] = TranscriptState(
            i, plan.window_start, plan.owned_to, tail, f"words/{i}", "m", "en"
        )
        if plan.takeover:
            self.rows[i - 1] = replace(
                self.rows[i - 1], owned_to=plan.prev_new_owned_to, tail_from=None
            )
        self.check_rows()
        return True

    def check_rows(self) -> None:
        for j, r in self.rows.items():
            if not r.owned_from <= r.owned_to:
                self.errors.append(f"CHECK owned_range of {j}: {r}")
            if r.tail_from is not None and not r.owned_from <= r.tail_from < r.owned_to:
                self.errors.append(f"CHECK tail of {j}: {r}")
            q = self.rows.get(j + 1)
            if q is None or not contiguous(self.ends[j], self.starts[j + 1], CHUNK_JITTER):
                continue
            if not self.jitter and r.owned_to != q.owned_from:
                self.errors.append(f"tiling {j}/{j + 1}: {r.owned_to} != {q.owned_from}")
            if abs(r.owned_to - q.owned_from) > CHUNK_JITTER:
                self.errors.append(f"owned ranges {j}/{j + 1} apart by more than the jitter")

    def run(self) -> list[str]:
        queue = list(range(self.n))
        self.rnd.shuffle(queue)
        inflight: list[Attempt] = []
        for _ in range(5000):
            if not queue and not inflight:
                break
            if queue and (self.rnd.random() < 0.5 or not inflight):
                i = queue.pop(self.rnd.randrange(len(queue)))
                if not self.duplicates and any(a.chunk == i for a in inflight):
                    queue.append(i)
                    continue
                inflight.append(self.plan(i, 1))
                continue
            a = inflight.pop(self.rnd.randrange(len(inflight)))
            if self.commit(a):
                if self.rnd.random() < 0.15:  # a re-run of some chunk later
                    queue.append(self.rnd.randrange(self.n))
            elif self.snapshot(a.chunk) != a.seen and a.plans < HANDOVER_ATTEMPTS:
                inflight.append(self.plan(a.chunk, a.plans + 1))  # re-plan in the handler
            else:
                queue.append(a.chunk)  # the job runner retries the job
        else:
            return [*self.errors, "did not finish"]

        if set(self.rows) != set(range(self.n)):
            self.errors.append("some chunks never got a transcript")
        audible = [w for w in self.words if any(self.hears(j, w) for j in range(self.n))]
        got = self.read_words()
        if got != audible:
            lost = len(set(audible) - set(got))
            self.errors.append(f"words: {lost} lost, {len(got) - len(set(got))} duplicated")
        return self.errors


@pytest.mark.parametrize("jitter", [False, True])
@pytest.mark.parametrize("duplicates", [False, True])
def test_randomised_handover_keeps_every_word_exactly_once(duplicates, jitter):
    failures = {}
    for seed in range(SEEDS):
        errors = Model(seed, duplicates=duplicates, jitter=jitter).run()
        if errors:
            failures[seed] = errors[:3]
    assert not failures, dict(list(failures.items())[:3])
