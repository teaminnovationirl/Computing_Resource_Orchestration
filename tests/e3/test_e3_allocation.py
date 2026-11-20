"""E3-2 allocate() under concurrency (plan 6.6; the file named in §13): the last unit, the best-fit GPU
from the locked rows, the lock order, and a stress run that never deadlocks.

Threads call allocate() on separate pooled connections, and every transaction commits. A race starts at
a Barrier while a gate holds the CPU row, the first row allocate() locks: every racer reads the rows
without a lock and then waits on the gate, so the stale read happens every run, never by luck. S0
(db/seed.sql): resources 1 cpu, 2 ram, 3 gpu 0, 4 gpu 1, with 28,800 millicores, 117,964 MiB and
14,745 MiB per GPU outside the reserve; new GPU rows get ids from 5. Each docstring names the bug the
test catches.
"""
from __future__ import annotations

import json
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

import psycopg

from app.common.db import close_pool, init_pool, tx
from app.common.types import Committed, Refused, Vector
from app.e3_allocation import allocate
from tests.helpers import TempDb, assert_invariants, gate, lockable, make_job, race, wait_until_waiting

RUNS = 100


def add_gpu(total: int) -> int:
    """A new GPU row of total MiB with the reserve the CHECK demands; returns its gpu_index."""
    with tx("E3") as cur:
        row = cur.execute("INSERT INTO resources (kind, gpu_index, total, reserved) "
                          "SELECT 'gpu', max(gpu_index) + 1, %s, %s FROM resources RETURNING gpu_index",
                          (total, (total + 9) // 10)).fetchone()
    assert row is not None
    return row[0]


def gpu_held(gpu_index: int) -> int:
    with tx("E4") as cur:
        row = cur.execute("SELECT allocated FROM resources WHERE kind = 'gpu' AND gpu_index = %s",
                          (gpu_index,)).fetchone()
    assert row is not None
    return row[0]


def states(jobs: list[int]) -> list[str]:
    with tx("E4") as cur:
        return sorted(state for (state,) in cur.execute("SELECT state FROM jobs WHERE job_id = ANY(%s)",
                                                        (jobs,)).fetchall())


def audited(jobs: list[int]) -> list[str]:
    """The outcomes of the jobs' allocate audit rows, sorted."""
    with tx("E4") as cur:
        return sorted(outcome for (outcome,) in cur.execute(
            "SELECT outcome FROM allocation_audit WHERE job_id = ANY(%s) AND action = 'allocate'", (jobs,)).fetchall())


def locked_elsewhere(db: TempDb, job: int) -> list[str]:
    """Which of RAM, the two S0 GPUs and the job's row another session holds a lock on."""
    with psycopg.connect(db.owner_url, autocommit=True) as probe:
        busy = [name for name, resource_id in [("ram", 2), ("gpu 0", 3), ("gpu 1", 4)]
                if not lockable(probe, "SELECT 1 FROM resources WHERE resource_id = %s FOR UPDATE NOWAIT",
                                resource_id)]
        if not lockable(probe, "SELECT 1 FROM jobs WHERE job_id = %s FOR UPDATE NOWAIT", job):
            busy.append("job row")
    return busy


def logged(path: Path, events: tuple[str, ...]) -> list[dict[str, Any]]:
    return [record for record in map(json.loads, path.read_text(encoding="utf-8").splitlines())
            if record["event"] in events]


def test_two_requests_for_the_last_unit_exactly_one_commits(s0: TempDb):
    """Catches the re-check left out, or made on the rows read before the lock: both racers have read
    the free unit before either holds a lock, so a decision on that read gives it twice and the second
    charge breaks the CHECK. Each run adds a GPU with 18,000 MiB allocatable (20,000 less the 2,000
    reserve) and both ask for all of it, more than S0's GPUs (14,745) or an earlier run's GPU (full)
    can give."""
    need = Vector(100, 64, 18000)
    with ThreadPoolExecutor(max_workers=2) as pool:
        for _ in range(RUNS):
            gpu = add_gpu(20000)
            jobs = [make_job("admitted", need) for _ in range(2)]
            outcomes = race(s0, pool, [partial(allocate, job, need) for job in jobs])
            assert sorted(type(outcome).__name__ for outcome in outcomes) == ["Committed", "Refused"]
            assert [outcome.gpu_index for outcome in outcomes if isinstance(outcome, Committed)] == [gpu]
            assert gpu_held(gpu) == 18000  # total - reserved, and never more
            assert states(jobs) == ["allocated", "queued"]
            assert audited(jobs) == ["committed", "refused"]  # the loser is refused and audited (context §7)
    assert_invariants(settled=True)


def test_concurrent_gpu_requests_get_the_best_fit_of_the_locked_rows(s0: TempDb):
    """Catches a GPU chosen from the rows read before the lock: both racers read two new GPUs free; the
    first takes the smaller (18,000 MiB allocatable, the best fit for 15,000), so the second must see
    it hold only 3,000 more and take the larger (27,000) instead of over-committing the smaller.
    15,000 is more than S0's GPUs (14,745) or any earlier leftover (3,000 and 12,000) can give."""
    need = Vector(100, 64, 15000)
    with ThreadPoolExecutor(max_workers=2) as pool:
        for _ in range(RUNS):
            small, large = add_gpu(20000), add_gpu(30000)
            jobs = [make_job("admitted", need) for _ in range(2)]
            outcomes = race(s0, pool, [partial(allocate, job, need) for job in jobs])
            # allocation ids are drawn under the CPU row lock, so their order is the commit order
            committed = sorted((outcome for outcome in outcomes if isinstance(outcome, Committed)),
                               key=lambda outcome: outcome.allocation_id)
            assert [outcome.gpu_index for outcome in committed] == [small, large]
            assert (gpu_held(small), gpu_held(large)) == (15000, 15000)
    assert_invariants(settled=True)


def test_the_same_job_allocated_twice_at_once_commits_once(s0: TempDb):
    """Catches the job read moved before the lock statement (D-70): both calls would read admitted, and
    the second, once the first commits, would charge capacity again and die on the one-open-allocation
    index as a raw 23505. Read after the lock, the second call sees the commit and raises
    IllegalTransition, writing nothing, so the job is charged, audited and dispatched once."""
    need = Vector(100, 64)
    with ThreadPoolExecutor(max_workers=2) as pool:
        for _ in range(RUNS):
            job = make_job("admitted", need)
            outcomes = race(s0, pool, [partial(allocate, job, need)] * 2)
            assert sorted(type(outcome).__name__ for outcome in outcomes) == ["Committed", "IllegalTransition"]
            assert any(f"job {job} is allocated, not admitted" in str(outcome) for outcome in outcomes)
            assert audited([job]) == ["committed"]
            assert [args[0] for name, args in s0.stubs.e1.calls if name == "dispatch"].count(job) == 1
    assert_invariants(settled=True)


def test_allocate_waits_on_the_cpu_row_holding_no_other_lock(s0: TempDb):
    """Catches rows locked out of resource_id order and the job row locked before the resource rows
    (transition() first): while allocate() waits on the CPU row, the lowest id, it must hold no row
    lock at all, so RAM, both GPUs and its job row can each be locked at once (NOWAIT)."""
    need = Vector(100, 64, 100)
    with ThreadPoolExecutor(max_workers=1) as pool:
        for _ in range(RUNS):
            job = make_job("admitted", need)
            with gate(s0):
                future = pool.submit(allocate, job, need)
                wait_until_waiting(s0, 1, [future])
                busy = locked_elsewhere(s0, job)
            assert busy == [], f"allocate() held {busy} while it waited on the CPU row"
            assert isinstance(future.result(timeout=30), Committed)
    assert_invariants(settled=True)


def test_many_concurrent_allocations_never_deadlock_or_overcommit(s0: TempDb, json_log: Path):
    """Catches a lock order that differs between calls, which deadlocks (40P01; retrying() would hide
    it, so the log is read), and any over-commit under load: 32 threads, 20 rounds, seeded vectors over
    CPU, RAM and both GPUs, a pool of 8 so that 8 transactions contend at once. Every call ends
    Committed or Refused, and the invariants hold after each round."""
    close_pool()
    init_pool(s0.app_url, min_size=1, max_size=8)
    rng = random.Random(1042)
    outcomes: list[str] = []
    with ThreadPoolExecutor(max_workers=32) as pool:
        for _ in range(20):
            requests: list[tuple[int, Vector]] = []
            for _ in range(32):
                need = Vector(rng.randint(10, 200), rng.randint(64, 1024), rng.choice((0, 0, rng.randint(1, 1000))))
                requests.append((make_job("admitted", need), need))
            barrier = threading.Barrier(len(requests))

            def run(job: int, need: Vector, barrier: threading.Barrier = barrier) -> Committed | Refused:
                barrier.wait(timeout=30)
                return allocate(job, need)

            futures = [pool.submit(run, job, need) for job, need in requests]
            outcomes += [type(future.result(timeout=60)).__name__ for future in futures]
            assert_invariants()
    assert set(outcomes) == {"Committed", "Refused"}  # both paths ran under load
    assert logged(json_log, ("tx_retry", "tx_retry_exhausted")) == []
    assert_invariants(settled=True)
