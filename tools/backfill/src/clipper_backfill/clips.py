"""Viewer clips (Helix Get Clips) -> `viewer_clips`: ground truth for the detector (DESIGN §17).

Per broadcast: the top clips by views made during it (`get_clips` over the stream's window,
± 2 min), kept when they were cut from that broadcast's VOD (or from no VOD). Each clip is linked
to our stream through its VOD: our `vods` row (a backfilled VOD), else Helix Get Videos'
`stream_id` matched to `streams.platform_stream_id` (a stream we recorded live).

Time span: `start_utc = T0 + vod_offset`, `end_utc = start_utc + duration`, the moment within
`± slack_s`. T0 is our `vods.t0_utc` (`time_source = vod_t0`, slack = duration + 10 s: Helix
`vod_offset` is reportedly off by about the clip length) or, for a VOD we didn't backfill, its
Helix `created_at` (`helix_created_at`, + 5 s: created_at is a few s off the PDT T0). No offset
yet (fresh clips, deleted VODs): the span stays NULL and a later run fills it in.

A clip keeps the stream it was first linked to: a link by VOD is the same from every window, and
only a clip without a (known) VOD, found by two overlapping stream windows, could be claimed by
both; the first one wins instead of the last.

Needs Helix credentials (`CLIPPER_TWITCH_CLIENT_ID`/`_SECRET`). The only writer of `viewer_clips`
is this tool (the live detector keeps fresh clips in memory, §18).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_backfill import rows
from clipper_core.contracts import ClipTimeSource, DataSource
from clipper_core.logs import get_logger
from clipper_core.timeline import Interval, utc_now
from clipper_twitch.helix import HelixClient, HelixClip, HelixVideo

log = get_logger(__name__)

CLIP_SLACK_S = 10.0  # + the clip's duration: Helix vod_offset is reportedly off by ~its length
HELIX_T0_ERROR_S = 5.0  # more slack when T0 is Helix created_at
WINDOW_MARGIN = timedelta(minutes=2)  # around the broadcast for get_clips (minute-truncated)


@dataclass(frozen=True, slots=True)
class VideoLink:
    """What a clip's VOD maps to: our stream and the T0 its offsets count from."""

    video_id: str
    stream_id: int | None
    t0: datetime | None
    time_source: str | None  # ClipTimeSource


def clip_values(clip: HelixClip, *, channel_id: int, link: VideoLink | None) -> dict[str, Any]:
    """The `viewer_clips` row of one Helix clip."""
    start = end = slack = source = None
    if link is not None and link.t0 is not None and clip.vod_offset is not None:
        start = link.t0 + timedelta(seconds=clip.vod_offset)
        end = start + timedelta(seconds=clip.duration)
        slack = clip.duration + CLIP_SLACK_S
        if link.time_source == ClipTimeSource.HELIX_CREATED_AT:
            slack += HELIX_T0_ERROR_S
        source = link.time_source
    return {
        "platform": rows.PLATFORM,
        "clip_id": clip.id,
        "channel_id": channel_id,
        "stream_id": link.stream_id if link else None,
        "platform_video_id": clip.video_id,
        "vod_offset_s": clip.vod_offset,
        "duration_s": clip.duration,
        "start_utc": start,
        "end_utc": end,
        "slack_s": slack,
        "time_source": source,
        "title": clip.title or None,
        "creator_login": clip.creator_name or None,  # Helix gives the display name only
        "view_count": clip.view_count,
        "is_featured": clip.is_featured,
        "platform_created_at": clip.created_at,
    }


@dataclass(slots=True)
class ClipsResult:
    label: str  # what was searched: "video 123" / "stream 7"
    fetched: int = 0  # returned by Helix
    kept: int = 0  # of this broadcast
    new: int = 0
    refreshed: int = 0
    filled: int = 0  # had no vod_offset before, have one now
    with_span: int = 0
    stream_id: int | None = None
    top: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"clips ({self.label}): {self.kept} of {self.fetched} fetched belong to it; "
            f"{self.new} new, {self.refreshed} refreshed ({self.filled} offsets filled), "
            f"{self.with_span} with a time span"
            + (f", stream {self.stream_id}" if self.stream_id else ", no stream of ours")
        ]
        out += [f"  {line}" for line in self.top]
        return out


