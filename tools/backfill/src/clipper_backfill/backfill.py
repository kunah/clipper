"""One VOD, end to end: metadata -> playlist -> T0 -> rows -> media (DESIGN §17).

Reads first (all of them also in `--dry-run`): GQL video metadata (+ Helix when there are
credentials), the renditions (streamlink), the media playlist (+ `audio_only` for its PDT on a
first run with `--video`), a few chat pages for the T0 cross-check (first run only), and our
own rows. Then, unless dry: the lease, stream + vods rows (first run), and the media pipeline.

Then, in order: media (`pipeline`), chat replay (`chat`: the live chat tables, unless the channel
has live chat in the VOD's range), viewer clips (`clips`, Helix credentials needed).

Skipped: highlights/uploads (no broadcast), archives still recording, broadcasts we recorded live
(clips only). Sub-only (usher 403): `media_status='restricted'` and a `not_recorded` gap over the
VOD; its chat is still fetched.

`--fill-gaps` (`VodOptions.fill_gaps`, DESIGN §17): a broadcast we recorded live and that has
ended gets its `not_recorded` gaps and unrecorded tail/head filled from the VOD instead: a `vods`
row referencing the live stream, `source='backfill'` chunks (seq >= `FILL_SEQ_BASE`, no gap
rows), chat of the seconds without live chat, then the usual clips. Anything recorded live stays
as it is. Not ours / backfilled / still live: `FillRefused` (the CLI exits 2).
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_backfill import fill as fills
from clipper_backfill import rows
from clipper_backfill.chat import (
    ChatRunner,
    EmoteSets,
    StaticEmoteSets,
    chat_clock,
    chat_spans,
    chat_window,
    window_for,
)
from clipper_backfill.chat import result_lines as chat_result_lines
from clipper_backfill.clips import ClipCollector
from clipper_backfill.download import Downloader
from clipper_backfill.pipeline import TEMP_PREFIX, LeaseKeeper, MediaResult, MediaRunner, Store
from clipper_backfill.planner import (
    FillRange,
    Plan,
    PlanError,
    Unit,
    at,
    check_aligned,
    fill_ranges,
    plan_fill,
    plan_units,
)
from clipper_backfill.settings import BackfillSettings
from clipper_backfill.t0 import ChatPoint, T0Estimate, estimate_t0, pdt_points
from clipper_core import leases
from clipper_core.contracts import (
    FILL_SEQ_BASE,
    DataSource,
    StreamStatus,
    VodChatStatus,
    VodMediaStatus,
    backfill_lease,
    recording_lease,
)
from clipper_core.logs import get_logger, log_context
from clipper_core.timeline import Interval, utc_now
from clipper_core.transcripts import ENDED_GRACE
from clipper_twitch.gql import ChatReplay, GqlError, ReplayUnavailable, VideoInfo, VideoInfoSource
from clipper_twitch.helix import HelixClient, HelixVideo
from clipper_twitch.hls import VodPlaylist, parse_vod_playlist
from clipper_twitch.vod import VodGone, VodRestricted, VodSource, parse_vod_path

log = get_logger(__name__)

AUDIO = "audio_only"
AUDIO_BYTES_PER_S = 27_400  # audio_only TS: ~274 KB per 10 s segment (DESIGN §3)
NO_HELIX = "needs Helix credentials (CLIPPER_TWITCH_CLIENT_ID / CLIPPER_TWITCH_CLIENT_SECRET)"
_VIDEO_PATH = re.compile(r"/videos?/(\d+)")


class GqlSource(VideoInfoSource, ChatReplay, Protocol):
    """`GqlChatReplay` (or its fake): video metadata + chat pages through one pacer."""


class UsageError(ValueError):
    """Bad input (unknown rendition, not a video id/URL): shown to the user, no traceback."""


class FillRefused(UsageError):
    """`--fill-gaps` can't run on this VOD: its broadcast is not one of our live streams, is a
    backfilled one, or is still live. The CLI exits 2."""


def parse_video_ref(ref: str) -> str:
    """`2932113307`, `https://www.twitch.tv/videos/2932113307?t=1h` -> `2932113307`."""
    ref = ref.strip()
    if ref.isdigit():
        return ref
    m = _VIDEO_PATH.search(urlsplit(ref if "://" in ref else f"https://{ref}").path)
    if not m:
        raise UsageError(f"not a Twitch video id or URL: {ref!r}")
    return m[1]


@dataclass(frozen=True, slots=True)
class VodOptions:
    dry_run: bool = False
    media: bool = True  # False (`--no-media`): rows only, `media_status='off'`
    video: str | None = None  # rendition to take (video + its audio); None: audio_only
    max_minutes: float | None = None  # media and chat per run (each from its own resume point)
    chat_max_minutes: float | None = None  # chat per run, instead of `max_minutes`
    start_offset_s: float = 0.0  # first run, media only: start here (before: `not_recorded`)
    transcribe: bool = True
    chat: bool = True  # False: no chat replay this run (`chat_status='off'` on a first run)
    clips: bool = True  # viewer clips (Helix credentials needed)
    # a live-recorded, ended broadcast: fill its not_recorded gaps + unrecorded tail/head
    fill_gaps: bool = False
    # `--accept-t0 audio|pdt`: a fill's first run writes with this T0 although it isn't trusted
    accept_t0: str | None = None


@dataclass(slots=True)
class VodOutcome:
    video_id: str
    # done | partial | dry_run | rows_only | restricted | skipped | gone | busy | refused | failed
    status: str
    message: str = ""
    lines: list[str] = field(default_factory=list)  # the report printed by the CLI
    media: MediaResult | None = None

    @property
    def ok(self) -> bool:
        return self.status not in ("busy", "failed", "refused")


@dataclass(frozen=True, slots=True)
class VodMeta:
    """What we know about the video before touching our rows."""

    video_id: str
    login: str
    user_id: str | None
    display_name: str | None
    title: str | None
    category: str | None
    created_at: datetime
    created_source: str  # "helix" | "gql_created_at" (the T0 candidate's key)
    length_s: float
    archive: bool  # a past broadcast (not a highlight/upload)
    recording: bool  # the broadcast is still live
    gql_created_at: datetime | None
    stream_ids: dict[str, str]  # source -> platform stream id (helix, preview path)
    preview_epoch: datetime | None


def vod_meta(video_id: str, info: VideoInfo | None, helix: HelixVideo | None) -> VodMeta:
    if info is None and helix is None:
        raise ValueError("no metadata")
    stream_ids: dict[str, str] = {}
    if helix and helix.stream_id:
        stream_ids["helix"] = helix.stream_id
    preview = parse_vod_path(info.seek_previews_url) if info else None
    if preview:
        stream_ids["preview_path"] = preview.stream_id
    kind = (info.broadcast_type if info else helix.type) or ""
    return VodMeta(
        video_id=video_id,
        login=((info and info.owner_login) or (helix and helix.user_login) or "").lower(),
        user_id=(info and info.owner_id) or (helix and helix.user_id) or None,
        display_name=(info and info.owner_display_name) or (helix and helix.user_name) or None,
        title=(info and info.title) or (helix and helix.title) or None,
        category=info.game if info else None,
        created_at=helix.created_at if helix else info.created_at,
        created_source="helix" if helix else "gql_created_at",
        length_s=float(info.length_s if info else helix.duration_s),
        archive=kind.lower() == "archive",
        recording=bool(info and (info.status or "").upper() == "RECORDING"),
        gql_created_at=info.created_at if info else None,
        stream_ids=stream_ids,
        preview_epoch=preview.epoch if preview else None,
    )


@dataclass(slots=True)
class _Media:
    """The playlists of one run."""

    renditions: dict[str, str] = field(default_factory=dict)
    rendition: str | None = None
    playlist: VodPlaylist | None = None  # the one chunks are cut from
    audio: VodPlaylist | None = None  # audio_only, for PDT (== playlist without --video)
    restricted: bool = False


class Backfiller:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        store: Store | None,
        vod_source: VodSource,
        gql: GqlSource,
        http: httpx.AsyncClient,
        settings: BackfillSettings,
        owner: str,
        helix: HelixClient | None = None,
        stop: asyncio.Event | None = None,
        after_commit: Callable[[Unit], Awaitable[Any]] | None = None,
        emotes: EmoteSets | None = None,
        after_chat_commit: Callable[[int], Awaitable[Any]] | None = None,
    ) -> None:
        self.sessions = sessions
        self.store = store
        self.vod_source = vod_source
        self.gql = gql
        self.http = http
        self.settings = settings
        self.owner = owner
        self.helix = helix
        self.stop = stop
        self.after_commit = after_commit
        self.after_chat_commit = after_chat_commit
        if emotes is None:
            log.warning("no third-party emote sets: only Twitch emotes are counted")
        self.emotes = emotes or StaticEmoteSets()
        self.downloader = Downloader(
            http,
            concurrency=settings.downloads,
            attempts=settings.segment_attempts,
            backoff_s=settings.retry_backoff_s,
        )

    async def vod(self, ref: str, opts: VodOptions | None = None) -> VodOutcome:
        video_id = parse_video_ref(ref)
        requests, size = self.downloader.requests, self.downloader.bytes
        gql_requests = getattr(self.gql, "requests", None)
        with log_context(video_id=video_id):
            outcome = await self._vod(video_id, opts or VodOptions())
        if self.downloader.requests > requests:
            outcome.lines.append(
                f"downloaded: {self.downloader.requests - requests} requests, "
                f"{(self.downloader.bytes - size) / 1e6:.2f} MB (playlists + segments)"
            )
        if gql_requests is not None:
            outcome.lines.append(
                f"gql: {self.gql.requests - gql_requests} requests (metadata + chat pages)"
            )
        return outcome

    # ------------------------------------------------------------------ reads

    async def _vod(self, video_id: str, opts: VodOptions) -> VodOutcome:
        if not opts.dry_run:  # cheap early answer; `_write` takes the lease for real
            holder = await self._lease(video_id).holder()
            if holder is not None and holder != self.owner:
                return VodOutcome(video_id, "busy", f"another run holds the lease ({holder})")
        info = await self.gql.video(video_id)
        helix_video = await self.helix.get_video(video_id) if self.helix else None
        async with self.sessions() as s:
            existing = await rows.find_vod(s, video_id)
        if info is None and helix_video is None:
            return await self._gone(video_id, existing, opts, "the platform doesn't know it")
        meta = vod_meta(video_id, info, helix_video)
        if not meta.archive:
            if opts.fill_gaps:
                raise FillRefused("not a past broadcast (highlight or upload): nothing to fill")
            return VodOutcome(video_id, "skipped", "not a past broadcast (highlight or upload)")
        if meta.recording:
            if opts.fill_gaps:
                raise FillRefused("the broadcast is still live: wait until it ended")
            return VodOutcome(video_id, "skipped", "the broadcast is still live")

        try:
            media = await self._playlists(video_id, opts, first_run=existing is None)
        except VodGone as exc:
            return await self._gone(video_id, existing, opts, str(exc))
        if media.playlist is not None and not media.playlist.ended:
            if opts.fill_gaps:
                raise FillRefused("the playlist is not finished (still live?): wait until it ended")
            return VodOutcome(video_id, "skipped", "the playlist is not finished (still live?)")

        if opts.fill_gaps:
            return await self._fill(video_id, meta, media, existing, opts)
        if existing is not None and existing.fill:  # a fill row: its stream is a live one
            message = (
                f"recorded live as stream {existing.stream_id}: live data wins (clips only; "
                f"--fill-gaps continues its fill)"
            )
            clips = await self._clips(video_id, opts, vod_id=existing.id)
            return VodOutcome(video_id, "skipped", message, clips)

        new = None
        if existing is None:
            broadcast = _broadcast(meta, media)
            async with self.sessions() as s:
                live = await rows.live_stream_id(
                    s,
                    login=meta.login,
                    user_id=meta.user_id,
                    platform_stream_id=broadcast.stream_id,
                )
            if live is not None:
                message = (
                    f"recorded live as stream {live}: live data wins (clips only; --fill-gaps "
                    "fills its not_recorded gaps from the VOD)"
                )
                clips = await self._clips(video_id, opts, vod_id=None)
                return VodOutcome(video_id, "skipped", message, clips)
            new = await self._new_vod(meta, media, broadcast, opts)

        lines = self._describe(meta, media, existing, new)
        if opts.dry_run:
            lines += await self._describe_plan(media, existing, opts)
            lines += await self._describe_chat(meta, existing, new, opts)
            if opts.clips:
                lines.append(
                    f"clips: would fetch the top {self.settings.clips_top} by views (Helix)"
                    if self.helix
                    else f"clips: skipped: {NO_HELIX}"
                )
            return VodOutcome(video_id, "dry_run", "nothing written", lines)
        return await self._write(video_id, meta, media, existing, new, opts, lines)

    async def _playlists(self, video_id: str, opts: VodOptions, *, first_run: bool) -> _Media:
        media = _Media()
        try:
            media.renditions = await self.vod_source.renditions(video_id)
        except VodRestricted as exc:
            log.info("vod is restricted (sub-only?)", error=str(exc))
            media.restricted = True
            return media
        if not opts.media and not first_run:
            return media
        media.rendition = _pick_rendition(media.renditions, opts.video)
        if media.rendition == AUDIO or (first_run and AUDIO in media.renditions):
            media.audio = await self._playlist(media.renditions[AUDIO])
        media.playlist = (
            media.audio
            if media.rendition == AUDIO
            else await self._playlist(media.renditions[media.rendition])
        )
        return media

    async def _playlist(self, url: str) -> VodPlaylist:
        return parse_vod_playlist(await self.downloader.text(url), url)

    async def _sample_chat(self, video_id: str, duration_s: float) -> tuple[list[ChatPoint], dict]:
        """Comments from `t0_chat_pages` offsets spread over the VOD (deduplicated by id)."""
        pages = self.settings.t0_chat_pages
        points: dict[str, ChatPoint] = {}
        info: dict[str, Any] = {}
        for i in range(pages):
            try:
                page = await self.gql.page(video_id, offset_s=int(duration_s * i / pages))
            except ReplayUnavailable as exc:
                info["chat_replay"] = "unavailable"
                log.info("no chat replay for the T0 check", error=str(exc))
                break
            except GqlError as exc:
                info["chat_error"] = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("chat sample failed, T0 without chat", error=str(exc))
                break
            for c in page.comments:
                if c.created_at is not None:
                    points.setdefault(c.id, ChatPoint(c.content_offset_s, c.created_at))
        return list(points.values()), info

    async def _new_vod(
        self, meta: VodMeta, media: _Media, broadcast: _Broadcast, opts: VodOptions
    ) -> rows.NewVod:
        playlist = media.playlist
        duration = playlist.duration_s if playlist and playlist.segments else meta.length_s
        flags = dict(broadcast.flags)
        chat, chat_info = await self._sample_chat(meta.video_id, duration)
        t0 = estimate_t0(
            pdt=pdt_points(media.audio.segments) if media.audio else (),
            chat=chat,
            created_at=meta.created_at,
            created_at_source=meta.created_source,
            path_epoch=broadcast.epoch,
        )
        candidates = {**t0.candidates, **chat_info, "stream_ids": broadcast.stream_ids}
        if meta.gql_created_at and meta.created_source != "gql_created_at":
            candidates["gql_created_at"] = meta.gql_created_at.isoformat()
        t0 = T0Estimate(t0.t0, t0.method, candidates, t0.flags)
        flags.update(t0.flags)
        if playlist:
            flags.update(_playlist_flags(playlist, media.audio))
        if media.restricted:
            flags["restricted"] = True
        return rows.NewVod(
            video_id=meta.video_id,
            login=meta.login,
            user_id=meta.user_id,
            display_name=meta.display_name,
            platform_stream_id=broadcast.stream_id,
            title=meta.title,
            category=meta.category,
            created_at=meta.created_at,
            duration_s=duration,
            t0=t0,
            rendition=media.rendition,
            media_status=VodMediaStatus.PENDING if opts.media else VodMediaStatus.OFF,
            chat_status=VodChatStatus.PENDING if opts.chat else VodChatStatus.OFF,
            flags=flags,
        )

    # ------------------------------------------------------------------ writes

    async def _gone(
        self, video_id: str, existing: rows.VodRow | None, opts: VodOptions, why: str
    ) -> VodOutcome:
        if existing is None:
            return VodOutcome(video_id, "gone", f"not found: {why}")
        if not opts.dry_run and existing.media_status != VodMediaStatus.GONE:
            lease = self._lease(video_id)
            if not await lease.acquire():
                return VodOutcome(video_id, "busy", f"lease held by {await lease.holder()}")
            try:
                async with self.sessions.begin() as s:
                    await lease.check(s)
                    await rows.update_vod(s, existing.id, media_status=str(VodMediaStatus.GONE))
            finally:
                await lease.release()
        return VodOutcome(video_id, "gone", f"gone, data kept: {why}")

    def _lease(self, video_id: str) -> LeaseKeeper:
        return LeaseKeeper(
            self.sessions,
            backfill_lease(video_id),
            self.owner,
            timedelta(seconds=self.settings.lease_ttl_s),
        )

    async def _write(
        self,
        video_id: str,
        meta: VodMeta,
        media: _Media,
        existing: rows.VodRow | None,
        new: rows.NewVod | None,
        opts: VodOptions,
        lines: list[str],
    ) -> VodOutcome:
        lease = self._lease(video_id)
        if not await lease.acquire():
            holder = await lease.holder()
            return VodOutcome(video_id, "busy", f"another run holds the lease ({holder})", lines)
        lease.start()
        live = chat = None
        try:
            async with self.sessions.begin() as s:
                await lease.check(s)
                vod = await rows.find_vod(s, video_id)
                if vod is None:
                    assert new is not None
                    try:
                        vod = await rows.create_vod(s, new)
                    except rows.RecordedLive as exc:
                        live = exc.stream_id
                        vod = None
                    else:
                        lines.append(f"created stream {vod.stream_id} and vods row {vod.id}")
            if vod is None:
                lines += await self._clips(video_id, opts, vod_id=None)
                return VodOutcome(
                    video_id, "skipped", f"recorded live as stream {live}: clips only", lines
                )
            status, message, result = await self._media_stage(vod, media, opts, lease, lines)
            if status == "refused":
                return VodOutcome(video_id, status, message, lines)
            if opts.chat and not (self.stop is not None and self.stop.is_set()):
                chat = await self._chat_stage(vod, meta, opts, lease, lines)
        finally:
            await lease.release()
        if chat is not None:
            message += f"; chat {chat}"
            if chat == VodChatStatus.PENDING and status in ("done", "rows_only"):
                status = "partial"
        if self.stop is None or not self.stop.is_set():
            lines += await self._clips(video_id, opts, vod_id=vod.id)
        return VodOutcome(video_id, status, message, lines, result)

    async def _media_stage(
        self,
        vod: rows.VodRow,
        media: _Media,
        opts: VodOptions,
        lease: LeaseKeeper,
        lines: list[str],
    ) -> tuple[str, str, MediaResult | None]:
        """(outcome status, message, result) of the media part of a run."""
        if media.restricted and vod.media_status == VodMediaStatus.DONE:
            return "done", "media complete already (now restricted)", None
        if media.restricted:
            async with self.sessions.begin() as s:
                await lease.check(s)
                marked = await rows.mark_restricted(s, vod)
            note = "marked restricted, not_recorded gap" if marked else "already restricted"
            return "restricted", f"sub-only: {note}", None
        if not opts.media:
            return "rows_only", "--no-media: no media fetched", None
        if opts.transcribe:
            async with self.sessions.begin() as s:
                await lease.check(s)
                added = await rows.enqueue_missing_jobs(s, vod.stream_id)
            if added:
                lines.append(f"queued {added} transcribe jobs left out by --no-transcribe")
        if vod.media_status == VodMediaStatus.DONE:
            return "done", "media complete already", None
        if vod.media_status != VodMediaStatus.PENDING:  # off/gone/restricted -> fetch again
            async with self.sessions.begin() as s:
                await lease.check(s)
                await rows.update_vod(s, vod.id, media_status=str(VodMediaStatus.PENDING))
            vod = replace(vod, media_status=str(VodMediaStatus.PENDING))
        assert media.playlist is not None
        try:  # another rendition may be cut elsewhere: its chunks would overlap ours
            check_aligned(media.playlist.segments, vod.media_done_s)
        except PlanError as exc:
            hint = f"; continue with --video {vod.rendition}" if vod.rendition else ""
            if vod.rendition == AUDIO:
                hint = "; continue without --video"
            return "refused", f"{media.rendition}: {exc}{hint}", None
        if vod.rendition and media.rendition != vod.rendition:
            log.info("rendition differs from the first run", first=vod.rendition)
        result = await self._media(vod, media, opts, lease)
        lines += _result_lines(result)
        status = "done" if result.status == VodMediaStatus.DONE else "partial"
        return status, f"media {result.status}", result

    async def _chat_stage(
        self,
        vod: rows.VodRow,
        meta: VodMeta,
        opts: VodOptions,
        lease: LeaseKeeper,
        lines: list[str],
    ) -> str:
        """Chat replay -> the live chat tables; returns `vods.chat_status` afterwards."""
        if vod.chat_status == VodChatStatus.DONE:
            lines.append("chat: complete already")
            return str(VodChatStatus.DONE)
        async with self.sessions() as s:
            foreign = await rows.foreign_chat_metrics(
                s, channel_id=vod.channel_id, stream_id=vod.stream_id, window=chat_window(vod)
            )
        what = "live chat" if foreign is None or foreign.live else "another stream's chat"
        if foreign is not None and foreign.rows >= self.settings.chat_guard_min_s:
            async with self.sessions.begin() as s:
                await lease.check(s)
                await rows.update_vod(s, vod.id, chat_status=str(VodChatStatus.SKIPPED))
            lines.append(
                f"chat: skipped: the channel has {what} in the VOD's range ({foreign.rows} "
                f"seconds, {foreign.first.isoformat()} .. {foreign.last.isoformat()}): live wins"
            )
            return str(VodChatStatus.SKIPPED)
        if foreign is not None:  # a few stray seconds: proceed, those seconds stay theirs
            async with self.sessions.begin() as s:
                await lease.check(s)
                await rows.merge_flags(s, vod.id, {"chat_foreign_seconds": foreign.rows})
            lines.append(
                f"chat: {foreign.rows} seconds of {what} in the VOD's range (< "
                f"{self.settings.chat_guard_min_s}): kept as they are, the rest is backfilled"
            )
        if vod.chat_status != VodChatStatus.PENDING:  # off/skipped/unavailable -> try again
            async with self.sessions.begin() as s:
                await lease.check(s)
                await rows.update_vod(s, vod.id, chat_status=str(VodChatStatus.PENDING))
        channel_emotes, global_emotes = await self._emote_sets(vod, meta, lease, lines)
        minutes = opts.chat_max_minutes if opts.chat_max_minutes is not None else opts.max_minutes
        runner = ChatRunner(
            self.sessions,
            self.gql,
            lease,
            batch_pages=self.settings.chat_batch_pages,
            stop=self.stop,
            after_commit=self.after_chat_commit,
        )
        try:
            result = await runner.run(
                vod,
                channel_emotes=channel_emotes,
                global_emotes=global_emotes,
                until_s=None if minutes is None else vod.chat_done_s + minutes * 60,
            )
        except ReplayUnavailable as exc:
            async with self.sessions.begin() as s:
                await lease.check(s)
                await rows.update_vod(s, vod.id, chat_status=str(VodChatStatus.UNAVAILABLE))
            lines.append(f"chat: unavailable: {exc}")
            return str(VodChatStatus.UNAVAILABLE)
        lines.append(
            f"chat sets: {len(channel_emotes)} channel + {len(global_emotes)} global "
            "third-party emotes (current sets)"
        )
        lines += chat_result_lines(result, vod.duration_s)
        return result.status

    async def _emote_sets(
        self, vod: rows.VodRow, meta: VodMeta, lease: LeaseKeeper, lines: list[str]
    ) -> tuple[dict[str, str], dict[str, str]]:
        """(channel, global) third-party emote sets; failed providers go into the row's flags."""
        twitch_id = vod.channel_user_id or meta.user_id
        channel_emotes = await self.emotes.channel_emotes(twitch_id) if twitch_id else {}
        global_emotes = await self.emotes.global_emotes()
        failed = self.emotes.failed(twitch_id)
        if failed:  # proceed (FFZ is unreachable from the dev PC), but say so in the row
            async with self.sessions.begin() as s:
                await lease.check(s)
                before = await rows.flag_value(s, vod.id, "emote_sets_failed", lock=True)
                failed = sorted({*(before or []), *failed})
                await rows.merge_flags(s, vod.id, {"emote_sets_failed": failed})
            lines.append(
                f"chat: emote sets of {', '.join(failed)} could not be fetched: their emotes "
                "are not counted (vods.flags.emote_sets_failed)"
            )
        return channel_emotes, global_emotes

    # ------------------------------------------------------------------ --fill-gaps

    async def _fill(
        self,
        video_id: str,
        meta: VodMeta,
        media: _Media,
        existing: rows.VodRow | None,
        opts: VodOptions,
    ) -> VodOutcome:
        """`--fill-gaps` (DESIGN §17): reads (our live stream, its chunks and gaps, on a first run
        the T0 measurement), then the plan (dry run) or the writes."""
        if media.restricted:
            return VodOutcome(video_id, "restricted", "sub-only (no playlist): nothing filled")
        assert media.playlist is not None
        broadcast = _broadcast(meta, media)
        async with self.sessions() as s:
            stream = await self._fill_stream(s, video_id, meta, broadcast, existing)
            live = await rows.live_media(s, stream.id)
            leftovers = await rows.fill_leftovers(s, stream.id) if existing is None else 0
        # a live seq at FILL_SEQ_BASE - 1 would be the first fill chunk's seq neighbour
        if live.max_seq is not None and live.max_seq >= FILL_SEQ_BASE - 1:
            raise FillRefused(
                f"stream {stream.id} has live seqs up to {live.max_seq}: fill seqs start at "
                f"{FILL_SEQ_BASE}"
            )
        if leftovers:
            raise FillRefused(
                f"stream {stream.id} has {leftovers} backfill chunks (source backfill or seq >= "
                f"{FILL_SEQ_BASE}) but no fill vods row (a deleted one?): their T0 is unknown; "
                "remove them before filling again"
            )
        lines = self._describe(meta, media, existing, None)
        lines.append(_live_line(stream, live))
        new = measured = None
        if existing is None:
            new, measured = await self._new_fill_vod(meta, media, broadcast, live, opts)
            t0, duration = new.t0.t0, new.duration_s
            lines += fills.t0_lines(measured)
            lines.append(f"  candidates: {_compact(new.t0.candidates)}")
            lines.append(f"  flags: {_compact(_shown_flags(new.flags))}")
            if measured.refusal and not opts.dry_run:
                raise FillRefused(measured.refusal)
        else:
            t0, duration = existing.t0_utc, existing.duration_s
            lines.append(
                f"T0 {t0.isoformat()} ({existing.t0_method}), frozen in vods row {existing.id}"
                + (" (--accept-t0 ignored)" if opts.accept_t0 else "")
            )
            lines.append(f"  candidates: {_compact(existing.t0_candidates)}")
            lines.append(f"  flags: {_compact(_shown_flags(existing.flags))}")
        if opts.dry_run:
            ranges = _fill_ranges(t0, duration, live)
            lines += await self._describe_fill(stream, media, existing, t0, ranges, opts)
            if opts.clips:
                lines.append(
                    f"clips: would fetch the top {self.settings.clips_top} by views (Helix)"
                    if self.helix
                    else f"clips: skipped: {NO_HELIX}"
                )
            return VodOutcome(video_id, "dry_run", "nothing written", lines)
        return await self._fill_write(video_id, meta, media, stream, live, new, opts, lines)

    async def _fill_stream(
        self,
        s: AsyncSession,
        video_id: str,
        meta: VodMeta,
        broadcast: _Broadcast,
        existing: rows.VodRow | None,
    ) -> rows.StreamRow:
        """Our ended LIVE stream of the broadcast, else FillRefused."""
        if existing is not None:
            stream = await rows.stream_by_id(s, existing.stream_id)
        else:
            channel_id = await rows.find_channel(s, login=meta.login, user_id=meta.user_id)
            stream = (
                await rows.find_stream(
                    s, channel_id=channel_id, platform_stream_id=broadcast.stream_id
                )
                if channel_id is not None
                else None
            )
        if stream is None:
            raise FillRefused(
                f"broadcast {broadcast.stream_id} was not recorded live by us: --fill-gaps "
                f"fills our live streams only; use `vod {video_id}` without --fill-gaps"
            )
        if stream.source != DataSource.LIVE:
            raise FillRefused(
                f"stream {stream.id} is a backfilled stream, not one we recorded live: use "
                f"`vod {video_id}` without --fill-gaps"
            )
        if stream.status != StreamStatus.ENDED or stream.ended_at is None:
            raise FillRefused(
                f"stream {stream.id} is still live (the recorder writes it): wait until it ended"
            )
        if utc_now() < stream.ended_at + ENDED_GRACE:
            raise FillRefused(
                f"stream {stream.id} ended at {stream.ended_at.isoformat()}, less than "
                f"{ENDED_GRACE.seconds // 60} min ago (the recorder may still upload its last "
                "chunk): wait until it ended"
            )
        holder = await leases.holder(s, recording_lease(stream.channel_id))
        if holder is not None:
            raise FillRefused(
                f"a recorder ({holder}) holds the recording lease of channel {stream.channel_id}: "
                "wait until it ended"
            )
        return stream

    async def _new_fill_vod(
        self,
        meta: VodMeta,
        media: _Media,
        broadcast: _Broadcast,
        live: rows.LiveMedia,
        opts: VodOptions,
    ) -> tuple[rows.NewVod, fills.FillT0]:
        """The fill's vods row as a first run knows it, with the T0 measured on the live audio."""
        if media.audio is None:
            raise FillRefused(f"no {AUDIO} playlist: --fill-gaps measures its T0 on it")
        if self.store is None or not hasattr(self.store, "get_bytes"):
            raise UsageError("--fill-gaps reads the live chunks' audio: it needs object storage")
        playlist = media.playlist
        assert playlist is not None
        duration = playlist.duration_s if playlist.segments else meta.length_s
        chat, chat_info = await self._sample_chat(meta.video_id, duration)
        estimate = estimate_t0(
            pdt=pdt_points(media.audio.segments),
            chat=chat,
            created_at=meta.created_at,
            created_at_source=meta.created_source,
            path_epoch=broadcast.epoch,
        )
        measured = await fills.measure_t0(
            self.downloader,
            self.store,  # type: ignore[arg-type]  # ObjectStore: put_file + get_bytes
            ffmpeg=self.settings.ffmpeg,
            temp_dir=self.settings.temp_dir,
            chunks=[c for c in live.chunks if c.audio_key is not None],
            ranges=_fill_ranges(estimate.t0, duration, live),
            segments=media.audio.segments,
            estimate=estimate,
            accept=opts.accept_t0,
            search_s=self.settings.fill_search_s,
            tries=self.settings.fill_seam_tries,
            min_windows=self.settings.fill_t0_min_windows,
            spread_flag_s=self.settings.fill_t0_spread_s,
            max_pdt_diff_s=self.settings.fill_t0_max_pdt_diff_s,
        )
        if measured.audio is None and measured.pdt is None:
            raise FillRefused("no T0: no live audio window matched and the playlist has no PDT")
        t0, method = measured.shown  # the dry run plans with it even when a run would refuse
        candidates = {**measured.candidates, **chat_info, "stream_ids": broadcast.stream_ids}
        if meta.gql_created_at and meta.created_source != "gql_created_at":
            candidates["gql_created_at"] = meta.gql_created_at.isoformat()
        ranges = _fill_ranges(t0, duration, live)
        whole = plan_fill(
            playlist.segments,
            ranges,
            t0,
            first_seq=FILL_SEQ_BASE,
            chunk_s=self.settings.chunk_seconds,
        )
        flags: dict[str, Any] = {
            "mode": "fill_gaps",
            **broadcast.flags,
            **measured.flags,
            **_playlist_flags(playlist, media.audio),
            "fill_ranges": [
                {
                    "kind": p.range.kind,
                    "start": p.range.interval.start.isoformat(),
                    "end": p.range.interval.end.isoformat(),
                    "gap_row": p.range.gap_row,
                    "segments": p.segments,
                }
                for p in whole.parts
            ],
        }
        muted = sum(p.muted_s for p in whole.parts)
        if muted:
            flags["fill_muted_s"] = round(muted, 3)
        new = rows.NewVod(
            video_id=meta.video_id,
            login=meta.login,
            user_id=meta.user_id,
            display_name=meta.display_name,
            platform_stream_id=broadcast.stream_id,
            title=meta.title,
            category=meta.category,
            created_at=meta.created_at,
            duration_s=duration,
            t0=T0Estimate(t0, method, candidates, {}),
            rendition=media.rendition,
            media_status=VodMediaStatus.PENDING,
            chat_status=VodChatStatus.PENDING if opts.chat else VodChatStatus.OFF,
            flags=flags,
        )
        return new, measured

    async def _fill_write(
        self,
        video_id: str,
        meta: VodMeta,
        media: _Media,
        stream: rows.StreamRow,
        live: rows.LiveMedia,
        new: rows.NewVod | None,
        opts: VodOptions,
        lines: list[str],
    ) -> VodOutcome:
        lease = self._lease(video_id)
        if not await lease.acquire():
            holder = await lease.holder()
            return VodOutcome(video_id, "busy", f"another run holds the lease ({holder})", lines)
        lease.start()
        chat = None
        try:
            async with self.sessions.begin() as s:
                await lease.check(s)
                vod = await rows.find_vod(s, video_id)
                if vod is None:
                    assert new is not None
                    vod = await rows.create_fill_vod(
                        s, new, channel_id=stream.channel_id, stream_id=stream.id
                    )
                    lines.append(
                        f"created vods row {vod.id} (mode fill_gaps) of stream {vod.stream_id}"
                    )
            # the T0 of the row (a concurrent first run may have frozen another one)
            ranges = _fill_ranges(vod.t0_utc, vod.duration_s, live)
            status, message, result = await self._fill_media_stage(
                vod, media, ranges, opts, lease, lines
            )
            if status == "refused":
                return VodOutcome(video_id, status, message, lines)
            if opts.chat and not (self.stop is not None and self.stop.is_set()):
                chat = await self._fill_chat_stage(vod, meta, ranges, opts, lease, lines)
        finally:
            await lease.release()
        if chat is not None:
            message += f"; chat {chat}"
            if chat == VodChatStatus.PENDING and status == "done":
                status = "partial"
        if self.stop is None or not self.stop.is_set():
            lines += await self._clips(video_id, opts, vod_id=vod.id)
        return VodOutcome(video_id, status, message, lines, result)

    async def _fill_media_stage(
        self,
        vod: rows.VodRow,
        media: _Media,
        ranges: list[FillRange],
        opts: VodOptions,
        lease: LeaseKeeper,
        lines: list[str],
    ) -> tuple[str, str, MediaResult | None]:
        if opts.transcribe:
            async with self.sessions.begin() as s:
                await lease.check(s)
                added = await rows.enqueue_missing_jobs(s, vod.stream_id)
            if added:
                lines.append(f"queued {added} transcribe jobs left out by --no-transcribe")
        if vod.media_status == VodMediaStatus.DONE:
            return "done", "fill media complete already", None
        if vod.media_status != VodMediaStatus.PENDING:  # gone -> playable again
            async with self.sessions.begin() as s:
                await lease.check(s)
                await rows.update_vod(s, vod.id, media_status=str(VodMediaStatus.PENDING))
            vod = replace(vod, media_status=str(VodMediaStatus.PENDING))
        assert self.store is not None and media.playlist is not None
        try:
            check_aligned(media.playlist.segments, vod.media_done_s)
        except PlanError as exc:
            hint = f"; continue with --video {vod.rendition}" if vod.rendition else ""
            if vod.rendition == AUDIO:
                hint = "; continue without --video"
            return "refused", f"{media.rendition}: {exc}{hint}", None
        with tempfile.TemporaryDirectory(
            prefix=TEMP_PREFIX, dir=self.settings.temp_dir, ignore_cleanup_errors=True
        ) as tmp:
            lease.touch = Path(tmp)
            runner = MediaRunner(
                self.sessions,
                self.store,
                self.downloader,
                lease,
                workdir=Path(tmp),
                ffmpeg=self.settings.ffmpeg,
                chunk_s=self.settings.chunk_seconds,
                with_video=media.rendition != AUDIO,
                transcribe=opts.transcribe,
                stop=self.stop,
                after_commit=self.after_commit,
            )
            budget = None if opts.max_minutes is None else opts.max_minutes * 60
            result = await runner.run_fill(vod, media.playlist.segments, ranges, budget_s=budget)
        lines += _fill_result_lines(result, vod.stream_id)
        status = "done" if result.status == VodMediaStatus.DONE else "partial"
        return status, f"fill media {result.status}", result

    async def _fill_chat_stage(
        self,
        vod: rows.VodRow,
        meta: VodMeta,
        ranges: list[FillRange],
        opts: VodOptions,
        lease: LeaseKeeper,
        lines: list[str],
    ) -> str:
        """The replay over the fill ranges' whole seconds (no guard 2: seconds with a metrics row
        are left alone one by one instead)."""
        if vod.chat_status == VodChatStatus.DONE:
            lines.append("chat: complete already")
            return str(VodChatStatus.DONE)
        if vod.chat_status != VodChatStatus.PENDING:  # off/unavailable -> try again
            async with self.sessions.begin() as s:
                await lease.check(s)
                await rows.update_vod(s, vod.id, chat_status=str(VodChatStatus.PENDING))
        channel_emotes, global_emotes = await self._emote_sets(vod, meta, lease, lines)
        minutes = opts.chat_max_minutes if opts.chat_max_minutes is not None else opts.max_minutes
        runner = ChatRunner(
            self.sessions,
            self.gql,
            lease,
            batch_pages=self.settings.chat_batch_pages,
            stop=self.stop,
            after_commit=self.after_chat_commit,
        )
        spans = chat_spans([r.interval for r in ranges])
        try:
            result = await runner.run_ranges(
                vod,
                spans,
                channel_emotes=channel_emotes,
                global_emotes=global_emotes,
                budget_s=None if minutes is None else minutes * 60,
            )
        except ReplayUnavailable as exc:
            async with self.sessions.begin() as s:
                await lease.check(s)
                await rows.update_vod(s, vod.id, chat_status=str(VodChatStatus.UNAVAILABLE))
            lines.append(f"chat: unavailable: {exc}")
            return str(VodChatStatus.UNAVAILABLE)
        lines.append(
            f"chat: {len(spans)} fill ranges ({fills.hms(sum(sp.seconds for sp in spans))} of "
            "chat time); seconds that had a chat_metrics_1s row stay as they were"
        )
        lines += chat_result_lines(result, vod.duration_s)
        return result.status

    async def _describe_fill(
        self,
        stream: rows.StreamRow,
        media: _Media,
        existing: rows.VodRow | None,
        t0: datetime,
        ranges: list[FillRange],
        opts: VodOptions,
    ) -> list[str]:
        """The dry run's plan: ranges, chunks, chat."""
        assert media.playlist is not None
        async with self.sessions() as s:
            seq = await rows.next_fill_seq(s, stream.id)
            spans = chat_spans([r.interval for r in ranges])
            taken = 0
            for sp in spans:
                taken += len(await rows.metric_seconds(s, channel_id=stream.channel_id, window=sp))
        done = existing.media_done_s if existing else 0.0
        chunk_s = self.settings.chunk_seconds
        segments = media.playlist.segments
        whole = plan_fill(segments, ranges, t0, first_seq=FILL_SEQ_BASE, chunk_s=chunk_s)
        lines = fills.range_lines(whole)
        if existing is not None and existing.media_status == VodMediaStatus.DONE:
            lines.append("plan: fill media complete already")
        else:
            try:
                plan = plan_fill(segments, ranges, t0, done_s=done, first_seq=seq, chunk_s=chunk_s)
            except PlanError as exc:
                plan = None
                lines.append(f"plan: refused: {exc}")
            if plan is not None:
                lines += fills.plan_lines(
                    plan, audio_only=media.rendition == AUDIO, bytes_per_s=AUDIO_BYTES_PER_S
                )
        if not opts.chat:
            lines.append("chat: --no-chat")
        elif existing is not None and existing.chat_status == VodChatStatus.DONE:
            lines.append("chat: complete already")
        else:
            mark = existing.chat_metrics_to if existing else None
            todo = [sp for sp in spans if mark is None or sp.end > mark]
            lines.append(
                f"chat: would replay {len(todo)} fill ranges "
                f"({fills.hms(sum(sp.seconds for sp in todo))} of chat time)"
                + (f" from {mark.isoformat()}" if mark else "")
                + f"; {taken} of their seconds have a chat_metrics_1s row (live chat): kept"
            )
        return lines

    async def _clips(self, video_id: str, opts: VodOptions, *, vod_id: int | None) -> list[str]:
        """Viewer clips of the VOD (`--no-clips` turns it off). Never fails the run: media and
        chat are committed by now; a failure is logged and kept in `vods.flags.clips_error`
        (cleared by the next successful fetch)."""
        if not opts.clips or opts.dry_run:
            return []
        if self.helix is None:
            return [f"clips: skipped: {NO_HELIX}"]
        try:
            found = await ClipCollector(
                self.sessions, self.helix, top=self.settings.clips_top
            ).for_video(video_id)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:300]
            log.exception("clips failed, a re-run tries again", error=error)
            await self._clips_flag(vod_id, error)
            return [f"clips: failed: {error}"]
        await self._clips_flag(vod_id, None)
        return found.lines()

    async def _clips_flag(self, vod_id: int | None, error: str | None) -> None:
        if vod_id is None:
            return
        try:
            async with self.sessions.begin() as s:
                if error is None:
                    await rows.drop_flags(s, vod_id, "clips_error")
                else:
                    await rows.merge_flags(s, vod_id, {"clips_error": error})
        except Exception:
            log.exception("could not record the clips outcome")

    async def _media(
        self, vod: rows.VodRow, media: _Media, opts: VodOptions, lease: LeaseKeeper
    ) -> MediaResult:
        assert self.store is not None and media.playlist is not None
        begin = max(vod.media_done_s, opts.start_offset_s)
        until = None if opts.max_minutes is None else begin + opts.max_minutes * 60
        with tempfile.TemporaryDirectory(
            prefix=TEMP_PREFIX, dir=self.settings.temp_dir, ignore_cleanup_errors=True
        ) as tmp:
            lease.touch = Path(tmp)  # renewals keep its mtime fresh: not stale for others
            runner = MediaRunner(
                self.sessions,
                self.store,
                self.downloader,
                lease,
                workdir=Path(tmp),
                ffmpeg=self.settings.ffmpeg,
                chunk_s=self.settings.chunk_seconds,
                with_video=media.rendition != AUDIO,
                transcribe=opts.transcribe,
                stop=self.stop,
                after_commit=self.after_commit,
            )
            return await runner.run(
                vod, media.playlist.segments, start_s=opts.start_offset_s, until_s=until
            )

    # ------------------------------------------------------------------ reports

    def _describe(
        self,
        meta: VodMeta,
        media: _Media,
        existing: rows.VodRow | None,
        new: rows.NewVod | None,
    ) -> list[str]:
        lines = [
            f"video {meta.video_id}: {meta.login} {_hms(meta.length_s)} created "
            f"{meta.created_at.isoformat()} ({meta.created_source}), {meta.title or ''!r}"[:160]
        ]
        if media.restricted:
            lines.append("media: restricted (sub-only?): no playlist")
        elif media.playlist is not None:
            pl = media.playlist
            muted = sum(s.duration for s in pl.segments if s.muted)
            lines.append(
                f"media: {media.rendition} ({len(media.renditions)} renditions), "
                f"{len(pl.segments)} segments, {_hms(pl.duration_s)}, muted {_hms(muted)}, "
                f"{sum(s.discontinuity for s in pl.segments)} discontinuities"
            )
        if existing is not None:
            lines.append(
                f"existing: stream {existing.stream_id}, T0 {existing.t0_utc.isoformat()} "
                f"({existing.t0_method}), media {existing.media_status} at "
                f"{existing.media_done_s:.1f}/{existing.duration_s:.1f} s"
            )
        elif new is not None:
            t0 = new.t0
            lines.append(
                f"new: stream {new.platform_stream_id}, T0 {t0.t0.isoformat()} ({t0.method})"
            )
            lines.append(f"  candidates: {_compact(t0.candidates)}")
            if new.flags:
                lines.append(f"  flags: {_compact(new.flags)}")
        return lines

    async def _describe_chat(
        self,
        meta: VodMeta,
        existing: rows.VodRow | None,
        new: rows.NewVod | None,
        opts: VodOptions,
    ) -> list[str]:
        if not opts.chat:
            return ["chat: --no-chat"]
        if existing is not None and existing.chat_status == VodChatStatus.DONE:
            return ["chat: complete already"]
        async with self.sessions() as s:
            if existing is not None:
                channel_id, stream_id = existing.channel_id, existing.stream_id
                window = chat_window(existing)
            else:
                assert new is not None
                channel_id = await rows.find_channel(s, login=meta.login, user_id=meta.user_id)
                stream_id = -1
                t0 = new.t0
                window = window_for(t0.t0, t0.method, t0.candidates, new.duration_s)
            foreign = None
            if channel_id is not None:
                foreign = await rows.foreign_chat_metrics(
                    s, channel_id=channel_id, stream_id=stream_id, window=window
                )
        if foreign is not None and foreign.rows >= self.settings.chat_guard_min_s:
            return [f"chat: would be skipped: {foreign.rows} seconds of other chat in range"]
        if existing is None or existing.chat_metrics_to is None:
            return ["chat: would page the replay from offset 0"]
        start = chat_clock(existing).resume_offset(existing.chat_metrics_to)
        return [
            f"chat: would resume at offset {min(start, existing.chat_done_s)} s (messages "
            f"committed to {existing.chat_done_s} s, metrics final before "
            f"{existing.chat_metrics_to.isoformat()})"
        ]

    async def _describe_plan(
        self, media: _Media, existing: rows.VodRow | None, opts: VodOptions
    ) -> list[str]:
        if media.restricted:
            return ["plan: restricted -> media_status=restricted + not_recorded gap over the VOD"]
        if not opts.media:
            return ["plan: --no-media -> rows only"]
        if media.playlist is None:
            return ["plan: nothing to fetch"]
        done = existing.media_done_s if existing else 0.0
        seq = 0
        if existing is not None:
            async with self.sessions() as s:
                seq = await rows.next_seq(s, existing.stream_id)
            if existing.media_status == VodMediaStatus.DONE:
                return ["plan: media complete already"]
        begin = max(done, opts.start_offset_s)
        until = None if opts.max_minutes is None else begin + opts.max_minutes * 60
        try:
            check_aligned(media.playlist.segments, done)
        except PlanError as exc:
            return [f"plan: refused: {exc}"]
        plan = plan_units(
            media.playlist.segments,
            done_s=done,
            start_s=opts.start_offset_s,
            first_seq=seq,
            chunk_s=self.settings.chunk_seconds,
            until_s=until,
            muted_video=media.rendition != AUDIO,
        )
        return _plan_lines(plan, media.rendition == AUDIO)


