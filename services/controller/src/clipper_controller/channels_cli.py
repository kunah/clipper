"""`clipper-channels`: manage monitored channels.

    uv run clipper-channels add xqc kaicenat --mode audio_only
    uv run clipper-channels list
    uv run clipper-channels mode xqc full
    uv run clipper-channels disable xqc      # stop monitoring (keeps history)
    uv run clipper-channels enable xqc

In Docker: docker compose run --rm controller clipper-channels list
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from clipper_core.config import Settings
from clipper_core.contracts import RecordingMode
from clipper_core.db import create_engine, session_factory
from clipper_core.runtime import run

MODES = [m.value for m in RecordingMode]


def normalize(login: str) -> str:
    """'@XQC' / '#xqc' / 'xqc' -> 'xqc'"""
    return login.strip().lstrip("#@").lower()


async def add(session: AsyncSession, logins: list[str], mode: str) -> list[str]:
    added = []
    for login in map(normalize, logins):
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO channels (platform, login, recording_mode)
                    VALUES ('twitch', :login, :mode)
                    ON CONFLICT (platform, login) DO UPDATE
                       SET active = true, recording_mode = EXCLUDED.recording_mode,
                           updated_at = now()
                    RETURNING (xmax = 0) AS created
                    """
                ),
                {"login": login, "mode": mode},
            )
        ).one()
        added.append(f"{login} ({'added' if row.created else 'updated'}, {mode})")
    return added


async def set_field(session: AsyncSession, login: str, column: str, value: object) -> bool:
    assert column in ("active", "recording_mode")
    result = await session.execute(
        text(
            f"UPDATE channels SET {column} = :v, updated_at = now() "
            "WHERE platform = 'twitch' AND login = :login"
        ),
        {"v": value, "login": normalize(login)},
    )
    return result.rowcount > 0


async def list_channels(session: AsyncSession) -> list[str]:
    rows = (
        await session.execute(
            text(
                """
                SELECT c.login, c.active, c.recording_mode, c.platform_user_id,
                       s.started_at AS live_since,
                       (SELECT viewers FROM viewer_snapshots v WHERE v.stream_id = s.id
                         ORDER BY ts DESC LIMIT 1) AS viewers
                  FROM channels c
                  LEFT JOIN streams s ON s.channel_id = c.id AND s.status = 'live'
                 ORDER BY c.login
                """
            )
        )
    ).all()
    out = [f"{'login':<24} {'active':<7} {'mode':<11} {'twitch id':<12} live"]
    for r in rows:
        live = f"since {r.live_since:%H:%M}Z, {r.viewers} viewers" if r.live_since else "-"
        out.append(
            f"{r.login:<24} {('yes' if r.active else 'no'):<7} {r.recording_mode:<11} "
            f"{(r.platform_user_id or '?'):<12} {live}"
        )
    return out


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="clipper-channels", description="Manage monitored channels")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="add or re-enable channels")
    a.add_argument("logins", nargs="+")
    a.add_argument("--mode", choices=MODES, default=RecordingMode.FULL.value)
    sub.add_parser("list", help="show channels and live status")
    m = sub.add_parser("mode", help="set recording mode")
    m.add_argument("login")
    m.add_argument("mode", choices=MODES)
    for name in ("enable", "disable"):
        sub.add_parser(name, help=f"{name} monitoring").add_argument("login")
    return p.parse_args(argv)


async def amain(argv: list[str]) -> int:
    args = parse_args(argv)
    engine = create_engine(Settings())
    try:
        async with session_factory(engine).begin() as session:
            if args.cmd == "add":
                lines = await add(session, args.logins, args.mode)
            elif args.cmd == "list":
                lines = await list_channels(session)
            else:
                column, value = {
                    "mode": ("recording_mode", getattr(args, "mode", None)),
                    "enable": ("active", True),
                    "disable": ("active", False),
                }[args.cmd]
                ok = await set_field(session, args.login, column, value)
                lines = [f"{args.login}: {'ok' if ok else 'not found'}"]
                if not ok:
                    print("\n".join(lines))
                    return 1
    finally:
        await engine.dispose()
    print("\n".join(lines))
    return 0


def main() -> None:
    sys.exit(run(lambda: amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
