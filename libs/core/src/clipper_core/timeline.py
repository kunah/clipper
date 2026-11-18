"""One clock for everything: timezone-aware UTC datetimes.

Video time comes from HLS EXT-X-PROGRAM-DATE-TIME, chat time from Twitch's `tmi-sent-ts`.
Viewers react later than the video shows, so: video_time ~= chat_time - channel_delay.
Naive datetimes are rejected everywhere, because mixing them silently ruins every clip cut.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"naive datetime is not allowed: {dt!r}")
    return dt.astimezone(UTC)


def from_unix_ms(ms: int | str) -> datetime:
    """Twitch IRC `tmi-sent-ts` tag (milliseconds since epoch) -> UTC datetime."""
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)


def chat_to_video_time(chat_ts: datetime, delay: timedelta) -> datetime:
    """Map a chat timestamp back to the moment on screen that viewers were reacting to."""
    return ensure_utc(chat_ts) - delay


@dataclass(frozen=True, slots=True, order=True)
class Interval:
    """Half-open time range [start, end) in UTC."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", ensure_utc(self.start))
        object.__setattr__(self, "end", ensure_utc(self.end))
        if self.end < self.start:
            raise ValueError(f"interval ends before it starts: {self.start} > {self.end}")

    @classmethod
    def around(cls, ts: datetime, before: timedelta, after: timedelta) -> Interval:
        return cls(ts - before, ts + after)

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def seconds(self) -> float:
        return self.duration.total_seconds()

    def contains(self, ts: datetime) -> bool:
        return self.start <= ensure_utc(ts) < self.end

    def overlaps(self, other: Interval) -> bool:
        return self.start < other.end and other.start < self.end

    def intersection(self, other: Interval) -> Interval | None:
        start, end = max(self.start, other.start), min(self.end, other.end)
        return Interval(start, end) if start < end else None

    def expand(self, before: timedelta = timedelta(0), after: timedelta = timedelta(0)) -> Interval:
        return Interval(self.start - before, self.end + after)

    def shift(self, delta: timedelta) -> Interval:
        return Interval(self.start + delta, self.end + delta)


def merge(intervals: Iterable[Interval]) -> list[Interval]:
    """Merge overlapping or touching intervals into a sorted, disjoint list."""
    merged: list[Interval] = []
    for iv in sorted(intervals):
        if merged and iv.start <= merged[-1].end:
            last = merged[-1]
            merged[-1] = Interval(last.start, max(last.end, iv.end))
        else:
            merged.append(iv)
    return merged


def uncovered(target: Interval, covered: Iterable[Interval]) -> list[Interval]:
    """Parts of `target` not covered by any interval in `covered` (e.g. missing transcripts)."""
    missing: list[Interval] = []
    cursor = target.start
    for iv in merge(covered):
        if iv.end <= cursor:
            continue
        if iv.start >= target.end:
            break
        if iv.start > cursor:
            missing.append(Interval(cursor, iv.start))
        cursor = max(cursor, iv.end)
        if cursor >= target.end:
            break
    if cursor < target.end:
        missing.append(Interval(cursor, target.end))
    return missing
