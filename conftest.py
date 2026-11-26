"""Shared pytest fixtures.

Integration tests use the Postgres from compose.yaml (`docker compose up -d postgres objectstore`)
and create their own throwaway databases, so they never touch the real `clipper` database.
They are skipped when Postgres isn't reachable.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

BASE_URL = os.environ.get(
    "CLIPPER_TEST_DATABASE_URL", "postgresql+psycopg://clipper:clipper@127.0.0.1:5433/clipper"
)
# Same checkout -> same id; another git worktree -> another id (and other test databases).
CHECKOUT_ID = hashlib.sha1(str(Path(__file__).resolve().parent).lower().encode()).hexdigest()[:8]


def database_url(name: str) -> str:
    return make_url(BASE_URL).set(database=name).render_as_string(hide_password=False)


def _libpq(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def pytest_collection_modifyitems(config, items):
    # Tests against the real Twitch run only on request: they are slow and depend on Twitch.
    if os.environ.get("CLIPPER_TWITCH_LIVE_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="real Twitch requests: set CLIPPER_TWITCH_LIVE_TESTS=1")
    for item in items:
        if "twitch_live" in item.keywords:
            item.add_marker(skip)


def pytest_asyncio_loop_factories(config, item):
    # psycopg async can't use Windows' default Proactor loop (same as clipper_core.runtime.run).
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


@pytest.fixture(scope="session")
def pg_admin_dsn() -> str:
    dsn = _libpq(database_url("postgres"))
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            pass
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable, run `docker compose up -d postgres` ({exc})")
    return dsn


@pytest.fixture(scope="session")
def fresh_database(pg_admin_dsn):
    """Factory: create an empty database and return its SQLAlchemy URL. Dropped at session end.

    Names get a suffix per checkout, so test runs in two worktrees never drop each other's database.
    """
    created: list[str] = []

    def make(name: str) -> str:
        name = f"{name}_{CHECKOUT_ID}"
        with psycopg.connect(pg_admin_dsn, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            conn.execute(f"CREATE DATABASE {name}")
        created.append(name)
        return database_url(name)

    yield make
    with psycopg.connect(pg_admin_dsn, autocommit=True) as conn:
        for name in created:
            conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture(scope="session")
def test_database_url(fresh_database) -> str:
    """Database with the schema at head, shared by the test session."""
    from clipper_db.cli import ensure_partitions, upgrade

    url = fresh_database("clipper_test")
    upgrade(url)
    ensure_partitions(url)
    return url


@pytest.fixture
def settings(test_database_url):
    from clipper_core.config import Settings

    return Settings(database_url=test_database_url, _env_file=None)


@pytest.fixture
async def sessions(settings):
    """Session factory on a clean database (all tables truncated before each test)."""
    from clipper_core.db import create_engine, session_factory
    from clipper_core.models import Base

    engine = create_engine(settings)
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield session_factory(engine)
    await engine.dispose()
