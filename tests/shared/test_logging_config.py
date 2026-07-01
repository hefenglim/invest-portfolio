"""Tests for structured JSON-lines logging (spec 19.4).

Hermetic: every test writes to a tmp ``log_dir`` (never the real ``data/``), and the
sentinel-tagged handler is removed from the root logger on teardown so configure_logging
state never leaks across tests.
"""

import json
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from portfolio_dash.shared.logging_config import (
    _HANDLER_NAME,
    JsonLinesFormatter,
    configure_logging,
)


@pytest.fixture(autouse=True)
def _clean_root_handler() -> Iterator[None]:
    """Strip our sentinel handler before and after each test (no cross-test leakage)."""
    root = logging.getLogger()
    before = list(root.handlers)
    yield
    for handler in list(root.handlers):
        if handler not in before or getattr(handler, "name", None) == _HANDLER_NAME:
            root.removeHandler(handler)
            handler.close()


def _read_lines(log_dir: Path) -> list[dict[str, Any]]:
    text = (log_dir / "app.log").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_formatter_emits_core_keys() -> None:
    record = logging.makeLogRecord(
        {"name": "demo", "levelno": logging.INFO, "levelname": "INFO", "msg": "hello %s",
         "args": ("world",)}
    )
    obj = json.loads(JsonLinesFormatter().format(record))
    assert obj["level"] == "INFO"
    assert obj["logger"] == "demo"
    assert obj["msg"] == "hello world"
    assert "ts" in obj and obj["ts"].endswith("+00:00")  # ISO-8601 UTC


def test_formatter_includes_traceback_on_exc_info() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "demo", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
        )
    obj = json.loads(JsonLinesFormatter().format(record))
    assert "traceback" in obj
    assert "ValueError: boom" in obj["traceback"]


def test_formatter_surfaces_extra_fields() -> None:
    record = logging.makeLogRecord(
        {"name": "demo", "levelno": logging.INFO, "levelname": "INFO", "msg": "llm_usage",
         "agent": "composer", "model": "gpt-x", "input_tokens": 12, "output_tokens": 34,
         "cost": "0.0012"}
    )
    obj = json.loads(JsonLinesFormatter().format(record))
    assert obj["agent"] == "composer"
    assert obj["model"] == "gpt-x"
    assert obj["input_tokens"] == 12
    assert obj["output_tokens"] == 34
    assert obj["cost"] == "0.0012"


def test_formatter_never_raises_on_unserializable_extra() -> None:
    record = logging.makeLogRecord(
        {"name": "demo", "levelno": logging.INFO, "levelname": "INFO", "msg": "x",
         "weird": object()}
    )
    obj = json.loads(JsonLinesFormatter().format(record))  # default=str coerces it
    assert "weird" in obj and isinstance(obj["weird"], str)


def test_configure_logging_writes_jsonlines(tmp_path: Path) -> None:
    configure_logging(log_dir=tmp_path)
    logging.getLogger("portfolio_dash.test").info(
        "llm_usage", extra={"agent": "a", "model": "m", "cost": "0.01"}
    )
    logging.shutdown()
    lines = _read_lines(tmp_path)
    assert lines, "expected at least one log line"
    last = lines[-1]
    assert last["msg"] == "llm_usage"
    assert last["agent"] == "a"
    assert last["cost"] == "0.01"
    assert {"ts", "level", "logger", "msg"} <= set(last)


def test_configure_logging_traceback_path(tmp_path: Path) -> None:
    configure_logging(log_dir=tmp_path)
    logger = logging.getLogger("portfolio_dash.test")
    try:
        raise RuntimeError("kapow")
    except RuntimeError:
        logger.exception("unhandled error")
    logging.shutdown()
    lines = _read_lines(tmp_path)
    assert any("traceback" in line and "RuntimeError: kapow" in line["traceback"]
               for line in lines)


def test_configure_logging_is_idempotent(tmp_path: Path) -> None:
    root = logging.getLogger()
    base = len(root.handlers)
    configure_logging(log_dir=tmp_path)
    after_first = len(root.handlers)
    configure_logging(log_dir=tmp_path)
    configure_logging(log_dir=tmp_path)
    after_repeats = len(root.handlers)
    assert after_first == base + 1
    assert after_repeats == after_first  # no duplicate handlers stacked
    tagged = [h for h in root.handlers if getattr(h, "name", None) == _HANDLER_NAME]
    assert len(tagged) == 1


def test_configure_logging_defaults_log_dir_under_db_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from portfolio_dash.shared import config as config_mod

    monkeypatch.setenv("DB_PATH", str(tmp_path / "nested" / "portfolio.db"))
    config_mod.get_settings.cache_clear()
    try:
        configure_logging()  # log_dir=None -> db_path.parent / "logs"
        assert (tmp_path / "nested" / "logs" / "app.log").exists()
    finally:
        config_mod.get_settings.cache_clear()
