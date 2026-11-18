from datetime import UTC, datetime, timedelta

import pytest

from clipper_twitch.hls import parse_attributes, parse_media_playlist

# Shape of a real Twitch live media playlist (2026-11), URLs shortened.
LIVE = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:26499
#EXT-X-TWITCH-LIVE-SEQUENCE:26499
#EXT-X-TWITCH-ELAPSED-SECS:52966.166
#EXT-X-DATERANGE:ID="playlist-creation-1795036262",CLASS="timestamp",START-DATE="2026-11-18T21:11:02.723Z",END-ON-NEXT=YES,X-SERVER-TIME="1795036262.72"
#EXT-X-DATERANGE:ID="source-1795036233",CLASS="twitch-stream-source",START-DATE="2026-11-18T21:10:33.950Z",END-ON-NEXT=YES,X-TV-TWITCH-STREAM-SOURCE="live"
#EXT-X-PROGRAM-DATE-TIME:2026-11-18T21:10:35.950Z
#EXTINF:2.000,live
https://cdn.example/v1/segment/a.ts?dna=1
#EXT-X-PROGRAM-DATE-TIME:2026-11-18T21:10:37.950Z
#EXTINF:2.000,live
https://cdn.example/v1/segment/b.ts?dna=2
#EXT-X-PROGRAM-DATE-TIME:2026-11-18T21:10:39.950Z
#EXTINF:2.000,live
https://cdn.example/v1/segment/c.ts?dna=3
#EXT-X-TWITCH-PREFETCH:https://cdn.example/v1/segment/d.ts
#EXT-X-TWITCH-PREFETCH:https://cdn.example/v1/segment/e.ts
"""

T0 = datetime(2026, 11, 18, 21, 10, 35, 950000, tzinfo=UTC)


def test_live_playlist_segments_have_utc_times():
    pl = parse_media_playlist(LIVE)
    assert pl.target_duration == 6.0
    assert pl.media_sequence == 26499
    assert not pl.ended and pl.init_uri is None
    assert [s.sequence for s in pl.segments] == [26499, 26500, 26501]
    first = pl.segments[0]
    assert first.start == T0 and first.start.tzinfo is not None
    assert first.end == T0 + timedelta(seconds=2)
    assert first.title == "live" and not first.ad and not first.discontinuity
    assert first.uri == "https://cdn.example/v1/segment/a.ts?dna=1"
    # prefetch segments are not finished yet: they come back as normal segments later
    assert all("d.ts" not in s.uri and "e.ts" not in s.uri for s in pl.segments)


def test_stitched_ad_daterange_and_amazon_title_mark_ads():
    text = """#EXTM3U
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:10
#EXT-X-DATERANGE:ID="stitched-ad-1",CLASS="twitch-stitched-ad",START-DATE="2026-11-18T21:00:02.000Z",DURATION=4.0,X-TV-TWITCH-AD-POD-LENGTH="1"
#EXT-X-PROGRAM-DATE-TIME:2026-11-18T21:00:00.000Z
#EXTINF:2.000,live
s1.ts
#EXT-X-DISCONTINUITY
#EXT-X-PROGRAM-DATE-TIME:2026-11-18T21:00:02.000Z
#EXTINF:2.000,Amazon|123
s2.ts
#EXTINF:2.000,
s3.ts
#EXT-X-DISCONTINUITY
#EXT-X-PROGRAM-DATE-TIME:2026-11-18T21:00:06.000Z
#EXTINF:2.000,live
s4.ts
"""
    pl = parse_media_playlist(text, "https://host/v1/playlist/x.m3u8")
    assert [s.ad for s in pl.segments] == [False, True, True, False]
    assert [s.discontinuity for s in pl.segments] == [False, True, False, True]
    # s3 has no PROGRAM-DATE-TIME: continues from s2; untitled but inside the ad daterange
    assert pl.segments[2].start == datetime(2026, 11, 18, 21, 0, 4, tzinfo=UTC)
    assert pl.segments[2].title is None
    assert pl.segments[0].uri == "https://host/v1/playlist/s1.ts"


def test_open_ended_ad_daterange_covers_everything_after_it():
    text = """#EXTM3U
#EXT-X-DATERANGE:ID="stitched-ad-9",START-DATE="2026-11-18T21:00:02.000Z"
#EXT-X-PROGRAM-DATE-TIME:2026-11-18T21:00:00.000Z
#EXTINF:2.000,live
a.ts
#EXTINF:2.000,live
b.ts
"""
    assert [s.ad for s in parse_media_playlist(text).segments] == [False, True]


def test_leading_segments_without_date_are_back_filled():
    text = """#EXTM3U
#EXTINF:2.000,live
a.ts
#EXT-X-PROGRAM-DATE-TIME:2026-11-18T21:00:02.000Z
#EXTINF:2.000,live
b.ts
#EXT-X-ENDLIST
"""
    pl = parse_media_playlist(text)
    assert pl.segments[0].start == datetime(2026, 11, 18, 21, 0, 0, tzinfo=UTC)
    assert pl.ended


def test_playlist_without_any_date_is_rejected():
    with pytest.raises(ValueError, match="PROGRAM-DATE-TIME"):
        parse_media_playlist("#EXTM3U\n#EXTINF:2.000,live\na.ts\n")
    with pytest.raises(ValueError, match="not an HLS"):
        parse_media_playlist("<html>")


def test_fmp4_init_section_is_reported():
    text = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n'
    assert parse_media_playlist(text, "https://h/p/x.m3u8").init_uri == "https://h/p/init.mp4"


def test_attribute_lists():
    assert parse_attributes('ID="a,b",DURATION=30.5,END-ON-NEXT=YES') == {
        "ID": "a,b",
        "DURATION": "30.5",
        "END-ON-NEXT": "YES",
    }
