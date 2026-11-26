"""Smoke test against the real Twitch: one public archive VOD, anonymous and read-only.

    CLIPPER_TWITCH_LIVE_TESTS=1 uv run pytest -m twitch_live libs/twitch/tests/test_live_smoke.py

It checks the facts DESIGN §3/§17 rely on (PDT in VOD playlists, container, muted names,
TOTAL-SECS, chat offsets vs createdAt) and writes them to `report.json`, next to sanitized
samples (`chat_page_*.json` with user names replaced, truncated playlists) for offline fixtures.

Env: CLIPPER_TWITCH_SMOKE_VIDEO (a public archive id; default: the newest recorded archive of
CLIPPER_TWITCH_SMOKE_CHANNEL, default xqc); CLIPPER_TWITCH_SMOKE_OUT (output dir; default:
pytest's tmp dir). The Helix part needs CLIPPER_TWITCH_CLIENT_ID/_SECRET and skips without.
Needs `ffprobe` on PATH for the media part (skipped without) and streamlink (the `vod` extra).
Budget: <= 2 audio + 1 video segment (+ init sections), <= 4 chat pages; requests are
sequential and >= 0.5 s apart; media goes to a temp dir that is deleted afterwards.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from clipper_twitch.gql import GqlChatReplay, ReplayUnavailable
from clipper_twitch.helix import HelixClient
from clipper_twitch.hls import VodPlaylist, parse_vod_playlist
from clipper_twitch.settings import TwitchSettings
from clipper_twitch.vod import StreamlinkVodSource, VodGone

pytestmark = pytest.mark.twitch_live

WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # Twitch's web player (as in streamlink)
GQL_URL = "https://gql.twitch.tv/gql"
MIN_GAP_S = 0.5
MAX_AUDIO_SEGMENTS = 2
MAX_VIDEO_SEGMENTS = 1
PATH_RE = re.compile(r"/(?P<hash>[0-9a-f]{20})_(?P<login>\w+?)_(?P<stream_id>\d+)_(?P<epoch>\d+)/")
MENTION_RE = re.compile(r"@(\w+)")


class Polite:
    """Sequential plain HTTP with >= MIN_GAP_S between requests (playlists, segments, GQL)."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http
        self.last = 0.0
        self.bytes = 0

    async def _pace(self) -> None:
        wait = self.last + MIN_GAP_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self.last = time.monotonic()

    async def get(self, url: str) -> httpx.Response:
        await self._pace()
        resp = await self.http.get(url)
        resp.raise_for_status()
        self.bytes += len(resp.content)
        return resp

    async def gql(self, query: str) -> dict[str, Any]:
        await self._pace()
        resp = await self.http.post(
            GQL_URL, json={"query": query}, headers={"Client-ID": WEB_CLIENT_ID}
        )
        resp.raise_for_status()
        return resp.json()


async def _video_meta(polite: Polite, video_id: str | None, channel: str) -> dict[str, Any]:
    fields = "id createdAt publishedAt lengthSeconds status broadcastType owner { id login }"
    if video_id:
        body = await polite.gql(f'query {{ video(id: "{video_id}") {{ {fields} }} }}')
        return body["data"]["video"]
    body = await polite.gql(
        f'query {{ user(login: "{channel}") {{ videos(first: 10, type: ARCHIVE, sort: TIME) '
        f"{{ edges {{ node {{ {fields} }} }} }} }} }}"
    )
    for edge in body["data"]["user"]["videos"]["edges"]:
        node = edge["node"]
        if node["status"] == "RECORDED" and node["lengthSeconds"] > 600:
            return node
    pytest.skip(f"{channel} has no finished archive")


