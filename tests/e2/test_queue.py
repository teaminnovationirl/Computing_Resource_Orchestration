"""JobQueue (plan 8.2 and 8.5, D-103, D-104): the hash map, the min-heap and the FIFO list always hold the
same live jobs, in (need_class, submitted_at, job_id) and (submitted_at, job_id) order. No database.

Expected orders come from a plain model, a list of the queued entries sorted with the keys written out
below, never with the queue's own key functions. assert_same() also reads the three private structures,
because keeping them in sync is the contract of plan 8.2: _map is job_id -> (entry, version), and every
heap and FIFO item is (key, version, entry), live while its version is the map's.
"""
from __future__ import annotations

import random
import sys
import threading
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from operator import attrgetter, le
from types import TracebackType
from zoneinfo import ZoneInfo

import pytest

from app.common.types import QueueEntry, Vector
from app.e2_scheduling.queue import JobQueue

T0 = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
ARRIVAL = attrgetter("submitted_at", "job_id")               # the two orders of plan 8.2, written out
NEED = attrgetter("need_class", "submitted_at", "job_id")


def job(job_id: int, need_class: int = 3, at: int = 0, cpu: int = 100) -> QueueEntry:
    """A job of 01FE24BCS901 that arrived `at` microseconds after T0."""
    return QueueEntry(job_id, "01FE24BCS901", need_class, T0 + timedelta(microseconds=at), Vector(cpu, 64))


def ids(entries: list[QueueEntry]) -> list[int]:
    return [e.job_id for e in entries]


def jid(entry: QueueEntry | None) -> int | None:
    return None if entry is None else entry.job_id


def assert_same(q: JobQueue, model: list[QueueEntry]) -> None:
    """The map, the live heap items and the live FIFO items hold exactly the model's jobs, each once and in
    the model's orders; the heap keeps the heap property and the FIFO list is sorted from its head. Calls
    nothing that changes the queue, so stale items stay where the operations left them."""
    by_arrival, by_need = sorted(model, key=ARRIVAL), sorted(model, key=NEED)
    assert {job_id: entry for job_id, (entry, _) in q._map.items()} == {e.job_id: e for e in model}
    version = {job_id: v for job_id, (_, v) in q._map.items()}
    heap, fifo = q._heap, q._fifo[q._head:]
    assert all(map(le, heap, heap[1::2])) and all(map(le, heap, heap[2::2]))  # each parent <= its children
    assert [e.job_id for _, _, e in sorted(i for i in heap if version.get(i[2].job_id) == i[1])] == ids(by_need)
    assert all(map(le, fifo, fifo[1:]))
    assert [e.job_id for _, v, e in fifo if version.get(e.job_id) == v] == ids(by_arrival)
    assert len(q) == len(model)
    assert q.in_fifo_order() == by_arrival and q.in_need_order() == by_need


@pytest.mark.parametrize("seed", range(20))
def test_property_20_seeds_by_10000_operations(seed: int):
    """Catches any change that lets the three structures drift apart or serve a wrong job: 10,000 random
    operations (push with frequent equal times and late commits, admit and cancel removes, remove of a job
    that is not queued, requeue of an admitted job with its original entry at most twice, duplicate push,
    fifo_head, need_head, first_fit), each followed by assert_same() against the plain model."""
    rnd = random.Random(seed)
    q = JobQueue()
    model: list[QueueEntry] = []
    admitted: list[QueueEntry] = []
    requeues: Counter[int] = Counter()
    next_id, newest = 1, 0
    for _ in range(10_000):
        op = rnd.random()
        if op < 0.30 and len(model) < 60:                    # a new job; equal times are common
            newest += rnd.choice((0, 0, 1, 3))
            late = rnd.choice((0, 0, 0, 2))                  # a late COMMIT: older than the newest job
            e = job(next_id, rnd.randint(1, 3), max(newest - late, 0), rnd.choice((100, 500, 2000)))
            next_id += 1
            q.push(e)
            model.append(e)
        elif op < 0.42 and model:                            # admitted: leaves after Committed
            e = model.pop(rnd.randrange(len(model)))
            assert q.remove(e.job_id) is e
            if requeues[e.job_id] < 2:
                admitted.append(e)
        elif op < 0.48 and model:                            # cancelled
            e = model.pop(rnd.randrange(len(model)))
            assert q.remove(e.job_id) is e
        elif op < 0.58 and admitted:                         # requeued with its original entry
            e = admitted.pop(rnd.randrange(len(admitted)))
            requeues[e.job_id] += 1
            q.push(e)
            model.append(e)
        elif op < 0.62:                                      # a job that is not queued
            assert q.remove(rnd.choice([e.job_id for e in admitted] or [next_id])) is None
        elif op < 0.66 and model:                            # a job that is already queued
            with pytest.raises(ValueError):
                q.push(rnd.choice(model))
        elif op < 0.76:
            assert q.fifo_head() is (min(model, key=ARRIVAL) if model else None)
        elif op < 0.86:
            assert q.need_head() is (min(model, key=NEED) if model else None)
        else:
            limit = rnd.choice((100, 500, 2000))
            want = next((e for e in sorted(model, key=NEED) if e.vector.cpu_millicores <= limit), None)
            assert q.first_fit(lambda e: e.vector.cpu_millicores <= limit) is want
        assert_same(q, model)


