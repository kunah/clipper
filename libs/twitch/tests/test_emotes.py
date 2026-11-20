import httpx

from clipper_twitch.emotes import fetch_channel_emotes, fetch_global_emotes

RESPONSES = {
    "https://7tv.io/v3/emote-sets/global": {"emotes": [{"name": "Clap"}, {"name": "Shared"}]},
    "https://7tv.io/v3/users/twitch/42": {"emote_set": {"emotes": [{"name": "KEKW"}]}},
    "https://api.betterttv.net/3/cached/emotes/global": [{"code": "Shared"}, {"code": "catJAM"}],
    "https://api.betterttv.net/3/cached/users/twitch/42": {
        "channelEmotes": [{"code": "OMEGALUL"}],
        "sharedEmotes": [{"code": "PogU"}],
    },
    "https://api.frankerfacez.com/v1/set/global": {
        "default_sets": [3],
        "sets": {
            "3": {"emoticons": [{"name": "ZreknarF"}]},
            "4": {"emoticons": [{"name": "NotDefault"}]},
        },
    },
}


def client(responses=RESPONSES, fail: set[str] = frozenset()) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if any(f in url for f in fail):
            return httpx.Response(500)
        if url in responses:
            return httpx.Response(200, json=responses[url])
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_global_emotes_from_all_providers_first_provider_wins():
    emotes = await fetch_global_emotes(client())
    assert emotes == {"Clap": "7tv", "Shared": "7tv", "catJAM": "bttv", "ZreknarF": "ffz"}


async def test_channel_emotes_and_404_means_none():
    emotes = await fetch_channel_emotes(client(), "42")
    assert emotes == {"KEKW": "7tv", "OMEGALUL": "bttv", "PogU": "bttv"}
    assert await fetch_channel_emotes(client(), "999") == {}


async def test_failing_provider_does_not_break_the_others():
    emotes = await fetch_channel_emotes(client(fail={"7tv.io"}), "42")
    assert emotes == {"OMEGALUL": "bttv", "PogU": "bttv"}
    failed: list[str] = []
    await fetch_channel_emotes(client(fail={"7tv.io"}), "42", failed=failed)
    assert failed == ["7tv"]  # reported to a caller that asks
    await fetch_channel_emotes(client(), "999", failed=failed)
    assert failed == ["7tv"]  # 404: the channel just doesn't use it
