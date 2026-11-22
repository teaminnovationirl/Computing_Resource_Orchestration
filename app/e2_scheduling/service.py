"""E2 service (plan 8.1 with D-77, D-78 and D-80): submit() and the queue (E2-2, D-103 to D-105), the
admission cycle run_cycle() with capacity_changed() (E2-3, plan 8.3, D-116 to D-119), and requeue,
cancel_queued, pause and resume, set_policy, rebuild, the resync and queue_view (E2-4, plan 8.4, D-121 to
D-127), all on one E2Service.

submit() reads what validation needs in one transaction, validates, writes the source under
DATA_DIR/payloads/<uuid4 hex>/ and inserts the queued job in a second tx("E2"). The state trigger
writes the first history row; the queue push, the job_state event and the wake of the admission loop
happen only after COMMIT (invariant 3).
"""
from __future__ import annotations

import shutil
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Final

from app.common.clock import Clock, RealClock
from app.common.db import Tx, assert_outside_tx, retrying, tx
from app.common.errors import ApiError, IllegalTransition, NotFound
from app.common.logging import log
from app.common.ports import engines
from app.common.types import Committed, QueueEntry, Refused, Snapshot, SubmitRequest, SubmitResult, User, Vector
from app.config import Config
from app.e2_scheduling.policy import Policy, as_policy, in_policy_order, oversized, select_fcfs, select_need
from app.e2_scheduling.queue import JobQueue
from app.e2_scheduling.validation import Accepted, FileName, Refs, Session, WorkloadType, validate
from app.e4_monitoring.state import JobState, clear_actor, set_actor, transition

_CLOCK: Final = RealClock()
SLOW_CYCLE_MS: Final = 100.0  # a cycle that holds E2's lock longer logs at INFO (proposal, D-119)
OVERSIZED: Final = "larger than the server's capacity"  # the reason of the cancel of D-116
RESYNC_S: Final = 30.0  # the periodic resync of D-106 (proposal): how often run_cycle() looks for orphans
_STATE: Final = "SELECT state FROM jobs WHERE job_id = %s"
_LOCK_STATE: Final = "SELECT state FROM jobs WHERE job_id = %s FOR UPDATE"
_LOCK_JOB: Final = ("SELECT state, retry_count, user_id, need_class, submitted_at, cpu_millicores, ram_mib, vram_mib "
                    "FROM jobs WHERE job_id = %s FOR UPDATE")
_COUNT_RETRY: Final = "UPDATE jobs SET retry_count = retry_count + 1 WHERE job_id = %s"
_QUEUED: Final = ("SELECT job_id, user_id, need_class, submitted_at, cpu_millicores, ram_mib, vram_mib FROM jobs "
                  "WHERE state = 'queued' ORDER BY submitted_at, job_id")
