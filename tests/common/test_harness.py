"""The test harness itself: assert_invariants() and the factories (plan 5.6).
Each docstring names the bug the test catches."""
from __future__ import annotations

import pytest

from app.common.db import close_pool, init_pool, tx
from tests.helpers import TempDb, assert_invariants, invariant_queries, make_job, make_problem


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
    with pytest.raises(ValueError, match="queued"):
        make_job(state="running")  # pyright: ignore[reportArgumentType]
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