@dataclass(frozen=True, slots=True)
class _Broadcast:
    """Which broadcast (platform stream id) a VOD is, from every source that names it."""

    stream_id: str
    stream_ids: dict[str, str]  # source -> stream id
    epoch: datetime | None  # the CDN path's epoch (a T0 hint)
    flags: dict[str, Any]


def _broadcast(meta: VodMeta, media: _Media) -> _Broadcast:
    """Helix `stream_id` > the playlist's CDN path > the storyboard's path; a placeholder that
    can't collide when nothing names it (flag `stream_id_unknown`)."""
    stream_ids = dict(meta.stream_ids)
    path = parse_vod_path(media.renditions.get(media.rendition or AUDIO)) or next(
        (p for p in map(parse_vod_path, media.renditions.values()) if p), None
    )
    if path:
        stream_ids["playlist_path"] = path.stream_id
    flags: dict[str, Any] = {}
    if len(set(stream_ids.values())) > 1:
        flags["stream_id_mismatch"] = stream_ids
    stream_id = next(
        (stream_ids[k] for k in ("helix", "playlist_path", "preview_path") if k in stream_ids),
        None,
    )
    if stream_id is None:
        stream_id = f"video:{meta.video_id}"
        flags["stream_id_unknown"] = True
    epoch = path.epoch if path else meta.preview_epoch
    return _Broadcast(stream_id, stream_ids, epoch, flags)


