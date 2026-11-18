import uuid
from datetime import date

import pytest

from clipper_core.config import Settings
from clipper_core.contracts import JobKind, Priority, dedupe_key
from clipper_core.partitions import days, partition_ddl, partition_name
from clipper_core.storage import ObjectStore, keys


def test_priority_order():
    assert Priority.BACKFILL < Priority.LIVE < Priority.TRIGGER < Priority.JUDGE_CONTEXT


def test_dedupe_key():
    assert dedupe_key(JobKind.TRANSCRIBE, 42) == "transcribe:42"


def test_storage_keys():
    assert keys.chunk_video(7, 3) == "streams/7/video/000003.ts"
    assert keys.chunk_audio(7, 3) == "streams/7/audio/000003.aac"
    assert keys.chunk_words(7, 3) == "streams/7/transcripts/000003.json"


def test_partition_ddl_uses_utc_day_bounds():
    assert partition_name(date(2026, 11, 18)) == "chat_messages_p20261118"
    ddl = partition_ddl(date(2026, 11, 18))
    assert "FROM ('2026-11-18T00:00:00+00:00') TO ('2026-11-19T00:00:00+00:00')" in ddl
    assert days(date(2026, 11, 30), date(2026, 12, 1)) == [date(2026, 11, 30), date(2026, 12, 1)]


def test_database_dsn_strips_driver():
    settings = Settings(database_url="postgresql+psycopg://u:p@h:5432/d", _env_file=None)
    assert settings.database_dsn == "postgresql://u:p@h:5432/d"


@pytest.mark.integration
def test_object_store_roundtrip():
    store = ObjectStore(Settings(s3_bucket="clipper-test", _env_file=None))
    try:
        store.ensure_bucket(wait_s=3)
    except Exception as exc:
        pytest.skip(f"object storage not reachable, run `docker compose up -d objectstore` ({exc})")
    key = f"tests/{uuid.uuid4()}.bin"
    assert not store.exists(key)
    store.put_bytes(key, b"hello")
    assert store.exists(key)
    assert store.get_bytes(key) == b"hello"
    store.delete(key)
    assert not store.exists(key)