_CANCELLED: Final = "SELECT job_id FROM jobs WHERE job_id = ANY(%s) AND state = 'cancelled' ORDER BY job_id"
_REACHED: Final = "_reached_commit"  # set in a log line's dict once a transaction body is done (D-121)
_Admission = tuple[bool, str | None, Policy, int | None]  # paused, reason, policy, in_doubt
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
        self._paused = False  # pause_admission() and resume_admission() set and clear it under the lock
        self._pause_reason: str | None = None
        # The job whose admit or allocate() raised: admitted, perhaps not, or perhaps allocated with the
        # answer lost. The next cycle calls allocate() for it before anything else (D-117).
        self._in_doubt: QueueEntry | None = None
        self._doubt_logged: int | None = None  # the job_id admission_in_doubt was logged for
        self._last_outcome: str | None = None  # of the previous cycle, for the INFO rule of D-119
        self._next_resync = self._clock.monotonic() + RESYNC_S  # D-106; rebuild() restarts it
        self._cycle_thread: int | None = None  # the thread inside run_cycle(), so a hook re-entering E2 fails loudly
        # The admission state the last admission event carried (the start state is not sent), and the state
        # whose failed push was last logged, so a hub that is down is logged once per state (D-123).
        self._shown: _Admission = self._admission_state()
        self._failed_state: _Admission | None = None

    def submit(self, user: User, req: SubmitRequest, source: bytes) -> SubmitResult:
        """Validate req and source and queue the job (message 2). A refused request raises ApiError(400,
        error, field) with no job, no payload and no event. Any other failure before COMMIT removes the
        payload folder and re-raises; a failure at COMMIT keeps it, because the job may be committed,
        and logs submit_commit_unknown. RuntimeError when no capacity is loaded. After COMMIT, in this
        order: the submit log line, the queue push, the job_state event (D-105), the wake of the
        admission loop.

        E2's lock is held only around the insert transaction and those hooks (D-125), so no resync, cancel
        or cycle runs between the COMMIT and the push; reading, validating and writing the payload happen
        before it is taken, so a large upload never holds the admission loop or another submit."""
        assert_outside_tx("e2.submit")
        start = _CLOCK.monotonic()
        refs = _read_refs(req)
        try:
            accepted = validate(req, source, refs, self._config)
        except ApiError as refusal:
            log.info("submit_refused", engine="E2", user_id=user.user_id, field=refusal.field, error=refusal.error)
            raise
        folder, payload_path = self._write_payload(source, accepted.file_name)
        reached_commit = False

        def insert_once(asked: float, locked: float) -> int:
            nonlocal reached_commit
            reached_commit = False
            with tx("E2") as t:
                job_id, submitted_at = insert_job(t, user.user_id, req, accepted.need_class, payload_path)
                event = {"type": "job_state", "job_id": job_id, "user_id": user.user_id, "state": "queued",
                         "reason": "submitted", "at": submitted_at.isoformat()}
                t.after_commit(partial(_log_submit, job_id, user.user_id, req.workload_type, accepted, start, asked,
                                       locked))
                vector = Vector(req.cpu_millicores, req.ram_mib, req.vram_mib)
                t.after_commit(partial(self.queue.push,
                                       QueueEntry(job_id, user.user_id, accepted.need_class, submitted_at, vector)))
                t.after_commit(partial(_publish, event))
                t.after_commit(self.wake.set)
                reached_commit = True  # the body is done: what fails from here on is COMMIT itself
            return job_id

        try:
            asked = _CLOCK.monotonic()
            with self.lock:  # the insert and its hooks only (D-125)
                job_id = retrying(partial(insert_once, asked, _CLOCK.monotonic()))
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

    def requeue(self, job_id: int) -> bool:
        """Message 11 (plan 8.4, FR-14): a released job goes back in the queue, at its arrival place, since
        its submitted_at never changes (invariant 8). True once it is queued again; False when its retries
        are used up (retry_count reached max_retries): it stays released and nothing is written. NotFound for
        an unknown job and IllegalTransition for one that is not released, nothing written. A raise at
        COMMIT may leave the job committed as queued but never pushed: the resync pushes it (D-106)."""
        with self._holding("requeue", warn_on=("retries_used",), job_id=job_id) as line:
            line.update(retrying(partial(self._requeue_once, job_id, line)))
        return line["outcome"] == "requeued"

    def cancel_queued(self, job_id: int, by: str) -> bool:
        """Message 11 (plan 8.4): cancel a job that is queued, decided by its locked row, not by the queue
        (D-122): a job whose COMMIT acknowledgement was lost is queued in the database but missing from
        memory, and it is cancelled too. Nothing was held, so nothing is released. True once cancelled.

        IllegalTransition, nothing written and the queue and the slot of D-117 untouched, for a job that reads
        admitted: the job in doubt, or one whose admit move lost its answer. No engine can cancel it until the
        next cycle settles it (there is no admitted to cancelled move, and E1's cancel acts only on allocated
        or running jobs), so the caller answers "try again" (E4-3: 409). False, nothing written, for any other
        state: E4 then tries E1 (plan 8.4). NotFound for an unknown job."""
        with self._holding("cancel_queued", job_id=job_id, by=by) as line:
            line.update(retrying(partial(self._cancel_once, job_id, by, line)))
        return line["outcome"] == "cancelled"

    def pause_admission(self, reason: str) -> None:
        """Message 11: stop admitting (FR-04: while the host is unhealthy, or by an administrator). Once this
        returns no cycle admits a job: it waits for a cycle in progress, and each cycle checks the flag
        first. Running jobs are not touched. Publishes the admission event."""
        with self._holding("admission_paused", reason=reason) as line:
            line.update(outcome="paused", was_paused=self._paused)
            self._paused, self._pause_reason = True, reason
            self._publish_admission()

    def resume_admission(self) -> None:
        """Message 11: admit again, at once (wake). Publishes the admission event."""
        with self._holding("admission_resumed") as line:
            line.update(outcome="resumed", was_paused=self._paused)
            self._paused, self._pause_reason = False, None
            self._publish_admission()
            self.wake.set()

    def set_policy(self, policy: Policy) -> None:
        """Admission by fcfs or need from the next cycle on, which is woken at once; kept in memory, the
        default from DEFAULT_POLICY (plan 8.4). ValueError for any other name, with nothing changed.
        Publishes the admission event."""
        with self._holding("admission_policy", refusals=(ValueError,), policy=policy) as line:
            chosen = as_policy(policy)
            line.update(outcome="set", previous=self._policy)
            self._policy = chosen
            self._publish_admission()
            self.wake.set()

    def rebuild(self) -> int:
        """At startup, after E4's reconciliation (plan 10.7) and before the API takes submits: push every
        queued job in (submitted_at, job_id) order, so each push is an O(1) append (plan 8.4). A job already
        in the queue is skipped, so a second run pushes nothing; the resync runs the same code. Returns how
        many it pushed; restarts the resync timer and wakes the loop."""
        with self._holding("queue_rebuilt") as line:
            pushed, dropped = self._push_missing()
            line.update(outcome="rebuilt", pushed=len(pushed), dropped=len(dropped), queued=len(self.queue))
            self.wake.set()
        return len(pushed)

    def queue_view(self) -> list[QueueEntry]:
        """Every queued job in the order the next cycles try them (plan 8.4, D-126): the job in doubt first,
        since the next cycle retries it before any other (D-117), then the active policy's order with its
        60 s rule. No database read, so a job whose admit move lost its answer keeps its place until its
        next pick finds it admitted. Each entry carries the need class and the vector; E4-3 derives the
        position (index + 1) and the wait (clock.time() - submitted_at)."""
        assert_outside_tx("e2.queue_view")
        with self.lock:
            order = in_policy_order(self.queue, self._policy, self._clock.time(), self._config.starvation_guard_s)
            doubt = self._in_doubt
            if doubt is None or doubt.job_id not in self.queue:
                return order
            return [doubt, *(e for e in order if e.job_id != doubt.job_id)]

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
            self._cycle_thread = threading.get_ident()
            start = self._clock.monotonic()
            tally = _Tally()
            outcome = "failed"
            try:
                outcome = self._cycle(tally)
            finally:
                self._cycle_thread = None
                self._end_cycle(outcome, tally, start - asked, start)

    def _cycle(self, tally: _Tally) -> str:
        if self._clock.monotonic() >= self._next_resync:
            self._resync_queue()  # not admission, so it runs while paused; an orphan it pushes is admitted below
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

    @contextmanager
    def _holding(self, event: str, refusals: tuple[type[Exception], ...] = (NotFound, IllegalTransition),
                 warn_on: tuple[str, ...] = (), **fields: object) -> Iterator[dict[str, object]]:
        """E2's lock for one public call (D-104). Refused inside a transaction (a caller holding row locks must
        never wait for it) and inside an admission cycle on the cycle's own thread (a hook re-entering the RLock
        would act on jobs the cycle still holds); taken before any tx() and held to the end, hooks included.
        The body fills the yielded line, and one line is written however the call ends, with waited_ms and
        held_ms as run_cycle() writes them: INFO (WARNING for an outcome in warn_on); WARNING with outcome
        refused for one of refusals, the caller's mistake; ERROR with the traceback for anything else, outcome
        commit_unknown when the body had finished (the failure came at COMMIT, so the move may have committed)
        or failed. The error goes on to the caller."""
        assert_outside_tx(f"e2.{event}")
        if self._cycle_thread == threading.get_ident():
            raise RuntimeError(f"e2.{event} called inside an admission cycle on its own thread (from a hook such as "
                               "dispatch): call it once the cycle has returned (D-104)")
        asked = self._clock.monotonic()
        with self.lock:
            start = self._clock.monotonic()
            line: dict[str, object] = dict(fields)

            def ended(**outcome: object) -> dict[str, object]:
                done = {key: value for key, value in line.items() if key != _REACHED}
                return {**done, **outcome, "waited_ms": _ms(start - asked),
                        "held_ms": _ms(self._clock.monotonic() - start)}

            try:
                yield line
            except refusals as exc:
                log.warning(event, engine="E2", **ended(outcome="refused", error=f"{type(exc).__name__}: {exc}"))
                raise
            except BaseException as exc:
                outcome = "commit_unknown" if line.get(_REACHED) else "failed"
                log.exception(event, engine="E2", **ended(outcome=outcome, error=f"{type(exc).__name__}: {exc}"))
                raise
            emit = log.warning if line.get("outcome") in warn_on else log.info
            emit(event, engine="E2", **ended())

    def _requeue_once(self, job_id: int, line: dict[str, object]) -> dict[str, object]:
        """One tx("E2") of requeue(). The job row stays locked until COMMIT, so its state, retry_count and
        submitted_at cannot change under the decision; the push comes before the job_state event (D-105)."""
        line[_REACHED] = False
        with tx("E2") as t:
            row = t.execute(_LOCK_JOB, (job_id,)).fetchone()
            if row is None:
                raise NotFound(f"job {job_id}")
            state, retries, user_id, need_class, submitted_at, cpu, ram, vram = row
            if state != "released":
                raise IllegalTransition(f"job {job_id} is {state}, not released: only a released job is requeued")
            if retries >= self._config.max_retries:
                return {"outcome": "retries_used", "retry_count": retries}
            t.execute(_COUNT_RETRY, (job_id,))
            entry = QueueEntry(job_id, user_id, need_class, submitted_at, Vector(cpu, ram, vram))
            t.after_commit(partial(self.queue.push, entry))
            transition(t, job_id, "queued", "E2", f"requeue {retries + 1} of {self._config.max_retries}",
                       expected="released")
            t.after_commit(self.wake.set)
            line[_REACHED] = True  # the body is done: what fails from here on is COMMIT itself
        return {"outcome": "requeued", "retry_count": retries + 1}

    def _cancel_once(self, job_id: int, by: str, line: dict[str, object]) -> dict[str, object]:
        """One tx("E2") of cancel_queued(): lock the job row and move it only if it reads queued. The job
        leaves the queue before its job_state event, so a page that refetches on the event misses it."""
        line[_REACHED] = False
        with tx("E2") as t:
            row = t.execute(_LOCK_STATE, (job_id,)).fetchone()
            if row is None:
                raise NotFound(f"job {job_id}")
            if row[0] == "admitted":
                raise IllegalTransition(f"job {job_id} is being admitted: no engine can cancel it until its admission "
                                        "settles, so try again (D-122)")
            if row[0] != "queued":
                return {"outcome": "not_queued", "state": row[0]}
            t.after_commit(partial(self._forget, job_id))
            transition(t, job_id, "cancelled", "E2", f"cancelled by {by}", expected="queued")
            t.after_commit(self.wake.set)  # the head may have been what blocked the queue
            line[_REACHED] = True  # the body is done: what fails from here on is COMMIT itself
        return {"outcome": "cancelled", "state": "queued"}

    def _forget(self, job_id: int) -> None:
        """After a cancel's COMMIT: out of the queue, if a push ever brought it there, and out of the slot of
        D-117 if it was the job in doubt (it read queued, so E3's refusal had committed)."""
        self.queue.remove(job_id)
        if self._in_doubt is not None and self._in_doubt.job_id == job_id:
            self._settled()
            self._show_admission()

    def _push_missing(self) -> tuple[list[int], list[int]]:
        """Under E2's lock, make the queue match the queued rows (D-124): push every job queued in the database
        but missing from the queue, in (submitted_at, job_id) order, and drop every job it holds that reads
        cancelled (E2's own cancel whose COMMIT acknowledgement was lost, so _forget never ran; only E2
        cancels a queued job, and under this lock). A job that reads admitted or allocated stays: D-117 settles
        it. One tx("E2") with no row lock, then the changes; every move into or out of queued holds this
        lock, so no row read changes before it is acted on (D-104). Restarts the resync timer. Returns
        (pushed, dropped)."""
        self._next_resync = self._clock.monotonic() + RESYNC_S
        with tx("E2") as t:
            rows = t.execute(_QUEUED).fetchall()
            queued_ids = {row[0] for row in rows}
            stale = sorted(e.job_id for e in self.queue.in_fifo_order() if e.job_id not in queued_ids)
            dropped: list[int] = [row[0] for row in t.execute(_CANCELLED, (stale,)).fetchall()] if stale else []
        for job_id in dropped:
            self._forget(job_id)
        pushed: list[int] = []
        for job_id, user_id, need_class, submitted_at, cpu, ram, vram in rows:
            if job_id not in self.queue:
                self.queue.push(QueueEntry(job_id, user_id, need_class, submitted_at, Vector(cpu, ram, vram)))
                pushed.append(job_id)
        return pushed, dropped

    def _resync_queue(self) -> list[int]:
        """The periodic resync of D-106, run by run_cycle() under E2's lock once RESYNC_S has passed, paused or
        not: _push_missing(). A warning names the jobs it pushed or dropped; a run that changed nothing writes
        a DEBUG line. Returns the jobs it pushed."""
        start = self._clock.monotonic()
        pushed, dropped = self._push_missing()
        ms = _ms(self._clock.monotonic() - start)
        if pushed or dropped:
            log.warning("queue_resync", engine="E2", pushed=pushed, dropped=dropped, queued=len(self.queue), ms=ms,
                        detail="the queue and the queued rows disagreed: a lost COMMIT acknowledgement, or a push "
                               "or remove hook that failed (after_commit_failed)")
        else:
            log.debug("queue_resync", engine="E2", pushed=pushed, dropped=dropped, queued=len(self.queue), ms=ms)
        return pushed

    def _admission_state(self) -> _Admission:
        return (self._paused, self._pause_reason, self._policy,
                None if self._in_doubt is None else self._in_doubt.job_id)

    def _publish_admission(self) -> None:
        """The admission event (plan 8.4, D-123) with the whole admission state, so a page can draw it from any
        one event: paused, the pause reason, the policy, the job admission is halted behind (D-117) and the
        wall time. Sent under E2's lock, outside any transaction, so events leave in the order of the changes.
        A failed push is not raised (as D-47): the change stands, and in run_cycle()'s finally a raise would
        replace the cycle's own error. It is logged with its traceback once per state, and the state counts
        as sent only once a push succeeds, so _show_admission() sends it again (nothing else serves it)."""
        state = self._admission_state()
        paused, reason, policy, in_doubt = state
        try:
            engines().e4.publish({"type": "admission", "paused": paused, "reason": reason, "policy": policy,
                                  "in_doubt": in_doubt, "at": datetime.fromtimestamp(self._clock.time(), UTC).isoformat()})
        except Exception:  # noqa: BLE001 (logged with its traceback; a failed push must not undo the change or hide a cycle's error, D-123)
            if state != self._failed_state:
                self._failed_state = state
                log.exception("admission_event_failed", engine="E2", paused=paused, reason=reason, policy=policy,
                              in_doubt=in_doubt, detail="sent again at the end of each cycle until a push succeeds")
            return
        self._shown, self._failed_state = state, None

    def _show_admission(self) -> None:
        """At the end of each cycle, and when a cancel settles the job in doubt: publish the admission state if
        it differs from the last one pushed, so the job in doubt going in or settling (an administrator sees
        admission halted behind it) and any change whose event failed reach the pages."""
        if self._admission_state() != self._shown:
            self._publish_admission()

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
        self._show_admission()  # never raises: a failed push is logged (D-123)

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


def _log_submit(job_id: int, user_id: str, type_code: str, accepted: Accepted, start: float, asked: float,
                locked: float) -> None:
    """The first hook after COMMIT (D-80): ms since submit() began, waited_ms for E2's lock and held_ms from
    taking it to COMMIT, retries included; the push, the event and the wake that follow are not counted."""
    now = _CLOCK.monotonic()
    log.info("submit", engine="E2", job_id=job_id, user_id=user_id, type_code=type_code,
             need_class=accepted.need_class, file=accepted.file_name, ms=_ms(now - start),
             waited_ms=_ms(locked - asked), held_ms=_ms(now - locked))


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


def _publish(event: dict[str, Any]) -> None:
    engines().e4.publish(event)


def _remove(folder: Path) -> None:
    try:
        shutil.rmtree(folder)
    except OSError as exc:
        log.warning("payload_cleanup_failed", engine="E2", path=str(folder), error=str(exc))
