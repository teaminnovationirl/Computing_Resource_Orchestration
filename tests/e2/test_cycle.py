"""run_cycle() (plan 8.3, D-03, D-104) on the seeded database with StubE3 behind the ports: the policies as
the cycle applies them, the admit step, what a refusal and a raise leave behind, the oversized rule, the
lock and the cycle line. Real transition() calls write the job rows; OffsetClock's time() starts at the
database's now, its monotonic() at 0.
"""
from __future__ import annotations

import math
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import psycopg
import pytest

import app.e2_scheduling.service as service
from app.common.db import tx
from app.common.errors import IllegalTransition
from app.common.types import Committed, Refused, Snapshot, SubmitRequest, User, Vector
from app.config import Config
from app.e2_scheduling.service import E2Service
from app.e4_monitoring.state import transition
from tests.helpers import OffsetClock, TempDb, assert_invariants, job_history, job_state, logged, queued
from tests.stubs import Stubs, StubE3


def free(cpu: int) -> Snapshot:
    """S0's free RAM and GPUs with `cpu` millicores free."""
    return Snapshot(cpu_free=cpu, ram_free=117964, gpu_free={0: 14745, 1: 14745})


def allocated(stubs: Stubs) -> list[object]:
    """The job ids StubE3.allocate() was called with, in order."""
    return [args[0] for name, args in stubs.e3.calls if name == "allocate"]


def in_order(e2: E2Service) -> list[int]:
    return [e.job_id for e in e2.queue.in_fifo_order()]


def admitted_moves(job_id: int) -> int:
    return sum(1 for _, to_state, _, _ in job_history(job_id) if to_state == "admitted")


def refuse_like_e3(stub: StubE3, monkeypatch: pytest.MonkeyPatch) -> None:
    """StubE3 writes nothing, but the real allocate() moves a refused job back to queued (actor E3) in the
    same transaction (app/e3_allocation/service.py). Do that too, so the next cycle sees what it would."""
    real = stub.allocate

    def allocate(job_id: int, vector: Vector) -> Committed | Refused:
        result = real(job_id, vector)
        if isinstance(result, Refused):
            with tx("E3") as t:
                transition(t, job_id, "queued", "E3", "refused: capacity taken first", expected="admitted")
        return result

    monkeypatch.setattr(stub, "allocate", allocate)


def shrink_cpu(db: TempDb, total: int) -> None:
    """What a capacity.py run after the jobs arrived does to the cpu row (reserved = ceil(10%))."""
    with psycopg.connect(db.owner_url, autocommit=True) as conn:
        conn.execute("UPDATE resources SET total = %s, reserved = %s WHERE kind = 'cpu'", (total, math.ceil(total / 10)))


def test_an_unknown_policy_is_refused_when_e2_is_built(tmp_path: Path):
    with pytest.raises(ValueError, match="lifo"):
        E2Service(Config(data_dir=tmp_path, default_policy="lifo"))


def test_fcfs_admits_in_arrival_order_and_the_head_waits_while_it_does_not_fit(e2_stubbed: E2Service, seeded: TempDb):
    a, b, c = queued(e2_stubbed.queue, Vector(2000, 64)), queued(e2_stubbed.queue), queued(e2_stubbed.queue)
    seeded.stubs.e3.snapshot_value = free(cpu=1000)
    e2_stubbed.run_cycle()
    assert allocated(seeded.stubs) == []
    assert in_order(e2_stubbed) == [a, b, c]

    seeded.stubs.e3.snapshot_value = free(cpu=28800)
    seeded.stubs.e3.allocate_results = [Committed(1, None), Committed(2, None), Committed(3, None)]
    e2_stubbed.run_cycle()
    assert allocated(seeded.stubs) == [a, b, c]
    assert len(e2_stubbed.queue) == 0


def test_need_admits_the_higher_need_class_first(seeded: TempDb, tmp_path: Path, db_clock: OffsetClock):
    e2 = E2Service(Config(data_dir=tmp_path, default_policy="need"), clock=db_clock)
    a, b = queued(e2.queue, need_class=3), queued(e2.queue, need_class=1)
    seeded.stubs.e3.allocate_results = [Committed(1, None), Committed(2, None)]
    e2.run_cycle()
    assert allocated(seeded.stubs) == [b, a]


def test_after_60_s_the_oldest_job_goes_first_whatever_its_need_class(
        seeded: TempDb, tmp_path: Path, db_clock: OffsetClock):
    e2 = E2Service(Config(data_dir=tmp_path, default_policy="need"), clock=db_clock)
    a, b = queued(e2.queue, need_class=3), queued(e2.queue, need_class=1)
    db_clock.advance(61)
    seeded.stubs.e3.allocate_results = [Committed(1, None), Committed(2, None)]
    e2.run_cycle()
    assert allocated(seeded.stubs) == [a, b]


