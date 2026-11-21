"""`clipper-backfill`: past Twitch broadcasts, written in the live format (DESIGN §17).

    uv run clipper-backfill vod 2932113307 --dry-run         # reads only, prints the plan
    uv run clipper-backfill vod https://www.twitch.tv/videos/2932113307 --max-minutes 5
    uv run clipper-backfill vod 2932113307 --video 720p60    # video + its audio (default: audio)
    uv run clipper-backfill vod 2932113307 --no-media --chat-max-minutes 30   # chat only
    uv run clipper-backfill channel xqc --since 2026-11-01 --limit 3   # lists only (Helix)
    uv run clipper-backfill clips xqc | clips 2932113307    # viewer clips (Helix credentials)
    uv run clipper-backfill calibrate 2932113307            # read-only: VOD vs our live recording
    uv run clipper-backfill status [VIDEO_ID ...]

In Docker: `docker compose run --rm backfill vod <id> --dry-run` (profile `tools`).
Backfill runs only on a VOD the user names (DESIGN §17): `channel <login>` lists the channel's
archives, newest first, with what we already have of each, and writes nothing.
A `vod` run does media (audio_only by default), then the chat replay (into the live chat tables),
then viewer clips (only with Helix credentials, else skipped with a note). Idempotent: a re-run
resumes where the last one committed (`vods.media_done_s`, `vods.chat_done_s`). Needs ffmpeg on
PATH (`CLIPPER_BACKFILL_FFMPEG`) for media and `calibrate`; `--no-media` needs no object storage.
A third-party emote provider that can't be reached (FFZ is unreachable from the dev PC) doesn't
stop the chat: its emotes aren't counted and `vods.flags.emote_sets_failed` names it. Viewer-clip
failures never fail a `vod` run (logged, `vods.flags.clips_error`).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import re
import shutil
import sys
from datetime import UTC, datetime

import httpx

from clipper_backfill import rows
from clipper_backfill.backfill import (
    NO_HELIX,
    Backfiller,
    UsageError,
    VodOptions,
    VodOutcome,
    parse_video_ref,
)
from clipper_backfill.chat import HttpEmoteSets
from clipper_backfill.clips import ClipCollector
from clipper_backfill.download import DownloadError
from clipper_backfill.pipeline import LeaseLost, MediaError, remove_stale_workdirs
from clipper_backfill.settings import BackfillSettings
from clipper_core.config import Settings
from clipper_core.db import create_engine, session_factory
from clipper_core.logs import get_logger, setup_logging
from clipper_core.runtime import run, stop_event_on_signals
from clipper_core.storage import ObjectStore
from clipper_core.timeline import ensure_utc
from clipper_twitch.gql import GqlBroken, GqlChatReplay, GqlError
from clipper_twitch.helix import HelixClient, HelixError, HelixUser, HelixVideo
from clipper_twitch.settings import TwitchSettings
from clipper_twitch.vod import StreamlinkVodSource, VodError, parse_vod_path

log = get_logger("clipper_backfill")

_HMS = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?$")


def parse_offset(value: str) -> float:
    """Seconds: `90`, `1:30`, `1:02:03`, `1h2m3s`."""
    value = value.strip()
    try:
        if ":" in value:
            parts = [float(p) for p in value.split(":")]
            if len(parts) > 3 or any(p < 0 for p in parts):
                raise ValueError
            seconds = 0.0
            for p in parts:
                seconds = seconds * 60 + p
            return seconds
        if re.fullmatch(r"\d+(?:\.\d+)?", value):
            return float(value)
        m = _HMS.match(value)
        if not value or not m:
            raise ValueError
        h, mi, s = m.groups()
        return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an offset: {value!r}") from None


def _positive(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return number


def _count(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


def _day(value: str) -> datetime:
    """`2026-11-01` (UTC midnight), or a datetime; one with a zone is converted to UTC."""
    try:
        day = datetime.fromisoformat(value)
        return ensure_utc(day if day.tzinfo else day.replace(tzinfo=UTC))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date (YYYY-MM-DD): {value!r}") from None


def parse_args(argv: list[str]) -> argparse.Namespace:
    media = argparse.ArgumentParser(add_help=False)
    media.add_argument("--dry-run", action="store_true", help="read only, print the plan")
    media.add_argument(
        "--no-media",
        action="store_true",
        help="no media (media_status=off); chat and clips still run",
    )
    media.add_argument(
        "--video",
        nargs="?",
        const="best",
        metavar="Q",
        help="take video too, from rendition Q (default best = source); its audio is used",
    )
    media.add_argument(
        "--max-minutes",
        type=_positive,
        metavar="N",
        help="fetch at most N minutes of media and of chat this run",
    )
    media.add_argument(
        "--chat-max-minutes",
        type=_positive,
        metavar="N",
        help="fetch at most N minutes of chat this run (instead of --max-minutes)",
    )
    media.add_argument(
        "--start-offset",
        type=parse_offset,
        default=0.0,
        metavar="S",
        help="media: start at VOD offset S (90, 1:30, 1h2m3s); the part before is a "
        "not_recorded gap (chat always starts at 0)",
    )
    media.add_argument("--no-transcribe", action="store_true", help="queue no transcribe jobs")
    media.add_argument("--no-chat", action="store_true", help="no chat replay this run")
    media.add_argument("--no-clips", action="store_true", help="no viewer clips this run")

    p = argparse.ArgumentParser(
        prog="clipper-backfill",
        description="Past Twitch broadcasts, written in the live format (DESIGN 17).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("vod", parents=[media], help="backfill archives by id or URL")
    v.add_argument("refs", nargs="+", metavar="ID_OR_URL")
    c = sub.add_parser(
        "channel",
        help="list a channel's archives, newest first, and what we have of each (Helix; "
        "writes nothing: backfill one with `vod <id>`)",
    )
    c.add_argument("login")
    c.add_argument("--since", type=_day, metavar="YYYY-MM-DD", help="archives created since")
    c.add_argument("--limit", type=_count, default=10, metavar="N", help="newest N archives (10)")
    k = sub.add_parser(
        "clips", help="viewer clips of a VOD, or of all our streams of a channel (Helix)"
    )
    k.add_argument("refs", nargs="+", metavar="LOGIN_OR_VIDEO")
    k.add_argument("--top", type=int, metavar="N", help="clips per broadcast, by views (100)")
    cal = sub.add_parser(
        "calibrate", help="read-only: a VOD vs our live recording of it (T0, chat clock)"
    )
    cal.add_argument("ref", metavar="ID_OR_URL")
    cal.add_argument("--windows", type=int, default=3, metavar="N", help="audio windows (3)")
    cal.add_argument(
        "--search", type=_positive, default=20.0, metavar="S", help="search +/- S s (20)"
    )
    s = sub.add_parser("status", help="backfilled VODs and their progress")
    s.add_argument("video_ids", nargs="*")
    return p.parse_args(argv)


def options(args: argparse.Namespace) -> VodOptions:
    return VodOptions(
        dry_run=args.dry_run,
        media=not args.no_media,
        video=args.video,
        max_minutes=args.max_minutes,
        chat_max_minutes=args.chat_max_minutes,
        start_offset_s=args.start_offset,
        transcribe=not args.no_transcribe,
        chat=not args.no_chat,
        clips=not args.no_clips,
    )


def error(message: str) -> None:
    print(message, file=sys.stderr)


def print_outcome(outcome: VodOutcome) -> None:
    """The report on stdout; a VOD that failed (or was refused, or is busy) on stderr."""
    out = sys.stdout if outcome.ok else sys.stderr
    print(f"[{outcome.status}] {outcome.video_id}: {outcome.message}", file=out)
    for line in outcome.lines:
        print(f"  {line}", file=out)


def status_lines(found: list) -> list[str]:
    if not found:
        return ["no backfilled VODs"]
    out = [
        f"{'video':<12} {'channel':<16} {'stream':>6} {'t0 (method)':<36} {'media':<11} "
        f"{'done':>15} {'chunks':>6} {'gaps':>4} {'muted':>7} {'transcribed':>11} "
        f"{'chat':<11} {'chat done':>15} {'chat s':>7} {'clips':>5}"
    ]
    for r in found:
        pct = 100 * r.media_done_s / r.duration_s if r.duration_s else 100.0
        chat_pct = 100 * r.chat_done_s / r.duration_s if r.duration_s else 100.0
        out.append(
            f"{r.video_id:<12} {r.login:<16} {r.stream_id:>6} "
            f"{r.t0_utc.isoformat(timespec='milliseconds') + ' (' + r.t0_method + ')':<36} "
            f"{r.media_status:<11} {f'{r.media_done_s:.0f}/{r.duration_s:.0f}s {pct:.0f}%':>15} "
            f"{r.chunks:>6} {r.gaps:>4} {f'{float(r.muted_s):.0f}s':>7} "
            f"{f'{r.transcribed}/{r.transcribed + r.transcribe_pending}':>11} "
            f"{r.chat_status:<11} "
            f"{f'{r.chat_done_s}/{r.duration_s:.0f}s {min(100, chat_pct):.0f}%':>15} "
            f"{r.chat_seconds:>7} {r.clips:>5}"
        )
        if r.flags:
            out.append(f"{'':<12} flags: {r.flags}")
    return out


async def channel_archives(
    helix: HelixClient, args: argparse.Namespace
) -> tuple[HelixUser, list[HelixVideo]]:
    """The channel and its newest `--limit` archives (created since `--since`), newest first."""
    users = await helix.get_users(logins=[args.login])
    if not users:
        raise UsageError(f"unknown channel {args.login!r}")
    page = await helix.get_videos(users[0].id, type="archive", limit=args.limit)
    return users[0], [v for v in page.items if args.since is None or v.created_at >= args.since]


def broadcast_id(video: HelixVideo) -> str | None:
    """The archive's platform stream id: Helix `stream_id`, else the one in the CDN path of its
    thumbnail (`<hash>_<login>_<stream id>_<epoch>/`, as `vod` reads it from the media paths)."""
    if video.stream_id:
        return video.stream_id
    path = parse_vod_path(video.thumbnail_url)
    return path.stream_id if path else None


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02}:{s % 60:02}"


def _cut(value: str, width: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= width else value[: width - 3] + "..."


def have_text(have: rows.Have | None) -> str:
    """Our stream of the broadcast (chunks, UTC span) and the backfill progress, else "-"."""
    if have is None:
        return "-"
    parts = []
    if (st := have.stream) is not None:
        span = (
            f" {st.first_start:%H:%M:%S}-{st.last_end:%H:%M:%S}Z"
            if st.first_start and st.last_end
            else ""
        )
        now = " (live now)" if st.status == "live" else ""
        parts.append(f"{st.source} stream {st.id}{now}: {st.chunks} chunks{span}")
    if (v := have.vod) is not None:
        media_pct = 100 * v.media_done_s / v.duration_s if v.duration_s else 100.0
        chat_pct = 100 * v.chat_done_s / v.duration_s if v.duration_s else 100.0
        parts.append(
            f"vod: media {v.media_status} {min(100, media_pct):.0f}%, "
            f"chat {v.chat_status} {min(100, chat_pct):.0f}%"
        )
    return "; ".join(parts)


def archive_lines(login: str, videos: list[HelixVideo], have: dict[str, rows.Have]) -> list[str]:
    if not videos:
        return [f"{login}: no archives found"]
    out = [f"{'video':<12} {'created (UTC)':<20} {'duration':>8}  {'title':<40}  have"]
    for v in videos:
        out.append(
            f"{v.id:<12} {v.created_at:%Y-%m-%dT%H:%M:%SZ} {_hms(v.duration_s):>8}  "
            f"{_cut(v.title, 40):<40}  {have_text(have.get(v.id))}"
        )
    out.append("backfill one with: clipper-backfill vod <id>")
    return out


async def amain(argv: list[str]) -> int:
    args = parse_args(argv)
    settings = Settings(service_name="backfill")
    setup_logging(settings, stream=sys.stderr)  # stdout carries only the report
    engine = create_engine(settings)
    sessions = session_factory(engine)
    try:
        if args.cmd == "status":
            async with sessions() as s:
                found = await rows.status_rows(s, args.video_ids)
            print("\n".join(status_lines(found)))
            return 0
        if args.cmd == "clips":
            return await _clips(args, sessions)
        if args.cmd == "channel":
            return await _channel(args, sessions, TwitchSettings())
        return await _backfill(args, settings, sessions)
    finally:
        await engine.dispose()


async def _channel(
    args: argparse.Namespace,
    sessions,
    twitch: TwitchSettings,
    *,
    helix: HelixClient | None = None,
) -> int:
    """List only (backfill never runs on its own, DESIGN §17): Helix reads and SELECTs, no
    writes, no leases, no object storage, no temp dirs."""
    if not twitch.has_credentials:
        error("`channel` lists archives with Helix: set CLIPPER_TWITCH_CLIENT_ID/_SECRET")
        error("(or pass video ids: clipper-backfill vod <id> ...)")
        return 2
    helix = helix or HelixClient.from_settings(twitch)
    try:
        user, videos = await channel_archives(helix, args)
    except UsageError as exc:
        error(f"[error] {args.login}: {exc}")
        return 2
    except (HelixError, httpx.HTTPError, ValueError, KeyError) as exc:  # also malformed Helix data
        error(f"[failed] {args.login}: {type(exc).__name__}: {exc}"[:300])
        return 1
    finally:
        await helix.aclose()
    async with sessions() as s:
        have = await rows.what_we_have(
            s,
            login=user.login,
            user_id=user.id,
            archives=[(v.id, broadcast_id(v)) for v in videos],
        )
    print("\n".join(archive_lines(args.login, videos, have)))
    return 0


async def _clips(args: argparse.Namespace, sessions) -> int:
    twitch = TwitchSettings()
    if not twitch.has_credentials:
        error(f"`clips` {NO_HELIX}")
        return 2
    bs = BackfillSettings()
    helix = HelixClient.from_settings(twitch)
    collector = ClipCollector(sessions, helix, top=args.top or bs.clips_top)
    failed = 0
    try:
        for ref in args.refs:
            try:
                if ref.isdigit() or "/" in ref:
                    results = [await collector.for_video(parse_video_ref(ref))]
                else:
                    results = await collector.for_channel(ref.lower())
            except Exception as exc:  # one bad ref must not stop the others
                if not isinstance(exc, UsageError | LookupError | HelixError | httpx.HTTPError):
                    log.exception("clips failed", ref=ref)
                error(f"[error] {ref}: {type(exc).__name__}: {exc}"[:300])
                failed += 1
                continue
            if not results:
                print(f"{ref}: no streams of ours")
            for result in results:
                print("\n".join(result.lines()))
    finally:
        await helix.aclose()
    return 1 if failed else 0


async def _calibrate(args: argparse.Namespace, settings: Settings, backfiller) -> int:
    from clipper_backfill.calibrate import calibrate  # numpy: only this command needs it

    store = ObjectStore(settings)  # read-only: the live chunks' audio
    try:
        found = await calibrate(
            backfiller, store, args.ref, windows=args.windows, search_s=args.search
        )
    except (UsageError, ValueError) as exc:  # ValueError: e.g. no T0 candidate at all
        error(f"[error] {args.ref}: {type(exc).__name__}: {exc}")
        return 2
    except (DownloadError, MediaError, VodError, GqlError, HelixError, httpx.HTTPError) as exc:
        log.exception("calibrate failed", ref=args.ref)
        error(f"[failed] {args.ref}: {type(exc).__name__}: {exc}"[:300])
        return 1
    print("\n".join(found.lines()))
    return 0


async def _backfill(args: argparse.Namespace, settings: Settings, sessions) -> int:
    bs = BackfillSettings()
    opts = options(args) if args.cmd != "calibrate" else VodOptions(dry_run=True)
    needs_ffmpeg = args.cmd == "calibrate" or (opts.media and not opts.dry_run)
    if needs_ffmpeg and shutil.which(bs.ffmpeg) is None:
        log.error("ffmpeg not found, set CLIPPER_BACKFILL_FFMPEG", ffmpeg=bs.ffmpeg)
        return 1
    twitch = TwitchSettings()
    helix = HelixClient.from_settings(twitch) if twitch.has_credentials else None
    store = None
    if not opts.dry_run:
        stale = remove_stale_workdirs(bs.temp_dir, older_than_s=bs.lease_ttl_s)
        if stale:
            log.info("removed temp dirs of killed runs", dirs=[str(p) for p in stale])
    if not opts.dry_run and opts.media:  # chat and clips need no object storage
        store = ObjectStore(settings)
        await asyncio.to_thread(store.ensure_bucket)
    gql = GqlChatReplay.from_settings(twitch)
    stop = stop_event_on_signals()
    failed = 0
    try:
        async with httpx.AsyncClient(timeout=bs.http_timeout_s, follow_redirects=True) as http:
            backfiller = Backfiller(
                sessions=sessions,
                store=store,
                vod_source=StreamlinkVodSource(),
                gql=gql,
                http=http,
                settings=bs,
                owner=settings.worker_id,
                helix=helix,
                stop=stop,
                emotes=HttpEmoteSets(http),
            )
            if args.cmd == "calibrate":
                return await _calibrate(args, settings, backfiller)
            for ref in args.refs:
                if stop.is_set():
                    break
                try:
                    outcome = await backfiller.vod(ref, opts)
                except UsageError as exc:
                    error(f"[error] {ref}: {exc}")
                    failed += 1
                    continue
                except GqlBroken:
                    log.exception("GQL changed or refuses us: stopping", ref=ref)
                    return 1
                except (
                    DownloadError,
                    MediaError,
                    LeaseLost,
                    VodError,
                    GqlError,
                    HelixError,
                    httpx.HTTPError,
                    ValueError,
                ) as exc:
                    log.exception("vod failed, a re-run resumes", ref=ref)
                    outcome = VodOutcome(ref, "failed", f"{type(exc).__name__}: {exc}"[:300])
                print_outcome(outcome)
                failed += not outcome.ok
    finally:
        await gql.aclose()
        if helix is not None:
            await helix.aclose()
    return 1 if failed else 0


def main() -> None:
    # VOD titles are full of emoji: a cp1252 console or pipe must not crash the report.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="backslashreplace")
    # Windows Ctrl+C cancels the run: the chunk in progress is not committed, the lease released.
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(run(lambda: amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
