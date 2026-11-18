"""One JSON object per line on stdout and in DATA_DIR/logs/server.log (plan 5.5).

    from app.common.logging import log
    log.info("allocate", job_id=1042, engine="E3", ms=3)
    log.warning("illegal_transition", job_id=1042, to="running")
    log.exception("admission_cycle_failed")  # in an except block: adds error and traceback

Every line has ts, level and event, which no field can overwrite. Until configure() runs (in
tests, for example), the records go to the standard logging tree.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

LOGGER: Final = logging.getLogger("orch")


class _JsonLines(logging.Formatter):
    """A record as one line of JSON; a traceback stays inside its field."""

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        fields: dict[str, object] = getattr(record, "fields", {})
        data.update((key, value) for key, value in fields.items() if key not in data)
        if record.exc_info and record.exc_info[1] is not None:
            data.setdefault("error", f"{type(record.exc_info[1]).__name__}: {record.exc_info[1]}")
            data.setdefault("traceback", self.formatException(record.exc_info))
        return json.dumps(data, default=str)


class EventLog:
    """log.<level>(event, **fields): one record per call, the form plan 7.1, 8.3 and 10.6 use."""

    def info(self, event: str, **fields: object) -> None:
        LOGGER.info(event, extra={"fields": fields})

    def warning(self, event: str, **fields: object) -> None:
        LOGGER.warning(event, extra={"fields": fields})

    def error(self, event: str, **fields: object) -> None:
        LOGGER.error(event, extra={"fields": fields})

    def exception(self, event: str, **fields: object) -> None:
        """An error with the exception being handled and its traceback; call it in an except block."""
        LOGGER.exception(event, extra={"fields": fields})


log: Final = EventLog()


def configure(data_dir: Path) -> None:
    """Send the records to stdout and data_dir/logs/server.log as JSON lines; main.py calls this
    once at startup. Calling it again replaces the handlers."""
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    for old in LOGGER.handlers[:]:
        LOGGER.removeHandler(old)
        old.close()
    formatter = _JsonLines()
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(log_dir / "server.log", encoding="utf-8")):
        handler.setFormatter(formatter)
        LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
