"""E2 service (plan 8.1 with D-77, D-78 and D-80): submit() and the queue (E2-2, D-103 to D-105), and the
admission cycle run_cycle() with capacity_changed() (E2-3, plan 8.3, D-116 to D-119); E2-4 adds requeue,
cancel, pause and policy to the same E2Service.

submit() reads what validation needs in one transaction, validates, writes the source under
DATA_DIR/payloads/<uuid4 hex>/ and inserts the queued job in a second tx("E2"). The state trigger
writes the first history row; the queue push, the job_state event and the wake of the admission loop
happen only after COMMIT (invariant 3).
"""
from __future__ import annotations

import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Final

from app.common.clock import Clock, RealClock
from app.common.db import Tx, retrying, tx
from app.common.errors import ApiError, IllegalTransition, NotFound
from app.common.logging import log
from app.common.ports import engines
from app.common.types import Committed, QueueEntry, Refused, Snapshot, SubmitRequest, SubmitResult, User, Vector
from app.config import Config
from app.e2_scheduling.policy import Policy, as_policy, oversized, select_fcfs, select_need
from app.e2_scheduling.queue import JobQueue
from app.e2_scheduling.validation import Accepted, FileName, Refs, Session, WorkloadType, validate
from app.e4_monitoring.state import JobState, clear_actor, set_actor, transition

_CLOCK: Final = RealClock()
SLOW_CYCLE_MS: Final = 100.0  # a cycle that holds E2's lock longer logs at INFO (proposal, D-119)
OVERSIZED: Final = "larger than the server's capacity"  # the reason of the cancel of D-116
_STATE: Final = "SELECT state FROM jobs WHERE job_id = %s"
_TYPES: Final = "SELECT type_code, default_need_class, max_time_limit_s FROM workload_types"
_PROBLEM: Final = "SELECT 1 FROM problems WHERE problem_id = %s"
_SESSION: Final = ("SELECT kind, opens_at <= clock_timestamp() AND clock_timestamp() < closes_at "
                   "FROM lab_sessions WHERE lab_session_id = %s")