def test_one_cycle_admits_until_nothing_fits_with_one_move_and_one_allocate_per_job(
        e2_stubbed: E2Service, seeded: TempDb, monkeypatch: pytest.MonkeyPatch):
    a, b, c = (queued(e2_stubbed.queue, Vector(1000, 64)) for _ in range(3))
    monkeypatch.setattr(seeded.stubs.e3, "snapshot", lambda: free(cpu=2500 - 1000 * len(allocated(seeded.stubs))))
    seeded.stubs.e3.allocate_results = [Committed(1, None), Committed(2, None)]
    e2_stubbed.run_cycle()
    assert allocated(seeded.stubs) == [a, b]
    assert in_order(e2_stubbed) == [c]
    assert [admitted_moves(job) for job in (a, b, c)] == [1, 1, 0]


def test_e2s_lock_is_held_and_the_job_still_queued_while_e3_is_called(
        e2_stubbed: E2Service, seeded: TempDb, monkeypatch: pytest.MonkeyPatch):
    job = queued(e2_stubbed.queue)
    stub = seeded.stubs.e3
    real_snapshot, real_allocate = stub.snapshot, stub.allocate
    seen: list[tuple[str, bool, bool]] = []

    def lock_free_elsewhere() -> bool:
        def try_lock() -> bool:
            if e2_stubbed.lock.acquire(blocking=False):
                e2_stubbed.lock.release()
                return True
            return False
        with ThreadPoolExecutor(1) as pool:
            return pool.submit(try_lock).result(timeout=5)

    def snapshot() -> Snapshot:
        seen.append(("snapshot", lock_free_elsewhere(), job in e2_stubbed.queue))
        return real_snapshot()

    def allocate(job_id: int, vector: Vector) -> Committed | Refused:
        seen.append(("allocate", lock_free_elsewhere(), job_id in e2_stubbed.queue))
        return real_allocate(job_id, vector)

    monkeypatch.setattr(stub, "snapshot", snapshot)
    monkeypatch.setattr(stub, "allocate", allocate)
    stub.allocate_results = [Committed(1, None)]
    e2_stubbed.run_cycle()
    assert seen[:2] == [("snapshot", False, True), ("allocate", False, True)]
    assert not any(lock_free for _, lock_free, _ in seen)
    assert job not in e2_stubbed.queue


def test_a_refusal_keeps_the_jobs_place_ends_the_cycle_and_leaves_nothing_in_doubt(
        e2_stubbed: E2Service, seeded: TempDb, monkeypatch: pytest.MonkeyPatch):
    a, b = queued(e2_stubbed.queue), queued(e2_stubbed.queue)
    stub = seeded.stubs.e3
    refuse_like_e3(stub, monkeypatch)
    stub.allocate_results = [Refused("insufficient capacity")]
    e2_stubbed.run_cycle()
    assert allocated(seeded.stubs) == [a]
    assert in_order(e2_stubbed) == [a, b]
    assert e2_stubbed._in_doubt is None

    first_call_of_next_cycle = len(stub.calls)
    stub.allocate_results = [Committed(1, None), Committed(2, None)]
    e2_stubbed.run_cycle()
    assert stub.calls[first_call_of_next_cycle][0] == "snapshot"
    assert allocated(seeded.stubs) == [a, a, b]


def test_after_a_commit_nothing_is_left_in_doubt(e2_stubbed: E2Service, seeded: TempDb):
    a = queued(e2_stubbed.queue)
    stub = seeded.stubs.e3
    stub.allocate_results = [Committed(1, None)]
    e2_stubbed.run_cycle()
    assert e2_stubbed._in_doubt is None

    b = queued(e2_stubbed.queue)
    first_call_of_next_cycle = len(stub.calls)
    stub.allocate_results = [Committed(2, None)]
    e2_stubbed.run_cycle()
    assert stub.calls[first_call_of_next_cycle][0] == "snapshot"
    assert allocated(seeded.stubs) == [a, b]


def test_after_a_raise_the_job_stays_admitted_and_queued_and_the_next_cycle_calls_allocate_again(
        e2_stubbed: E2Service, seeded: TempDb, json_log: Path):
    job = queued(e2_stubbed.queue)
    stub = seeded.stubs.e3
    stub.allocate_results = [psycopg.OperationalError("server closed the connection unexpectedly"),
                             Committed(1, None)]
    with pytest.raises(psycopg.OperationalError):
        e2_stubbed.run_cycle()
    assert job_state(job) == "admitted"
    assert job in e2_stubbed.queue

    first_call_of_next_cycle = len(stub.calls)
    e2_stubbed.run_cycle()
    assert stub.calls[first_call_of_next_cycle] == ("allocate", (job, Vector(100, 64)))
    assert job not in e2_stubbed.queue
    assert admitted_moves(job) == 1
    assert [line["job_id"] for line in logged(json_log, "admission_in_doubt")] == [job]


