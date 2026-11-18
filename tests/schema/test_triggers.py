"""Faculty-only links and the M1 test-case check (plan 4.2 triggers 2, 3 and 5; baseline section 11)."""

NEW_SESSION = ("INSERT INTO lab_sessions (faculty_id, kind, opens_at, closes_at) "
               "VALUES (%s, 'lab', now(), now() + interval '2 hours')")
NEW_PROBLEM = "INSERT INTO problems (created_by, title, time_limit_ms) VALUES (%s, 'Echo', 1000) RETURNING problem_id"
NEW_FACULTY = ("INSERT INTO users (user_id, role, password_hash) "
               "SELECT %s, 'faculty', password_hash FROM users WHERE user_id = 'FAC001'")
NEW_ATTEMPT = "INSERT INTO attempts (job_id, attempt_no) VALUES (%s, 1) RETURNING attempt_id"
NEW_RESULT = "INSERT INTO test_results (attempt_id, case_no, verdict, time_ms) VALUES (%s, %s, 'AC', 12)"
MAKE_ADMIN = "UPDATE users SET role = 'administrator' WHERE user_id = %s"


def test_lab_session_needs_a_faculty_user(owner_tx):
    for not_faculty in ("01FE24BCS901", "ADM001"):
        with owner_tx.refused("FA001"):
            owner_tx.run(NEW_SESSION, (not_faculty,))
    with owner_tx.refused("FA001"):
        owner_tx.run("UPDATE lab_sessions SET faculty_id = '01FE24BCS901' WHERE lab_session_id = 7")
    with owner_tx.conn.transaction(force_rollback=True):  # a temp "users" must not fool the check
        owner_tx.run("CREATE TEMP TABLE users (user_id text, role text)")
        owner_tx.run("INSERT INTO pg_temp.users VALUES ('01FE24BCS901', 'faculty')")
        with owner_tx.refused("FA001"):
            owner_tx.run(NEW_SESSION, ("01FE24BCS901",))
    owner_tx.run(NEW_SESSION, ("FAC001",))


def test_problem_needs_a_faculty_author(owner_tx):
    for not_faculty in ("01FE24BCS901", "ADM001"):
        with owner_tx.refused("FA001"):
            owner_tx.run(NEW_PROBLEM, (not_faculty,))
    with owner_tx.refused("FA001"):
        owner_tx.run("UPDATE problems SET created_by = 'ADM001' WHERE problem_id = 12")
    owner_tx.run(NEW_PROBLEM, ("FAC001",))


def test_faculty_role_is_fixed_while_owning_sessions_or_problems(owner_tx):
    for user_id in ("FAC002", "FAC003", "FAC004"):
        owner_tx.run(NEW_FACULTY, (user_id,))
    owner_tx.run(NEW_SESSION, ("FAC003",))  # FAC003 owns a lab session only
    owner_tx.run(NEW_PROBLEM, ("FAC004",))  # FAC004 owns a problem only
    for owner in ("FAC001", "FAC003", "FAC004"):
        with owner_tx.refused("FA002"):
            owner_tx.run(MAKE_ADMIN, (owner,))
    with owner_tx.conn.transaction(force_rollback=True):  # empty temp tables must not hide what FAC001 owns
        owner_tx.run("CREATE TEMP TABLE lab_sessions (faculty_id text)")
        owner_tx.run("CREATE TEMP TABLE problems (created_by text)")
        with owner_tx.refused("FA002"):
            owner_tx.run(MAKE_ADMIN, ("FAC001",))
    owner_tx.run(MAKE_ADMIN, ("FAC002",))  # owns nothing, so the role may change
    assert owner_tx.one("SELECT role FROM users WHERE user_id = 'FAC002'") == ("administrator",)


def test_result_case_must_belong_to_the_attempts_problem(owner_tx):
    other = owner_tx.one(NEW_PROBLEM, ("FAC001",))[0]
    owner_tx.run("INSERT INTO test_cases VALUES (%s, 11, 'in', 'out')", (other,))  # case 11 exists elsewhere
    job = owner_tx.new_job(type_code="code_eval", problem_id=12, need_class=2)
    attempt = owner_tx.one(NEW_ATTEMPT, (job,))[0]
    with owner_tx.refused("TR001"):
        owner_tx.run(NEW_RESULT, (attempt, 11))  # problem 12 has cases 1 to 10
    with owner_tx.conn.transaction(force_rollback=True):  # a temp test_cases must not add case 11
        owner_tx.run("CREATE TEMP TABLE test_cases (problem_id bigint, case_no integer)")
        owner_tx.run("INSERT INTO pg_temp.test_cases VALUES (12, 11)")
        with owner_tx.refused("TR001"):
            owner_tx.run(NEW_RESULT, (attempt, 11))
    owner_tx.run(NEW_RESULT, (attempt, 10))
    with owner_tx.refused("TR001"):
        owner_tx.run("UPDATE test_results SET case_no = 11 WHERE attempt_id = %s", (attempt,))
    no_problem = owner_tx.one(NEW_ATTEMPT, (owner_tx.new_job(),))[0]
    with owner_tx.refused("TR001"):
        owner_tx.run(NEW_RESULT, (no_problem, 1))
