"""E2 service (plan 8.1 with D-77, D-78 and D-80): submit() now; E2-2 to E2-4 add the queue, the
admission loop, requeue, cancel, pause and policy to the same E2Service.

submit() reads what validation needs in one transaction, validates, writes the source under
DATA_DIR/payloads/<uuid4 hex>/ and inserts the queued job in a second tx("E2"). The state trigger
writes the first history row; the job_state event leaves only after COMMIT (invariant 3).
"""
from __future__ import annotations

import shutil
import uuid
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Final

from app.common.clock import RealClock
from app.common.db import Tx, retrying, tx
from app.common.errors import ApiError
from app.common.logging import log
from app.common.ports import engines
from app.common.types import SubmitRequest, SubmitResult, User
from app.config import Config
from app.e2_scheduling.validation import Accepted, FileName, Refs, Session, WorkloadType, validate
from app.e4_monitoring.state import clear_actor, set_actor

_CLOCK: Final = RealClock()
_TYPES: Final = "SELECT type_code, default_need_class, max_time_limit_s FROM workload_types"
_PROBLEM: Final = "SELECT 1 FROM problems WHERE problem_id = %s"
_SESSION: Final = ("SELECT kind, opens_at <= clock_timestamp() AND clock_timestamp() < closes_at "
                   "FROM lab_sessions WHERE lab_session_id = %s")
_MAXIMA: Final = "SELECT kind, max(total - reserved) FROM resources GROUP BY kind"
_INSERT: Final = ("INSERT INTO jobs (user_id, type_code, lab_session_id, problem_id, cpu_millicores, ram_mib, "
                  "vram_mib, time_limit_s, need_class, payload_path) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                  "RETURNING job_id, submitted_at")


class E2Service:
    """E2 for one process (D-77): main.py installs the instance as the E2Port."""

    def __init__(self, config: Config) -> None:
        self._config = config

    def submit(self, user: User, req: SubmitRequest, source: bytes) -> SubmitResult:
        """Validate req and source and queue the job (message 2). A refused request raises ApiError(400,
        error, field) with no job, no payload and no event. Any other failure before COMMIT removes the
        payload folder and re-raises; a failure at COMMIT keeps it, because the job may be committed,
        and logs submit_commit_unknown. RuntimeError when no capacity is loaded."""
        start = _CLOCK.monotonic()
        refs = _read_refs(req)
        try:
            accepted = validate(req, source, refs, self._config)
        except ApiError as refusal:
            log.info("submit_refused", engine="E2", user_id=user.user_id, field=refusal.field, error=refusal.error)
            raise
        folder, payload_path = self._write_payload(source, accepted.file_name)
        reached_commit = False

        def insert_once() -> int:
            nonlocal reached_commit
            reached_commit = False
            with tx("E2") as t:
                job_id, submitted_at = insert_job(t, user.user_id, req, accepted.need_class, payload_path)
                event = {"type": "job_state", "job_id": job_id, "user_id": user.user_id, "state": "queued",
                         "reason": "submitted", "at": submitted_at.isoformat()}
                t.after_commit(partial(_log_submit, job_id, user.user_id, req.workload_type, accepted, start))
                t.after_commit(partial(_publish, event))
                reached_commit = True  # the body is done: what fails from here on is COMMIT itself
            return job_id

        try:
            job_id = retrying(insert_once)
        except BaseException:
            if reached_commit:
                log.warning("submit_commit_unknown", engine="E2", user_id=user.user_id, payload_path=payload_path)
            else:
                _remove(folder)
            raise
        return SubmitResult(job_id, "queued", accepted.need_class)

    def _write_payload(self, source: bytes, file_name: FileName) -> tuple[Path, str]:
        """DATA_DIR/payloads/<uuid4 hex>/<file_name>, created new; the folder is what E1 mounts read-only.
        Returns the folder and payload_path, the folder relative to DATA_DIR."""
        name = uuid.uuid4().hex
        root = self._config.data_dir.resolve()
        folder = (root / "payloads" / name).resolve()
        if not folder.is_relative_to(root):
            raise RuntimeError(f"payload folder {folder} resolves outside DATA_DIR {root}")
        folder.mkdir(parents=True)
        try:
            with open(folder / file_name, "xb") as out:
                out.write(source)
        except BaseException:
            _remove(folder)
            raise
        return folder, f"payloads/{name}"


def insert_job(t: Tx, user_id: str, req: SubmitRequest, need_class: int, payload_path: str) -> tuple[int, datetime]:
    """Insert one queued job in the caller's tx("E2") and return its job_id and submitted_at. The state
    trigger demands app.actor and app.reason on the INSERT too (JT002) and writes the first history
    row; both settings are emptied again for the rest of the transaction. submit() and the test factory
    make_job both insert through here (D-81)."""
    set_actor(t, "E2", "submitted")
    row = t.execute(_INSERT, (user_id, req.workload_type, req.lab_session_id, req.problem_id, req.cpu_millicores,
                              req.ram_mib, req.vram_mib, req.time_limit_s, need_class, payload_path)).fetchone()
    clear_actor(t)
    if row is None:
        raise RuntimeError("INSERT INTO jobs returned no row")
    return row[0], row[1]


def _read_refs(req: SubmitRequest) -> Refs:
    """Every workload type, the problem and the session the request names (only when the id is an
    integer), and the allocatable maxima, read on every submit so a capacity.py run counts at once (D-78).
    One read-only transaction, no lock, no retry."""
    with tx("E2") as t:
        types = {code: WorkloadType(need_class, limit) for code, need_class, limit in t.execute(_TYPES).fetchall()}
        problem_found = (req.workload_type == "code_eval" and type(req.problem_id) is int
                         and t.execute(_PROBLEM, (req.problem_id,)).fetchone() is not None)
        session = None
        if type(req.lab_session_id) is int:
            row = t.execute(_SESSION, (req.lab_session_id,)).fetchone()
            session = None if row is None else Session(row[0], row[1])
        maxima: dict[str, int] = {kind: high for kind, high in t.execute(_MAXIMA).fetchall()}
    if "cpu" not in maxima or "ram" not in maxima:
        raise RuntimeError("no capacity loaded: resources has no cpu or ram row; run scripts/capacity.py")
    return Refs(types, problem_found, session, maxima["cpu"], maxima["ram"], maxima.get("gpu", 0))


def _log_submit(job_id: int, user_id: str, type_code: str, accepted: Accepted, start: float) -> None:
    log.info("submit", engine="E2", job_id=job_id, user_id=user_id, type_code=type_code,
             need_class=accepted.need_class, file=accepted.file_name, ms=round((_CLOCK.monotonic() - start) * 1000, 1))


def _publish(event: dict[str, Any]) -> None:
    engines().e4.publish(event)


def _remove(folder: Path) -> None:
    try:
        shutil.rmtree(folder)
    except OSError as exc:
        log.warning("payload_cleanup_failed", engine="E2", path=str(folder), error=str(exc))
