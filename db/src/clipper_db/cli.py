"""`clipper-migrate`: everything that must happen once before the services start.

1. wait for Postgres  2. alembic upgrade head  3. chat partitions (yesterday..+7 days)
4. object storage bucket. Safe to run any number of times.
"""

from __future__ import annotations

import time
from datetime import timedelta

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from clipper_core.config import Settings
from clipper_core.logs import get_logger, setup_logging
from clipper_core.partitions import ensure_chat_partitions_sync
from clipper_core.storage import ObjectStore
from clipper_core.timeline import utc_now

log = get_logger("clipper_db")


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", "clipper_db:migrations")
    if database_url:
        cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


def wait_for_db(database_url: str, timeout_s: float = 60.0) -> None:
    engine = create_engine(database_url, connect_args={"connect_timeout": 3})
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                return
            except OperationalError:
                if time.monotonic() > deadline:
                    raise
                log.info("waiting for Postgres...")
                time.sleep(2)
    finally:
        engine.dispose()


def upgrade(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")


def ensure_partitions(database_url: str, days_back: int = 1, days_ahead: int = 7) -> None:
    today = utc_now().date()
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            ensure_chat_partitions_sync(
                conn, today - timedelta(days=days_back), today + timedelta(days=days_ahead)
            )
    finally:
        engine.dispose()


def main() -> None:
    settings = Settings(service_name="migrate")
    setup_logging(settings)
    wait_for_db(settings.database_url)
    upgrade(settings.database_url)
    ensure_partitions(settings.database_url)
    log.info("chat partitions ready")
    ObjectStore(settings).ensure_bucket()
    log.info("bucket ready", bucket=settings.s3_bucket)


if __name__ == "__main__":
    main()
