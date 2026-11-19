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

VOD (archive) playlists are parsed by `parse_vod_playlist`: they are timed by VOD offset (the sum
of EXTINF), because they probably carry no EXT-X-PROGRAM-DATE-TIME (DESIGN §17).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlsplit

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


# --------------------------------------------------------------------------- VOD playlists


@dataclass(frozen=True, slots=True)
class VodSegment:
    """One segment of a VOD media playlist, placed by VOD offset (not by UTC)."""

    index: int  # EXT-X-MEDIA-SEQUENCE + position (Twitch VODs start at 0 and name segments by it)
    uri: str  # absolute (resolved against the playlist URL)
    offset_s: float  # VOD time of its start: sum of the EXTINF durations before it
    duration: float  # EXTINF, seconds
    muted: bool  # "N-muted.ts": the platform silenced its audio (copyrighted music)
    discontinuity: bool  # EXT-X-DISCONTINUITY before this segment
    init_uri: str | None = None  # EXT-X-MAP in effect: fMP4 fragment, needs this init section
    program_date_time: datetime | None = None  # UTC, only if the playlist has a PDT for it

    @property
    def end_s(self) -> float:
        return self.offset_s + self.duration


@dataclass(frozen=True, slots=True)
class VodPlaylist:
    segments: list[VodSegment]
    target_duration: float
    media_sequence: int  # EXT-X-MEDIA-SEQUENCE: index of the first segment (0 for Twitch VODs)
    total_secs: float | None  # EXT-X-TWITCH-TOTAL-SECS (Twitch's length of the VOD), if present
    ended: bool  # EXT-X-ENDLIST: complete (an archive of a stream still live has none)

    @property
    def duration_s(self) -> float:
        """Sum of EXTINF: the VOD offset where the last segment ends."""
        return self.segments[-1].end_s if self.segments else 0.0


def _is_muted(uri: str) -> bool:
    return PurePosixPath(urlsplit(uri).path).stem.endswith("-muted")


def parse_vod_playlist(text: str, base_url: str = "") -> VodPlaylist:
    """Parse a VOD media playlist (`index-dvr.m3u8`). Raises ValueError if it is not one.

    Segment offsets are the running sum of EXTINF (VOD time); mapping them to UTC is up to the
    caller (`T0 + offset`). EXT-X-PROGRAM-DATE-TIME is kept per segment where present, as is.
    Byte-range segments (`EXT-X-BYTERANGE`, several segments in one file) are not supported: a
    ValueError, instead of downloading the whole file once per segment.
    """
    lines = [line.strip() for line in text.splitlines()]
    if not lines or lines[0] != "#EXTM3U":
        raise ValueError("not an HLS playlist")

    target = 0.0
    media_sequence = 0
    total_secs = None
    ended = False
    init_uri = None
    offset = 0.0
    segments: list[VodSegment] = []
    pending: dict = {}

    for line in lines[1:]:
        if not line:
            continue
        if not line.startswith("#"):
            if "duration" not in pending:
                raise ValueError(f"segment without EXTINF: {line}")
            duration = pending["duration"]
            segments.append(
                VodSegment(
                    index=media_sequence + len(segments),
                    uri=urljoin(base_url, line),
                    offset_s=round(offset, 6),
                    duration=duration,
                    muted=_is_muted(line),
                    discontinuity=pending.get("discontinuity", False),
                    init_uri=init_uri,
                    program_date_time=pending.get("start"),
                )
            )
            offset += duration
            pending = {}
            continue
        tag, _, value = line.partition(":")
        if tag == "#EXTINF":
            pending["duration"] = float(value.partition(",")[0])
        elif tag == "#EXT-X-PROGRAM-DATE-TIME":
            pending["start"] = parse_date(value)
        elif tag == "#EXT-X-DISCONTINUITY":
            pending["discontinuity"] = True
        elif tag == "#EXT-X-TARGETDURATION":
            target = float(value)
        elif tag == "#EXT-X-MEDIA-SEQUENCE":
            media_sequence = int(value)
        elif tag == "#EXT-X-TWITCH-TOTAL-SECS":
            total_secs = float(value)
        elif tag == "#EXT-X-BYTERANGE":
            raise ValueError("byte-range segments (EXT-X-BYTERANGE) are not supported")
        elif tag == "#EXT-X-ENDLIST":
            ended = True
        elif tag == "#EXT-X-MAP":
            uri = parse_attributes(value).get("URI")
            init_uri = urljoin(base_url, uri) if uri else None

    return VodPlaylist(segments, target, media_sequence, total_secs, ended)
