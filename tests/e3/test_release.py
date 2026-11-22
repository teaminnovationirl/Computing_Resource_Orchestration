"""E3-3 release() (plan 6.4, 6.6; D-47, D-65, D-71, D-74, D-75) on real PostgreSQL with the S0 seed.

One tx("E3") locks the job's open allocation, then the job's resource rows in id order with the lock
statement of allocate(), gives the capacity back, closes the allocation, writes the release audit row
and moves the job to released, its row locked last. The log line and the notices to E2 and the pages
leave only after COMMIT. Races run real threads on separate pooled connections behind the gate of
tests/helpers.py, which holds the CPU row: every job holds CPU, so every release() locks that row
first after its allocation. S0 (db/seed.sql): resources 1 cpu, 2 ram, 3 gpu 0, 4 gpu 1, with 28,800
millicores, 117,964 MiB and 14,745 MiB per GPU outside the reserve. Each docstring names the bug the
test catches.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import errors

from app.common.db import tx
from app.common.errors import IllegalTransition, NotFound
from app.common.types import AlreadyReleased, Committed, Refused, Released, Vector
from app.e3_allocation import allocate, release, service
from app.e4_monitoring.state import JobState, transition
from tests.helpers import (TempDb, assert_invariants, fail_at_end_of_body, gate, lockable, make_job, race,
                           wait_until_waiting)

RUNS = 100
NOTHING_HELD = {1: 0, 2: 0, 3: 0, 4: 0}
FULL = {1: 28800, 2: 117964, 3: 14745, 4: 14745}  # total - reserved on every S0 row
NO_SUCH_JOB = 999_999_999  # job ids start at 1042 and every test has a fresh database
SMALL_VECTOR = Vector(100, 64)  # finished()'s default; a call in the signature trips B008


def finished(state: JobState = "completed", vector: Vector = SMALL_VECTOR) -> int:
    """A job that ran and ended in state, still holding its allocation of vector, as E1 leaves it once
    the stop is confirmed: make_job("running") allocates for real, then E1 makes the terminal move."""
    job = make_job("running", vector)
    with tx("E1") as cur:
        transition(cur, job, state, "E1", f"test: {state}")
    return job


def open_allocation(job: int) -> int:
    with tx("E4") as cur:
        row = cur.execute("SELECT allocation_id FROM allocations WHERE job_id = %s AND status = 'open'",
                          (job,)).fetchone()
    assert row is not None, f"job {job} holds no open allocation"
    return row[0]


def held() -> dict[int, int]:
    """resources.allocated by resource_id."""
    with tx("E4") as cur:
        rows = cur.execute("SELECT resource_id, allocated FROM resources ORDER BY resource_id").fetchall()
    return {resource_id: allocated for resource_id, allocated in rows}


def written(job: int) -> dict[str, list[tuple[Any, ...]]]:
    """What E3 and the state trigger hold for job: its allocations with their items, its audit rows,
    its history and its state."""
    with tx("E4") as cur:
        return {
            "allocations": cur.execute(
                "SELECT a.allocation_id, a.status, a.released_at IS NOT NULL, i.resource_id, i.amount "
                "FROM allocations a JOIN allocation_items i USING (allocation_id) WHERE a.job_id = %s "
                "ORDER BY a.allocation_id, i.resource_id", (job,)).fetchall(),
            "audit": cur.execute("SELECT allocation_id, action, outcome FROM allocation_audit WHERE job_id = %s "
                                 "ORDER BY audit_id", (job,)).fetchall(),
            "history": cur.execute("SELECT from_state, to_state, actor, reason FROM job_state_history "
                                   "WHERE job_id = %s ORDER BY event_id", (job,)).fetchall(),
            "state": cur.execute("SELECT state FROM jobs WHERE job_id = %s", (job,)).fetchall(),
        }


def notices(db: TempDb) -> tuple[int, int]:
    """The capacity_changed calls E2 has had and the events the pages have had so far."""
    return sum(name == "capacity_changed" for name, _ in db.stubs.e2.calls), len(db.stubs.hub.events)


def logged(path: Path, event: str) -> list[dict[str, Any]]:
    return [record for record in map(json.loads, path.read_text(encoding="utf-8").splitlines())
            if record["event"] == event]


def locked_elsewhere(db: TempDb, job: int, allocation_id: int) -> list[str]:
    """Which of the job's allocation, RAM, the two S0 GPUs and the job's row another session holds a
    lock on."""
    busy: list[str] = []
    with psycopg.connect(db.owner_url, autocommit=True) as probe:
        if not lockable(probe, "SELECT 1 FROM allocations WHERE allocation_id = %s FOR UPDATE NOWAIT",
                        allocation_id):
            busy.append("allocation")
        busy += [name for name, resource_id in [("ram", 2), ("gpu 0", 3), ("gpu 1", 4)]
                 if not lockable(probe, "SELECT 1 FROM resources WHERE resource_id = %s FOR UPDATE NOWAIT",
                                 resource_id)]
        if not lockable(probe, "SELECT 1 FROM jobs WHERE job_id = %s FOR UPDATE NOWAIT", job):
            busy.append("job row")
    return busy


def waiting_statements(db: TempDb) -> list[str]:
    """The statements that sessions of this database are waiting in, on a lock."""
    with psycopg.connect(db.owner_url, autocommit=True) as watch:
        return [query for (query,) in watch.execute(
            "SELECT query FROM pg_stat_activity WHERE datname = current_database() "
            "AND cardinality(pg_blocking_pids(pid)) > 0").fetchall()]


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled"])
def test_a_release_gives_back_closes_audits_and_moves_together(s0: TempDb, monkeypatch: pytest.MonkeyPatch,
                                                               json_log: Path, state: JobState):
    """Catches capacity given back without closing the allocation (invariant 1), a missing audit row or
    move, and a notice sent before COMMIT or before the release line: the E2 stub refuses a call inside
    a transaction, and this capacity_changed also looks from another connection, which sees committed
    rows only, and at the log. Each state that may move to released is released (plan 7.2)."""
    job = finished(state, Vector(1500, 2048, 4000))
    a = open_allocation(job)
    seen_by_e2: list[tuple[Any, ...]] = []
    capacity_changed = s0.stubs.e2.capacity_changed

    def look_then_record() -> None:
        with psycopg.connect(s0.owner_url) as other:
            seen_by_e2.extend(other.execute("SELECT a.status, r.allocated FROM allocations a, resources r "
                                            "WHERE a.allocation_id = %s AND r.resource_id = 1", (a,)).fetchall())
        seen_by_e2.extend((line["outcome"], line["allocation_id"]) for line in logged(json_log, "release"))
        capacity_changed()

    monkeypatch.setattr(s0.stubs.e2, "capacity_changed", look_then_record)
    published = len(s0.stubs.hub.events)
    assert release(job) == Released(a)
    assert held() == NOTHING_HELD
    facts = written(job)
    assert facts["allocations"] == [(a, "released", True, 1, 1500), (a, "released", True, 2, 2048),
                                    (a, "released", True, 3, 4000)]
    assert facts["audit"] == [(a, "allocate", "committed"), (a, "release", "committed")]
    assert facts["history"][-1] == (state, "released", "E3", "release committed")
    assert seen_by_e2 == [("released", 0), ("released", a)]
    assert s0.stubs.e2.calls == [("capacity_changed", ())]
    events = s0.stubs.hub.events[published:]
    assert [(event["type"], event.get("state")) for event in events] == [("job_state", "released"), ("capacity", None)]
    assert events[1] == {"type": "capacity", "at": events[0]["at"], "resources": [
        {"resource_id": 1, "kind": "cpu", "gpu_index": None, "total": 32000, "reserved": 3200, "allocated": 0},
        {"resource_id": 2, "kind": "ram", "gpu_index": None, "total": 131072, "reserved": 13108, "allocated": 0},
        {"resource_id": 3, "kind": "gpu", "gpu_index": 0, "total": 16384, "reserved": 1639, "allocated": 0},
        {"resource_id": 4, "kind": "gpu", "gpu_index": 1, "total": 16384, "reserved": 1639, "allocated": 0}]}
    assert_invariants(settled=True)


def test_a_second_release_is_already_released_and_changes_nothing(s0: TempDb, json_log: Path):
    """Catches a release not keyed by the open allocation (context §7: a second release returns
    ALREADY_RELEASED and changes nothing): the repeat must give nothing back twice, write no second
    audit row and send no notice; its log line says already_released and the state it found, which
    tells a repeat (released) from a job that never held anything."""
    job = finished()
    a = open_allocation(job)
    assert release(job) == Released(a)
    before = (written(job), held(), notices(s0))
    assert release(job) == AlreadyReleased()
    assert (written(job), held(), notices(s0)) == before
    assert [(line["outcome"], line["allocation_id"], line["state"]) for line in logged(json_log, "release")] == [
        ("released", a, "completed"), ("already_released", None, "released")]
    assert_invariants(settled=True)


def test_two_releases_of_one_job_at_once_give_one_released_and_one_already_released(s0: TempDb):
    """Catches the open allocation read without its lock, or found by job alone: both calls are inside
    their transactions before either commits (one holds the allocation and waits on the gated CPU row,
    the other waits on the allocation), so the second must see the first's commit and change nothing.
    The capacity comes back once, one release audit row is written and E2 hears once."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        for run in range(1, RUNS + 1):
            job = finished()
            outcomes = race(s0, pool, [partial(release, job)] * 2)
            assert sorted(type(outcome).__name__ for outcome in outcomes) == ["AlreadyReleased", "Released"]
            assert held() == NOTHING_HELD
            assert [action for _, action, _ in written(job)["audit"]] == ["allocate", "release"]
            assert notices(s0)[0] == run
    assert_invariants(settled=True)


