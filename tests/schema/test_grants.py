"""Engine roles and grants (plan 4.1 and 4.2; baseline sections 7 and 12): each engine writes only
its own tables, and UPDATE of jobs.state is the one cross-table write (what transition() runs)."""

ROLES = ["e1_role", "e2_role", "e3_role", "e4_role", "orch_app"]

# Table ownership from CLAUDE.md. allocation_audit is append-only, so E3 may only INSERT there.
OWNED = {
    "e1_role": {"attempts", "problems", "test_cases", "test_results"},
    "e2_role": {"jobs", "workload_types", "lab_sessions"},
    "e3_role": {"resources", "allocations", "allocation_items"},
    "e4_role": {"users", "login_sessions", "worker_liveness"},
}
GRANTED_WRITES = {(role, table, priv) for role, tables in OWNED.items()
                  for table in tables for priv in ("INSERT", "UPDATE", "DELETE")}
GRANTED_WRITES.add(("e3_role", "allocation_audit", "INSERT"))
GRANTED_WRITES |= {(role, "jobs", "UPDATE") for role in ("e1_role", "e3_role", "e4_role")}  # column state only

NEW_TYPE = "INSERT INTO workload_types VALUES ('extra_type', 3, 60)"
NEW_PROBLEM = "INSERT INTO problems (created_by, title, time_limit_ms) VALUES ('FAC001', 'Echo', 1000)"
TOUCH_CPU = "UPDATE resources SET allocated = allocated WHERE resource_id = 1"
TOUCH_ADMIN = "UPDATE users SET password_hash = password_hash WHERE user_id = 'ADM001'"
INSERT_JOB = ("INSERT INTO jobs (user_id, type_code, cpu_millicores, ram_mib, time_limit_s, need_class, "
              "payload_path) VALUES ('01FE24BCS901', 'cpu_bound', 1000, 512, 5, 3, 'payloads/test')")

OWN_WRITES = [("e1_role", NEW_PROBLEM), ("e2_role", NEW_TYPE), ("e3_role", TOUCH_CPU), ("e4_role", TOUCH_ADMIN)]
OTHER_WRITES = [
    ("e1_role", NEW_TYPE), ("e1_role", TOUCH_CPU),
    ("e2_role", TOUCH_ADMIN), ("e2_role", "DELETE FROM allocations"),
    ("e3_role", NEW_PROBLEM), ("e3_role", "UPDATE allocation_audit SET outcome = outcome"),
    ("e4_role", NEW_TYPE), ("e4_role", "DELETE FROM test_results"),
]


def test_write_privileges_match_table_ownership(owner_tx):
    granted = owner_tx.all("""
        SELECT r.rolname::text, c.relname::text, p.priv
        FROM pg_roles r
        CROSS JOIN pg_class c
        CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'TRIGGER']) AS p(priv)
        WHERE r.rolname = ANY(%s) AND c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'
          AND CASE WHEN p.priv IN ('INSERT', 'UPDATE')
                   THEN has_any_column_privilege(r.oid, c.oid, p.priv)  -- column grants count too
                   ELSE has_table_privilege(r.oid, c.oid, p.priv) END""", (ROLES,))
    assert set(granted) == GRANTED_WRITES  # nothing on job_state_history, no TRUNCATE or TRIGGER, none for orch_app
    assert owner_tx.all("""
        SELECT r.rolname::text, array_agg(a.attname::text ORDER BY a.attname)
        FROM pg_roles r CROSS JOIN pg_attribute a
        WHERE r.rolname IN ('e1_role', 'e3_role', 'e4_role', 'orch_app')
          AND a.attrelid = 'jobs'::regclass AND a.attnum > 0 AND NOT a.attisdropped
          AND has_column_privilege(r.oid, a.attrelid, a.attnum, 'UPDATE')
        GROUP BY r.rolname ORDER BY 1""") == [("e1_role", ["state"]), ("e3_role", ["state"]), ("e4_role", ["state"])]
    callers = owner_tx.all("SELECT rolname::text FROM pg_roles WHERE rolname = ANY(%s) "
                           "AND has_function_privilege(oid, 'job_state_track()', 'EXECUTE')", (ROLES,))
    assert callers == []  # the SECURITY DEFINER trigger function is fired, never called (D-40)
    sequences = owner_tx.all("""
        SELECT r.rolname::text, c.relname::text FROM pg_roles r CROSS JOIN pg_class c
        WHERE r.rolname = ANY(%s) AND c.relnamespace = 'public'::regnamespace AND c.relkind = 'S'
          AND has_sequence_privilege(r.oid, c.oid, 'USAGE, SELECT, UPDATE')""", (ROLES,))
    assert sequences == []  # identity columns need no sequence grant (D-40)
    assert owner_tx.all("SELECT rolname::text, rolinherit, rolcanlogin FROM pg_roles "
                        "WHERE rolname = ANY(%s) ORDER BY rolname", (ROLES,)) == [
        ("e1_role", True, False), ("e2_role", True, False), ("e3_role", True, False),
        ("e4_role", True, False), ("orch_app", False, True)]
    assert owner_tx.one("""
        SELECT count(*) FROM pg_roles r CROSS JOIN pg_class c
        WHERE r.rolname = ANY(%s) AND c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'
          AND NOT has_table_privilege(r.oid, c.oid, 'SELECT')""", (ROLES,)) == (0,)  # reads are open