def _playlist_facts(text: str, url: str, pl: VodPlaylist) -> dict[str, Any]:
    tags = sorted({line.split(":")[0] for line in text.splitlines() if line.startswith("#")})
    header = [
        line
        for line in text.splitlines()
        if line.startswith("#")
        and not line.startswith(("#EXTINF", "#EXT-X-PROGRAM-DATE-TIME", "#EXT-X-DISCONTINUITY"))
    ]
    uris = [s.uri.rsplit("/", 1)[-1] for s in pl.segments]
    durations = [s.duration for s in pl.segments]
    muted = [u for u, s in zip(uris, pl.segments, strict=True) if s.muted]
    m = PATH_RE.search(url)
    pdt = [s for s in pl.segments if s.program_date_time is not None]
    pdt_facts = None
    if pdt:
        t0 = pdt[0].program_date_time - timedelta(seconds=pdt[0].offset_s)
        dev = [((s.program_date_time - t0).total_seconds() - s.offset_s, s.index) for s in pdt]
        jumps = [(i, round(d, 3)) for d, i in dev if abs(d) > 0.5]
        pdt_facts = {
            "count": len(pdt),
            "first": pdt[0].program_date_time.isoformat(),
            "last": pdt[-1].program_date_time.isoformat(),
            "t0_from_pdt": t0.isoformat(),
            # PDT - (T0 + offset): 0 everywhere = PDT is just T0 + offset; jumps = wall clock gaps
            "deviation_min_s": round(min(d for d, _ in dev), 3),
            "deviation_max_s": round(max(d for d, _ in dev), 3),
            "jumps": jumps[:5],
            "jump_count": len(jumps),
        }
    return {
        "pdt": pdt_facts,
        "tags": tags,
        "header": header[:15],
        "has_pdt": "#EXT-X-PROGRAM-DATE-TIME" in text,
        "segments": len(pl.segments),
        "extensions": sorted({u.rsplit(".", 1)[-1] for u in uris}),
        "init_uri": pl.segments[0].init_uri.rsplit("/", 1)[-1] if pl.segments[0].init_uri else None,
        "first_uris": uris[:3],
        "extinf_values": sorted(set(durations[:-1]))[:5],
        "last_extinf": durations[-1],
        "sum_extinf": round(pl.duration_s, 3),
        "total_secs": pl.total_secs,
        "total_minus_sum": None
        if pl.total_secs is None
        else round(pl.total_secs - pl.duration_s, 3),
        "muted_segments": len(muted),
        "muted_sample": muted[:3],
        "unmuted_names": sorted({u for u in uris if "unmuted" in u})[:3],
        "discontinuities": sum(s.discontinuity for s in pl.segments),
        "ended": pl.ended,
        "target_duration": pl.target_duration,
        "path": None if not m else {"stream_id": m["stream_id"], "epoch": int(m["epoch"])},
        "bytes": len(text.encode()),
    }


def _truncated_playlist(text: str, keep: int = 4, extra: list[str] | None = None) -> str:
    """Header + the first `keep` segments (+ named extra segments, e.g. a muted one) + ENDLIST."""
    out: list[str] = []
    seen = 0
    pending: list[str] = []
    wanted = set(extra or [])
    for line in text.splitlines():
        if line.startswith("#EXTINF") or line.startswith("#EXT-X-PROGRAM-DATE-TIME"):
            pending.append(line)
        elif line.startswith("#EXT-X-ENDLIST"):
            continue
        elif line.startswith("#") and seen == 0:
            out.append(line)
        elif line.startswith("#"):
            pending.append(line)
        elif line:
            if seen < keep or line in wanted:
                out += [*pending, line]
            seen += 1
            pending = []
    if "#EXT-X-ENDLIST" in text:
        out.append("#EXT-X-ENDLIST")
    return "\n".join(out) + "\n"