class ClipCollector:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        helix: HelixClient,
        *,
        top: int = 100,
    ) -> None:
        self.sessions = sessions
        self.helix = helix
        self.top = top
        self._videos: dict[str, HelixVideo | None] = {}

    async def video(self, video_id: str) -> HelixVideo | None:
        if video_id not in self._videos:
            self._videos[video_id] = await self.helix.get_video(video_id)
        return self._videos[video_id]

    async def link(self, session: AsyncSession, channel_id: int, video_id: str) -> VideoLink:
        """Our stream of a VOD and the T0 to count its offsets from."""
        vod = await rows.find_vod(session, video_id)
        if vod is not None:
            return VideoLink(video_id, vod.stream_id, vod.t0_utc, ClipTimeSource.VOD_T0)
        video = await self.video(video_id)
        if video is None:
            return VideoLink(video_id, None, None, None)
        stream = None
        if video.stream_id:
            stream = await rows.find_stream(
                session, channel_id=channel_id, platform_stream_id=video.stream_id
            )
            if stream is not None and stream.source != DataSource.LIVE:  # backfill, no vods row
                theirs = await rows.find_vod_of_stream(session, stream.id)
                if theirs is not None:
                    return VideoLink(video_id, stream.id, theirs.t0_utc, ClipTimeSource.VOD_T0)
        return VideoLink(
            video_id,
            stream.id if stream else None,
            video.created_at,
            ClipTimeSource.HELIX_CREATED_AT,
        )

    async def for_video(self, video_id: str) -> ClipsResult:
        """The clips of one VOD (`clips <video>` and the `vod` run)."""
        video = await self.video(video_id)
        if video is None:
            raise LookupError(f"Helix doesn't know video {video_id}")
        async with self.sessions.begin() as s:
            channel_id = await rows.ensure_channel(
                s, login=video.user_login, user_id=video.user_id, display_name=video.user_name
            )
            own = await self.link(s, channel_id, video_id)
        window = Interval(
            video.created_at - WINDOW_MARGIN,
            min(
                video.created_at + timedelta(seconds=video.duration_s) + WINDOW_MARGIN,
                utc_now(),
            ),
        )
        return await self._collect(
            f"video {video_id}",
            broadcaster_id=video.user_id,
            channel_id=channel_id,
            window=window,
            video_ids={video_id},
            default_link=own,
        )

    async def for_channel(self, login: str) -> list[ClipsResult]:
        """The clips of every stream of ours of a channel (live and backfilled)."""
        async with self.sessions() as s:
            channel_id = await rows.find_channel(s, login=login, user_id=None)
            if channel_id is None:
                raise LookupError(f"no channel {login!r} in the database")
            streams = await rows.channel_streams(s, channel_id)
            user_id = await rows.channel_user_id(s, channel_id)
        if not user_id:
            users = await self.helix.get_users(logins=[login])
            if not users:
                raise LookupError(f"Helix doesn't know channel {login!r}")
            user_id = users[0].id
        results = []
        for stream in streams:
            end = stream.ended_at or utc_now()
            window = Interval(
                stream.started_at - WINDOW_MARGIN, min(end + WINDOW_MARGIN, utc_now())
            )
            results.append(
                await self._collect(
                    f"stream {stream.id} ({stream.source} {stream.platform_stream_id})",
                    broadcaster_id=user_id,
                    channel_id=channel_id,
                    window=window,
                    video_ids=None,
                    default_link=VideoLink("", stream.id, None, None),
                    stream=stream,
                )
            )
        return results

    async def _collect(
        self,
        label: str,
        *,
        broadcaster_id: str,
        channel_id: int,
        window: Interval,
        video_ids: set[str] | None,
        default_link: VideoLink | None,
        stream: rows.StreamRow | None = None,
    ) -> ClipsResult:
        """Fetch the window's top clips, keep this broadcast's, upsert them in one transaction.

        `video_ids`: the broadcast's VOD (None: whichever VOD names `stream`)."""
        page = await self.helix.get_clips(
            broadcaster_id, started_at=window.start, ended_at=window.end, limit=self.top
        )
        result = ClipsResult(label, fetched=len(page.items))
        unique: dict[str, HelixClip] = {}
        for clip in page.items:  # Helix can repeat a clip across pages: one row each, first wins
            unique.setdefault(clip.id, clip)
        async with self.sessions.begin() as s:
            links: dict[str, VideoLink] = {}
            kept: list[tuple[HelixClip, VideoLink | None]] = []
            for clip in unique.values():
                if clip.video_id is None:  # no VOD: this broadcast (by time), no offset
                    kept.append((clip, default_link))
                    continue
                if video_ids is not None and clip.video_id not in video_ids:
                    continue  # another video's clip: not even looked up
                if clip.video_id not in links:
                    links[clip.video_id] = await self.link(s, channel_id, clip.video_id)
                link = links[clip.video_id]
                if stream is not None and link.stream_id is None and link.t0 is None:
                    link = default_link  # a VOD Helix doesn't know (any more): this broadcast
                elif stream is not None and link.stream_id != stream.id:
                    continue  # another broadcast's VOD (overlapping window)
                kept.append((clip, link))
            values = [clip_values(c, channel_id=channel_id, link=lk) for c, lk in kept]
            before = await rows.clip_offsets(s, [v["clip_id"] for v in values])
            await rows.upsert_clips(s, values)
        result.kept = len(values)
        result.stream_id = next((v["stream_id"] for v in values if v["stream_id"]), None)
        if stream is not None:
            result.stream_id = stream.id
        for v in values:
            if v["clip_id"] in before:
                result.refreshed += 1
                result.filled += before[v["clip_id"]] is None and v["vod_offset_s"] is not None
            else:
                result.new += 1
            result.with_span += v["start_utc"] is not None
        for v in values[:5]:
            span = (
                f"{v['start_utc'].isoformat(timespec='seconds')} ±{v['slack_s']:.0f}s"
                if v["start_utc"]
                else "no offset yet"
            )
            result.top.append(f"{v['view_count']:>7} views  {span}  {v['title'] or ''}"[:120])
        log.info(
            "clips saved",
            label=label,
            fetched=result.fetched,
            kept=result.kept,
            new=result.new,
            filled=result.filled,
        )
        return result
