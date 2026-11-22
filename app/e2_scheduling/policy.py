"""E2's admission policies (plan 8.3, context §10): pure functions on the queue and one snapshot.

They read the queue under its lock (E2's RLock, which run_cycle() already holds) and never call the
database or an engine; first_fit's predicate is Snapshot.fits on the cycle's snapshot (D-104).
"""
from __future__ import annotations

from typing import Literal

from app.common.types import QueueEntry, Snapshot, Vector
from app.e2_scheduling.queue import JobQueue

Policy = Literal["fcfs", "need"]
POLICIES: tuple[Policy, ...] = ("fcfs", "need")


def as_policy(name: str) -> Policy:
    """name as a Policy; ValueError for any other name, so a bad DEFAULT_POLICY never falls back silently."""
    for policy in POLICIES:
        if name == policy:
            return policy
    raise ValueError(f"unknown admission policy {name!r}; expected one of {', '.join(POLICIES)}")


def select_fcfs(q: JobQueue, snap: Snapshot) -> QueueEntry | None:
    """The oldest queued job if it fits, else None: the head waits and nobody overtakes it."""
    head = q.fifo_head()
    return head if head is not None and snap.fits(head.vector) else None


def select_need(q: JobQueue, snap: Snapshot, now: float, guard_s: float) -> QueueEntry | None:
    """Priority by need: once the oldest job has waited guard_s since its arrival (now is wall time,
    clock.time(); submitted_at is the database's arrival time), it goes first or nobody does (the 60 s
    no-overtaking guard, FR-01); before that, the first job in (need_class, submitted_at, job_id) order
    that fits."""
    oldest = q.fifo_head()
    if oldest is None:
        return None
    if now - oldest.submitted_at.timestamp() >= guard_s:
        return oldest if snap.fits(oldest.vector) else None
    return q.first_fit(lambda e: snap.fits(e.vector))


def oversized(v: Vector, most: Vector) -> bool:
    """True when v can never fit, even on an empty server: most holds the allocatable maxima, total -
    reserved of the cpu and ram rows and of the largest GPU (0 when there is none, D-78)."""
    return v.cpu_millicores > most.cpu_millicores or v.ram_mib > most.ram_mib or v.vram_mib > most.vram_mib
