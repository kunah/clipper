"""Every worker upserts its row regularly; the status page/alerts read last_seen."""

from __future__ import annotations

import socket
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import bindparam
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession


async def beat(
    session: AsyncSession,
    *,
    worker_id: str,
    service: str,
    started_at: datetime,
    info: Mapping[str, Any] | None = None,
) -> None:
    await session.execute(
        sql_text(
            """
            INSERT INTO worker_heartbeats (worker_id, service, host, started_at, last_seen, info)
            VALUES (:worker_id, :service, :host, :started_at, now(), :info)
            ON CONFLICT (worker_id) DO UPDATE
               SET last_seen = now(), info = EXCLUDED.info, service = EXCLUDED.service
            """
        ).bindparams(bindparam("info", type_=JSONB)),
        {
            "worker_id": worker_id,
            "service": service,
            "host": socket.gethostname(),
            "started_at": started_at,
            "info": dict(info or {}),
        },
    )