def _pick_rendition(renditions: dict[str, str], wanted: str | None) -> str:
    if wanted is None:
        if AUDIO not in renditions:
            raise UsageError(f"no {AUDIO} rendition (has {sorted(renditions)}); use --video")
        return AUDIO
    if wanted not in renditions:
        raise UsageError(f"no rendition {wanted!r} (has {sorted(renditions)})")
    if wanted in ("best", "worst"):  # streamlink synonyms: store the real name
        url = renditions[wanted]
        return next(
            (n for n, u in renditions.items() if u == url and n not in ("best", "worst")), wanted
        )
    return wanted


def _playlist_flags(playlist: VodPlaylist, audio: VodPlaylist | None) -> dict[str, Any]:
    flags: dict[str, Any] = {}
    muted = sum(s.duration for s in playlist.segments if s.muted)
    if muted:
        flags["muted_s"] = round(muted, 3)
    if playlist.total_secs is not None and playlist.total_secs - playlist.duration_s > 1.0:
        flags["timeline_uncertain_s"] = round(playlist.total_secs - playlist.duration_s, 3)
    if any(s.discontinuity for s in playlist.segments):
        flags["discontinuities"] = sum(s.discontinuity for s in playlist.segments)
    if audio is not None and audio is not playlist:
        diff = audio.duration_s - playlist.duration_s
        if abs(diff) > 1.0:
            flags["rendition_length_mismatch_s"] = round(diff, 3)
    return flags


