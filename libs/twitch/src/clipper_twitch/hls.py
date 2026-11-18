"""Twitch live HLS media playlists: segments with UTC start times and ad markers.

Only what the recorder needs, following Twitch's format (ad rules as in streamlink's plugin):
- `EXT-X-PROGRAM-DATE-TIME` before each segment gives its UTC start: this is video time.
  Segments without one continue from the previous segment's end.
- Live segments are titled "live" (`#EXTINF:2.000,live`). Ads are segments inside an
  `EXT-X-DATERANGE` with `CLASS="twitch-stitched-ad"` (or an ID starting with "stitched-ad-"),
  or with "Amazon" in the title.
- `EXT-X-TWITCH-PREFETCH` (low-latency segments that aren't finished yet) is ignored: they show
  up as normal segments on a later reload.
- Twitch serves either MPEG-TS segments or fragmented MP4 (`EXT-X-MAP` init section + `.mp4`
  fragments), depending on the channel. Each segment carries the init section in effect for it.
- The playlist only holds the last ~30 s, so it has to be reloaded every couple of seconds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urljoin

from clipper_core.timeline import ensure_utc

_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


@dataclass(frozen=True, slots=True)
class Segment:
    sequence: int  # EXT-X-MEDIA-SEQUENCE based; resets with a new playlist session
    uri: str
    start: datetime  # UTC, from EXT-X-PROGRAM-DATE-TIME
    duration: float  # seconds
    title: str | None
    ad: bool
    discontinuity: bool  # EXT-X-DISCONTINUITY before this segment
    init_uri: str | None = None  # EXT-X-MAP in effect: fMP4 fragment, needs this init section first

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration)


@dataclass(frozen=True, slots=True)
class MediaPlaylist:
    segments: list[Segment]
    target_duration: float
    media_sequence: int
    ended: bool  # EXT-X-ENDLIST: no more segments will come


@dataclass(frozen=True, slots=True)
class _AdRange:
    start: datetime
    end: datetime | None  # None: open-ended

    def contains(self, ts: datetime) -> bool:
        return ts >= self.start and (self.end is None or ts < self.end)


def parse_attributes(value: str) -> dict[str, str]:
    """`ID="a",DURATION=30.0` -> {"ID": "a", "DURATION": "30.0"}"""
    return {k: v[1:-1] if v.startswith('"') else v for k, v in _ATTR.findall(value)}


def parse_date(value: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(value.strip()))


def _ad_range(attrs: dict[str, str]) -> _AdRange | None:
    is_ad = attrs.get("CLASS") == "twitch-stitched-ad" or attrs.get("ID", "").startswith(
        "stitched-ad-"
    )
    if not is_ad or "START-DATE" not in attrs:
        return None
    start = parse_date(attrs["START-DATE"])
    end = None
    if "END-DATE" in attrs:
        end = parse_date(attrs["END-DATE"])
    else:
        seconds = attrs.get("DURATION") or attrs.get("PLANNED-DURATION")
        if seconds:
            end = start + timedelta(seconds=float(seconds))
    return _AdRange(start, end)


def parse_media_playlist(text: str, base_url: str = "") -> MediaPlaylist:
    """Parse a media (not multivariant) playlist. Raises ValueError if it can't be timed."""
    lines = [line.strip() for line in text.splitlines()]
    if not lines or lines[0] != "#EXTM3U":
        raise ValueError("not an HLS playlist")

    target = 0.0
    media_sequence = 0
    ended = False
    init_uri = None
    ads: list[_AdRange] = []
    raw: list[dict] = []  # segments before times are filled in
    pending: dict = {}

    for line in lines[1:]:
        if not line:
            continue
        if not line.startswith("#"):
            raw.append(
                {
                    "uri": urljoin(base_url, line),
                    "sequence": media_sequence + len(raw),
                    "duration": pending.get("duration", 0.0),
                    "title": pending.get("title"),
                    "start": pending.get("start"),
                    "discontinuity": pending.get("discontinuity", False),
                    "init_uri": init_uri,
                }
            )
            pending = {}
            continue
        tag, _, value = line.partition(":")
        if tag == "#EXTINF":
            duration, _, title = value.partition(",")
            pending["duration"] = float(duration)
            pending["title"] = title or None
        elif tag == "#EXT-X-PROGRAM-DATE-TIME":
            pending["start"] = parse_date(value)
        elif tag == "#EXT-X-DISCONTINUITY":
            pending["discontinuity"] = True
        elif tag == "#EXT-X-TARGETDURATION":
            target = float(value)
        elif tag == "#EXT-X-MEDIA-SEQUENCE":
            media_sequence = int(value)
        elif tag == "#EXT-X-ENDLIST":
            ended = True
        elif tag == "#EXT-X-MAP":
            uri = parse_attributes(value).get("URI")
            init_uri = urljoin(base_url, uri) if uri else None
        elif tag == "#EXT-X-DATERANGE":
            ad = _ad_range(parse_attributes(value))
            if ad:
                ads.append(ad)

    _fill_times(raw)
    segments = [
        Segment(
            sequence=r["sequence"],
            uri=r["uri"],
            start=r["start"],
            duration=r["duration"],
            title=r["title"],
            ad="Amazon" in (r["title"] or "") or any(a.contains(r["start"]) for a in ads),
            discontinuity=r["discontinuity"],
            init_uri=r["init_uri"],
        )
        for r in raw
    ]
    return MediaPlaylist(segments, target, media_sequence, ended)


def _fill_times(raw: list[dict]) -> None:
    """Segments without a PROGRAM-DATE-TIME continue from their neighbours."""
    if not raw:
        return
    first = next((i for i, r in enumerate(raw) if r["start"] is not None), None)
    if first is None:
        raise ValueError("playlist has no EXT-X-PROGRAM-DATE-TIME, can't place it in time")
    for i in range(first - 1, -1, -1):
        raw[i]["start"] = raw[i + 1]["start"] - timedelta(seconds=raw[i]["duration"])
    for i in range(first + 1, len(raw)):
        if raw[i]["start"] is None:
            prev = raw[i - 1]
            raw[i]["start"] = prev["start"] + timedelta(seconds=prev["duration"])
