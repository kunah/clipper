"""parse_vod_playlist: VOD (archive) media playlists timed by VOD offset (sum of EXTINF)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clipper_twitch.hls import parse_vod_playlist

BASE = "https://d1m7jfoe9zdc1j.cloudfront.net/abc_alpha_42_1795000000/audio_only/index-dvr.m3u8"

# Shape of a Twitch VOD playlist (TwitchDownloader fixtures, research notes 2026-11-19):
# 10 s segments named by index, "-muted" for silenced ones, no EXT-X-PROGRAM-DATE-TIME.
TS_VOD = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#ID3-EQUIV-TDTG:2026-11-01T17:37:06
#EXT-X-PLAYLIST-TYPE:EVENT
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-TWITCH-ELAPSED-SECS:0.000
#EXT-X-TWITCH-TOTAL-SECS:44.500
#EXTINF:10.000,
0.ts
#EXTINF:10.000,
1-muted.ts
#EXTINF:10.000,
2-muted.ts
#EXT-X-DISCONTINUITY
#EXTINF:10.000,
3.ts
#EXTINF:2.500,
4-unmuted.ts
#EXT-X-ENDLIST
"""

FMP4_VOD = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-MAP:URI="init-0.mp4"
#EXTINF:10.000,
0.mp4
#EXTINF:10.000,
1-muted.mp4
#EXT-X-DISCONTINUITY
#EXT-X-MAP:URI="init-2.mp4"
#EXTINF:4.000,
2.mp4
"""


def test_ts_vod_offsets_muted_and_discontinuity():
    pl = parse_vod_playlist(TS_VOD, BASE)
    assert pl.target_duration == 10.0 and pl.total_secs == 44.5 and pl.ended
    assert pl.media_sequence == 0 and [s.index for s in pl.segments] == [0, 1, 2, 3, 4]
    assert [s.offset_s for s in pl.segments] == [0.0, 10.0, 20.0, 30.0, 40.0]
    assert [s.duration for s in pl.segments] == [10.0] * 4 + [2.5]
    assert [s.muted for s in pl.segments] == [False, True, True, False, False]  # "-unmuted"
    assert [s.discontinuity for s in pl.segments] == [False, False, False, True, False]
    assert pl.duration_s == 42.5 and pl.segments[-1].end_s == 42.5
    first = pl.segments[0]
    assert first.uri == BASE.replace("index-dvr.m3u8", "0.ts")
    assert first.init_uri is None and first.program_date_time is None


def test_fmp4_vod_carries_the_init_section_in_effect():
    pl = parse_vod_playlist(FMP4_VOD, BASE)
    init0, init2 = (BASE.replace("index-dvr.m3u8", f"init-{i}.mp4") for i in (0, 2))
    assert [s.init_uri for s in pl.segments] == [init0, init0, init2]
    assert [s.muted for s in pl.segments] == [False, True, False]
    assert pl.segments[2].discontinuity and pl.segments[2].offset_s == 20.0
    assert pl.total_secs is None and not pl.ended  # archive of a stream that is still live
    assert pl.duration_s == 24.0


def test_program_date_time_is_kept_where_present():
    t = datetime(2026, 11, 1, 18, 0, tzinfo=UTC)
    text = f"""#EXTM3U
#EXT-X-TARGETDURATION:10
#EXT-X-PROGRAM-DATE-TIME:{t.isoformat()}
#EXTINF:10.000,
0.ts
#EXTINF:10.000,
1.ts
#EXT-X-PROGRAM-DATE-TIME:{(t + timedelta(seconds=25)).isoformat()}
#EXTINF:10.000,
2.ts
"""
    pl = parse_vod_playlist(text, BASE)
    assert [s.program_date_time for s in pl.segments] == [t, None, t + timedelta(seconds=25)]
    assert [s.offset_s for s in pl.segments] == [0.0, 10.0, 20.0]  # offsets ignore PDT


def test_query_strings_and_absolute_uris():
    text = "#EXTM3U\n#EXTINF:10.0,\nhttps://cdn.example/x/7-muted.ts?token=abc\n"
    (seg,) = parse_vod_playlist(text, BASE).segments
    assert seg.uri == "https://cdn.example/x/7-muted.ts?token=abc" and seg.muted


def test_empty_and_invalid_playlists():
    pl = parse_vod_playlist("#EXTM3U\n#EXT-X-ENDLIST\n")
    assert pl.segments == [] and pl.duration_s == 0.0 and pl.ended
    with pytest.raises(ValueError, match="not an HLS playlist"):
        parse_vod_playlist("<html>403</html>")
    with pytest.raises(ValueError, match="without EXTINF"):
        parse_vod_playlist("#EXTM3U\n0.ts\n")


def test_segments_are_numbered_from_the_media_sequence():
    text = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:7\n#EXTINF:10.0,\n7.ts\n#EXTINF:10.0,\n8.ts\n"
    pl = parse_vod_playlist(text, BASE)
    assert pl.media_sequence == 7 and [s.index for s in pl.segments] == [7, 8]
    assert [s.offset_s for s in pl.segments] == [0.0, 10.0]  # offsets still start at 0


def test_byte_range_playlists_are_refused():
    text = "#EXTM3U\n#EXTINF:10.0,\n#EXT-X-BYTERANGE:1000@0\nall.ts\n"
    with pytest.raises(ValueError, match="EXT-X-BYTERANGE"):
        parse_vod_playlist(text, BASE)


# Real playlists (xqc archive, smoke test 2026-11-19), cut to segments 0-3 plus the first muted
# one (18): segments 4-17 are left out, so the last entry's offset is 40 s, its PDT says 180 s.
FIXTURES = Path(__file__).parent / "fixtures"
REAL_BASE = "https://d1m7jfoe9zdc1j.cloudfront.net/0123456789abcdef0123_alpha_42_1794983267/"


def test_real_audio_only_playlist_has_pdt_equal_to_t0_plus_offset():
    text = (FIXTURES / "vod_audio_only.m3u8").read_text()
    pl = parse_vod_playlist(text, REAL_BASE + "audio_only/index-dvr.m3u8")
    assert pl.ended and pl.total_secs == 83735.25 and pl.target_duration == 13.0
    assert [s.uri.rsplit("/", 1)[1] for s in pl.segments] == [
        "0.ts", "1.ts", "2.ts", "3.ts", "18-muted.ts"
    ]  # fmt: skip
    assert [s.muted for s in pl.segments] == [False] * 4 + [True]
    t0 = datetime(2026, 11, 18, 6, 27, 49, 784000, tzinfo=UTC)
    assert all(s.program_date_time is not None for s in pl.segments)
    assert [s.program_date_time - t0 for s in pl.segments[:4]] == [
        timedelta(seconds=s.offset_s) for s in pl.segments[:4]
    ]
    assert pl.segments[4].program_date_time == t0 + timedelta(seconds=18 * 10)


def test_real_video_playlist_has_no_pdt():
    text = (FIXTURES / "vod_1080p60.m3u8").read_text()
    pl = parse_vod_playlist(text, REAL_BASE + "1080p60/index-dvr.m3u8")
    assert all(s.program_date_time is None for s in pl.segments)
    assert pl.segments[0].uri == REAL_BASE + "1080p60/0.ts" and pl.segments[-1].muted
    assert pl.total_secs == 83735.25 and pl.ended
