"""E2's job queue (plan 8.2, D-103): every queued job lives in three structures that never disagree.

- map:  job_id -> (QueueEntry, version): O(1) lookup, cancel and requeue.
- heap: min-heap of ((need_class, submitted_at, job_id), version, entry): priority by need.
- fifo: list of ((submitted_at, job_id), version, entry), kept sorted and read from a head index: FCFS.

remove() drops only the map entry. The job's heap and FIFO items go stale (their version is no longer the
map's) and are skipped when reached, or dropped by a compaction once more than 64 removals since the last
one outnumber the live jobs. Every push draws a new version, so the stale items of a requeued job never
count as live, and tuples are decided by the version before a QueueEntry would be compared.

Every method holds E2's lock (the RLock of E2Service, D-104), and none calls the database or an engine.
"""
from __future__ import annotations

import bisect
import heapq
import itertools
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Final

from app.common.types import QueueEntry

COMPACT_AFTER: Final = 64  # removals before a compaction may run (plan 8.2; proposal, D-103)

_HeapItem = tuple[tuple[int, datetime, int], int, QueueEntry]
_FifoItem = tuple[tuple[datetime, int], int, QueueEntry]


# Keys use UTC: two times in one zone compare by wall clock, ignoring fold, so in a daylight-saving
# fall-back hour of the session's time zone a later job would sort first.
def _need_key(e: QueueEntry) -> tuple[int, datetime, int]:
    return e.need_class, e.submitted_at.astimezone(UTC), e.job_id


def _arrival_key(e: QueueEntry) -> tuple[datetime, int]:
    return e.submitted_at.astimezone(UTC), e.job_id


class JobQueue:
    """The queued jobs in arrival order (FCFS) and in need order (priority by need)."""

    def __init__(self, lock: AbstractContextManager[object] | None = None) -> None:
        """lock is E2's RLock, which E2Service passes (D-104); a queue on its own gets a new RLock."""
        self._lock: AbstractContextManager[object] = lock if lock is not None else threading.RLock()
        self._map: dict[int, tuple[QueueEntry, int]] = {}
        self._heap: list[_HeapItem] = []
        self._fifo: list[_FifoItem] = []
        self._head = 0  # self._fifo[:self._head] holds stale items only
        self._dead = 0  # removals since the last compaction
        self._versions = itertools.count(1)

    def __len__(self) -> int:
        with self._lock:
            return len(self._map)

    def __contains__(self, job_id: int) -> bool:
        with self._lock:
            return job_id in self._map

    def push(self, entry: QueueEntry) -> None:
        """Queue a new job, or requeue one with its original entry: same submitted_at, so it goes back to
        its arrival place (invariant 8). O(1) map, O(log n) heap, O(1) list append; a job older than the
        newest one (a requeue or a late COMMIT) goes in arrival order: O(log n) search, O(n) shift.
        ValueError, with nothing changed, for a job already queued or a submitted_at without a time zone
        (comparing it would fail halfway through the heap)."""
        with self._lock:
            if entry.submitted_at.utcoffset() is None:
                raise ValueError(f"job {entry.job_id}: submitted_at has no time zone")
            if entry.job_id in self._map:
                raise ValueError(f"job {entry.job_id} is already queued")
            version = next(self._versions)
            self._map[entry.job_id] = (entry, version)
            heapq.heappush(self._heap, (_need_key(entry), version, entry))
            item = (_arrival_key(entry), version, entry)
            if self._head == len(self._fifo) or item >= self._fifo[-1]:
                self._fifo.append(item)
            else:
                bisect.insort(self._fifo, item, lo=self._head)

    def remove(self, job_id: int) -> QueueEntry | None:
        """Take a job out: a cancel, or a job that leaves once allocate() returned Committed (E2-3). O(1):
        its heap and FIFO items go stale. None when the job is not queued."""
        with self._lock:
            slot = self._map.pop(job_id, None)
            if slot is None:
                return None
            self._dead += 1
            if self._dead > COMPACT_AFTER and self._dead > len(self._map):
                self._compact()
            return slot[0]

    def fifo_head(self) -> QueueEntry | None:
        """The oldest queued job (FCFS head). Amortised O(1): each stale item is skipped once."""
        with self._lock:
            while self._head < len(self._fifo):
                _, version, entry = self._fifo[self._head]
                if self._live(entry.job_id, version):
                    return entry
                self._head += 1
            return None

    def need_head(self) -> QueueEntry | None:
        """The first job in (need_class, submitted_at, job_id) order. Amortised O(log n): each stale item
        at the top is popped once."""
        with self._lock:
            while self._heap:
                _, version, entry = self._heap[0]
                if self._live(entry.job_id, version):
                    return entry
                heapq.heappop(self._heap)
            return None

    def first_fit(self, fits: Callable[[QueueEntry], bool]) -> QueueEntry | None:
        """The first job in need order for which fits() holds, or None; every job stays queued. Pops past
        the k live jobs that do not fit and pushes them back, also when fits() raises: O(k log n); stale
        items met on the way are dropped. fits() runs under E2's lock and must not call the database or
        an engine (E2-3 passes Snapshot.fits on the cycle's snapshot)."""
        with self._lock:
            popped: list[_HeapItem] = []
            try:
                while self._heap:
                    item = heapq.heappop(self._heap)
                    _, version, entry = item
                    if not self._live(entry.job_id, version):
                        continue
                    popped.append(item)
                    if fits(entry):
                        return entry
                return None
            finally:
                for item in popped:
                    heapq.heappush(self._heap, item)

    def in_fifo_order(self) -> list[QueueEntry]:
        """Every queued job in (submitted_at, job_id) order: O(n) plus the stale items."""
        with self._lock:
            return [entry for _, version, entry in itertools.islice(self._fifo, self._head, None)
                    if self._live(entry.job_id, version)]

    def in_need_order(self) -> list[QueueEntry]:
        """Every queued job in (need_class, submitted_at, job_id) order: O(n log n)."""
        with self._lock:
            return sorted((entry for entry, _ in self._map.values()), key=_need_key)

    def _live(self, job_id: int, version: int) -> bool:
        slot = self._map.get(job_id)
        return slot is not None and slot[1] == version

    def _compact(self) -> None:
        """Drop every stale item, O(n); it runs after more than max(64, n) removals, so O(1) amortised."""
        self._fifo = [item for item in itertools.islice(self._fifo, self._head, None)
                      if self._live(item[2].job_id, item[1])]
        self._heap = [item for item in self._heap if self._live(item[2].job_id, item[1])]
        heapq.heapify(self._heap)
        self._head = 0
        self._dead = 0