def test_a_job_moved_out_of_queued_elsewhere_is_dropped_and_the_cycle_goes_on(
        e2_stubbed: E2Service, seeded: TempDb, json_log: Path):
    gone, after = queued(e2_stubbed.queue), queued(e2_stubbed.queue)
    with tx("E2") as t:
        transition(t, gone, "cancelled", "E2", "test: cancelled without the queue")
    seeded.stubs.e3.allocate_results = [Committed(1, None)]
    e2_stubbed.run_cycle()
    assert gone not in e2_stubbed.queue
    assert allocated(seeded.stubs) == [after]
    [line] = logged(json_log, "admission_resync")
    assert (line["job_id"], line["state"], line["stage"], line["level"]) == (gone, "cancelled", "admit", "WARNING")
    assert line["error"]  # the refusal that sent it there


def test_capacity_changed_only_sets_wake_even_while_another_thread_holds_the_lock(
        e2_stubbed: E2Service, seeded: TempDb):
    queued(e2_stubbed.queue)
    holding, done = threading.Event(), threading.Event()

    def hold() -> None:
        with e2_stubbed.lock:
            holding.set()
            done.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert holding.wait(5)
        with ThreadPoolExecutor(1) as pool:
            pool.submit(e2_stubbed.capacity_changed).result(timeout=5)
        assert e2_stubbed.wake.is_set()
        assert seeded.stubs.e3.calls == []
    finally:
        done.set()
        holder.join(5)


def test_an_oversized_head_is_cancelled_and_the_job_behind_it_admitted(e2_stubbed: E2Service, seeded: TempDb):
    big, small = queued(e2_stubbed.queue, Vector(8000, 64)), queued(e2_stubbed.queue, Vector(1000, 64))
    shrink_cpu(seeded, total=4000)  # 3600 millicores allocatable from now on
    seeded.stubs.e3.snapshot_value = free(cpu=3600)
    seeded.stubs.e3.allocate_results = [Committed(1, None)]
    e2_stubbed.run_cycle()
    reason = "larger than the server's capacity"
    assert job_state(big) == "cancelled"
    assert job_history(big)[-1] == ("queued", "cancelled", "E2", reason)
    assert [(e["state"], e["reason"]) for e in seeded.stubs.hub.events if e["job_id"] == big][-1] == ("cancelled", reason)
    assert allocated(seeded.stubs) == [small]
    assert len(e2_stubbed.queue) == 0
    assert_invariants()


def test_a_job_within_the_maxima_that_does_not_fit_now_waits(e2_stubbed: E2Service, seeded: TempDb):
    job = queued(e2_stubbed.queue, Vector(3000, 64))
    seeded.stubs.e3.snapshot_value = free(cpu=1000)
    e2_stubbed.run_cycle()
    assert job_state(job) == "queued"
    assert in_order(e2_stubbed) == [job]
    assert allocated(seeded.stubs) == []


def test_the_cycle_line_is_info_only_when_the_cycle_changed_something_or_held_the_lock_long(
        e2_stubbed: E2Service, seeded: TempDb, db_clock: OffsetClock, json_log: Path,
        monkeypatch: pytest.MonkeyPatch):
    queued(e2_stubbed.queue, Vector(2000, 64))
    seeded.stubs.e3.snapshot_value = free(cpu=1000)
    e2_stubbed.run_cycle()
    e2_stubbed.run_cycle()  # the same outcome again: DEBUG, not in the file
    assert [(line["outcome"], line["held_ms"]) for line in logged(json_log, "admission_cycle")] == [("blocked", 0.0)]

    def slow_snapshot() -> Snapshot:
        db_clock.advance(0.2)
        return free(cpu=1000)

    monkeypatch.setattr(seeded.stubs.e3, "snapshot", slow_snapshot)
    e2_stubbed.run_cycle()
    assert [(line["outcome"], line["held_ms"]) for line in logged(json_log, "admission_cycle")] == [
        ("blocked", 0.0), ("blocked", 200.0)]


def test_a_submit_wakes_the_loop(e2_stubbed: E2Service):
    assert not e2_stubbed.wake.is_set()
    e2_stubbed.submit(User("01FE24BCS901", "student"),
                      SubmitRequest("cpu_bound", 100, 64, 0, 5, source_name="prog.py"), b"print(42)\n")
    assert e2_stubbed.wake.is_set()


