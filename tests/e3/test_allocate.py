"""E3-2 allocate() (plan 6.3, 6.6; D-01, D-47, D-68, D-70 to D-72) on real PostgreSQL with the S0 seed.

One tx("E3") locks the resource rows in id order and re-checks with fit() on the locked rows. It then
either commits the capacity, the allocation, its items, the audit row and the move to allocated, or
commits the refused audit row and the move back to queued. Dispatch and the pushes leave only after
COMMIT. S0 leaves 28,800 millicores, 117,964 MiB and 14,745 MiB per GPU outside the reserve; resource
ids are 1 cpu, 2 ram, 3 gpu 0, 4 gpu 1. Each docstring names the bug the test catches.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import errors

from app.common.db import tx
from app.common.errors import IllegalTransition, NotFound
from app.common.types import Committed, Limits, Refused, Vector
from app.e3_allocation import allocate, service
from tests.helpers import TempDb, assert_invariants, fail_at_end_of_body, make_job

NOTHING_HELD = {1: 0, 2: 0, 3: 0, 4: 0}
NO_SUCH_JOB = 999_999_999  # job ids start at 1042 and every test has a fresh database


def held() -> dict[int, int]:
    """resources.allocated by resource_id."""
    with tx("E4") as cur:
        rows = cur.execute("SELECT resource_id, allocated FROM resources ORDER BY resource_id").fetchall()
    return {resource_id: allocated for resource_id, allocated in rows}


def counts() -> tuple[int, int, int]:
    """Rows in allocations, allocation_items and allocation_audit."""
    with tx("E4") as cur:
        row = cur.execute("SELECT (SELECT count(*) FROM allocations), (SELECT count(*) FROM allocation_items), "
                          "(SELECT count(*) FROM allocation_audit)").fetchone()
    assert row is not None
    return row[0], row[1], row[2]


def written(job: int) -> dict[str, list[tuple[Any, ...]]]:
    """What E3 and the state trigger hold for job: its allocations with their items, its audit rows,
    its history, and its state with submitted_at."""
    with tx("E4") as cur:
        return {
            "allocations": cur.execute(
                "SELECT a.allocation_id, a.status, i.resource_id, i.amount FROM allocations a "
                "LEFT JOIN allocation_items i USING (allocation_id) WHERE a.job_id = %s "
                "ORDER BY a.allocation_id, i.resource_id", (job,)).fetchall(),
            "audit": cur.execute("SELECT allocation_id, action, outcome FROM allocation_audit WHERE job_id = %s "
                                 "ORDER BY audit_id", (job,)).fetchall(),
            "history": cur.execute("SELECT from_state, to_state, actor, reason FROM job_state_history "
                                   "WHERE job_id = %s ORDER BY event_id", (job,)).fetchall(),
            "job": cur.execute("SELECT state, submitted_at FROM jobs WHERE job_id = %s", (job,)).fetchall(),
        }


def logged(path: Path, event: str) -> list[dict[str, Any]]:
    return [record for record in map(json.loads, path.read_text(encoding="utf-8").splitlines())
            if record["event"] == event]


def test_a_commit_writes_capacity_allocation_items_audit_and_move_together(s0: TempDb,
                                                                           monkeypatch: pytest.MonkeyPatch):
    """Catches capacity charged without its items (invariant 1), a missing audit row or move, the
    wrong GPU or Limits sent to E1, and a dispatch before COMMIT: the stub refuses a call inside a
    transaction, and this dispatch also looks from another connection, which sees committed rows only."""
    need = Vector(1500, 2048, 4000)
    job = make_job("admitted", need)
    seen_by_dispatch: list[tuple[Any, ...]] = []
    dispatch = s0.stubs.e1.dispatch

    def dispatch_and_look(job_id: int, allocation_id: int, limits: Limits, file_ref: str) -> int:
        with psycopg.connect(s0.owner_url) as other:
            seen_by_dispatch.extend(other.execute("SELECT allocation_id, status FROM allocations WHERE job_id = %s",
                                                  (job_id,)).fetchall())
        return dispatch(job_id, allocation_id, limits, file_ref)

    monkeypatch.setattr(s0.stubs.e1, "dispatch", dispatch_and_look)
    published = len(s0.stubs.hub.events)
    result = allocate(job, need)
    # GPU 0 and GPU 1 both have 14,745 MiB free: the tie goes to the lower resource_id, GPU 0.
    assert isinstance(result, Committed) and result.gpu_index == 0
    a = result.allocation_id
    assert held() == {1: 1500, 2: 2048, 3: 4000, 4: 0}
    facts = written(job)
    assert facts["allocations"] == [(a, "open", 1, 1500), (a, "open", 2, 2048), (a, "open", 3, 4000)]
    assert facts["audit"] == [(a, "allocate", "committed")]
    assert facts["history"][-1] == ("admitted", "allocated", "E3", "allocation committed")
    assert seen_by_dispatch == [(a, "open")]
    assert s0.stubs.e1.calls == [("dispatch", (job, a, Limits(1500, 2048, 0, 5), "payloads/test"))]
    events = {event["type"]: event for event in s0.stubs.hub.events[published:]}
    assert len(s0.stubs.hub.events) - published == 2 and events["job_state"]["state"] == "allocated"
    assert events["capacity"] == {"type": "capacity", "at": events["job_state"]["at"], "resources": [
        {"resource_id": 1, "kind": "cpu", "gpu_index": None, "total": 32000, "reserved": 3200, "allocated": 1500},
        {"resource_id": 2, "kind": "ram", "gpu_index": None, "total": 131072, "reserved": 13108, "allocated": 2048},
        {"resource_id": 3, "kind": "gpu", "gpu_index": 0, "total": 16384, "reserved": 1639, "allocated": 4000},
        {"resource_id": 4, "kind": "gpu", "gpu_index": 1, "total": 16384, "reserved": 1639, "allocated": 0}]}
    assert_invariants(settled=True)


@pytest.mark.parametrize("need", [Vector(28801, 64), Vector(100, 117965), Vector(100, 64, 14746)],
                         ids=["cpu short", "ram short", "vram short"])
def test_a_refusal_commits_one_refused_audit_row_and_requeues_the_job(s0: TempDb, json_log: Path, need: Vector):
    """Catches a refused audit row rolled back with the refusal (D-01), capacity charged for a refused
    job, a job left admitted or sent to the back of the queue (submitted_at must stay), and a dispatch
    or capacity push for a job that got nothing. Each case is one unit past what is free."""
    job = make_job("admitted", need)
    before = written(job)
    published = len(s0.stubs.hub.events)
    assert isinstance(allocate(job, need), Refused)
    facts = written(job)
    assert held() == NOTHING_HELD and facts["allocations"] == []
    assert facts["audit"] == [(None, "allocate", "refused")]
    assert facts["history"] == before["history"] + [("admitted", "queued", "E3", "refused: capacity taken first")]
    assert facts["job"] == [("queued", before["job"][0][1])]
    assert [(event["type"], event["state"]) for event in s0.stubs.hub.events[published:]] == [("job_state", "queued")]
    assert s0.stubs.e1.calls == []
    assert [(line["job_id"], line["outcome"]) for line in logged(json_log, "allocate")] == [(job, "refused")]
    assert_invariants(settled=True)


def test_all_that_is_free_commits_and_the_reserve_is_never_given(s0: TempDb):
    """Catches a re-check that ignores the 10% reserve: the next millicore would get past fit() and
    reach the CHECK as a raw CheckViolation instead of a Refused (NFR-06, invariant 7)."""
    everything = Vector(28800, 117964, 14745)
    assert isinstance(allocate(make_job("admitted", everything), everything), Committed)
    assert held() == {1: 28800, 2: 117964, 3: 14745, 4: 0}
    one_more = Vector(1, 64)
    assert isinstance(allocate(make_job("admitted", one_more), one_more), Refused)
    assert held() == {1: 28800, 2: 117964, 3: 14745, 4: 0}
    assert_invariants(settled=True)


@pytest.mark.parametrize(("need", "first", "state"), [(Vector(1000, 512), Committed, "allocated"),
                                                      (Vector(28801, 64), Refused, "queued")],
                         ids=["after a commit", "after a refusal"])
def test_a_repeated_allocate_changes_nothing_and_says_why(s0: TempDb, need: Vector,
                                                          first: type[Committed | Refused], state: str):
    """Catches a repeat that charges capacity again, writes a second audit row, fails on the
    one-open-allocation index as a raw database error, or dispatches or publishes again (D-70): the
    second call raises IllegalTransition naming the job's state, and nothing changes."""
    job = make_job("admitted", need)
    assert isinstance(allocate(job, need), first)
    before = (held(), written(job), len(s0.stubs.hub.events), len(s0.stubs.e1.calls))
    with pytest.raises(IllegalTransition, match=f"job {job} is {state}, not admitted"):
        allocate(job, need)
    assert (held(), written(job), len(s0.stubs.hub.events), len(s0.stubs.e1.calls)) == before
    assert_invariants(settled=True)


