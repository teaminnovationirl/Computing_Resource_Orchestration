"""The admission policies (plan 8.3 and 8.5, context §10): FCFS with a waiting head, priority by need,
the 60 s no-overtaking rule and the oversized check. Pure functions on a JobQueue and a Snapshot: no
database. Expected picks are job ids written out by hand.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.common.types import QueueEntry, Snapshot, Vector
from app.e2_scheduling.policy import oversized, select_fcfs, select_need
from app.e2_scheduling.queue import JobQueue

T0 = datetime(2026, 11, 22, 9, 0, tzinfo=UTC)
GUARD_S = 60.0
FREE = Snapshot(cpu_free=1000, ram_free=4096, gpu_free={0: 2048})
S0_MOST = Vector(28800, 117964, 14745)  # S0's total - reserved per kind (db/seed.sql)


def job(job_id: int, need_class: int = 3, at_s: float = 0.0, cpu: int = 100, vram: int = 0) -> QueueEntry:
    return QueueEntry(job_id, "01FE24BCS901", need_class, T0 + timedelta(seconds=at_s), Vector(cpu, 64, vram))


def queue_of(*entries: QueueEntry) -> JobQueue:
    q = JobQueue()
    for e in entries:
        q.push(e)
    return q


def at(seconds: float) -> float:
    """Wall time `seconds` after T0, as clock.time() gives it."""
    return T0.timestamp() + seconds


def picked(entry: QueueEntry | None) -> int | None:
    return None if entry is None else entry.job_id


def test_an_empty_queue_gives_nothing():
    assert select_fcfs(JobQueue(), FREE) is None
    assert select_need(JobQueue(), FREE, at(100), GUARD_S) is None


def test_fcfs_head_that_does_not_fit_waits_and_no_later_job_overtakes_it():
    q = queue_of(job(1, at_s=0, cpu=2000), job(2, at_s=1, cpu=100))
    assert select_fcfs(q, FREE) is None


def test_fcfs_takes_the_oldest_job_whatever_its_need_class():
    q = queue_of(job(5, need_class=1, at_s=2), job(3, need_class=3, at_s=1))
    assert picked(select_fcfs(q, FREE)) == 3


def test_need_takes_the_higher_need_class_first_and_arrival_order_within_a_class():
    q = queue_of(job(1, need_class=3, at_s=0), job(2, need_class=1, at_s=1), job(3, need_class=1, at_s=2))
    order = []
    while (pick := select_need(q, FREE, at(5), GUARD_S)) is not None:
        order.append(pick.job_id)
        q.remove(pick.job_id)
    assert order == [2, 3, 1]


def test_need_skips_a_job_that_does_not_fit_before_the_guard():
    q = queue_of(job(1, need_class=3, at_s=0, cpu=100), job(2, need_class=1, at_s=1, cpu=5000))
    assert picked(select_need(q, FREE, at(5), GUARD_S)) == 1


@pytest.mark.parametrize(("now_s", "want"), [(59.9, 2), (60.0, 1), (61.0, 1)])
def test_the_oldest_job_goes_first_once_it_has_waited_60_s_from_its_own_arrival(now_s: float, want: int):
    q = queue_of(job(1, need_class=3, at_s=0), job(2, need_class=1, at_s=50))  # job 2 has waited only 10 s
    assert picked(select_need(q, FREE, at(now_s), GUARD_S)) == want


def test_after_60_s_an_oldest_job_that_does_not_fit_blocks_every_other_job():
    q = queue_of(job(1, need_class=3, at_s=0, cpu=2000), job(2, need_class=1, at_s=50, cpu=100))
    assert select_need(q, FREE, at(61), GUARD_S) is None
    assert picked(select_need(q, FREE, at(59), GUARD_S)) == 2  # before the guard it may overtake


@pytest.mark.parametrize(("v", "most", "want"), [
    (Vector(28800, 117964, 14745), S0_MOST, False),
    (Vector(28801, 64), S0_MOST, True),
    (Vector(100, 117965), S0_MOST, True),
    (Vector(100, 64, 14746), S0_MOST, True),
    (Vector(100, 64, 1), Vector(28800, 117964, 0), True),   # VRAM asked, no GPU left
    (Vector(100, 64, 0), Vector(28800, 117964, 0), False),
])
def test_oversized_compares_each_kind_with_its_allocatable_maximum(v: Vector, most: Vector, want: bool):
    assert oversized(v, most) is want
