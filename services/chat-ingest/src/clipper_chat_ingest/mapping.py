"""IRC messages -> rows for chat_messages / chat_events (pure functions, easy to test)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from clipper_core.timeline import from_unix_ms
from clipper_twitch.irc import IrcMessage, count_tokens, parse_badges, parse_emotes, strip_action

ROOMSTATE_KEYS = ("emote-only", "followers-only", "r9k", "slow", "subs-only")


@dataclass(slots=True)
class ChannelState:
    channel_id: int
    login: str
    twitch_id: str | None = None
    stream_id: int | None = None
    emotes: Mapping[str, str] = field(default_factory=dict)  # third-party name -> provider


@dataclass(slots=True)
class MessageRow:
    sent_at: datetime
    msg_id: str
    channel_id: int
    stream_id: int | None
    received_at: datetime
    user_id: str | None
    user_login: str | None
    text: str
    emotes: list[dict[str, Any]] | None
    badges: dict[str, str] | None
    deleted: bool = False
    source: str = "live"


@dataclass(slots=True)
class EventRow:
    channel_id: int
    stream_id: int | None
    sent_at: datetime
    type: str
    msg_id: str | None = None
    user_login: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Mapped:
    message: MessageRow | None = None
    events: list[EventRow] = field(default_factory=list)
    delete_msg_id: str | None = None
    room_id: str | None = None


def sent_at_of(msg: IrcMessage, received_at: datetime) -> datetime:
    ts = msg.tags.get("tmi-sent-ts")
    return from_unix_ms(ts) if ts and ts.isdigit() else received_at


def message_emotes(
    text: str,
    twitch_tag: str | None,
    third_party: Mapping[str, str],
    global_emotes: Mapping[str, str],
) -> list[dict[str, Any]]:
    emotes = [
        {"name": name, "count": count, "source": "twitch", "id": emote_id}
        for emote_id, name, count in parse_emotes(twitch_tag, text)
    ]
    twitch_names = {e["name"] for e in emotes}
    known = {**global_emotes, **third_party}
    for name, count in count_tokens(text, known).items():
        if name not in twitch_names:
            emotes.append({"name": name, "count": count, "source": known[name]})
    return emotes


def map_message(
    msg: IrcMessage,
    received_at: datetime,
    channel: ChannelState,
    global_emotes: Mapping[str, str],
) -> Mapped:
    tags = msg.tags
    sent_at = sent_at_of(msg, received_at)
    base = {"channel_id": channel.channel_id, "stream_id": channel.stream_id, "sent_at": sent_at}

    if msg.command == "PRIVMSG":
        text, is_action = strip_action(msg.text or "")
        emotes = message_emotes(text, tags.get("emotes"), channel.emotes, global_emotes)
        badges = parse_badges(tags.get("badges"))
        row = MessageRow(
            sent_at=sent_at,
            msg_id=tags.get("id") or f"noid-{received_at.timestamp()}-{msg.nick}",
            channel_id=channel.channel_id,
            stream_id=channel.stream_id,
            received_at=received_at,
            user_id=tags.get("user-id"),
            user_login=msg.nick,
            text=text,
            emotes=emotes or None,
            badges=badges or None,
        )
        out = Mapped(message=row)
        if is_action:
            row.badges = {**(row.badges or {}), "_action": "1"}
        bits = tags.get("bits")
        if bits and bits.isdigit():
            out.events.append(
                EventRow(
                    **base,
                    type="bits",
                    msg_id=row.msg_id,
                    user_login=msg.nick,
                    data={"bits": int(bits)},
                )
            )
        return out

    if msg.command == "USERNOTICE":
        params = {k[len("msg-param-") :]: v for k, v in tags.items() if k.startswith("msg-param-")}
        data: dict[str, Any] = {**params, "system_msg": tags.get("system-msg")}
        if msg.text:
            data["text"] = msg.text
        return Mapped(
            events=[
                EventRow(
                    **base,
                    type=tags.get("msg-id") or "usernotice",
                    msg_id=tags.get("id"),
                    user_login=tags.get("login"),
                    data=data,
                )
            ]
        )

    if msg.command == "CLEARMSG":
        target = tags.get("target-msg-id")
        return Mapped(
            events=[
                EventRow(
                    **base,
                    type="clearmsg",
                    user_login=tags.get("login"),
                    data={"target_msg_id": target},
                )
            ],
            delete_msg_id=target,
        )

    if msg.command == "CLEARCHAT":
        target_user = tags.get("target-user-id")
        if target_user and tags.get("ban-duration"):
            event = EventRow(
                **base,
                type="timeout",
                user_login=msg.text,
                data={"target_user_id": target_user, "duration_s": int(tags["ban-duration"])},
            )
        elif target_user:
            event = EventRow(
                **base, type="ban", user_login=msg.text, data={"target_user_id": target_user}
            )
        else:
            event = EventRow(**base, type="clearchat")
        return Mapped(events=[event])

    if msg.command == "ROOMSTATE":
        changed = {k: tags[k] for k in ROOMSTATE_KEYS if k in tags}
        full_state = len(changed) == len(ROOMSTATE_KEYS)  # sent once on join, not a change
        out = Mapped(room_id=tags.get("room-id"))
        if changed and not full_state:
            out.events.append(EventRow(**base, type="roomstate", data=changed))
        return out

    return Mapped()