def test_lazy_deletion_never_returns_a_removed_job():
    """Catches a stale item served after remove() (planted bug 2): a removed job keeps its heap and FIFO
    items until they are popped or compacted, and no read may return it; pushed again, its old items stay
    stale."""
    q = JobQueue()
    a, b, c = job(1, need_class=1, at=0), job(2, need_class=2, at=1), job(3, need_class=3, at=2)
    for e in (a, b, c):
        q.push(e)
    assert q.remove(1) is a and q.remove(1) is None
    assert 1 not in q and len(q) == 2
    assert jid(q.first_fit(lambda e: True)) == 2             # a's stale item is still the heap top
    assert q.first_fit(lambda e: e.job_id == 1) is None
    assert (jid(q.need_head()), jid(q.fifo_head())) == (2, 2)
    assert (ids(q.in_fifo_order()), ids(q.in_need_order())) == ([2, 3], [2, 3])
    q.push(a)
    assert (ids(q.in_fifo_order()), ids(q.in_need_order())) == ([1, 2, 3], [1, 2, 3])
    assert (q.remove(1), q.remove(2), q.remove(3)) == (a, b, c)
    assert (q.fifo_head(), q.need_head(), q.first_fit(lambda e: True), len(q)) == (None, None, None, 0)
    assert (q.in_fifo_order(), q.in_need_order()) == ([], [])


def test_ties_are_broken_by_job_id():
    """Catches a heap key without job_id (planted bug 1): with equal need class and arrival, the lower
    job_id comes first, whatever the push order."""
    q = JobQueue()
    for e in (job(5, need_class=2), job(3, need_class=2), job(4, need_class=1, at=7), job(9, need_class=2)):
        q.push(e)
    assert ids(q.in_fifo_order()) == [3, 5, 9, 4]
    assert ids(q.in_need_order()) == [4, 3, 5, 9]
    assert q.remove(4) is not None                           # the tie is now at the heap top
    assert (jid(q.first_fit(lambda e: True)), jid(q.need_head()), jid(q.fifo_head())) == (3, 3, 3)
    assert jid(q.first_fit(lambda e: e.job_id != 3)) == 5


def test_a_requeue_keeps_its_arrival_order():
    """Catches a requeued job placed as a new arrival or missing from the map (planted bugs 3 and 4): it
    comes back with its original submitted_at, in its old place, ahead of jobs that arrived after it
    (invariant 8), also after a compaction has rebuilt the list and reset its head."""
    q = JobQueue()
    a, b, c, d = job(1, at=10), job(2, at=20), job(3, at=30), job(4, at=40)
    for e in (a, b, c):
        q.push(e)
    assert q.remove(1) is a                                  # admitted: it left after Committed
    q.push(d)
    q.push(a)                                                # requeued with its original entry
    assert 1 in q and len(q) == 4
    assert (ids(q.in_fifo_order()), ids(q.in_need_order())) == ([1, 2, 3, 4], [1, 2, 3, 4])
    assert (jid(q.fifo_head()), jid(q.need_head())) == (1, 1)
    for n in range(100, 170):
        q.push(job(n, at=1000 + n))
    for n in range(100, 170):                                # 70 removals: a compaction on the 65th
        q.remove(n)
    assert q.remove(1) is a
    q.push(a)
    assert ids(q.in_fifo_order()) == [1, 2, 3, 4] and jid(q.fifo_head()) == 1
    assert_same(q, [a, b, c, d])


