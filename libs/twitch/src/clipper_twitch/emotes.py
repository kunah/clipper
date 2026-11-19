"""Third-party emote sets (7TV, BTTV, FFZ). Most "meaningful" emotes (KEKW, OMEGALUL,
Pog...) come from these, not from Twitch, so chat stats must recognise them by name.

Every provider is optional: failures are logged and return an empty set, so one provider
being down never stops chat logging. Result: {emote_name: provider}.
`message_emotes()` combines a message's Twitch emotes with these sets (live chat and VOD replay).
Verified 2026-11-18: 7TV v3 and BTTV v3 respond. FFZ was unreachable from the dev PC that day,
so its parsing follows the documented v1 format but is not verified yet.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

import httpx

from clipper_core.logs import get_logger
from clipper_twitch.irc import count_tokens

log = get_logger(__name__)

USER_AGENT = "clipper/0.1"
PROVIDER_ORDER = ("7tv", "bttv", "ffz")  # first one wins when names collide
REQUEST_TIMEOUT_S = 8.0  # a slow/down provider must not stall the others


async def _get_json(http: httpx.AsyncClient, url: str) -> Any | None:
    resp = await http.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT_S)
    if resp.status_code == 404:
        return None  # channel doesn't use this provider
    resp.raise_for_status()
    return resp.json()


async def _7tv_global(http: httpx.AsyncClient) -> list[str]:
    data = await _get_json(http, "https://7tv.io/v3/emote-sets/global")
    return [e["name"] for e in (data or {}).get("emotes") or []]


async def _7tv_channel(http: httpx.AsyncClient, twitch_id: str) -> list[str]:
    data = await _get_json(http, f"https://7tv.io/v3/users/twitch/{twitch_id}")
    emote_set = (data or {}).get("emote_set") or {}
    return [e["name"] for e in emote_set.get("emotes") or []]


async def _bttv_global(http: httpx.AsyncClient) -> list[str]:
    data = await _get_json(http, "https://api.betterttv.net/3/cached/emotes/global")
    return [e["code"] for e in data or []]


async def _bttv_channel(http: httpx.AsyncClient, twitch_id: str) -> list[str]:
    data = await _get_json(http, f"https://api.betterttv.net/3/cached/users/twitch/{twitch_id}")
    data = data or {}
    return [e["code"] for e in (data.get("channelEmotes") or []) + (data.get("sharedEmotes") or [])]


def _ffz_names(data: dict[str, Any] | None, set_ids: list[Any] | None = None) -> list[str]:
    sets = (data or {}).get("sets") or {}
    wanted = {str(s) for s in set_ids} if set_ids is not None else set(sets)
    return [e["name"] for sid, s in sets.items() if sid in wanted for e in s.get("emoticons") or []]


async def _ffz_global(http: httpx.AsyncClient) -> list[str]:
    data = await _get_json(http, "https://api.frankerfacez.com/v1/set/global")
    return _ffz_names(data, (data or {}).get("default_sets"))


async def _ffz_channel(http: httpx.AsyncClient, twitch_id: str) -> list[str]:
    return _ffz_names(await _get_json(http, f"https://api.frankerfacez.com/v1/room/id/{twitch_id}"))


async def _collect(fetchers: dict[str, Callable[[], Awaitable[list[str]]]]) -> dict[str, str]:
    """Query all providers in parallel; merge in PROVIDER_ORDER so precedence is stable."""
    outcomes = await asyncio.gather(
        *(fetchers[p]() for p in PROVIDER_ORDER), return_exceptions=True
    )
    result: dict[str, str] = {}
    for provider, names in zip(PROVIDER_ORDER, outcomes, strict=True):
        if isinstance(names, BaseException):
            log.warning(
                "emote provider failed",
                provider=provider,
                error=f"{type(names).__name__}: {names}".strip(),
            )
            continue
        for name in names:
            result.setdefault(name, provider)
    return result


async def fetch_global_emotes(http: httpx.AsyncClient) -> dict[str, str]:
    return await _collect(
        {
            "7tv": lambda: _7tv_global(http),
            "bttv": lambda: _bttv_global(http),
            "ffz": lambda: _ffz_global(http),
        }
    )


async def fetch_channel_emotes(http: httpx.AsyncClient, twitch_id: str) -> dict[str, str]:
    return await _collect(
        {
            "7tv": lambda: _7tv_channel(http, twitch_id),
            "bttv": lambda: _bttv_channel(http, twitch_id),
            "ffz": lambda: _ffz_channel(http, twitch_id),
        }
    )


def message_emotes(
    text: str,
    twitch_emotes: Iterable[tuple[str, str, int]],
    third_party: Mapping[str, str],
    global_emotes: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Emotes of one chat message, as stored in `chat_messages.emotes`.

    `twitch_emotes`: the message's parsed Twitch emotes as `(emote_id, name, count)` (live:
    `irc.parse_emotes` of the `emotes` tag; replay: from the comment's fragments).
    `third_party` (the channel's sets) and `global_emotes` map names to providers; they are
    matched by whole tokens of `text`, and a name that is a Twitch emote here is not counted
    twice. Channel sets win over global ones.
    """
    emotes = [
        {"name": name, "count": count, "source": "twitch", "id": emote_id}
        for emote_id, name, count in twitch_emotes
    ]
    twitch_names = {e["name"] for e in emotes}
    known = {**global_emotes, **third_party}
    for name, count in count_tokens(text, known).items():
        if name not in twitch_names:
            emotes.append({"name": name, "count": count, "source": known[name]})
    return emotes