def test_a_job_that_does_not_exist_is_not_found_and_nothing_is_written(s0: TempDb):
    """Catches a refusal or a charge recorded for a job id that names nothing."""
    with pytest.raises(NotFound):
        allocate(NO_SUCH_JOB, Vector(1000, 512))
    assert held() == NOTHING_HELD and counts() == (0, 0, 0)
    assert s0.stubs.hub.events == [] and s0.stubs.e1.calls == []
    assert_invariants(settled=True)


def test_with_no_capacity_loaded_allocate_fails_loudly_and_writes_nothing(db: TempDb):
    """Catches an empty resources table taken as zero capacity: every job would be refused and
    requeued without a word, forever (D-69)."""
    job = make_job("admitted")
    before = written(job)
    with pytest.raises(ValueError, match="no cpu row"):
        allocate(job, Vector(1000, 512))
    assert written(job) == before and counts() == (0, 0, 0)
    assert_invariants(settled=True)


def test_a_failure_after_the_writes_leaves_nothing_and_sends_nothing(s0: TempDb, monkeypatch: pytest.MonkeyPatch,
                                                                    json_log: Path):
    """Catches a hook that runs after a rollback and work that leaves before COMMIT, the allocate log
    line included: the failure comes after every write and every after_commit registration."""
    need = Vector(1000, 512, 1000)
    job = make_job("admitted", need)
    before = (written(job), len(s0.stubs.hub.events))
    fail_at_end_of_body(monkeypatch, RuntimeError("forced failure"))
    with pytest.raises(RuntimeError, match="forced failure"):
        allocate(job, need)
    assert held() == NOTHING_HELD and counts() == (0, 0, 0)
    assert (written(job), len(s0.stubs.hub.events)) == before and s0.stubs.e1.calls == []
    assert logged(json_log, "allocate") == []
    assert_invariants(settled=True)


