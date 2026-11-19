"""One VOD, end to end: metadata -> playlist -> T0 -> rows -> media (DESIGN §17).

Reads first (all of them also in `--dry-run`): GQL video metadata (+ Helix when there are
credentials), the renditions (streamlink), the media playlist (+ `audio_only` for its PDT on a
first run with `--video`), a few chat pages for the T0 cross-check (first run only), and our
own rows. Then, unless dry: the lease, stream + vods rows (first run), and the media pipeline.

Skipped: highlights/uploads (no broadcast), archives still recording, broadcasts we recorded live
(clips only, step 5d). Sub-only (usher 403): `media_status='restricted'` and a `not_recorded` gap
over the VOD. Chat replay and viewer clips are step 5d (not written yet).
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

from clipper_backfill import rows
from clipper_backfill.download import Downloader
from clipper_backfill.pipeline import TEMP_PREFIX, LeaseKeeper, MediaResult, MediaRunner, Store
from clipper_backfill.planner import Plan, PlanError, Unit, check_aligned, plan_units
from clipper_backfill.settings import BackfillSettings
from clipper_backfill.t0 import ChatPoint, T0Estimate, estimate_t0, pdt_points
from clipper_core.contracts import VodChatStatus, VodMediaStatus, backfill_lease
from clipper_core.logs import get_logger, log_context
from clipper_twitch.gql import ChatReplay, GqlError, ReplayUnavailable, VideoInfo, VideoInfoSource
from clipper_twitch.helix import HelixClient, HelixVideo
from clipper_twitch.hls import VodPlaylist, parse_vod_playlist
from clipper_twitch.vod import VodGone, VodRestricted, VodSource, parse_vod_path

log = get_logger(__name__)

AUDIO = "audio_only"
AUDIO_BYTES_PER_S = 27_400  # audio_only TS: ~274 KB per 10 s segment (DESIGN §3)
_VIDEO_PATH = re.compile(r"/videos?/(\d+)")


class GqlSource(VideoInfoSource, ChatReplay, Protocol):
    """`GqlChatReplay` (or its fake): video metadata + chat pages through one pacer."""


class UsageError(ValueError):
    """Bad input (unknown rendition, not a video id/URL): shown to the user, no traceback."""


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
    max_minutes: float | None = None  # media per run
    start_offset_s: float = 0.0  # first run: start here (the part before is `not_recorded`)
    transcribe: bool = True
    chat: bool = True  # step 5d (not fetched yet): False sets `chat_status='off'`
    clips: bool = True  # step 5d (not fetched yet)


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
        self.downloader = Downloader(
            http,
            concurrency=settings.downloads,
            attempts=settings.segment_attempts,
            backoff_s=settings.retry_backoff_s,
        )

    async def vod(self, ref: str, opts: VodOptions | None = None) -> VodOutcome:
        video_id = parse_video_ref(ref)
        requests, size = self.downloader.requests, self.downloader.bytes
        with log_context(video_id=video_id):
            outcome = await self._vod(video_id, opts or VodOptions())
        if self.downloader.requests > requests:
            outcome.lines.append(
                f"downloaded: {self.downloader.requests - requests} requests, "
                f"{(self.downloader.bytes - size) / 1e6:.2f} MB (playlists + segments)"
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
            return VodOutcome(video_id, "skipped", "not a past broadcast (highlight or upload)")
        if meta.recording:
            return VodOutcome(video_id, "skipped", "the broadcast is still live")

        try:
            media = await self._playlists(video_id, opts, first_run=existing is None)
        except VodGone as exc:
            return await self._gone(video_id, existing, opts, str(exc))
        if media.playlist is not None and not media.playlist.ended:
            return VodOutcome(video_id, "skipped", "the playlist is not finished (still live?)")

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
                return VodOutcome(
                    video_id,
                    "skipped",
                    f"recorded live as stream {live}: live data wins (clips only: step 5d)",
                )
            new = await self._new_vod(meta, media, broadcast, opts)

        lines = self._describe(meta, media, existing, new)
        if opts.dry_run:
            lines += await self._describe_plan(media, existing, opts)
            return VodOutcome(video_id, "dry_run", "nothing written", lines)
        return await self._write(video_id, media, existing, new, opts, lines)

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
        try:
            async with self.sessions.begin() as s:
                await lease.check(s)
                vod = await rows.find_vod(s, video_id)
                if vod is None:
                    assert new is not None
                    try:
                        vod = await rows.create_vod(s, new)
                    except rows.RecordedLive as exc:
                        return VodOutcome(video_id, "skipped", f"{exc}: clips only (5d)", lines)
                    lines.append(f"created stream {vod.stream_id} and vods row {vod.id}")
            if media.restricted and vod.media_status == VodMediaStatus.DONE:
                note = "media complete already (now restricted)"
                return VodOutcome(video_id, "done", note, lines)
            if media.restricted:
                async with self.sessions.begin() as s:
                    await lease.check(s)
                    marked = await rows.mark_restricted(s, vod)
                note = "marked restricted, not_recorded gap" if marked else "already restricted"
                return VodOutcome(video_id, "restricted", f"sub-only: {note}", lines)
            if not opts.media:
                return VodOutcome(video_id, "rows_only", "--no-media: no media fetched", lines)
            if opts.transcribe:
                async with self.sessions.begin() as s:
                    await lease.check(s)
                    added = await rows.enqueue_missing_jobs(s, vod.stream_id)
                if added:
                    lines.append(f"queued {added} transcribe jobs left out by --no-transcribe")
            if vod.media_status == VodMediaStatus.DONE:
                return VodOutcome(video_id, "done", "media complete already", lines)
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
                return VodOutcome(video_id, "refused", f"{media.rendition}: {exc}{hint}", lines)
            if vod.rendition and media.rendition != vod.rendition:
                log.info("rendition differs from the first run", first=vod.rendition)
            result = await self._media(vod, media, opts, lease)
        finally:
            await lease.release()
        lines += _result_lines(result)
        status = "done" if result.status == VodMediaStatus.DONE else "partial"
        return VodOutcome(video_id, status, f"media {result.status}", lines, result)

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


def _hms(seconds: float) -> str:
    whole = round(seconds)
    return f"{whole // 3600}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


def _compact(data: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in data.items())