def _plan_lines(plan: Plan, audio_only: bool) -> list[str]:
    chunks = plan.chunks
    seconds = sum(u.end_s - u.start_s for u in chunks)
    gaps: dict[str, float] = {}
    for g in plan.gaps:
        gaps[g.reason] = gaps.get(g.reason, 0.0) + g.seconds
    if not plan.units:
        return [f"plan: nothing to do from {plan.start_s:.1f} s"]
    first, last = chunks[0] if chunks else None, chunks[-1] if chunks else None
    lines = [
        f"plan: {len(chunks)} chunks ({_hms(seconds)}) from {plan.start_s:.1f} s to "
        f"{plan.end_s:.1f} s of {plan.vod_end_s:.1f} s"
        + (f", seq {first.seq}..{last.seq}" if first and last else "")
        + (" (complete)" if plan.complete else ""),
        "  gaps: " + (", ".join(f"{r} {_hms(s)}" for r, s in gaps.items()) or "none"),
        f"  downloads: {len(plan.fetch_segments())} segments"
        + (f", ~{seconds * AUDIO_BYTES_PER_S / 1e6:.1f} MB" if audio_only else ""),
    ]
    return lines


def _result_lines(result: MediaResult) -> list[str]:
    gaps = ", ".join(f"{r} {_hms(s)}" for r, s in result.gaps.items()) or "none"
    return [
        f"committed: {result.chunks} chunks ({_hms(result.media_s)}), gaps: {gaps}; media "
        f"{result.status} at {result.done_s:.1f} s" + (" (stopped)" if result.stopped else ""),
        f"  stored: audio {result.audio_bytes / 1e6:.2f} MB, video "
        f"{result.video_bytes / 1e6:.2f} MB; lost segments: {result.lost_segments}",
    ]


