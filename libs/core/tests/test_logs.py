import asyncio
import io
import json
import logging
import sys
from datetime import UTC, datetime
from enum import Enum
from pathlib import PurePosixPath

import pytest

from clipper_core.config import Settings
from clipper_core.logs import (
    bind_context,
    current_context,
    get_logger,
    log_context,
    parse_levels,
    setup_logging,
)


@pytest.fixture(autouse=True)
def restore_logging():
    root = logging.getLogger()
    handlers, level, hook = root.handlers[:], root.level, sys.excepthook
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    sys.excepthook = hook
    logging.captureWarnings(False)


def capture(fmt: str = "json", **kwargs) -> io.StringIO:
    stream = io.StringIO()
    settings = Settings(service_name="recorder", worker_id="pc-42", _env_file=None)
    setup_logging(settings, fmt=fmt, stream=stream, **kwargs)
    return stream


def lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_json_line_has_core_keys_and_fields():
    stream = capture()
    get_logger("clipper.test").info("chunk saved", chunk_id=5, seq=3)
    (doc,) = lines(stream)
    assert doc["message"] == "chunk saved"
    assert doc["level"] == "INFO"
    assert doc["service"] == "recorder" and doc["worker"] == "pc-42"
    assert doc["logger"] == "clipper.test"
    assert doc["chunk_id"] == 5 and doc["seq"] == 3
    assert doc["timestamp"].endswith("Z")


def test_library_loggers_use_the_same_format():
    stream = capture()
    logging.getLogger("some.library").warning("plain %s logging", "stdlib")
    (doc,) = lines(stream)
    assert doc["message"] == "plain stdlib logging" and doc["service"] == "recorder"


def test_fields_cannot_override_core_keys():
    stream = capture()
    get_logger("t").info("real", message="fake", level="DEBUG", service="other")
    (doc,) = lines(stream)
    assert (doc["message"], doc["level"], doc["service"]) == ("real", "INFO", "recorder")


def test_context_nests_and_resets():
    stream = capture()
    log = get_logger("t")
    with log_context(channel="xqc"):
        with log_context(stream_id=7):
            log.info("inner", chunk_id=1)
        log.info("outer")
    log.info("none")
    inner, outer, none = lines(stream)
    assert (inner["channel"], inner["stream_id"], inner["chunk_id"]) == ("xqc", 7, 1)
    assert outer["channel"] == "xqc" and "stream_id" not in outer
    assert "channel" not in none
    assert current_context() == {}


async def test_each_task_keeps_its_own_context():
    stream = capture()
    log = get_logger("t")

    async def channel_loop(name: str) -> None:
        bind_context(channel=name)
        await asyncio.sleep(0)
        log.info("tick")

    await asyncio.gather(channel_loop("a"), channel_loop("b"))
    assert sorted(doc["channel"] for doc in lines(stream)) == ["a", "b"]
    assert current_context() == {}


def test_exceptions_are_structured():
    stream = capture()
    try:
        raise ValueError("bad chunk")
    except ValueError:
        get_logger("t").exception("job failed", job_id=9)
    (doc,) = lines(stream)
    assert doc["level"] == "ERROR" and doc["job_id"] == 9
    assert doc["exc_type"] == "ValueError"
    assert "bad chunk" in doc["exception"] and "Traceback" in doc["exception"]


def test_unusual_values_are_serialized():
    class Mode(Enum):
        FULL = "full"

    stream = capture()
    get_logger("t").info(
        "values",
        at=datetime(2026, 11, 18, tzinfo=UTC),
        mode=Mode.FULL,
        path=PurePosixPath("/data/x.ts"),
        tags={"a"},
        obj=object(),
    )
    (doc,) = lines(stream)
    assert doc["at"] == "2026-11-18T00:00:00+00:00"
    assert doc["mode"] == "full" and doc["path"] == "/data/x.ts" and doc["tags"] == ["a"]
    assert doc["obj"].startswith("<object")


def test_console_format_is_readable():
    stream = capture(fmt="console")
    with log_context(channel="xqc"):
        get_logger("clipper.rec").info("chunk saved", chunk_id=5, title="big moment")
    line = stream.getvalue().strip()
    assert "INFO" in line and "recorder clipper.rec: chunk saved" in line
    assert 'channel=xqc chunk_id=5 title="big moment"' in line


def test_levels_and_quiet_libraries():
    stream = capture(level="WARNING", levels="clipper.noisy=ERROR")
    get_logger("clipper.app").info("hidden")
    get_logger("clipper.app").warning("shown")
    get_logger("clipper.noisy").warning("hidden too")
    logging.getLogger("botocore").info("hidden by default")
    assert [d["message"] for d in lines(stream)] == ["shown"]


def test_parse_levels():
    assert parse_levels("") == {}
    assert parse_levels(" a=info, b.c=DEBUG ") == {"a": "INFO", "b.c": "DEBUG"}
    with pytest.raises(ValueError):
        parse_levels("nonsense")


def test_bad_format_is_rejected():
    with pytest.raises(ValueError):
        setup_logging(fmt="xml", stream=io.StringIO())
