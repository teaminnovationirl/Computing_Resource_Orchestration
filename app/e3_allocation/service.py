"""E3 Transaction-Safe Resource Allocation (plan 6): snapshot() from slice E3-1 and allocate() from E3-2;
release() follows in E3-3.
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
from app.common.types import Committed, Limits, Refused, Snapshot, Vector
from app.e3_allocation.placement import Row, fit, snapshot_of
from app.e3_allocation.queries import (add_allocated, insert_allocation, insert_audit, insert_items, lock_resources,
                                       read_job, read_resources)
from app.e4_monitoring.state import transition

_CLOCK: Final = RealClock()


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