def _fill_ranges(t0: datetime, duration_s: float, live: rows.LiveMedia) -> list[FillRange]:
    """`planner.fill_ranges` of a live stream for a VOD at `t0` of `duration_s`."""
    return fill_ranges(
        vod=Interval(t0, at(t0, duration_s)),
        chunks=[Interval(c.start_utc, c.end_utc) for c in live.chunks],
        gaps=[(Interval(g.start_utc, g.end_utc), g.reason) for g in live.gaps],
    )


def _shown_flags(flags: dict[str, Any]) -> dict[str, Any]:
    """A fill row's flags for the report, without the ones it prints in detail."""
    return {k: v for k, v in flags.items() if k not in ("fill_audio", "fill_ranges")}


def _live_line(stream: rows.StreamRow, live: rows.LiveMedia) -> str:
    chunks = live.chunks
    span = (
        f" {min(c.start_utc for c in chunks).isoformat()} .. "
        f"{max(c.end_utc for c in chunks).isoformat()}, seq 0..{live.max_seq}"
        if chunks
        else ""
    )
    reasons: dict[str, float] = {}
    for g in live.gaps:
        reasons[g.reason] = reasons.get(g.reason, 0.0) + (g.end_utc - g.start_utc).total_seconds()
    gaps = ", ".join(f"{r} {_hms(sec)}" for r, sec in reasons.items()) or "none"
    ended = stream.ended_at.isoformat() if stream.ended_at else "?"
    return (
        f"fill: live stream {stream.id} (broadcast {stream.platform_stream_id}, ended {ended}): "
        f"{len(chunks)} live chunks{span}; gap rows: {gaps}"
    )


def _fill_result_lines(result: MediaResult, stream_id: int) -> list[str]:
    return [
        f"filled: {result.chunks} chunks ({_hms(result.media_s)}) into live stream {stream_id}; "
        f"fill media {result.status} at {result.done_s:.1f} s"
        + (" (stopped)" if result.stopped else ""),
        f"  stored: audio {result.audio_bytes / 1e6:.2f} MB, video "
        f"{result.video_bytes / 1e6:.2f} MB; lost segments: {result.lost_segments}",
    ]


def _hms(seconds: float) -> str:
    whole = round(seconds)
    return f"{whole // 3600}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


def _compact(data: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in data.items())