_MAXIMA: Final = "SELECT kind, max(total - reserved) FROM resources GROUP BY kind"
_INSERT: Final = ("INSERT INTO jobs (user_id, type_code, lab_session_id, problem_id, cpu_millicores, ram_mib, "
                  "vram_mib, time_limit_s, need_class, payload_path) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                  "RETURNING job_id, submitted_at")


@dataclass
class _Tally:
    """What one cycle did, for its admission_cycle line."""
    admitted: int = 0
    cancelled: int = 0


class E2Service:
    """E2 for one process (D-77): main.py installs the instance as the E2Port and runs its run_cycle() on
    the one AdmissionLoop (loop.py)."""

    def __init__(self, config: Config, clock: Clock | None = None) -> None:
        """ValueError for a default_policy other than fcfs or need."""
        self._config = config
        self._clock: Clock = clock if clock is not None else RealClock()
        self._policy: Policy = as_policy(config.default_policy)
        # E2's one lock, the outermost in the process (D-104): taken before any transaction and never
        # waited for while a database lock is held; the queue's methods take it too.
        self.lock = threading.RLock()
        self.queue = JobQueue(self.lock)
        # Set by capacity_changed() and after a submit's COMMIT; the admission loop waits on it (D-75).
        self.wake = threading.Event()
        self._paused = False  # E2-4: pause_admission() and resume_admission() set and clear it under the lock
        # The job whose admit or allocate() raised: admitted, perhaps not, or perhaps allocated with the
        # answer lost. The next cycle calls allocate() for it before anything else (D-117).
        self._in_doubt: QueueEntry | None = None
        self._doubt_logged: int | None = None  # the job_id admission_in_doubt was logged for
        self._last_outcome: str | None = None  # of the previous cycle, for the INFO rule of D-119

    def submit(self, user: User, req: SubmitRequest, source: bytes) -> SubmitResult:
        """Validate req and source and queue the job (message 2). A refused request raises ApiError(400,
        error, field) with no job, no payload and no event. Any other failure before COMMIT removes the
        payload folder and re-raises; a failure at COMMIT keeps it, because the job may be committed,
        and logs submit_commit_unknown. RuntimeError when no capacity is loaded. After COMMIT, in this
        order: the submit log line, the queue push, the job_state event (D-105), the wake of the
        admission loop."""
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
                vector = Vector(req.cpu_millicores, req.ram_mib, req.vram_mib)
                t.after_commit(partial(self.queue.push,
                                       QueueEntry(job_id, user.user_id, accepted.need_class, submitted_at, vector)))
                t.after_commit(partial(_publish, event))
                t.after_commit(self.wake.set)
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

    def capacity_changed(self) -> None:
        """Message 8. Runs in release()'s after-COMMIT hook on the releasing thread, so it only wakes the
        admission loop: it never blocks, never takes the lock and never runs a cycle (D-75)."""
        self.wake.set()

    def run_cycle(self) -> None:
        """One admission cycle (plan 8.3, context §10), holding E2's lock from start to end (D-104): it is
        taken before any transaction, so no submit push, requeue or cancel lands between a pick and its
        allocate(). A job left in doubt by an earlier raise gets allocate() again first (D-117); then
        jobs are admitted one at a time (D-03) until the queue is empty, nothing fits, E3 refuses or the
        database refuses a move. A head that can never fit is cancelled on the way (D-116).

        Outcome of the cycle line: empty, blocked, refused, in_doubt, kept (a move was refused but the
        job is still queued), paused, failed. Raises whatever snapshot(), allocate() or a transaction
        raised; the loop logs it once per change. Calls engines only outside transactions."""
        asked = self._clock.monotonic()
        with self.lock:
            start = self._clock.monotonic()
            tally = _Tally()
            outcome = "failed"
            try:
                outcome = self._cycle(tally)
            finally:
                self._end_cycle(outcome, tally, start - asked, start)

    def _cycle(self, tally: _Tally) -> str:
        if self._paused:
            return "paused"
        if self._in_doubt is not None:
            outcome = self._allocate(self._in_doubt, tally, "retry")
            if outcome in ("refused", "in_doubt"):
                return outcome
        while len(self.queue):
            snap = engines().e3.snapshot()
            pick = self._select(snap)
            if pick is None:
                head = self.queue.fifo_head()
                if head is None or snap.fits(head.vector) or not self._cancel_if_oversized(head, tally):
                    return "blocked"
                continue
            outcome = self._admit(pick, tally)
            if outcome != "left":
                return outcome
        return "empty"

    def _select(self, snap: Snapshot) -> QueueEntry | None:
        if self._policy == "fcfs":
            return select_fcfs(self.queue, snap)
        # time(), not monotonic(): submitted_at is the database's wall-clock arrival time
        return select_need(self.queue, snap, self._clock.time(), self._config.starvation_guard_s)

    def _admit(self, entry: QueueEntry, tally: _Tally) -> str:
        """Move entry's job queued to admitted in its own tx("E2"), then ask E3 to allocate it. Returns
        left (it left the queue), refused, in_doubt or kept. A move that raises leaves nothing in doubt:
        if its COMMIT did happen, the next pick finds the job admitted (_resync) and asks allocate()."""
        try:
            retrying(partial(_move, entry.job_id, "admitted", f"picked by {self._policy}"))
        except (IllegalTransition, NotFound) as exc:
            return self._resync(entry, "admit", exc)
        return self._allocate(entry, tally, "allocate")

    def _allocate(self, entry: QueueEntry, tally: _Tally, stage: str) -> str:
        """allocate() for an admitted job (stage allocate) or for the job in doubt (stage retry); safe to
        repeat (D-70). The job leaves the queue only on Committed; on Refused E3 has moved it back to
        queued and it keeps its place (D-01). Until allocate() answers, the job is in doubt."""
        self._in_doubt = entry
        try:
            result = engines().e3.allocate(entry.job_id, entry.vector)
        except (IllegalTransition, NotFound) as exc:
            return self._resync(entry, stage, exc)
        if isinstance(result, Committed):
            self._settled()
            self.queue.remove(entry.job_id)
            tally.admitted += 1
            return "left"
        if isinstance(result, Refused):
            self._settled()
            return "refused"
        raise TypeError(f"allocate() answered {result!r} for job {entry.job_id}: neither Committed nor Refused")

    def _resync(self, entry: QueueEntry, stage: str, error: Exception) -> str:
        """The database refused entry's job at stage (admit, allocate, retry or cancel), with error. Neither
        IllegalTransition nor NotFound says the state, so read it (one read in tx("E2"), no lock) and act
        on it. A state that contradicts the refusal (a refused admit or cancel of a job that reads queued;
        allocate() refusing a job that reads admitted) is logged as an error and error is re-raised: E2
        is the only mover of queued jobs and holds its lock, so it would recur every cycle and stall the
        queue unseen. A job that reads queued at stage retry is the normal case of a lost answer."""
        state = _job_state(entry.job_id)
        if (state == "queued" and stage != "retry") or (state == "admitted" and stage in ("allocate", "retry")):
            log.error("admission_resync", engine="E2", job_id=entry.job_id, stage=stage, state=state, error=str(error))
            raise error
        if state == "admitted":  # admitted earlier with no answer from allocate(): ask it next cycle
            self._in_doubt = entry
            return "in_doubt"
        self._settled()
        if state == "queued":  # the earlier admit or allocate() never took effect: it waits in its place
            return "kept"
        self.queue.remove(entry.job_id)
        if state == "allocated":
            log.warning("allocate_ack_lost", engine="E2", job_id=entry.job_id, stage=stage, state=state,
                        detail="allocate() committed but its answer was lost; the allocation may never "
                               "have been dispatched (D-72)")
        elif state is None:
            log.error("admission_resync", engine="E2", job_id=entry.job_id, stage=stage, state=None, error=str(error))
        else:
            log.warning("admission_resync", engine="E2", job_id=entry.job_id, stage=stage, state=state,
                        error=str(error))
        return "left"

    def _cancel_if_oversized(self, head: QueueEntry, tally: _Tally) -> bool:
        """The FIFO head does not fit now: cancel it when it can never fit, because capacity shrank after
        it was submitted (D-116), so it cannot block the queue forever. True when it left the queue."""
        try:
            cancelled = retrying(partial(_cancel_oversized, head))
        except (IllegalTransition, NotFound) as exc:
            return self._resync(head, "cancel", exc) == "left"
        if cancelled:
            self.queue.remove(head.job_id)
            tally.cancelled += 1
        return cancelled

    def _settled(self) -> None:
        self._in_doubt = None
        self._doubt_logged = None

    def _end_cycle(self, outcome: str, tally: _Tally, waited_s: float, start: float) -> None:
        """The cycle line (D-119), for every cycle, failed ones included: held_ms measured each time,
        INFO when the cycle admitted or cancelled a job, its outcome changed, or it held the lock over
        SLOW_CYCLE_MS; DEBUG otherwise. The loop logs a failed cycle's error once per change."""
        held_ms = round((self._clock.monotonic() - start) * 1000, 1)
        doubt = self._in_doubt
        if doubt is not None and doubt.job_id != self._doubt_logged:
            self._doubt_logged = doubt.job_id
            log.warning("admission_in_doubt", engine="E2", job_id=doubt.job_id,
                        detail="admitted with no answer from allocate(); the next cycle calls allocate() first")
        changed = outcome != self._last_outcome
        self._last_outcome = outcome
        emit = (log.info if tally.admitted or tally.cancelled or changed or held_ms > SLOW_CYCLE_MS
                else log.debug)
        emit("admission_cycle", engine="E2", outcome=outcome, admitted=tally.admitted, cancelled=tally.cancelled,
             queued=len(self.queue), policy=self._policy, waited_ms=round(waited_s * 1000, 1), held_ms=held_ms)

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
        most = read_allocatable(t)
    return Refs(types, problem_found, session, most.cpu_millicores, most.ram_mib, most.vram_mib)


def read_allocatable(t: Tx) -> Vector:
    """The allocatable maxima (D-78): total - reserved of the cpu and the ram row and of the largest GPU,
    0 when there is none. This is E2's one read of E3's resources table (kind, total, reserved in
    db/schema.sql, plan 4.2): no port gives totals, only free capacity (message 3). RuntimeError when
    no cpu or ram row exists, that is when no capacity is loaded."""
    maxima: dict[str, int] = {kind: high for kind, high in t.execute(_MAXIMA).fetchall()}
    if "cpu" not in maxima or "ram" not in maxima:
        raise RuntimeError("no capacity loaded: resources has no cpu or ram row; run scripts/capacity.py")
    return Vector(maxima["cpu"], maxima["ram"], maxima.get("gpu", 0))


def _move(job_id: int, to_state: JobState, reason: str) -> None:
    """One move of a queued job in its own tx("E2"); expected="queued" makes it atomic (D-65)."""
    with tx("E2") as t:
        transition(t, job_id, to_state, "E2", reason, expected="queued")


def _cancel_oversized(entry: QueueEntry) -> bool:
    """In one tx("E2"): read the maxima and, only when entry can never fit, move it queued to cancelled
    (D-116). The log line is the first hook after COMMIT, so it never claims a cancel that failed."""
    with tx("E2") as t:
        most = read_allocatable(t)
        if not oversized(entry.vector, most):
            return False
        t.after_commit(partial(log.warning, "admission_oversized", engine="E2", job_id=entry.job_id,
                               cpu_millicores=entry.vector.cpu_millicores, ram_mib=entry.vector.ram_mib,
                               vram_mib=entry.vector.vram_mib, allocatable=[most.cpu_millicores, most.ram_mib,
                                                                            most.vram_mib]))
        transition(t, entry.job_id, "cancelled", "E2", OVERSIZED, expected="queued")
    return True


def _job_state(job_id: int) -> str | None:
    """The job's state now, None for no row; one read in tx("E2"), no lock."""
    with tx("E2") as t:
        row = t.execute(_STATE, (job_id,)).fetchone()
    return None if row is None else row[0]


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