def test_a_release_waiting_on_the_cpu_row_holds_its_allocation_and_nothing_else(s0: TempDb):
    """Catches the lock order of invariant 2 broken: the allocation not locked first, the resource rows
    locked out of id order or by the writes instead of the one lock statement, or the job row locked
    (transition()) before the resource rows. While release() waits on the CPU row, the lowest id of
    every job's rows, it must wait in the lock statement and hold its allocation and no other row:
    RAM, both GPUs and the job row each lock at once (NOWAIT), the allocation does not."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        for _ in range(RUNS):
            job = finished(vector=Vector(100, 64, 100))
            a = open_allocation(job)
            with gate(s0):
                future = pool.submit(release, job)
                wait_until_waiting(s0, 1, [future])
                waiting = waiting_statements(s0)
                busy = locked_elsewhere(s0, job, a)
            assert len(waiting) == 1 and "ORDER BY resource_id FOR UPDATE" in waiting[0], waiting
            assert busy == ["allocation"], f"release() held {busy} while it waited on the CPU row"
            assert future.result(timeout=30) == Released(a)
    assert_invariants(settled=True)


def test_releases_racing_allocations_on_the_same_rows_never_deadlock(s0: TempDb, json_log: Path):
    """Catches a release whose lock order differs from allocate()'s, which deadlocks (40P01; retrying()
    would hide it, so the log is read), and any leak or double count where the two meet: each round
    two releases and two allocations of other jobs start together behind the gated CPU row, all on CPU,
    RAM and the GPUs, and every call must succeed with the invariants clean after the round."""
    need = Vector(100, 64, 100)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for _ in range(RUNS):
            done = [finished(vector=need) for _ in range(2)]
            admitted = [make_job("admitted", need) for _ in range(2)]
            outcomes = race(s0, pool, [partial(release, job) for job in done]
                            + [partial(allocate, job, need) for job in admitted])
            assert [type(outcome).__name__ for outcome in outcomes] == ["Released", "Released", "Committed", "Committed"]
            assert_invariants()
    assert logged(json_log, "tx_retry") + logged(json_log, "tx_retry_exhausted") == []
    assert_invariants(settled=True)


def test_released_capacity_can_be_allocated_again_down_to_the_last_unit(s0: TempDb):
    """Catches units given back short, twice or to the wrong row: jobs A and B take every unit outside
    the reserve, A all of GPU 0 and B all of GPU 1. Once A is released, a job with A's vector must get
    all of it again on GPU 0, and then nothing more fits."""
    a_need = Vector(28700, 117900, 14745)
    a = finished(vector=a_need)  # GPU 0: both GPUs are free and the tie goes to the lower resource_id
    make_job("running", Vector(100, 64, 14745))  # GPU 1, the only one left
    assert held() == FULL
    first = open_allocation(a)
    assert release(a) == Released(first)
    assert held() == {1: 100, 2: 64, 3: 0, 4: 14745}
    again = allocate(make_job("admitted", a_need), a_need)
    assert isinstance(again, Committed) and again.gpu_index == 0
    assert held() == FULL
    assert isinstance(allocate(make_job("admitted", Vector(1, 64)), Vector(1, 64)), Refused)
    assert_invariants(settled=True)


def test_a_requeued_job_releases_its_new_allocation_and_never_the_old_one(s0: TempDb):
    """Catches a release that finds the allocation by job alone: after a lost worker the job is
    released, requeued under the same job_id, allocated again and finished again, and the second
    release must close the second allocation and give back its units, once."""
    need = Vector(100, 64, 100)
    job = finished("failed", need)
    first = open_allocation(job)
    assert release(job) == Released(first)
    for step in ("queued", "admitted"):
        with tx("E2") as cur:
            transition(cur, job, step, "E2", f"test: {step}")
    assert isinstance(allocate(job, need), Committed)
    for step in ("running", "completed"):
        with tx("E1") as cur:
            transition(cur, job, step, "E1", f"test: {step}")
    second = open_allocation(job)
    assert release(job) == Released(second)
    assert held() == NOTHING_HELD
    assert written(job)["audit"] == [(first, "allocate", "committed"), (first, "release", "committed"),
                                     (second, "allocate", "committed"), (second, "release", "committed")]
    assert_invariants(settled=True)


@pytest.mark.parametrize("state", ["allocated", "running"])
def test_a_release_before_the_stop_is_confirmed_is_refused_and_changes_nothing(s0: TempDb, json_log: Path,
                                                                               state: JobState):
    """Catches capacity handed back while the job's processes may still run (invariant 4: stop,
    confirm, release): a job still allocated or running is refused before any write, nothing is sent,
    and the refusal is logged like every illegal move."""
    job = make_job(state, Vector(100, 64))
    before = (written(job), held(), notices(s0))
    with pytest.raises(IllegalTransition, match=f"job {job} is {state}"):
        release(job)
    assert (written(job), held(), notices(s0)) == before
    assert [line["job_id"] for line in logged(json_log, "illegal_transition")] == [job]
    assert logged(json_log, "release") == []
    assert_invariants(settled=True)


def test_a_job_holding_nothing_is_already_released_and_an_unknown_job_is_not_found(s0: TempDb, json_log: Path):
    """Catches a no-op that writes or notifies, and a wrong id that passes silently: a job cancelled
    while queued held nothing and ends cancelled (context §9), so release() returns AlreadyReleased,
    changes nothing and logs the state it found; a job id that does not exist raises NotFound."""
    job = make_job("cancelled")
    before = (written(job), held(), notices(s0))
    assert release(job) == AlreadyReleased()
    assert (written(job), held(), notices(s0)) == before
    assert before[0]["state"] == [("cancelled",)]
    assert [(line["outcome"], line["state"]) for line in logged(json_log, "release")] == [
        ("already_released", "cancelled")]
    with pytest.raises(NotFound, match=str(NO_SUCH_JOB)):
        release(NO_SUCH_JOB)
    assert (held(), notices(s0)) == before[1:]
    assert_invariants(settled=True)


def test_a_failure_between_the_capacity_update_and_the_audit_rolls_everything_back(
        s0: TempDb, monkeypatch: pytest.MonkeyPatch):
    """Catches release() split over more than one transaction (context §7: a failure between update
    and audit rolls everything back): the audit insert fails after the capacity is given back and the
    allocation closed, and all of it must roll back with nothing sent; a later release() then works."""
    job = finished(vector=Vector(1000, 512, 1000))
    a = open_allocation(job)
    before = (written(job), held(), notices(s0))

    def audit_fails(*args: object) -> None:
        raise RuntimeError("audit insert failed")

    with monkeypatch.context() as patched:  # undo() would also undo the db fixture's engines
        patched.setattr(service, "insert_release_audit", audit_fails)
        with pytest.raises(RuntimeError, match="audit insert failed"):
            release(job)
    assert (written(job), held(), notices(s0)) == before
    assert release(job) == Released(a)
    calls, events = before[2]
    assert notices(s0) == (calls + 1, events + 2)  # the later release sends its notices
    assert_invariants(settled=True)


def test_a_try_that_rolls_back_sends_nothing(s0: TempDb, monkeypatch: pytest.MonkeyPatch, json_log: Path):
    """Catches a notice or a release line from a transaction that never committed: the failure comes
    after every write and every after_commit registration."""
    job = finished(vector=Vector(1000, 512, 1000))
    a = open_allocation(job)
    before = (written(job), held(), notices(s0))
    with monkeypatch.context() as patched:  # undo() would also undo the db fixture's engines
        fail_at_end_of_body(patched, RuntimeError("forced failure"))
        with pytest.raises(RuntimeError, match="forced failure"):
            release(job)
    assert (written(job), held(), notices(s0)) == before
    assert logged(json_log, "release") == []
    assert release(job) == Released(a)
    calls, events = before[2]
    assert notices(s0) == (calls + 1, events + 2)  # the later release sends its notices
    assert_invariants(settled=True)


def test_a_failing_e2_notice_keeps_the_release_and_the_log_names_the_job(s0: TempDb, monkeypatch: pytest.MonkeyPatch,
                                                                         json_log: Path):
    """Catches an E2 notice that undoes the release or cancels the capacity push when it fails after
    COMMIT (D-47), and a failure line that does not name the job: the release stands, the capacity
    event still goes out, E2's admission loop still finds the capacity on its 0.5 s cycle, and
    after_commit_failed names the job."""
    job = finished()
    a = open_allocation(job)
    published = len(s0.stubs.hub.events)

    def unreachable() -> None:
        raise ConnectionError("E2 unreachable")

    monkeypatch.setattr(s0.stubs.e2, "capacity_changed", unreachable)
    assert release(job) == Released(a)
    assert held() == NOTHING_HELD and written(job)["state"] == [("released",)]
    assert [event["type"] for event in s0.stubs.hub.events[published:]] == ["job_state", "capacity"]
    failed = logged(json_log, "after_commit_failed")
    assert len(failed) == 1 and failed[0]["engine"] == "E3" and "E2 unreachable" in failed[0]["error"]
    assert "_capacity_changed" in failed[0]["hook"] and f", {job})" in failed[0]["hook"]
    assert_invariants(settled=True)


def test_a_deadlock_replays_the_release_and_its_notices_leave_once(s0: TempDb, monkeypatch: pytest.MonkeyPatch,
                                                                  json_log: Path):
    """Catches notices kept from a try that rolled back (E2 woken twice, a second job_state event) and
    capacity given back twice: the first try ends in 40P01 after every write, and retrying() runs the
    whole transaction again."""
    job = finished(vector=Vector(1000, 512, 1000))
    a = open_allocation(job)
    calls, published = notices(s0)
    fail_at_end_of_body(monkeypatch, errors.DeadlockDetected("forced deadlock"))
    assert release(job) == Released(a)
    assert held() == NOTHING_HELD
    assert notices(s0) == (calls + 1, published + 2)
    assert [event["type"] for event in s0.stubs.hub.events[published:]] == ["job_state", "capacity"]
    assert [record["sqlstate"] for record in logged(json_log, "tx_retry")] == ["40P01"]
    assert [line["outcome"] for line in logged(json_log, "release")] == ["released"]
    assert_invariants(settled=True)
