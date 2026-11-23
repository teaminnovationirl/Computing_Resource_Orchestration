"""Tables, foreign keys and constraints of db/schema.sql (plan 4.2 and 4.6, baseline section 11)."""
import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg

TABLES = {
    "users", "login_sessions", "lab_sessions", "problems", "test_cases", "workload_types",
    "jobs", "job_state_history", "attempts", "worker_liveness", "test_results",
    "resources", "allocations", "allocation_items", "allocation_audit",
}

# (child, columns, parent, delete rule) with c = CASCADE, n = SET NULL, r = RESTRICT (baseline section 11)
FOREIGN_KEYS = {
    ("login_sessions", ("user_id",), "users", "c"),
    ("lab_sessions", ("faculty_id",), "users", "r"),
    ("problems", ("created_by",), "users", "r"),
    ("test_cases", ("problem_id",), "problems", "c"),
    ("jobs", ("user_id",), "users", "r"),
    ("jobs", ("type_code",), "workload_types", "r"),
    ("jobs", ("lab_session_id",), "lab_sessions", "n"),
    ("jobs", ("problem_id",), "problems", "r"),
    ("job_state_history", ("job_id",), "jobs", "r"),
    ("attempts", ("job_id",), "jobs", "r"),
    ("worker_liveness", ("attempt_id",), "attempts", "c"),
    ("test_results", ("attempt_id",), "attempts", "c"),
    ("allocations", ("job_id",), "jobs", "r"),
    ("allocation_items", ("allocation_id",), "allocations", "c"),
    ("allocation_items", ("resource_id",), "resources", "r"),
    ("allocation_audit", ("job_id",), "jobs", "r"),
    ("allocation_audit", ("allocation_id", "job_id"), "allocations", "r"),  # M2
}

AMOUNTS = ["jobs.cpu_millicores", "jobs.ram_mib", "jobs.vram_mib", "resources.total",
           "resources.reserved", "resources.allocated", "allocation_items.amount"]

AUDIT = "INSERT INTO allocation_audit (job_id, allocation_id, action, outcome) VALUES (%s, %s, %s, %s)"
NEW_GPU = "INSERT INTO resources (kind, gpu_index, total, reserved) VALUES ('gpu', %s, %s, %s)"
NEW_ALLOCATION = "INSERT INTO allocations (job_id) VALUES (%s) RETURNING allocation_id"
RACES = 100


