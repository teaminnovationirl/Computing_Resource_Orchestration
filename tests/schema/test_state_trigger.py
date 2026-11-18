"""The state tracker on jobs (plan 4.2 trigger 4, baseline section 9) and the append-only tables."""

STATES = ["queued", "admitted", "allocated", "running", "completed", "failed", "cancelled", "released"]

# The 13 transitions of baseline section 9, copied by hand
ALLOWED = {
    ("queued", "admitted"), ("queued", "cancelled"),
    ("admitted", "allocated"), ("admitted", "queued"),
    ("allocated", "running"), ("allocated", "cancelled"),
    ("running", "completed"), ("running", "failed"), ("running", "cancelled"),
    ("completed", "released"), ("failed", "released"), ("cancelled", "released"),
    ("released", "queued"),
}

# One allowed way to bring a new (queued) job to each state
PATH = {
    "queued": [],
    "admitted": ["admitted"],
    "allocated": ["admitted", "allocated"],
    "running": ["admitted", "allocated", "running"],
    "completed": ["admitted", "allocated", "running", "completed"],
    "failed": ["admitted", "allocated", "running", "failed"],
    "cancelled": ["cancelled"],
    "released": ["cancelled", "released"],
}

INSERT_JOB = ("INSERT INTO jobs (user_id, type_code, cpu_millicores, ram_mib, time_limit_s, need_class, "
              "payload_path) VALUES ('01FE24BCS901', 'cpu_bound', 1000, 512, 5, 3, 'payloads/test')")
HISTORY = ("SELECT from_state, to_state, reason, actor FROM job_state_history "
           "WHERE job_id = %s ORDER BY event_id")
SET_STATE = "UPDATE jobs SET state = %s WHERE job_id = %s"
STATE_OF = "SELECT state FROM jobs WHERE job_id = %s"


def test_new_job_starts_queued_with_one_history_row(owner_tx):
    with owner_tx.refused("JT001"):
        owner_tx.new_job(state="admitted")
    job = owner_tx.new_job()
    assert owner_tx.all(HISTORY, (job,)) == [(None, "queued", "submitted", "E2")]


def test_each_baseline_transition_writes_one_history_row(owner_tx):
    assert len(ALLOWED) == 13
    for frm, to in sorted(ALLOWED):
        job = owner_tx.new_job()
        owner_tx.move(job, *PATH[frm])
        owner_tx.actor("E3", f"{frm} to {to}")
        owner_tx.run(SET_STATE, (to, job))
        rows = owner_tx.all(HISTORY, (job,))
        assert len(rows) == len(PATH[frm]) + 2, (frm, to)  # first row, the path, this change
        assert rows[-1] == (frm, to, f"{frm} to {to}", "E3")
        assert owner_tx.one(STATE_OF, (job,)) == (to,)


def test_every_other_state_pair_is_refused(owner_tx):
    tried = 0
    for frm in STATES:
        job = owner_tx.new_job()
        owner_tx.move(job, *PATH[frm])
        owner_tx.actor("E4", "not allowed")
        for to in STATES:
            if (frm, to) not in ALLOWED:
                with owner_tx.refused("JT001"):
                    owner_tx.run(SET_STATE, (to, job))
                tried += 1
        assert owner_tx.one(STATE_OF, (job,)) == (frm,)
    assert tried == 51  # 8 x 8 pairs minus the 13 allowed


def test_state_write_without_transition_settings_is_refused(owner_tx):
    with owner_tx.refused("JT002"):  # this connection never set app.actor or app.reason
        owner_tx.run(INSERT_JOB)
    job = owner_tx.new_job()
    owner_tx.actor(None, None)
    with owner_tx.refused("JT002"):
        owner_tx.run(SET_STATE, ("admitted", job))
    owner_tx.actor("E2", None)  # an actor without a reason is not enough
    with owner_tx.refused("JT002"):
        owner_tx.run(SET_STATE, ("admitted", job))


def test_history_times_come_from_clock_timestamp(owner_tx):
    job = owner_tx.new_job()
    owner_tx.move(job, "admitted", "queued", "admitted")
    times = [at for (at,) in owner_tx.all(
        "SELECT at FROM job_state_history WHERE job_id = %s ORDER BY event_id", (job,))]
    assert len(times) == 4
    assert times == sorted(set(times))  # rising inside one transaction; now() would repeat one value
    assert owner_tx.one("SELECT min(at) > now() FROM job_state_history WHERE job_id = %s", (job,)) == (True,)
    together = [owner_tx.new_job() for _ in range(5)]
    owner_tx.actor("E2", "admitted together")
    owner_tx.run("UPDATE jobs SET state = 'admitted' WHERE job_id = ANY(%s)", (together,))
    assert owner_tx.one("SELECT count(DISTINCT at) FROM job_state_history "  # one statement, five rows:
                        "WHERE job_id = ANY(%s) AND to_state = 'admitted'",  # statement_timestamp() gives one
                        (together,))[0] > 1


def test_history_is_append_only(owner_tx):
    job = owner_tx.new_job()
    with owner_tx.refused("AO001"):
        owner_tx.run("UPDATE job_state_history SET reason = 'edited' WHERE job_id = %s", (job,))
    with owner_tx.refused("AO001"):
        owner_tx.run("DELETE FROM job_state_history WHERE job_id = %s", (job,))


def test_audit_is_append_only(owner_tx):
    job = owner_tx.new_job()
    owner_tx.run("INSERT INTO allocation_audit (job_id, action, outcome) VALUES (%s, 'allocate', 'refused')",
                 (job,))
    with owner_tx.refused("AO001"):
        owner_tx.run("UPDATE allocation_audit SET outcome = 'refused' WHERE job_id = %s", (job,))
    with owner_tx.refused("AO001"):
        owner_tx.run("DELETE FROM allocation_audit WHERE job_id = %s", (job,))
