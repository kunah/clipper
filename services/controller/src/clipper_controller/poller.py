"""Live status: resolve channel ids, start/end streams, record viewer counts.

Writer of: channels (ids/display names), streams, viewer_snapshots.
Helix sometimes leaves a live stream out of one response, so a stream is only ended after
`end_after_misses` consecutive polls without it.
Only live streams (`streams.source = 'live'`) are touched: a stream the backfill tool wrote
(`source = 'backfill'`) is never updated, even when Helix reports its stream id live again
(it would turn live and the recorder would write into it; DESIGN §17).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from clipper_core.logs import get_logger
from clipper_core.timeline import utc_now
from clipper_twitch.helix import HelixStream, HelixUser

log = get_logger(__name__)


class Helix(Protocol):
    async def get_users(
        self, *, logins: Sequence[str] = (), ids: Sequence[str] = ()
    ) -> list[HelixUser]: ...

    async def get_streams(self, user_ids: Sequence[str]) -> list[HelixStream]: ...


@dataclass
class PollResult:
    live: int = 0
    started: list[str] = field(default_factory=list)
    ended: list[str] = field(default_factory=list)
    unknown_logins: list[str] = field(default_factory=list)


class StreamPoller:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        helix: Helix,
        end_after_misses: int = 2,
    ) -> None:
        self.sessions = sessions
        self.helix = helix
        self.end_after_misses = end_after_misses
        self._misses: dict[int, int] = {}  # stream id -> consecutive polls missing
        self._backfilled_seen: set[tuple[int, str]] = set()  # logged once per (channel, stream)

    async def poll_once(self, now: datetime | None = None) -> PollResult:
        now = now or utc_now()
        result = PollResult()
        async with self.sessions() as session:
            channels = (
                await session.execute(
                    text(
                        "SELECT id, login, platform_user_id FROM channels "
                        "WHERE active AND platform = 'twitch' ORDER BY id"
                    )
                )
            ).all()

        unresolved = [c.login for c in channels if not c.platform_user_id]
        resolved: dict[str, HelixUser] = {}
        if unresolved:
            users = await self.helix.get_users(logins=unresolved)
            resolved = {u.login.lower(): u for u in users}
            result.unknown_logins = sorted(set(unresolved) - set(resolved))
            for login in result.unknown_logins:
                log.warning("twitch channel not found", channel=login)

        user_ids = {
            c.id: c.platform_user_id or getattr(resolved.get(c.login), "id", None) for c in channels
        }
        live = {
            s.user_id: s for s in await self.helix.get_streams([u for u in user_ids.values() if u])
        }
        result.live = len(live)

        async with self.sessions.begin() as session:
            for login, user in resolved.items():
                await session.execute(
                    text(
                        "UPDATE channels SET platform_user_id = :uid, display_name = :name, "
                        "updated_at = now() WHERE platform = 'twitch' AND login = :login"
                    ),
                    {"uid": user.id, "name": user.display_name, "login": login},
                )
            open_streams = (
                await session.execute(
                    text(
                        "SELECT s.id, s.channel_id, s.platform_stream_id, c.login "
                        "FROM streams s JOIN channels c ON c.id = s.channel_id "
                        "WHERE s.status = 'live' AND s.source = 'live'"
                    )
                )
            ).all()
            current: dict[int, int] = {}  # channel id -> stream id that is live now
            for channel in channels:
                stream = live.get(user_ids.get(channel.id) or "")
                if stream is None:
                    continue
                upserted = await self._upsert_stream(session, channel.id, stream)
                if upserted is None:  # the row belongs to a backfilled stream: leave it alone
                    if (channel.id, stream.id) not in self._backfilled_seen:
                        self._backfilled_seen.add((channel.id, stream.id))
                        log.warning(
                            "live stream id belongs to a backfilled stream, not tracking it",
                            channel=channel.login,
                            platform_stream_id=stream.id,
                        )
                    continue
                stream_id, created = upserted
                current[channel.id] = stream_id
                await session.execute(
                    text(
                        "INSERT INTO viewer_snapshots (stream_id, ts, viewers) "
                        "VALUES (:s, :ts, :v) ON CONFLICT DO NOTHING"
                    ),
                    {"s": stream_id, "ts": now.replace(microsecond=0), "v": stream.viewer_count},
                )
                if created:
                    result.started.append(channel.login)
                    log.info(
                        "stream started",
                        channel=channel.login,
                        stream_id=stream_id,
                        title=stream.title,
                        category=stream.game_name,
                        viewers=stream.viewer_count,
                    )

            for s in open_streams:
                if current.get(s.channel_id) == s.id:
                    self._misses.pop(s.id, None)
                    continue
                replaced = s.channel_id in current  # a new broadcast started meanwhile
                misses = self._misses.get(s.id, 0) + 1
                if replaced or misses >= self.end_after_misses:
                    await session.execute(
                        text("UPDATE streams SET status = 'ended', ended_at = :t WHERE id = :id"),
                        {"t": now, "id": s.id},
                    )
                    self._misses.pop(s.id, None)
                    result.ended.append(s.login)
                    log.info("stream ended", channel=s.login, stream_id=s.id)
                else:
                    self._misses[s.id] = misses
        return result

    @staticmethod
    async def _upsert_stream(
        session: AsyncSession, channel_id: int, stream: HelixStream
    ) -> tuple[int, bool] | None:
        """(stream id, created), or None when the row is a backfilled stream (not updated)."""
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO streams (channel_id, platform_stream_id, title, category,
                                         started_at, status)
                    VALUES (:c, :sid, :title, :cat, :started, 'live')
                    ON CONFLICT (channel_id, platform_stream_id) DO UPDATE
                       SET title = EXCLUDED.title, category = EXCLUDED.category,
                           status = 'live', ended_at = NULL
                     WHERE streams.source = 'live'
                    RETURNING id, (xmax = 0) AS created
                    """
                ),
                {
                    "c": channel_id,
                    "sid": stream.id,
                    "title": stream.title,
                    "cat": stream.game_name,
                    "started": stream.started_at,
                },
            )
        ).first()
        return (row.id, row.created) if row else None
