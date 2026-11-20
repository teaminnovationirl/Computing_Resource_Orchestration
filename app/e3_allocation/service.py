"""E3 Transaction-Safe Resource Allocation (plan 6): snapshot() from slice E3-1, allocate() from E3-2
and release() from E3-3.
"""
from __future__ import annotations

from datetime import datetime
from functools import partial
from typing import Any, Final

from app.common.clock import RealClock
from app.common.db import retrying, tx
from app.common.errors import IllegalTransition, NotFound
from app.common.logging import log
from app.common.ports import engines
from app.common.types import AlreadyReleased, Committed, Limits, Refused, Released, Snapshot, Vector
from app.e3_allocation.placement import Row, fit, snapshot_of
from app.e3_allocation.queries import (add_allocated, close_allocation, insert_allocation, insert_audit, insert_items,
                                       insert_release_audit, lock_open_allocation, lock_resources, read_items,
                                       read_job, read_resources, subtract_allocated)
from app.e4_monitoring.state import JobState, transition

_CLOCK: Final = RealClock()
# The states that may move to released (plan 7.2): the job has ended and its stop is confirmed.
_RELEASABLE: Final[tuple[JobState, ...]] = ("completed", "failed", "cancelled")


def snapshot() -> Snapshot:
    """Free capacity now (message 3, E3Port.snapshot): one read without a lock in one tx("E3"). It can be
    stale by the time E2 uses it; allocate() re-checks inside its row locks, which makes that harmless.
    Raises ValueError when the cpu or the ram row is missing, that is when no capacity is loaded."""
    with tx("E3") as t:
        rows = read_resources(t)
    return snapshot_of(rows)


def allocate(job_id: int, vector: Vector) -> Committed | Refused:
    """Message 4 (E3Port.allocate): give job job_id the resources of vector, or refuse, in one tx("E3").

    The resource ids come from read_resources() (D-68). One lock statement then locks CPU, RAM and,
    when vector.vram_mib > 0, every GPU, in resource_id order (invariant 2), and fit() decides on the
    locked rows only, the best-fit GPU included. Committed: the capacity, the allocation, its items,
    the audit row and the move admitted to allocated commit together, and the dispatch to E1 and the
    capacity event leave after COMMIT (invariant 3). Refused (D-01): the refused audit row and the move
    back to queued commit, nothing else changes, and the job keeps its submitted_at.

    Raises, with nothing written: NotFound for a job that does not exist; IllegalTransition for a job
    that is not admitted, which is how a repeated call ends (D-70); ValueError when no capacity is
    loaded (D-69). A deadlock or a serialization failure replays the whole transaction, 3 tries in all
    (D-55), and the hooks of a try that rolled back never run. A dispatch that fails after COMMIT does
    not undo it: tx() logs the failure, and E1 or the startup reconciliation ends the job (D-47, D-72)."""
    return retrying(partial(_allocate_once, job_id, vector))


def _allocate_once(job_id: int, vector: Vector) -> Committed | Refused:
    """One try of allocate(): exactly one transaction, so that retrying() can replay it whole. Each
    outcome registers its log line before transition() registers the job_state event, so the line is
    the first hook after COMMIT."""
    with tx("E3") as t:
        start = _CLOCK.monotonic()
        ids = [r.resource_id for r in read_resources(t) if r.kind != "gpu" or vector.vram_mib > 0]
        rows = lock_resources(t, ids)
        # Read after the lock: every allocate() locks the CPU row before this read, so a repeat of the
        # call waits here until the first one commits and then sees its state (D-70). The job row is
        # not locked yet: transition() locks it last.
        job = read_job(t, job_id)
        if job is None:
            raise NotFound(f"job {job_id}")
        if job.state != "admitted":
            raise _not_admitted(job_id, job.state)
        plan = fit(rows, vector)  # the re-check inside the lock
        result: Committed | Refused
        if plan is None:
            insert_audit(t, job_id, None, "refused")
            t.after_commit(partial(_log_allocate, job_id, start, "refused", None, None))
            transition(t, job_id, "queued", "E3", "refused: capacity taken first", expected="admitted")
            result = Refused("insufficient capacity")
        else:
            items, gpu_index = plan
            add_allocated(t, items)
            allocation_id = insert_allocation(t, job_id)
            insert_items(t, allocation_id, items)
            insert_audit(t, job_id, allocation_id, "committed")
            t.after_commit(partial(_log_allocate, job_id, start, "committed", allocation_id, gpu_index))
            at = transition(t, job_id, "allocated", "E3", "allocation committed", expected="admitted")
            limits = Limits(vector.cpu_millicores, vector.ram_mib, gpu_index, job.time_limit_s)
            # Partials, not lambdas: tx() logs a failed hook by its repr, which then names the job (D-47).
            t.after_commit(partial(_dispatch, job_id, allocation_id, limits, job.payload_path))
            t.after_commit(partial(_publish, _capacity_event(read_resources(t), at)))
            result = Committed(allocation_id, gpu_index)
    return result


def _log_allocate(job_id: int, start: float, outcome: str, allocation_id: int | None, gpu_index: int | None) -> None:
    """The allocate line (plan 5.5), the first hook after COMMIT: it never claims a commit that did not
    happen, a dispatch that hangs cannot hide it, and ms is the transaction, lock waits and COMMIT
    included."""
    log.info("allocate", engine="E3", job_id=job_id, outcome=outcome, allocation_id=allocation_id,
             gpu_index=gpu_index, ms=round((_CLOCK.monotonic() - start) * 1000, 1))


