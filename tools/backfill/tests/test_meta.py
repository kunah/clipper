"""Pure pieces around a VOD run: metadata from GQL/Helix, which broadcast it is, the CLI's dates,
and the cleanup of temp dirs left by killed runs."""

import os
import time
from datetime import UTC, datetime, timedelta

from clipper_backfill.backfill import AUDIO, _broadcast, _Media, vod_meta
from clipper_backfill.cli import _day
from clipper_backfill.pipeline import TEMP_PREFIX, remove_stale_workdirs
from clipper_twitch.gql import VideoInfo
from clipper_twitch.helix import HelixVideo

CREATED = datetime(2026, 11, 18, 6, 27, 53, tzinfo=UTC)
PATH = "https://cdn.test/8173ac3b86530f54c2d4_xqc_{sid}_1794983267"


def info(**changes) -> VideoInfo:
    values = {
        "id": "2932113307",
        "title": "gql title",
        "created_at": CREATED,
        "published_at": CREATED,
        "length_s": 83735,
        "status": "RECORDED",
        "broadcast_type": "ARCHIVE",
        "owner_id": "71092938",
        "owner_login": "xQc",
        "owner_display_name": "xQc",
        "game": "Just Chatting",
        "seek_previews_url": PATH.format(sid="321206426716") + "/storyboards/x-info.json",
    }
    return VideoInfo(**{**values, **changes})


def helix(**changes) -> HelixVideo:
    values = {
        "id": "2932113307",
        "stream_id": "321206426716",
        "user_id": "71092938",
        "user_login": "xqc",
        "user_name": "xQc",
        "title": "helix title",
        "description": "",
        "created_at": CREATED - timedelta(seconds=1),
        "published_at": CREATED,
        "url": "",
        "thumbnail_url": "",
        "viewable": "public",
        "view_count": 1,
        "language": "en",
        "type": "archive",
        "duration_s": 83735,
        "muted_segments": None,
    }
    return HelixVideo(**{**values, **changes})


def media(sid: str | None = "321206426716") -> _Media:
    url = (PATH.format(sid=sid) if sid else "https://usher.test/x") + "/audio_only/index-dvr.m3u8"
    return _Media(renditions={AUDIO: url}, rendition=AUDIO)


def test_helix_wins_for_created_at_and_stream_id_and_gql_fills_the_rest():
    meta = vod_meta("2932113307", info(), helix())
    assert (meta.created_at, meta.created_source) == (CREATED - timedelta(seconds=1), "helix")
    assert meta.gql_created_at == CREATED and meta.login == "xqc" and meta.archive
    assert (meta.title, meta.category, meta.length_s) == ("gql title", "Just Chatting", 83735.0)
    assert meta.stream_ids == {"helix": "321206426716", "preview_path": "321206426716"}
    b = _broadcast(meta, media())
    assert b.stream_id == "321206426716" and b.flags == {}
    assert b.stream_ids == {
        "helix": "321206426716",
        "preview_path": "321206426716",
        "playlist_path": "321206426716",
    }
    assert b.epoch == datetime(2026, 11, 18, 6, 27, 47, tzinfo=UTC)


def test_helix_only():
    meta = vod_meta("1", None, helix(type="highlight", stream_id=None))
    assert meta.created_source == "helix" and not meta.archive and not meta.recording
    assert meta.gql_created_at is None and meta.stream_ids == {} and meta.category is None


def test_gql_only_still_live():
    meta = vod_meta("1", info(status="RECORDING"), None)
    assert meta.recording and meta.created_source == "gql_created_at"


def test_disagreeing_sources_are_flagged_and_helix_is_taken():
    meta = vod_meta("1", info(), helix(stream_id="999"))
    b = _broadcast(meta, media("888"))
    assert b.stream_id == "999"
    assert b.flags == {
        "stream_id_mismatch": {
            "helix": "999",
            "preview_path": "321206426716",
            "playlist_path": "888",
        }
    }


def test_nothing_names_the_broadcast():
    meta = vod_meta("42", info(seek_previews_url=None), None)
    b = _broadcast(meta, media(None))
    assert (b.stream_id, b.flags, b.epoch) == ("video:42", {"stream_id_unknown": True}, None)


def test_since_dates_are_utc():
    assert _day("2026-11-01") == datetime(2026, 11, 1, tzinfo=UTC)
    aware = _day("2026-11-01T02:00:00+02:00")
    assert aware == datetime(2026, 11, 1, tzinfo=UTC) and aware.tzinfo == UTC
    assert _day("2026-11-01T00:00:00+00:00").utcoffset() == UTC.utcoffset(None)


def test_stale_temp_dirs_of_killed_runs_are_removed(tmp_path):
    old, fresh = tmp_path / f"{TEMP_PREFIX}old", tmp_path / f"{TEMP_PREFIX}fresh"
    other = tmp_path / "something-else"
    for d in (old, fresh, other):
        d.mkdir()
        (d / "000001.ts").write_bytes(b"x")
    hour_ago = time.time() - 3600
    for d in (old, other):
        os.utime(d, (hour_ago, hour_ago))
    assert remove_stale_workdirs(tmp_path, older_than_s=120) == [old]
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{TEMP_PREFIX}fresh", "something-else"]