def test_jobs_in_a_daylight_saving_fall_back_hour_keep_their_real_order():
    """Catches ordering by wall-clock time: psycopg returns submitted_at in the session's time zone, and
    two times that share one zone compare by their wall clocks, ignoring fold. In the hour London repeats
    on 2026-10-25, 01:30 BST (00:30 UTC) arrived before 01:10 GMT (01:10 UTC)."""
    london = ZoneInfo("Europe/London")
    first = QueueEntry(2, "01FE24BCS901", 3, datetime(2026, 10, 25, 1, 30, tzinfo=london), Vector(100, 64))
    second = QueueEntry(1, "01FE24BCS901", 3, datetime(2026, 10, 25, 1, 10, fold=1, tzinfo=london),
                        Vector(100, 64))
    q = JobQueue()
    q.push(first)
    q.push(second)
    assert (ids(q.in_fifo_order()), ids(q.in_need_order())) == ([2, 1], [2, 1])
    assert (jid(q.fifo_head()), jid(q.need_head()), jid(q.first_fit(lambda e: True))) == (2, 2, 2)


def test_first_fit_restores_the_heap_when_fits_raises():
    """Catches jobs lost from the heap when the predicate raises (plan 8.2's sketch pushes the popped
    items back without a finally): the error passes through and every job is still in need order."""
    q = JobQueue()
    entries = [job(n, need_class=n % 3 + 1, at=n) for n in range(1, 8)]
    for e in entries:
        q.push(e)
    calls = 0

    def fails_on_the_third(e: QueueEntry) -> bool:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("fit check failed")
        return False

    with pytest.raises(RuntimeError, match="fit check failed"):
        q.first_fit(fails_on_the_third)
    assert_same(q, entries)
    assert ids(q.in_need_order()) == [3, 6, 1, 4, 7, 2, 5]
    assert jid(q.first_fit(lambda e: True)) == 3


def test_first_fit_evaluates_k_plus_one_jobs_and_keeps_them():
    """Catches stale items handed to the predicate, the jobs that do not fit dropped from the heap, and a
    fit that is not the first in need order: past k = 3 live jobs that do not fit, the predicate runs 4
    times, on those jobs only (O(k log n))."""
    q = JobQueue()
    entries = [job(1, 1, 1, cpu=100), job(2, 1, 2, cpu=4000), job(3, 1, 3, cpu=4000), job(4, 2, 4, cpu=100),
               job(5, 2, 5, cpu=4000), job(6, 3, 6, cpu=100), job(7, 3, 7, cpu=100)]
    for e in entries:
        q.push(e)
    q.remove(1)
    q.remove(4)
    seen: list[int] = []

    def small(e: QueueEntry) -> bool:
        seen.append(e.job_id)
        return e.vector.cpu_millicores <= 1000

    for _ in range(2):
        seen.clear()
        assert jid(q.first_fit(small)) == 6
        assert seen == [2, 3, 5, 6]
    assert_same(q, [entries[i] for i in (1, 2, 4, 5, 6)])


def test_push_refuses_a_queued_job_and_a_naive_time_and_changes_nothing():
    """Catches a refused push that still writes: a duplicate job_id replacing the live entry, or a naive
    submitted_at that would raise inside heappush after the map write and leave the heap broken."""
    q = JobQueue()
    a, b = job(1, at=5), job(2, at=9)
    q.push(a)
    q.push(b)
    with pytest.raises(ValueError):
        q.push(job(1, need_class=1, at=0))
    with pytest.raises(ValueError):
        q.push(QueueEntry(3, "01FE24BCS901", 3, datetime(2026, 11, 21, 9, 0), Vector(100, 64)))
    assert_same(q, [a, b])


