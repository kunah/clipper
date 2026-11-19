"""clipper_twitch.vod: streamlink-backed VOD playlist resolution, failure classification, fake."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from clipper_twitch.vod import (
    FakeVodSource,
    StreamlinkVodSource,
    UsherResponse,
    VodError,
    VodGone,
    VodRenditionMissing,
    VodRestricted,
    classify_failure,
    usher_error_codes,
)

USHER = "https://usher.ttvnw.net/vod/v2/2932113307.m3u8?nauthsig=x&nauth=y"
CDN = "https://d1m7jfoe9zdc1j.cloudfront.net/abc_alpha_42_1794983267"
RESTRICTED_BODY = (
    '[{"url":"https://usher.ttvnw.net/vod/v2/1.m3u8","error":"Manifest is restricted",'
    '"type":"error","error_code":"vod_manifest_restricted"}]'
)


@dataclass
class FakeResponse:
    url: str
    status_code: int
    text: str = ""


@dataclass
class FakeStream:
    url: str


@dataclass
class FakeHttp:
    hooks: dict = field(default_factory=lambda: {"response": []})


class FakeStreamlink:
    """Stands in for `streamlink.Streamlink`: replays HTTP responses through the session's
    response hooks (as requests does), then returns `streams` or raises `error`."""

    def __init__(self, responses=(), streams=None, error: Exception | None = None) -> None:
        self.http = FakeHttp()
        self.responses = list(responses)
        self.result = streams or {}
        self.error = error
        self.urls: list[str] = []

    def streams(self, url: str):
        self.urls.append(url)
        for resp in self.responses:
            for hook in self.http.hooks["response"]:
                hook(resp)
        if self.error:
            raise self.error
        return self.result


def source(session: FakeStreamlink) -> StreamlinkVodSource:
    return StreamlinkVodSource(session_factory=lambda: session)


STREAMS = {
    "audio_only": FakeStream(f"{CDN}/audio_only/index-dvr.m3u8"),
    "1080p60": FakeStream(f"{CDN}/chunked/index-dvr.m3u8"),
    "160p30": FakeStream(f"{CDN}/160p30/index-dvr.m3u8"),
    "best": FakeStream(f"{CDN}/chunked/index-dvr.m3u8"),
    "worst": FakeStream(f"{CDN}/160p30/index-dvr.m3u8"),
}


async def test_renditions_and_playlist_url():
    session = FakeStreamlink([FakeResponse(USHER, 200)], STREAMS)
    vods = source(session)
    assert await vods.playlist_url("2932113307") == f"{CDN}/audio_only/index-dvr.m3u8"
    assert await vods.playlist_url("2932113307", "best") == f"{CDN}/chunked/index-dvr.m3u8"
    renditions = await vods.renditions("2932113307")
    assert sorted(renditions) == ["1080p60", "160p30", "audio_only", "best", "worst"]
    assert session.urls[0] == "https://www.twitch.tv/videos/2932113307"


async def test_missing_rendition_lists_what_exists():
    vods = source(FakeStreamlink([FakeResponse(USHER, 200)], {"audio_only": STREAMS["audio_only"]}))
    with pytest.raises(VodRenditionMissing) as err:
        await vods.playlist_url("2932113307", "720p60")
    assert err.value.available == ["audio_only"]


async def test_sub_only_vod_is_restricted():
    session = FakeStreamlink([FakeResponse(USHER, 403, RESTRICTED_BODY)])
    with pytest.raises(VodRestricted, match="vod_manifest_restricted"):
        await source(session).playlist_url("2932113307")


async def test_no_token_means_gone():
    # streamlink returns no streams without asking usher when the access token is empty
    with pytest.raises(VodGone):
        await source(FakeStreamlink([FakeResponse("https://gql.twitch.tv/gql", 200)])).renditions(
            "1"
        )


async def test_streamlink_errors_become_vod_errors():
    session = FakeStreamlink(error=RuntimeError("Unable to open URL: gql"))
    with pytest.raises(VodError, match="Unable to open URL") as err:
        await source(session).renditions("2932113307")
    assert type(err.value) is VodError


@pytest.mark.parametrize(
    ("usher", "kind"),
    [
        (FakeResponse(USHER, 403, RESTRICTED_BODY), VodRestricted),
        (FakeResponse(USHER, 404, '[{"error_code":"vod_manifest_not_found"}]'), VodGone),
        (FakeResponse(USHER, 200), VodError),  # usher fine: the error is something else
    ],
)
async def test_streamlink_errors_after_an_usher_answer_are_classified(usher, kind):
    session = FakeStreamlink([usher], error=RuntimeError("Unable to open URL"))
    with pytest.raises(VodError) as err:
        await source(session).renditions("2932113307")
    assert type(err.value) is kind


async def test_video_ids_must_be_numeric():
    with pytest.raises(ValueError):
        await source(FakeStreamlink()).renditions("https://www.twitch.tv/videos/1")


@pytest.mark.parametrize(
    ("responses", "kind"),
    [
        ([], VodGone),
        ([UsherResponse(403, RESTRICTED_BODY)], VodRestricted),
        ([UsherResponse(403, '[{"error_code":"unauthorized_entitlements"}]')], VodRestricted),
        ([UsherResponse(403, "<html>forbidden</html>")], VodRestricted),
        ([UsherResponse(404, '[{"error_code":"vod_manifest_not_found"}]')], VodGone),
        ([UsherResponse(500, "oops")], VodError),
        ([UsherResponse(200, "")], VodError),  # usher fine, but no rendition playlist readable
        ([UsherResponse(500, "oops"), UsherResponse(403, RESTRICTED_BODY)], VodRestricted),
    ],
)
def test_classify_failure(responses, kind):
    assert type(classify_failure("7", responses)) is kind


def test_usher_error_codes():
    assert usher_error_codes(RESTRICTED_BODY) == {"vod_manifest_restricted"}
    assert usher_error_codes('{"error_code": "x"}') == {"x"}
    assert usher_error_codes("not json") == set() and usher_error_codes("[1, 2]") == set()


async def test_fake_vod_source():
    fake = FakeVodSource({"1": {"audio_only": "https://x/a.m3u8"}, "2": VodRestricted("sub-only")})
    assert await fake.playlist_url("1") == "https://x/a.m3u8"
    with pytest.raises(VodRestricted):
        await fake.playlist_url("2")
    with pytest.raises(VodGone):
        await fake.renditions("3")
    with pytest.raises(VodRenditionMissing):
        await fake.playlist_url("1", "best")
    assert fake.calls == [("1", "audio_only"), ("2", "audio_only"), ("3", None), ("1", "best")]


def test_real_streamlink_session_has_requests_hooks():
    """The hook mechanism StreamlinkVodSource relies on (no network)."""
    streamlink = pytest.importorskip("streamlink")
    session = streamlink.Streamlink()
    assert isinstance(session.http.hooks.get("response"), list)