def test_engines_write_only_their_own_tables(app_tx):
    with app_tx.refused("42501"):  # orch_app itself, before any SET ROLE
        app_tx.run(NEW_TYPE)
    for role, statement in OWN_WRITES:
        app_tx.role(role)
        app_tx.run(statement)
    for role, statement in OTHER_WRITES:
        with app_tx.refused("42501"):
            app_tx.role(role)
            app_tx.run(statement)


def test_jobs_state_is_the_only_cross_table_write(app_tx):
    app_tx.run("CREATE TEMP TABLE fake_jobs (job_id bigint, state text)")
    with app_tx.refused("42501"):  # the history writer cannot be attached to a table of our own
        app_tx.run("CREATE TRIGGER fake_track AFTER UPDATE ON fake_jobs "
                   "FOR EACH ROW EXECUTE FUNCTION public.job_state_track()")
    app_tx.role("e2_role")
    job = app_tx.new_job()
    app_tx.move(job, "admitted", actor="E2")
    for role, actor, state in [("e3_role", "E3", "allocated"), ("e1_role", "E1", "running"),
                               ("e4_role", "E4", "failed")]:
        app_tx.role(role)
        app_tx.actor(actor, f"to {state}")
        app_tx.run("UPDATE jobs SET state = %s WHERE job_id = %s", (state, job))  # what transition() runs
        with app_tx.refused("42501"):
            app_tx.run("UPDATE jobs SET retry_count = 1 WHERE job_id = %s", (job,))
        with app_tx.refused("42501"):
            app_tx.run("INSERT INTO job_state_history (job_id, to_state, reason, actor) "
                       "VALUES (%s, 'queued', 'forged', %s)", (job, actor))
        with app_tx.refused("42501"):
            app_tx.run(INSERT_JOB)  # only E2 creates jobs
    assert app_tx.all("SELECT to_state, actor FROM job_state_history WHERE job_id = %s ORDER BY event_id",
                      (job,)) == [("queued", "E2"), ("admitted", "E2"), ("allocated", "E3"),
                                  ("running", "E1"), ("failed", "E4")]
    with app_tx.conn.transaction(force_rollback=True):  # a temp table must not swallow a history row
        app_tx.run("CREATE TEMP TABLE job_state_history (job_id bigint, from_state text, to_state text, "
                   "reason text, actor text, at timestamptz)")
        app_tx.role("e2_role")
        shadowed = app_tx.new_job()
        assert app_tx.one("SELECT count(*) FROM public.job_state_history WHERE job_id = %s",
                          (shadowed,)) == (1,)