def test_compaction_bounds_stale_items_and_keeps_order():
    """Catches stale items that grow without bound, and a compaction that drops a live job, keeps a stale
    one or breaks an order: after 150 of 200 jobs are removed in a random order, heap and FIFO hold at
    most the live jobs plus max(64, live) stale items."""
    q = JobQueue()
    entries = [job(n, need_class=n % 3 + 1, at=n // 2) for n in range(1, 201)]
    for e in entries:
        q.push(e)
    gone = set(random.Random(7).sample(range(1, 201), 150))
    for n in sorted(gone, key=lambda n: (n * 7919) % 211):
        assert jid(q.remove(n)) == n
    left = [e for e in entries if e.job_id not in gone]
    assert_same(q, left)
    assert len(q._heap) <= 50 + 64 and len(q._fifo) <= 50 + 64


class Probe:
    """E2's lock for one test: the test holds the inner RLock directly; any call that enters the probe
    first records that it asked, then waits for the lock."""

    def __init__(self, progress: threading.Event) -> None:
        self.inner = threading.RLock()
        self.progress = progress
        self.asked = False

    def __enter__(self) -> bool:
        self.asked = True
        self.progress.set()
        return self.inner.__enter__()

    def __exit__(self, t: type[BaseException] | None, v: BaseException | None, tb: TracebackType | None) -> None:
        self.inner.__exit__(t, v, tb)


CALLS: dict[str, Callable[[JobQueue], object]] = {
    "push": lambda q: q.push(job(9, at=99)),
    "remove": lambda q: q.remove(1),
    "fifo_head": lambda q: q.fifo_head(),
    "need_head": lambda q: q.need_head(),
    "first_fit": lambda q: q.first_fit(lambda e: True),
    "in_fifo_order": lambda q: q.in_fifo_order(),
    "in_need_order": lambda q: q.in_need_order(),
    "len": lambda q: len(q),
    "contains": lambda q: 1 in q,
}


@pytest.mark.parametrize("name", CALLS)
def test_every_public_method_takes_e2s_lock(name: str):
    """Catches a method that reads or changes the queue without E2's lock (planted bug 5: push): while
    the test holds the lock, the call must reach it and wait, and nothing changes until it is released."""
    progress = threading.Event()
    probe = Probe(progress)
    q = JobQueue(probe)
    q.push(job(1, at=1))
    q.push(job(2, at=2))
    probe.asked = False
    progress.clear()
    results: list[object] = []

    def call() -> None:
        try:
            results.append(CALLS[name](q))
        finally:
            progress.set()

    worker = threading.Thread(target=call)
    with probe.inner:
        worker.start()
        assert progress.wait(5)                              # the call reached the lock, or ran without it
        assert probe.asked and results == []
        assert sorted(q._map) == [1, 2]
    worker.join(5)
    assert not worker.is_alive() and len(results) == 1


def test_two_threads_pushing_and_removing_at_once_stay_consistent():
    """Catches items lost or duplicated when push and remove interleave, also a method that takes the lock
    but finishes its work outside it: one thread pushes 120 jobs (their arrival order is not their id
    order), then keeps requeueing whatever is gone until the other thread is done with 1,000 random
    removals and its first_fit sweeps, so pushes overlap compactions; afterwards the three structures agree
    and hold exactly the jobs pushed once more than removed. 100 repetitions start at a Barrier; a
    1 microsecond GIL switch interval forces interleavings, instead of sleeps."""
    entries = {n: job(n, need_class=n % 3 + 1, at=(n * 7919) % 500) for n in range(1, 121)}
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for rep in range(100):
            q = JobQueue()
            barrier = threading.Barrier(2)
            done = threading.Event()
            pushes: Counter[int] = Counter()
            removes: Counter[int] = Counter()
            errors: list[BaseException] = []

            def pusher() -> None:
                barrier.wait(timeout=30)
                for n, e in entries.items():
                    q.push(e)
                    pushes[n] += 1
                while not done.is_set():
                    for n, e in entries.items():
                        try:
                            q.push(e)
                            pushes[n] += 1
                        except ValueError:                   # still queued: nothing to requeue
                            pass

            def remover(rnd: random.Random = random.Random(rep)) -> None:
                barrier.wait(timeout=30)
                try:
                    for _ in range(1000):
                        n = rnd.randint(1, 120)
                        if q.remove(n) is not None:
                            removes[n] += 1
                        if rnd.random() < 0.1:
                            q.fifo_head()
                            q.first_fit(lambda e: e.vector.cpu_millicores > 10_000)
                finally:
                    done.set()

            def guarded(fn: Callable[[], None]) -> Callable[[], None]:
                def run() -> None:
                    try:
                        fn()
                    except BaseException as exc:
                        errors.append(exc)
                return run

            threads = [threading.Thread(target=guarded(pusher)), threading.Thread(target=guarded(remover))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            assert errors == [] and not any(thread.is_alive() for thread in threads)
            assert all(pushes[n] - removes[n] in (0, 1) for n in entries)
            assert_same(q, [e for n, e in entries.items() if pushes[n] - removes[n] == 1])
    finally:
        sys.setswitchinterval(old)
