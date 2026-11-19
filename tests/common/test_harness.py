"""The test harness itself: assert_invariants() and the factories (plan 5.6).
Each docstring names the bug the test catches."""
from __future__ import annotations

import pytest

from app.common.db import close_pool, init_pool, tx
from app.common.types import Vector
from tests.helpers import ALLOWED, STATES, TempDb, assert_invariants, invariant_queries, make_job, make_problem


def test_the_invariant_file_has_its_two_named_queries():
    """Catches a split of db/invariants.sql that loses a query or glues the two together."""
    queries = invariant_queries()
    assert sorted(queries) == ["invariants", "leaks"]
    assert all(query.count(b";") == 1 and query.rstrip().endswith(b";") for query in queries.values())


def test_assert_invariants_passes_on_a_clean_database(db: TempDb):
    """Catches a helper that fails on an empty, consistent schema."""
    assert_invariants()
    assert_invariants(settled=True)


def test_assert_invariants_fails_and_lists_the_violating_row(db: TempDb):
    """Catches a helper that passes silently: allocated = 5 with no allocation item behind it."""
    with tx("E3") as cur:
        cur.execute("INSERT INTO resources (kind, total, reserved, allocated) VALUES ('cpu', 1000, 100, 5)")
    with pytest.raises(AssertionError) as failed:
        assert_invariants()
    assert "capacity" in str(failed.value) and "allocated=5 held=0" in str(failed.value)


def test_leaks_are_checked_only_once_settled(db: TempDb):
    """Catches settled=True not running the leaks query: an open attempt on a queued job is a leak
    (NFR-07) but breaks none of the invariants that hold while jobs run."""
    job = make_job()
    with tx("E1") as cur:
        cur.execute("INSERT INTO attempts (job_id, attempt_no) VALUES (%s, 1)", (job,))
    assert_invariants()
    with pytest.raises(AssertionError, match="open_attempt_not_running"):
        assert_invariants(settled=True)


def test_make_job_inserts_one_queued_job_through_the_trigger(db: TempDb):
    """Catches a factory that skips the state trigger (no history row), or that leaves app.actor or
    app.reason on the pooled connection, where a later write would pass the JT002 guard with stale
    values. A pool of one, so the check reads the factory's own connection."""
    close_pool()
    init_pool(db.app_url, min_size=1, max_size=1)
    job = make_job()
    with tx("E4") as cur:
        history = cur.execute("SELECT from_state, to_state, actor, reason FROM job_state_history "
                              "WHERE job_id = %s", (job,)).fetchall()
        state = cur.execute("SELECT state FROM jobs WHERE job_id = %s", (job,)).fetchone()
        leftover = cur.execute("SELECT nullif(current_setting('app.actor', true), ''), "
                               "nullif(current_setting('app.reason', true), '')").fetchone()
    assert history == [(None, "queued", "E2", "submitted")]
    assert state == ("queued",) and leftover == (None, None)
    assert_invariants(settled=True)


def test_make_job_reaches_every_state_through_transition(db: TempDb):
    """Catches a factory that writes a state without transition() (no history row), walks a path
    the trigger would refuse, or stops in the wrong state; later slices build test data on it."""
    jobs = {state: make_job(state) for state in STATES}
    with tx("E4") as cur:
        for state, job in jobs.items():
            row = cur.execute("SELECT state FROM jobs WHERE job_id = %s", (job,)).fetchone()
            steps = cur.execute("SELECT from_state, to_state FROM job_state_history WHERE job_id = %s "
                                "ORDER BY event_id", (job,)).fetchall()
            assert row == (state,) and steps[0] == (None, "queued") and steps[-1][1] == state
            assert all(old[1] == new[0] and (new[0], new[1]) in ALLOWED for old, new in zip(steps, steps[1:]))
    assert_invariants(settled=True)


def test_make_job_allocates_allocated_and_running_jobs_through_allocate(db: TempDb):
    """Catches a factory that moves a job to allocated without an allocation: invariant 4 would fail on
    the test data of every later slice, and E3's audit row and dispatch would be missing (D-73). The
    resources table starts empty, so make_job loads S0 first (ids 1 cpu, 2 ram, 3 and 4 GPUs); a
    completed job holds nothing, as after a release."""
    jobs = {state: make_job(state, Vector(1000, 512)) for state in ("allocated", "running", "completed")}
    allocated, running = jobs["allocated"], jobs["running"]
    with tx("E4") as cur:
        held = cur.execute("SELECT a.job_id, i.resource_id, i.amount FROM allocations a "
                           "JOIN allocation_items i USING (allocation_id) WHERE a.status = 'open' "
                           "ORDER BY a.job_id, i.resource_id").fetchall()
        audited = cur.execute("SELECT job_id, outcome FROM allocation_audit ORDER BY audit_id").fetchall()
    assert held == [(allocated, 1, 1000), (allocated, 2, 512), (running, 1, 1000), (running, 2, 512)]
    assert audited == [(allocated, "committed"), (running, "committed")]
    assert [args[0] for name, args in db.stubs.e1.calls if name == "dispatch"] == [allocated, running]
    assert_invariants(settled=True)


def test_make_problem_gives_a_faculty_owner_and_numbered_cases(db: TempDb):
    """Catches a factory that breaks FA001 (only faculty own problems) or the case paths of the seed."""
    problem = make_problem(cases=3)
    with tx("E1") as cur:
        owner = cur.execute("SELECT u.role FROM problems p JOIN users u ON u.user_id = p.created_by "
                            "WHERE p.problem_id = %s", (problem,)).fetchone()
        cases = cur.execute("SELECT case_no, input_path, expected_path FROM test_cases WHERE problem_id = %s "
                            "ORDER BY case_no", (problem,)).fetchall()
    assert owner == ("faculty",)
    assert cases == [(n, f"problems/{problem}/{n:02d}.in", f"problems/{problem}/{n:02d}.out") for n in (1, 2, 3)]