def test_a_failed_cycle_writes_its_line_and_an_identical_failure_drops_to_debug(
        e2_stubbed: E2Service, seeded: TempDb, json_log: Path):
    queued(e2_stubbed.queue)
    queued(e2_stubbed.queue)
    lost = psycopg.OperationalError("server closed the connection unexpectedly")
    seeded.stubs.e3.allocate_results = [Committed(1, None), lost, lost, Committed(2, None)]
    for _ in range(2):  # the first admits one job and fails on the second; the next fails the same way
        with pytest.raises(psycopg.OperationalError):
            e2_stubbed.run_cycle()
    e2_stubbed.run_cycle()
    lines = [(line["outcome"], line["admitted"]) for line in logged(json_log, "admission_cycle")]
    assert lines == [("failed", 1), ("empty", 1)]


def test_a_move_that_fails_before_commit_leaves_the_job_queued_and_not_in_doubt(
        e2_stubbed: E2Service, seeded: TempDb, monkeypatch: pytest.MonkeyPatch):
    job = queued(e2_stubbed.queue)
    real_move, failures = service._move, [psycopg.OperationalError("connection lost before COMMIT")]

    def move(job_id: int, to_state: service.JobState, reason: str) -> None:
        if failures:
            raise failures.pop()
        real_move(job_id, to_state, reason)

    monkeypatch.setattr(service, "_move", move)
    with pytest.raises(psycopg.OperationalError):
        e2_stubbed.run_cycle()
    assert (job_state(job), job in e2_stubbed.queue, e2_stubbed._in_doubt) == ("queued", True, None)
    assert allocated(seeded.stubs) == []

    seeded.stubs.e3.allocate_results = [Committed(1, None)]
    e2_stubbed.run_cycle()
    assert allocated(seeded.stubs) == [job]
    assert admitted_moves(job) == 1


def test_a_lost_answer_of_the_admitted_move_is_found_at_the_next_pick(
        e2_stubbed: E2Service, seeded: TempDb, monkeypatch: pytest.MonkeyPatch):
    job = queued(e2_stubbed.queue)
    real_move, failures = service._move, [psycopg.OperationalError("server closed the connection unexpectedly")]

    def move(job_id: int, to_state: service.JobState, reason: str) -> None:
        real_move(job_id, to_state, reason)  # commits
        if failures:
            raise failures.pop()

    monkeypatch.setattr(service, "_move", move)
    with pytest.raises(psycopg.OperationalError):
        e2_stubbed.run_cycle()
    assert (job_state(job), job in e2_stubbed.queue) == ("admitted", True)

    e2_stubbed.run_cycle()  # picked again: the move is refused, the job reads admitted, so it is in doubt
    assert allocated(seeded.stubs) == []
    assert e2_stubbed._in_doubt is not None and e2_stubbed._in_doubt.job_id == job

    seeded.stubs.e3.allocate_results = [Committed(1, None)]
    e2_stubbed.run_cycle()
    assert allocated(seeded.stubs) == [job]
    assert admitted_moves(job) == 1
    assert job not in e2_stubbed.queue


def test_a_refused_move_of_a_job_that_still_reads_queued_is_an_error_not_a_quiet_stall(
        e2_stubbed: E2Service, json_log: Path, monkeypatch: pytest.MonkeyPatch):
    job = queued(e2_stubbed.queue)

    def refused(job_id: int, to_state: service.JobState, reason: str) -> None:
        raise IllegalTransition(f"job {job_id}: refused by the trigger")

    monkeypatch.setattr(service, "_move", refused)
    with pytest.raises(IllegalTransition):
        e2_stubbed.run_cycle()
    [line] = logged(json_log, "admission_resync")
    assert (line["job_id"], line["stage"], line["state"], line["level"]) == (job, "admit", "queued", "ERROR")
    assert job in e2_stubbed.queue


def test_allocate_calling_a_job_not_admitted_while_it_reads_admitted_is_an_error(
        e2_stubbed: E2Service, seeded: TempDb, json_log: Path):
    job = queued(e2_stubbed.queue)
    seeded.stubs.e3.allocate_results = [IllegalTransition(f"job {job} is admitted, not admitted")]
    with pytest.raises(IllegalTransition):
        e2_stubbed.run_cycle()
    [line] = logged(json_log, "admission_resync")
    assert (line["stage"], line["state"], line["level"]) == ("allocate", "admitted", "ERROR")
    assert e2_stubbed._in_doubt is not None and e2_stubbed._in_doubt.job_id == job


def test_an_answer_that_is_neither_committed_nor_refused_is_a_type_error(e2_stubbed: E2Service, seeded: TempDb):
    queued(e2_stubbed.queue)
    seeded.stubs.e3.allocate_results = [cast(Refused, "garbage")]
    with pytest.raises(TypeError, match="garbage"):
        e2_stubbed.run_cycle()