def test_a_failure_between_update_and_audit_rolls_everything_back(s0: TempDb, monkeypatch: pytest.MonkeyPatch):
    """Catches allocate() split into more than one transaction (invariant 2): the audit insert fails
    after the capacity, the allocation and its items are written, and all of it must roll back
    together, with nothing dispatched or published (context §7: a failure between update and audit)."""
    need = Vector(1000, 512, 1000)
    job = make_job("admitted", need)
    before = (written(job), len(s0.stubs.hub.events))

    def audit_fails(*args: object) -> None:
        raise RuntimeError("audit insert failed")

    monkeypatch.setattr(service, "insert_audit", audit_fails)
    with pytest.raises(RuntimeError, match="audit insert failed"):
        allocate(job, need)
    assert held() == NOTHING_HELD and counts() == (0, 0, 0)
    assert (written(job), len(s0.stubs.hub.events)) == before and s0.stubs.e1.calls == []
    assert_invariants(settled=True)


def test_the_allocate_line_follows_commit_and_comes_before_dispatch(s0: TempDb, monkeypatch: pytest.MonkeyPatch,
                                                                    json_log: Path):
    """Catches an allocate line (plan 5.5) written only after the hooks, so a dispatch that hangs hides
    that the job holds capacity: the line is already there when dispatch starts, with the outcome, the
    allocation and the transaction time."""
    need = Vector(1000, 512)
    job = make_job("admitted", need)
    lines_at_dispatch: list[dict[str, Any]] = []
    dispatch = s0.stubs.e1.dispatch

    def dispatch_after_the_line(job_id: int, allocation_id: int, limits: Limits, file_ref: str) -> int:
        lines_at_dispatch.extend(logged(json_log, "allocate"))
        return dispatch(job_id, allocation_id, limits, file_ref)

    monkeypatch.setattr(s0.stubs.e1, "dispatch", dispatch_after_the_line)
    result = allocate(job, need)
    assert isinstance(result, Committed)
    assert [(line["engine"], line["job_id"], line["outcome"], line["allocation_id"], line["gpu_index"])
            for line in lines_at_dispatch] == [("E3", job, "committed", result.allocation_id, None)]
    assert lines_at_dispatch[0]["ms"] >= 0
    assert_invariants(settled=True)


