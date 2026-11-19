"""Decides, segment by segment, what gets recorded. Pure logic, no IO.

- Duplicates: playlists overlap between reloads (and with what an earlier run recorded), so a
  segment is new only if it starts at or after everything already seen.
- Ads replace the stream: they are skipped and end the open chunk.
- Gaps: when the next live segment doesn't start where the recorded media ended, the hole is a
  gap with the reason we know (ad, reconnect, not recorded) or "unknown".
- A chunk must be continuous media (the transcriber maps chunk-relative times linearly to UTC),
  so every gap or discontinuity starts a new chunk.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from clipper_core.contracts import CHUNK_JITTER, GapReason
from clipper_twitch.hls import Segment


@dataclass(frozen=True, slots=True)
class Gap:
    start: datetime
    end: datetime
    reason: GapReason

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass(frozen=True, slots=True)
class LivePlan:
    split: bool  # close the open chunk before writing this segment
    gap: Gap | None  # hole between the media recorded so far and this segment


class SegmentTracker:
    def __init__(
        self,
        *,
        media_end: datetime | None,
        seen_until: datetime | None = None,
        reason: GapReason | None = None,
        tolerance_s: float = CHUNK_JITTER.total_seconds(),
    ) -> None:
        """
        media_end: where recorded media ends (resume) or the stream started (fresh); gaps are
            measured from here. seen_until: skip segments starting before this (resume).
        reason: why media may be missing before the first segment we get.
        """
        self.media_end = media_end
        self.seen_until = seen_until
        self.reason = reason
        self.tolerance = timedelta(seconds=tolerance_s)
        self.in_ad = False

    @classmethod
    def fresh(cls, stream_started_at: datetime) -> SegmentTracker:
        """First recording of a stream: anything between go-live and our first segment is missed."""
        return cls(media_end=stream_started_at, reason=GapReason.NOT_RECORDED)

    @classmethod
    def resume(cls, recorded_until: datetime) -> SegmentTracker:
        """Continue a stream recorded before (e.g. after a restart)."""
        return cls(
            media_end=recorded_until, seen_until=recorded_until, reason=GapReason.NOT_RECORDED
        )

    def is_new(self, seg: Segment) -> bool:
        return self.seen_until is None or seg.start >= self.seen_until - self.tolerance

    def ad(self, seg: Segment) -> bool:
        """An ad segment was skipped. True if it starts an ad break."""
        self._seen(seg)
        started = not self.in_ad
        self.in_ad = True
        self.interrupted(GapReason.AD)
        return started

    def lost(self, seg: Segment) -> None:
        """A live segment couldn't be downloaded."""
        self._seen(seg)
        self.interrupted(GapReason.RECONNECT)

    def interrupted(self, reason: GapReason) -> None:
        """Remember why media may go missing (the first known reason wins)."""
        if self.reason is None:
            self.reason = reason

    def live(self, seg: Segment) -> LivePlan:
        """A live segment was downloaded and will be written."""
        gap = None
        if self.media_end is not None and seg.start - self.media_end > self.tolerance:
            gap = Gap(self.media_end, seg.start, self.reason or GapReason.UNKNOWN)
        self.media_end = seg.end
        self._seen(seg)
        self.reason = None
        self.in_ad = False
        return LivePlan(split=gap is not None or seg.discontinuity, gap=gap)

    def _seen(self, seg: Segment) -> None:
        if self.seen_until is None or seg.end > self.seen_until:
            self.seen_until = seg.end