def _ffprobe(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    info = json.loads(proc.stdout)
    fmt = info.get("format") or {}
    return {
        "format": fmt.get("format_name"),
        "duration": float(fmt.get("duration") or 0),
        "start_time": float(fmt.get("start_time") or 0),
        "bytes": int(fmt.get("size") or 0),
        "streams": [
            {
                k: s.get(k)
                for k in ("codec_type", "codec_name", "sample_rate", "channels", "width", "height")
                if s.get(k) is not None
            }
            for s in info.get("streams") or []
        ],
    }


async def _probe_segments(polite: Polite, pl: VodPlaylist, indexes: list[int], tmp: Path) -> list:
    results = []
    init = b""
    if pl.segments[0].init_uri:
        init = (await polite.get(pl.segments[0].init_uri)).content
    for i in indexes:
        seg = pl.segments[i]
        data = (await polite.get(seg.uri)).content
        suffix = Path(seg.uri.split("?")[0]).suffix or ".bin"
        path = tmp / f"seg_{i}{suffix}"
        await asyncio.to_thread(path.write_bytes, init + data)
        info = await asyncio.to_thread(_ffprobe, path)
        results.append({"index": i, "muted": seg.muted, "extinf": seg.duration, **info})
    return results


def _sanitize_pages(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace commenter ids/logins/display names (and @mentions of them) by placeholders."""
    pages = copy.deepcopy(pages)
    names: dict[str, str] = {}
    ids: dict[str, str] = {}

    def alias(login: str) -> str:
        return names.setdefault(login.lower(), f"user{len(names) + 1}")

    for page in pages:
        for edge in page["data"]["video"]["comments"]["edges"]:
            commenter = edge["node"].get("commenter")
            if commenter:
                name = alias(commenter["login"])
                names.setdefault((commenter.get("displayName") or "").lower(), name)
                commenter["login"] = name
                commenter["displayName"] = name.capitalize()
                commenter["id"] = ids.setdefault(commenter["id"], str(900000 + len(ids)))
    # @mentions first (they may name non-commenters), then commenters' names anywhere in the
    # text (sub notices quote them)
    fragments = [
        frag
        for page in pages
        for edge in page["data"]["video"]["comments"]["edges"]
        for frag in (edge["node"].get("message") or {}).get("fragments") or []
        if not frag.get("emote")
    ]
    for frag in fragments:
        frag["text"] = MENTION_RE.sub(lambda m: "@" + alias(m[1]), frag["text"])
    known = sorted((n for n in names if n), key=len, reverse=True)
    if known:
        name_re = re.compile(r"\b(" + "|".join(map(re.escape, known)) + r")\b", re.I)
        for frag in fragments:
            frag["text"] = name_re.sub(lambda m: names[m[1].lower()], frag["text"])
    return pages


def _chat_facts(
    pages: list[dict[str, Any]], video_created: datetime, t0_hints: dict[str, datetime]
) -> dict[str, Any]:
    nodes = [e["node"] for p in pages for e in p["data"]["video"]["comments"]["edges"]]
    raw_offsets = [n["contentOffsetSeconds"] for n in nodes]
    created = [datetime.fromisoformat(n["createdAt"]) for n in nodes]
    x = [c - timedelta(seconds=o) for c, o in zip(created, raw_offsets, strict=True)]
    xs = [(v - video_created).total_seconds() for v in x]
    return {
        "comments": len(nodes),
        "offset_types": sorted({type(o).__name__ for o in raw_offsets}),
        "offset_range": [min(raw_offsets), max(raw_offsets)] if raw_offsets else None,
        "created_at_samples": [n["createdAt"] for n in nodes[:3]],
        "created_at_suffixes": sorted({n["createdAt"][-1] for n in nodes}),
        "created_at_fraction_digits": sorted(
            {len(n["createdAt"].rstrip("Z").partition(".")[2]) for n in nodes}
        ),
        "x_minus_video_created_s": None
        if not xs
        else {
            "min": round(min(xs), 3),
            "median": round(statistics.median(xs), 3),
            "max": round(max(xs), 3),
            "spread": round(max(xs) - min(xs), 3),
            "whole_hour_outliers": sum(abs(v) > 1800 for v in xs),
        },
        # x = createdAt - contentOffsetSeconds: Twitch's own T0 (+ the offset's rounding)
        "x_minus_hints_s": {
            k: {
                "min": round(min(xs) - (v - video_created).total_seconds(), 3),
                "median": round(statistics.median(xs) - (v - video_created).total_seconds(), 3),
                "max": round(max(xs) - (v - video_created).total_seconds(), 3),
            }
            for k, v in t0_hints.items()
        }
        if xs
        else None,
        "without_commenter": sum(n.get("commenter") is None for n in nodes),
        "emote_fragments": sum(
            1
            for n in nodes
            for f in (n.get("message") or {}).get("fragments") or []
            if f.get("emote")
        ),
    }


@pytest.fixture
def out_dir(tmp_path) -> Path:
    path = Path(os.environ.get("CLIPPER_TWITCH_SMOKE_OUT") or tmp_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write(path: Path, data: Any) -> None:
    text = data if isinstance(data, str) else json.dumps(data, indent=1, ensure_ascii=False)
    path.write_bytes(text.encode("utf-8"))


async def test_vod_media_and_chat(out_dir):
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe is not on PATH (needed to probe the sample segments)")
    channel = os.environ.get("CLIPPER_TWITCH_SMOKE_CHANNEL", "xqc")
    report: dict[str, Any] = {"run_at": datetime.now(UTC).isoformat()}
    raw_pages: list[dict[str, Any]] = []

    async def keep_chat_body(resp: httpx.Response) -> None:
        if resp.request.url.host == "gql.twitch.tv" and b"VideoComments" in resp.request.content:
            await resp.aread()
            if resp.status_code == 200:
                raw_pages.append(resp.json())

    async with httpx.AsyncClient(timeout=30, event_hooks={"response": [keep_chat_body]}) as http:
        polite = Polite(http)
        meta = await _video_meta(polite, os.environ.get("CLIPPER_TWITCH_SMOKE_VIDEO"), channel)
        video_id = meta["id"]
        video_created = datetime.fromisoformat(meta["createdAt"])
        report["video"] = meta

        # --- playlists via streamlink (one resolution: all renditions)
        renditions = await StreamlinkVodSource().renditions(video_id)
        report["renditions"] = sorted(renditions)
        assert "audio_only" in renditions
        source_name = next(
            (n for n, u in renditions.items() if n != "best" and u == renditions.get("best")),
            "best",
        )
        report["source_rendition"] = source_name
        playlists: dict[str, VodPlaylist] = {}
        for name in ("audio_only", source_name):
            url = renditions[name]
            text = (await polite.get(url)).text
            pl = parse_vod_playlist(text, url)
            playlists[name] = pl
            facts = _playlist_facts(text, url, pl)
            report[f"playlist_{name}"] = facts
            await asyncio.to_thread(
                _write,
                out_dir / f"playlist_{name}.m3u8",
                _truncated_playlist(text, extra=facts["muted_sample"][:1]),
            )

        # --- media: tiny sample, probed and deleted
        audio = playlists["audio_only"]
        muted = [s.index - audio.media_sequence for s in audio.segments if s.muted]
        audio_idx = [0, muted[0] if muted else 1][:MAX_AUDIO_SEGMENTS]
        with tempfile.TemporaryDirectory(prefix="clipper-smoke-") as tmp:
            report["probe_audio"] = await _probe_segments(polite, audio, audio_idx, Path(tmp))
            report["probe_video"] = await _probe_segments(
                polite, playlists[source_name], [0][:MAX_VIDEO_SEGMENTS], Path(tmp)
            )
        report["downloaded_bytes"] = polite.bytes

        # --- chat replay: 3 pages from the start, 1 from the middle
        await asyncio.sleep(MIN_GAP_S)
        replay = GqlChatReplay.from_settings(TwitchSettings(), http=http)
        first = [await replay.page(video_id, offset_s=0)]
        while len(first) < 3 and first[-1].has_next_page:
            first.append(await replay.page(video_id, cursor=first[-1].cursor))
        start_raw = list(raw_pages)
        mid_offset = int(meta["lengthSeconds"]) // 2
        mid = await replay.page(video_id, offset_s=mid_offset)
        mid_raw = raw_pages[len(start_raw) :]
        report["chat_requests"] = replay.requests
        report["chat_pages"] = [
            {"comments": len(p.comments), "next": p.has_next_page, "prev": p.has_previous_page}
            for p in [*first, mid]
        ]
        hints = {"video_created_at": video_created}
        pdt = report["playlist_audio_only"]["pdt"]
        if pdt:
            hints["pdt_t0"] = datetime.fromisoformat(pdt["t0_from_pdt"])
        path = report["playlist_audio_only"]["path"]
        if path:
            hints["path_epoch"] = datetime.fromtimestamp(path["epoch"], UTC)
        tdtg = next(
            (h for h in report["playlist_audio_only"]["header"] if h.startswith("#ID3-EQUIV-TDTG")),
            None,
        )
        if tdtg:
            report["tdtg_raw"] = tdtg
        report["chat_start"] = _chat_facts(start_raw, video_created, hints)
        at_mid = next(
            (
                seg
                for seg in audio.segments
                if seg.offset_s <= mid_offset < seg.end_s and seg.program_date_time
            ),
            None,
        )
        if at_mid:  # T0 implied by the PDT of the segment at the mid offset (drift check)
            hints["pdt_t0_at_mid"] = at_mid.program_date_time - timedelta(seconds=at_mid.offset_s)
        report["chat_mid"] = {"offset_s": mid_offset, **_chat_facts(mid_raw, video_created, hints)}
        report["chat_first_page_offsets"] = [c.content_offset_s for c in first[0].comments][:10]
        report["chat_mid_first_offsets"] = [c.content_offset_s for c in mid.comments][:5]
        report["creator_id"] = first[0].creator_id

    sanitized = _sanitize_pages([*start_raw[:1], *mid_raw[:1]])
    for i, page in enumerate(sanitized):
        await asyncio.to_thread(_write, out_dir / f"chat_page_{i}.json", page)
    await asyncio.to_thread(_write, out_dir / "report.json", report)

    assert first[0].comments, "no comments on the first page"
    assert report["downloaded_bytes"] < 50_000_000
    assert any(s["codec_name"] == "aac" for p in report["probe_audio"] for s in p["streams"])


async def test_missing_vod_and_replay_are_classified():
    with pytest.raises(VodGone):
        await StreamlinkVodSource().renditions("1")
    await asyncio.sleep(MIN_GAP_S)
    replay = GqlChatReplay.from_settings(TwitchSettings())
    try:
        with pytest.raises(ReplayUnavailable):
            await replay.page("1", offset_s=0)
    finally:
        await replay.aclose()


async def test_helix_videos_and_clips(out_dir):
    settings = TwitchSettings()
    if not settings.has_credentials:
        pytest.skip("Helix needs CLIPPER_TWITCH_CLIENT_ID / CLIPPER_TWITCH_CLIENT_SECRET")
    channel = os.environ.get("CLIPPER_TWITCH_SMOKE_CHANNEL", "xqc")
    helix = HelixClient.from_settings(settings)
    try:
        (user,) = await helix.get_users(logins=[channel])
        videos = await helix.get_videos(user.id, limit=3)
        assert videos.items and all(v.type == "archive" for v in videos.items)
        video = next((v for v in videos.items if v.stream_id), videos.items[0])
        clips = await helix.get_clips(
            user.id,
            started_at=video.created_at,
            ended_at=video.created_at + timedelta(seconds=video.duration_s),
            limit=20,
        )
        report = {
            "video": {
                "id": video.id,
                "stream_id": video.stream_id,
                "created_at": video.created_at.isoformat(),
                "duration_s": video.duration_s,
                "muted_segments": video.muted_segments,
            },
            "clips": len(clips.items),
            "clips_of_video": sum(c.video_id == video.id for c in clips.items),
            "clips_without_offset": sum(c.vod_offset is None for c in clips.items),
            "views_sorted_desc": [c.view_count for c in clips.items]
            == sorted((c.view_count for c in clips.items), reverse=True),
        }
        await asyncio.to_thread(_write, out_dir / "report_helix.json", report)
    finally:
        await helix.aclose()