def _not_admitted(job_id: int, state: str) -> IllegalTransition:
    """The answer for a job that is not admitted, a repeated call among them (D-70). Logged like every
    refused move: it is the caller's case, not a fault."""
    message = f"job {job_id} is {state}, not admitted: allocate() changed nothing"
    log.warning("illegal_transition", job_id=job_id, to="allocated", actor="E3", error=message)
    return IllegalTransition(message)


def release(job_id: int) -> Released | AlreadyReleased:
    """Messages 7 and 10 (E3Port.release): give back what job job_id holds, in one tx("E3"). Callers
    call it only once the job's processes are stopped and the stop confirmed (invariant 4), and after
    moving the job to completed, failed or cancelled: E1 after its own stop, E4 after e1.stop().

    Locks the job's open allocation first, then the job's resource rows with the one lock statement in
    resource_id order, and the job row last, inside transition() (invariant 2). Released: the capacity
    given back, the allocation closed, the release audit row and the move to released commit together;
    after COMMIT the release log line, the job_state event, E2's capacity_changed (message 8) and the
    capacity event leave in that order (invariant 3, D-71, D-75).

    AlreadyReleased, with nothing written and nothing sent but the log line: the job holds no open
    allocation, either because it was released already (a repeat, or the second of two calls at once,
    which waits on the allocation and then finds it closed) or because it never held one (a job
    cancelled while queued ends cancelled, context §9).

    Raises, with nothing written (D-74): NotFound for a job that does not exist; IllegalTransition for
    a job that holds an allocation but is not completed, failed or cancelled, that is a job whose stop
    is not confirmed. A deadlock or a serialization failure replays the whole transaction, 3 tries in
    all (D-55), and the hooks of a try that rolled back never run."""
    return retrying(partial(_release_once, job_id))


def _release_once(job_id: int) -> Released | AlreadyReleased:
    """One try of release(): exactly one transaction, so that retrying() can replay it whole. The log
    line is registered before transition() registers the job_state event, so it is the first hook
    after COMMIT."""
    with tx("E3") as t:
        start = _CLOCK.monotonic()
        allocation_id = lock_open_allocation(t, job_id)  # lock 1
        # Read after lock 1: only release() moves a job out of completed, failed or cancelled, and it
        # holds lock 1 while it does, so the state read here still holds at transition(). The job row
        # is not locked: transition() locks it last.
        job = read_job(t, job_id)
        if job is None:
            raise NotFound(f"job {job_id}")
        result: Released | AlreadyReleased
        if allocation_id is None:
            t.after_commit(partial(_log_release, job_id, start, None, job.state))
            result = AlreadyReleased()
        else:
            state = job.state
            if state not in _RELEASABLE:
                raise _not_releasable(job_id, state)
            items = read_items(t, allocation_id)
            lock_resources(t, [resource_id for resource_id, _ in items])  # lock 2, in resource_id order
            subtract_allocated(t, items)
            close_allocation(t, allocation_id)
            insert_release_audit(t, job_id, allocation_id)
            t.after_commit(partial(_log_release, job_id, start, allocation_id, state))
            at = transition(t, job_id, "released", "E3", "release committed", expected=state)
            t.after_commit(partial(_capacity_changed, job_id))
            t.after_commit(partial(_publish, _capacity_event(read_resources(t), at)))
            result = Released(allocation_id)
    return result


def _log_release(job_id: int, start: float, allocation_id: int | None, state: str) -> None:
    """The release line (plan 5.5), the first hook after COMMIT, so it never claims a release that did
    not commit and a slow notice cannot hide it; ms is the transaction, lock waits and COMMIT
    included. allocation_id None means AlreadyReleased. state is the job's state as release() found
    it: the state it left, or for AlreadyReleased released after a repeat and any other state for a
    job that held nothing, which a caller expecting a release should look into (D-75)."""
    log.info("release", engine="E3", job_id=job_id,
             outcome="already_released" if allocation_id is None else "released",
             allocation_id=allocation_id, state=state, ms=round((_CLOCK.monotonic() - start) * 1000, 1))


def _not_releasable(job_id: int, state: str) -> IllegalTransition:
    """The answer for a job that holds an allocation but has not ended, so its stop is not confirmed
    (invariant 4, D-74). Logged like every refused move: it is the caller's case, not a fault."""
    message = f"job {job_id} is {state}, not completed, failed or cancelled: release() changed nothing"
    log.warning("illegal_transition", job_id=job_id, to="released", actor="E3", error=message)
    return IllegalTransition(message)


def _capacity_changed(job_id: int) -> None:
    """Message 8, after COMMIT, on the thread that called release(): E2 only wakes its admission loop
    and returns (plan 8.4). job_id is for the hook's repr only: a failure is logged by it, and the line
    then names the job (D-47, D-72); the release stands and E2's 0.5 s cycle still finds the capacity."""
    engines().e2.capacity_changed()


def _dispatch(job_id: int, allocation_id: int, limits: Limits, file_ref: str) -> None:
    """Message 5, after COMMIT. A failure is E1's to end; tx() logs it and the commit stands (D-47, D-72)."""
    engines().e1.dispatch(job_id, allocation_id, limits, file_ref)


def _publish(event: dict[str, Any]) -> None:
    """Message 12, after COMMIT."""
    engines().e4.publish(event)


def _capacity_event(rows: list[Row], at: datetime) -> dict[str, Any]:
    """The capacity event (plan 10.4, D-71): every resources row as this transaction commits it, read
    under its lock on the CPU row, which every writer of resources takes, and the database time of the
    job's move."""
    return {"type": "capacity", "resources": [r._asdict() for r in rows], "at": at.isoformat()}
