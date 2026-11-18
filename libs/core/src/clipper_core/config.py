from __future__ import annotations

import os
import socket
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


class Settings(BaseSettings):
    """Configuration shared by all services. Read from CLIPPER_* env vars (and .env)."""

    model_config = SettingsConfigDict(env_prefix="CLIPPER_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://clipper:clipper@127.0.0.1:5433/clipper"

    s3_endpoint: str = "http://127.0.0.1:8333"
    s3_region: str = "us-east-1"
    s3_access_key: str = "clipper"
    s3_secret_key: SecretStr = SecretStr("clipper-dev-secret")
    s3_bucket: str = "clipper"

    service_name: str = "unknown"
    worker_id: str = Field(default_factory=_default_worker_id)
    log_level: str = "INFO"
    log_format: Literal["auto", "json", "console"] = "auto"  # auto: console on a TTY, else json
    log_levels: str = ""  # per-logger overrides, e.g. "sqlalchemy.engine=INFO,botocore=DEBUG"

    @property
    def database_dsn(self) -> str:
        """Plain libpq DSN for raw psycopg connections (e.g. LISTEN/NOTIFY)."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
