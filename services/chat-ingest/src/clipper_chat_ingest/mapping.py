"""IRC messages -> rows for chat_messages / chat_events (pure functions, easy to test).

The row types live in `clipper_core.chat` (shared with the backfill tool's chat replay).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from clipper_core.chat import EventRow, MessageRow
from clipper_core.timeline import from_unix_ms
from clipper_twitch.emotes import message_emotes
from clipper_twitch.irc import IrcMessage, parse_badges, parse_emotes, strip_action

ROOMSTATE_KEYS = ("emote-only", "followers-only", "r9k", "slow", "subs-only")


@dataclass(slots=True)
class ChannelState:
    channel_id: int
    login: str
    twitch_id: str | None = None
    stream_id: int | None = None
    emotes: Mapping[str, str] = field(default_factory=dict)  # third-party name -> provider


@dataclass(slots=True)
class Mapped:
    message: MessageRow | None = None
    events: list[EventRow] = field(default_factory=list)
    delete_msg_id: str | None = None
    room_id: str | None = None


def sent_at_of(msg: IrcMessage, received_at: datetime) -> datetime:
    ts = msg.tags.get("tmi-sent-ts")
    return from_unix_ms(ts) if ts and ts.isdigit() else received_at


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
        twitch_emotes = parse_emotes(tags.get("emotes"), text)
        emotes = message_emotes(text, twitch_emotes, channel.emotes, global_emotes)
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