def test_a_deadlock_replays_the_whole_transaction_and_its_hooks_run_once(s0: TempDb, monkeypatch: pytest.MonkeyPatch,
                                                                         json_log: Path):
    """Catches hooks kept from a try that rolled back (a dispatch of an allocation never committed, a
    second push) and capacity charged twice: the first try ends in 40P01 after every write, and
    retrying() runs the whole transaction again."""
    need = Vector(1000, 512, 1000)
    job = make_job("admitted", need)
    published = len(s0.stubs.hub.events)
    fail_at_end_of_body(monkeypatch, errors.DeadlockDetected("forced deadlock"))
    result = allocate(job, need)
    assert isinstance(result, Committed)
    assert held() == {1: 1000, 2: 512, 3: 1000, 4: 0} and counts() == (1, 3, 1)
    assert s0.stubs.e1.calls == [("dispatch", (job, result.allocation_id, Limits(1000, 512, 0, 5), "payloads/test"))]
    assert sorted(event["type"] for event in s0.stubs.hub.events[published:]) == ["capacity", "job_state"]
    assert [record["sqlstate"] for record in logged(json_log, "tx_retry")] == ["40P01"]
    assert_invariants(settled=True)


def test_a_failing_dispatch_keeps_the_commit_and_the_log_names_the_job(s0: TempDb, monkeypatch: pytest.MonkeyPatch,
                                                                       json_log: Path):
    """Catches a dispatch error that undoes the allocation, cancels the capacity push or reaches E2 as
    a failed allocate (D-47, D-72): the job stays allocated with its open allocation for E1 and the
    startup reconciliation to end, and the log line names the job and the allocation."""
    need = Vector(1000, 512)
    job = make_job("admitted", need)

    def unreachable(job_id: int, allocation_id: int, limits: Limits, file_ref: str) -> int:
        raise ConnectionError("E1 unreachable")

    monkeypatch.setattr(s0.stubs.e1, "dispatch", unreachable)
    published = len(s0.stubs.hub.events)
    result = allocate(job, need)
    assert isinstance(result, Committed)
    assert written(job)["job"][0][0] == "allocated" and held() == {1: 1000, 2: 512, 3: 0, 4: 0}
    assert sorted(event["type"] for event in s0.stubs.hub.events[published:]) == ["capacity", "job_state"]
    failed = logged(json_log, "after_commit_failed")
    assert len(failed) == 1 and failed[0]["engine"] == "E3" and "E1 unreachable" in failed[0]["error"]
    assert "_dispatch" in failed[0]["hook"] and f">, {job}, {result.allocation_id}, " in failed[0]["hook"]
    assert_invariants(settled=True)
