"""Structured JSON-lines logging (spec 19.4).

A leaf utility: it imports ONLY stdlib + ``portfolio_dash.shared.config`` (for the
configured ``db_path``). It never imports higher layers (architecture.md — shared is
the base everything may import; it imports nothing internal but its sibling config).

The :class:`JsonLinesFormatter` renders each record as one self-contained JSON object
(``ts``/``level``/``logger``/``msg`` always; ``traceback`` when ``exc_info`` is set;
plus any structured ``extra`` fields the caller passed, e.g. ``agent``/``model``/
``input_tokens``/``output_tokens``/``cost`` for LLM usage). :func:`configure_logging`
attaches a size-rotating file handler to the root logger and is idempotent — repeated
calls do not stack duplicate handlers.
"""

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from portfolio_dash.shared.config import get_settings

# Sentinel marking our handler so configure_logging stays idempotent.
_HANDLER_NAME = "portfolio_dash.jsonlines"

# Attributes present on every stdlib LogRecord; anything else on the record's __dict__
# was injected by the caller via ``extra=...`` and is surfaced into the JSON object.
_RESERVED_ATTRS = frozenset(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonLinesFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object.

    Always emits ``ts`` (ISO-8601 UTC), ``level``, ``logger``, ``msg``. Adds a
    ``traceback`` string when ``record.exc_info`` is set, and merges any non-standard
    record attributes (the caller's ``extra=...`` fields). Robust by contract: it never
    raises from :meth:`format` — a non-serializable extra is coerced via ``str``.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["traceback"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key.startswith("_"):
                continue
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(*, log_dir: Path | None = None) -> None:
    """Attach a rotating JSON-lines file handler to the root logger (idempotent).

    Writes ``<log_dir>/app.log`` (``maxBytes=10 MiB``, ``backupCount=5``) at INFO. When
    *log_dir* is None it defaults to ``get_settings().db_path.parent / "logs"`` and is
    created (parents, exist_ok). Repeated calls are a no-op: the handler is tagged with a
    sentinel name and skipped when already present. Other handlers and propagation are
    left untouched so pytest ``caplog`` keeps working.
    """
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, "name", None) == _HANDLER_NAME:
            return

    target_dir = log_dir if log_dir is not None else get_settings().db_path.parent / "logs"
    target_dir.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        target_dir / "app.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.name = _HANDLER_NAME
    handler.setFormatter(JsonLinesFormatter())

    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
