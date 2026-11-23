"""app.common.logging (plan 5.5): one JSON object per line. Later slices call log.info, log.warning,
log.error and log.exception with an event name and keyword fields (plan 7.1, 8.3, 10.6)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app.common.logging import log


def test_every_event_is_one_json_line_with_fixed_base_keys(json_log: Path):
    """Catches a traceback that breaks the one-line format (plan 8.3 calls log.exception), a caller
    field that overwrites ts, level or event, and a level helper that a later slice calls but that
    does not exist."""
    log.info("allocate", job_id=1042, engine="E3", ms=3, level="forged")
    log.warning("illegal_transition", job_id=1042, to="running")
    log.error("stop_not_confirmed", job_id=1042)
    try:
        raise ValueError("cycle broke")
    except ValueError:
        log.exception("admission_cycle_failed")
    records = [json.loads(line) for line in json_log.read_text(encoding="utf-8").splitlines()]
    assert [(r["event"], r["level"]) for r in records] == [
        ("allocate", "INFO"), ("illegal_transition", "WARNING"), ("stop_not_confirmed", "ERROR"),
        ("admission_cycle_failed", "ERROR")]
    assert records[0]["job_id"] == 1042 and records[0]["engine"] == "E3" and records[0]["ms"] == 3
    assert "ValueError: cycle broke" in records[3]["error"] and "Traceback" in records[3]["traceback"]
    assert all(r["ts"].endswith("+00:00") for r in records)


def test_psycopg_warnings_reach_the_log_and_its_info_lines_do_not(json_log: Path):
    """Catches handlers on the orch logger alone (Q1-03): psycopg and psycopg_pool report a refused or a
    broken connection on the logger psycopg, which then reached stderr only, so server.log showed the
    callers' PoolTimeout with no cause. The pool's INFO line for every connection it hands out stays out
    (D-136)."""
    pool_log = logging.getLogger("psycopg.pool")
    pool_log.warning("error connecting in %r: %s", "pool-1", "connection refused")
    pool_log.info("connection given by %r", "pool-1")
    records = [json.loads(line) for line in json_log.read_text(encoding="utf-8").splitlines()]
    assert [(r["level"], r["event"]) for r in records] == [
        ("WARNING", "error connecting in 'pool-1': connection refused")]