def test_fifteen_tables(owner_tx):
    rows = owner_tx.all("SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
    assert {name for (name,) in rows} == TABLES


def test_seventeen_foreign_keys_with_baseline_delete_rules(owner_tx):
    rows = owner_tx.all("""
        SELECT c.conrelid::regclass::text, array_agg(a.attname::text ORDER BY k.ord),
               c.confrelid::regclass::text, c.confdeltype::text
        FROM pg_constraint c
        CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace
        GROUP BY c.oid, c.conrelid, c.confrelid, c.confdeltype""")
    keys = [(child, tuple(cols), parent, rule) for child, cols, parent, rule in rows]
    assert len(keys) == 17
    assert set(keys) == FOREIGN_KEYS


def test_final_names_and_integer_units(owner_tx):
    types = dict(owner_tx.all("SELECT table_name || '.' || column_name, data_type "
                              "FROM information_schema.columns WHERE table_schema = 'public'"))
    for column in ["users.user_id", "login_sessions.user_id", "jobs.user_id", "jobs.need_class", *AMOUNTS]:
        assert column in types, column
    assert {c: types[c] for c in AMOUNTS} == dict.fromkeys(AMOUNTS, "integer")
    assert [c for c in types if c.endswith(".usn")] == []


def test_allocated_cannot_exceed_total_minus_reserved(owner_tx):
    owner_tx.run("UPDATE resources SET allocated = 28800 WHERE resource_id = 1")  # all allocatable CPU
    with owner_tx.refused("23514", "resources_allocated_ck"):
        owner_tx.run("UPDATE resources SET allocated = 28801 WHERE resource_id = 1")


def test_allocated_cannot_go_below_zero(owner_tx):
    with owner_tx.refused("23514", "resources_allocated_ck"):
        owner_tx.run("UPDATE resources SET allocated = -1 WHERE resource_id = 2")


def test_reserved_is_ceil_of_ten_percent(owner_tx):
    # S0 seed (baseline section 3): free 28,800 millicores, 117,964 MiB of RAM, 14,745 MiB on each GPU
    assert owner_tx.all("SELECT resource_id, total - reserved FROM resources ORDER BY resource_id") == [
        (1, 28800), (2, 117964), (3, 14745), (4, 14745)]
    for gpu_index, total, reserved in [(10, 1, 1), (11, 10, 1), (12, 11, 2), (13, 16384, 1639)]:
        owner_tx.run(NEW_GPU, (gpu_index, total, reserved))
    with owner_tx.refused("23514", "resources_reserve_ck"):  # rounded down instead of up
        owner_tx.run(NEW_GPU, (20, 16384, 1638))
    with owner_tx.refused("23514", "resources_reserve_ck"):  # more than ceil(10%)
        owner_tx.run(NEW_GPU, (21, 10, 2))
    with owner_tx.refused("23514", "resources_reserve_ck"):  # total changed, reserve left behind
        owner_tx.run("UPDATE resources SET total = 64000 WHERE resource_id = 1")


def test_one_open_allocation_per_job(owner_tx):
    job = owner_tx.new_job()
    first = owner_tx.one(NEW_ALLOCATION, (job,))[0]
    with owner_tx.refused("23505", "allocations_one_open_uk"):
        owner_tx.run(NEW_ALLOCATION, (job,))
    owner_tx.run("UPDATE allocations SET status = 'released', released_at = clock_timestamp() "
                 "WHERE allocation_id = %s", (first,))
    owner_tx.run(NEW_ALLOCATION, (job,))  # once the first is released, a new one may open


def test_one_open_attempt_per_job(owner_tx):
    job = owner_tx.new_job()
    first = owner_tx.one("INSERT INTO attempts (job_id, attempt_no) VALUES (%s, 1) RETURNING attempt_id",
                         (job,))[0]
    with owner_tx.refused("23505", "attempts_one_open_uk"):
        owner_tx.run("INSERT INTO attempts (job_id, attempt_no) VALUES (%s, 2)", (job,))
    owner_tx.run("UPDATE attempts SET ended_at = clock_timestamp(), end_reason = 'worker_lost' "
                 "WHERE attempt_id = %s", (first,))
    owner_tx.run("INSERT INTO attempts (job_id, attempt_no) VALUES (%s, 2)", (job,))


def test_a_job_holds_at_most_two_retries(owner_tx):
    """Catches the database's guard of at most 2 retries (FR-14, invariant 8) loosened or dropped: E2
    refuses a third requeue in its own code, so no other test ever writes 3 (Q1-17)."""
    job = owner_tx.new_job()
    owner_tx.run("UPDATE jobs SET retry_count = 2 WHERE job_id = %s", (job,))
    for retries in (3, -1):
        with owner_tx.refused("23514", "jobs_retry_count_check"):
            owner_tx.run("UPDATE jobs SET retry_count = %s WHERE job_id = %s", (retries, job))


def test_audit_row_cannot_name_another_job(owner_tx):
    job_a, job_b = owner_tx.new_job(), owner_tx.new_job()
    allocation = owner_tx.one(NEW_ALLOCATION, (job_a,))[0]
    with owner_tx.refused("23503", "audit_allocation_fk"):
        owner_tx.run(AUDIT, (job_b, allocation, "allocate", "committed"))
    owner_tx.run(AUDIT, (job_a, allocation, "allocate", "committed"))


def test_refused_audit_rows_are_allocate_rows_without_allocation(owner_tx):
    job = owner_tx.new_job()
    allocation = owner_tx.one(NEW_ALLOCATION, (job,))[0]
    owner_tx.run(AUDIT, (job, None, "allocate", "refused"))  # D-01: what a refused allocate leaves
    with owner_tx.refused("23514", "audit_refused_ck"):
        owner_tx.run(AUDIT, (job, allocation, "allocate", "refused"))
    with owner_tx.refused("23514", "audit_refused_ck"):
        owner_tx.run(AUDIT, (job, None, "allocate", "committed"))
    with owner_tx.refused("23514", "audit_refused_allocate_ck"):
        owner_tx.run(AUDIT, (job, None, "release", "refused"))


def _take_last_unit(conn: psycopg.Connection, resource_id: int, barrier: threading.Barrier):
    barrier.wait(timeout=10)
    try:
        with conn.transaction():
            conn.execute("SET LOCAL ROLE e3_role")
            conn.execute("UPDATE resources SET allocated = allocated + 1 WHERE resource_id = %s",
                         (resource_id,))
    except psycopg.Error as exc:
        return ("refused", exc.sqlstate, exc.diag.constraint_name)
    return ("committed", None, None)


def test_last_unit_race_commits_exactly_once(urls):
    """Two transactions on separate connections race for the last free unit, 100 times:
    each time exactly one commits and the other gets 23514 from resources_allocated_ck."""
    with psycopg.connect(urls.owner, autocommit=True) as admin:
        row = admin.execute(NEW_GPU + " RETURNING resource_id", (99, 10, 1)).fetchone()  # limit 9
        assert row is not None
        rid = row[0]
        try:
            with (psycopg.connect(urls.app, autocommit=True) as first,
                  psycopg.connect(urls.app, autocommit=True) as second,
                  ThreadPoolExecutor(max_workers=2) as pool):
                for _ in range(RACES):
                    admin.execute("UPDATE resources SET allocated = 8 WHERE resource_id = %s", (rid,))
                    barrier = threading.Barrier(2)
                    futures = [pool.submit(_take_last_unit, c, rid, barrier) for c in (first, second)]
                    outcomes = sorted((f.result(timeout=30) for f in futures), key=str)
                    assert outcomes == [("committed", None, None),
                                        ("refused", "23514", "resources_allocated_ck")]
                    assert admin.execute("SELECT allocated FROM resources WHERE resource_id = %s",
                                         (rid,)).fetchone() == (9,)
        finally:
            admin.execute("DELETE FROM resources WHERE resource_id = %s", (rid,))
