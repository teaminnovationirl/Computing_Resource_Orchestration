# Implementation Plan: Computing Resource Allocation & Utilization Orchestration System

Nov 15, 2026 · @InnovationIRL

## 0. How to use this plan with Claude Code

This document is the build specification for the whole product, from the first commit to the final demo. Claude Code builds it one slice at a time, and the engine owner reviews and runs every slice so they can explain each line at the viva.

The project context file stays the baseline. Where this plan and the baseline disagree, the baseline wins and the plan is corrected. Everything this plan adds that the baseline does not state is marked **proposal** and logged in section 17.

### 0.1 One-time setup of the repository

1. Create a private GitHub repository with an empty `main` branch.
2. Export this doc as Markdown and commit it as `docs/IMPLEMENTATION_PLAN.md`.
3. Commit the project context file as `docs/PROJECT_CONTEXT.md`.
4. Commit the `CLAUDE.md` below at the repository root. Claude Code reads it at the start of every session.
5. Commit `docs/decisions.md` with the decision lines from section 17.

### 0.2 The loop for every slice

1. Create a branch named after the slice, for example `slice/E3-2`.
2. Start a fresh Claude Code session and paste the slice prompt from section 15.
3. Claude Code answers with a plan of at most 3 lines. Approve it or correct it.
4. It writes the SQL, code and tests, runs them, and shows the pytest summary line and the invariant query result.
5. The owner reads the whole diff, runs the slice's run command from a clean checkout, and merges.

Rules: one slice per session; one engine per slice except the integration slices; `db/schema.sql` changes only in the slices that list it, and only after all four owners agree.

### 0.3 CLAUDE.md (commit at the repository root)

```markdown
# CLAUDE.md

Product: Computing Resource Allocation & Utilization Orchestration System
(Team 10 Mini Project, Theme 11). A LAN server where students submit jobs,
mainly C programs to be compiled and judged, that compete for limited CPU,
RAM and GPU memory. One FastAPI process with four engine modules, one
PostgreSQL 16 database of 15 tables, one Docker container per job under
cgroups v2.

Read before writing code, in this order:
1. docs/PROJECT_CONTEXT.md  baseline (FR-01..FR-15, NFR-01..NFR-10, schema,
   messages, states). It wins over everything else.
2. docs/IMPLEMENTATION_PLAN.md  the build plan. Work only on the slice named
   in the prompt (for example E3-2) and read only the sections it cites.
3. docs/decisions.md  decisions already taken. Append one line per new one.

## Engines and table ownership
| Engine        | Package            | Writes only                                         |
|---------------|--------------------|-----------------------------------------------------|
| E1 execution  | app/e1_execution   | attempts, problems, test_cases, test_results        |
| E2 scheduling | app/e2_scheduling  | jobs, workload_types, lab_sessions                  |
| E3 allocation | app/e3_allocation  | resources, allocations, allocation_items, allocation_audit |
| E4 monitoring | app/e4_monitoring  | users, login_sessions, worker_liveness              |
job_state_history is written only by the state trigger. The one cross-table
write is app.e4_monitoring.state.transition(cur, job_id, to_state, actor,
reason), called inside the caller's transaction. Every engine transaction
runs as its own database role: `with tx("E3") as cur:`.
Engines call each other only through app/common/ports.py. Never import
another engine's internal modules.

## Hard invariants (never break)
1. Over-allocation is impossible: allocated never exceeds total minus
   reserved, enforced by row locks, a re-check inside the lock and the CHECK.
2. allocate and release are single transactions. Lock with one statement:
   SELECT ... FROM resources WHERE resource_id = ANY(%s) ORDER BY resource_id
   FOR UPDATE (CPU, RAM and every candidate GPU). Re-check, pick the best-fit
   GPU from the locked rows, then write. Release locks the job's open
   allocation first, then its resource rows in id order. No other lock order.
3. Nothing leaves the process while locks are held: no dispatch, WebSocket
   push or E2 notice inside a transaction. Use cur.after_commit(fn).
4. Release is idempotent (second call returns ALREADY_RELEASED, changes
   nothing) and happens only after the stop is confirmed.
   Order: stop, confirm, release, requeue.
5. Job state changes only through transition(). The trigger checks the
   allowed transitions and writes history; application code never does.
6. Each engine writes only its own tables (enforced by database roles).
7. Integer units only (millicores, MiB). reserved = ceil(10% of total).
8. At most 2 retries. A requeued job keeps its submitted_at. A late
   heartbeat from a lost worker is ignored. A worker is marked lost once.
9. Names: user_id (never usn), cpu_millicores, ram_mib, vram_mib, need_class.

## Coding rules
- Python 3.11+, FastAPI, psycopg 3 with a pool, raw SQL, no ORM, pydantic v2.
- Endpoints are plain def. Background loops are threads, one instance each.
- All timers go through app/common/clock.py. Deadlines use time.monotonic.
  Database times use clock_timestamp().
- Retry only SQLSTATE 40P01 and 40001, at most 3 times, then raise.
- Never shell=True. Every file path must resolve inside DATA_DIR.
- No new tables, columns, endpoints or libraries unless the plan lists them.
  If the plan or the baseline looks wrong, stop and report in 3 lines
  (what, evidence, proposed fix) and wait.
- Numbers come from the baseline. Anything else is a proposal: log it.
- Written text (docs, UI strings): no em or en dashes.

## Tests
- Real PostgreSQL 16 built from db/schema.sql for anything touching locks,
  triggers or constraints. Never SQLite or mocks for the database.
- pytest -q runs everything that needs no Docker. pytest -q -m docker needs
  Linux with cgroup v2 (stat -fc %T /sys/fs/cgroup prints cgroup2fs).
- Concurrency tests: real threads, separate connections, a Barrier, 100
  repetitions. A flaky test is a race: fix the race, never add a sleep.
- Every system test ends with assert_invariants() from tests/helpers.py.

## Commands
docker compose up -d db          # PostgreSQL 16 for development
./scripts/reset_db.sh            # roles, schema, seed
pytest -q                        # unit and integration tests
pytest -q -m docker              # sandbox tests (Linux only)
uvicorn app.main:create_app --factory --workers 1 --port 8000

## A slice is done when
Tests pass (show the summary line), the invariant query returns no rows,
interfaces match app/common/ports.py, docs/decisions.md is updated, and the
slice's run command works from a clean checkout.
```

## 1. What the finished product does

The finished system takes job submissions over the LAN, admits them by FCFS or priority by need, allocates CPU, RAM and GPU memory in one locked transaction, runs and judges each job in its own container under limits, recovers jobs whose worker dies, and shows every state change live within 2 s.

### 1.1 Capabilities, requirements and where they are built

| Capability | Requirements | Engine | Slices (section 14) |
| --- | --- | --- | --- |
| Login, role-based sessions, job visibility | FR-15 | E4 | E4-2, E4-3 |
| Submit, validate every field, Job ID, queue | FR-02, NFR-03 | E2 | E2-1 |
| FCFS and priority by need with the 60 s guard | FR-01, NFR-01 | E2 | E2-2, E2-3 |
| Fit check; a job that does not fit waits in place | FR-03 | E2 | E2-3 |
| Atomic allocation with row locks and the 10% reserve | FR-05, NFR-06 | E3 | E3-2 |
| Audit row for every allocate, refuse and release | FR-06, NFR-05 | E3 | E3-2, E3-3 |
| Dispatch to a worker | FR-07 | E1 | E1-2 |
| CPU, memory and time limits; stop before release | FR-08, NFR-10 | E1 | E1-2, E1-3 |
| Compile, run test cases, verdict per case and overall | FR-09, NFR-03 | E1 | E1-1, E1-4 |
| State history and live dashboard per role | FR-11, NFR-09 | E4 | E4-1, E4-4, C-2, C-3 |
| Heartbeats; worker lost after 6 s | FR-12 | E4 | E4-5 |
| Atomic release after the stop is confirmed | FR-13, NFR-07 | E3 | E3-3 |
| Stop, release, requeue; 2 retries; reconcile at start | FR-14, NFR-08 | E4 | E4-6, E4-7 |
| Cancel by role (Challenge) | FR-10 | E1, E2, E4 | X-1 |
| Pause admission while the host is unhealthy (Challenge) | FR-04 | E4 | X-2 |
| Throughput and utilisation targets | NFR-02, NFR-04 | All | X-4, X-6 |

### 1.2 What done means

- **Sprint 2 exit (end of Week 12):** every Mandatory slice of Phases A, B and C passes its tests, and one lab-exam job runs from submission to release on Docker with the invariant query clean. This is the baseline's Sprint 2 exit condition.
- **Sprint 3 exit (end of Week 15):** both Challenge requirements work; V1, B1, B2, F1, F2 and R1 have run on the demo machine; the KPI report compares every measured value with its NFR target.
- **Order of work:** Mandatory requirements first, then Challenge (FR-04, FR-10), as the baseline requires.

### 1.3 Not built

The baseline's out-of-scope list holds: no multi-node cluster, no distributed transactions, no learning-based scheduling, no Kubernetes, no security certification. Languages for code evaluation are C (required) and C++ (same image, proposal); anything else is out.

## 2. Architecture

The product is one Python process. FastAPI serves the pages and the API, the four engines are packages inside that process, and a few threads drive the clocks. PostgreSQL holds all shared state and Docker runs the untrusted code.

&#91;embedded content: Architecture · one process, four engines, §8 message numbers on the arrows\]

Numbers on the arrows are the message numbers of §8. E3 is highlighted because every other engine depends on its schema and its two transactions, which is why it is built first.

Run uvicorn with exactly one worker process (`--workers 1`). The queue lives in E2's memory and the loops are single instances, so a second process would break both.

### 2.1 What runs, and who owns it

| Runs in | Owner | Triggered by | Database use |
| --- | --- | --- | --- |
| uvicorn event loop | E4 | WebSocket connections | None. Async code never touches the database. |
| Request threads (FastAPI thread pool) | E4 API, then E2 or E1 | Each REST call | One pooled connection per transaction |
| Admission loop thread | E2 | Every 0.5 s, or at once on capacity changed | One connection per cycle |
| Liveness sweep thread | E4 | Every 1 s | One connection per sweep |
| Health probe thread | E4 | Every 1 s (proposal) | None |
| Recovery executor, 4 threads (proposal) | E4 | One task per lost worker | Pooled, short transactions |
| Supervisor thread, one per attempt | E1 | dispatch() | Pooled, short transactions |
| Worker child process, one per attempt | E1 | Started by its supervisor | None. Talks to the supervisor over stdout. |
| Sandbox container, one per attempt | E1 | `docker run` from the supervisor | None. No network. |

### 2.2 One job, message by message

The numbers in brackets are the message numbers of §8.

1. **Submit \[1, 2\].** `POST /api/jobs` reaches E4, which checks the token and calls `e2.submit()`. E2 validates, stores the source file, and inserts the job in `tx("E2")`. The trigger writes the first history row (queued). After commit, E2 adds the job to its queue structures and the hub pushes the new state.
2. **Admit \[3, 4\].** The admission loop reads `e3.snapshot()`, picks a job by the active policy, moves it queued to admitted in `tx("E2")`, and calls `e3.allocate()`.
3. **Allocate \[5\].** In one `tx("E3")`: lock the rows, re-check, write the allocation and its items, write the audit row, move admitted to allocated. After commit: `e1.dispatch()` and a capacity push.
4. **Start.** `dispatch()` inserts the attempt row and hands it to a supervisor thread. The supervisor starts the container, then moves allocated to running and calls `e4.register_worker()`.
5. **Run \[6\].** The worker child compiles and runs the test cases and prints a heartbeat line every 2 s. The supervisor relays each one to `e4.heartbeat()`.
6. **Finish \[7, 8\].** The supervisor claims the end of the attempt with `e4.close_worker()`, stops and confirms the container, stores the verdicts and moves running to completed, then calls `e3.release()`. Release moves completed to released; after commit E2 gets capacity changed and starts a cycle at once.
7. **Lost worker \[9, 10, 11\].** If no heartbeat arrives for 6 s, the sweep marks the worker lost. Recovery calls `e1.stop()`, moves running to failed, calls `e3.release()`, then `e2.requeue()` if fewer than 2 retries were used.
8. **Live view \[12\].** Every committed transition and every capacity change is pushed over `/ws/events`.

### 2.3 Who finishes an attempt

Two parties can try to end the same attempt: the supervisor (job done, deadline, cancel) and the liveness sweep (worker lost). Exactly one wins, decided by one statement on `worker_liveness`:

```sql
UPDATE worker_liveness SET status = %(to)s          -- 'closed' (supervisor) or 'lost' (sweep)
WHERE attempt_id = %(attempt_id)s AND status = 'alive'
RETURNING attempt_id;
```

The caller that gets a row back owns the rest of the attempt: stop, confirm, terminal state, release. The other caller does nothing. This is invariant 8 (a worker is marked lost once) applied to both paths, and it rules out a double release or a double verdict.

## 3. Stack, configuration and repository layout

The stack is the Gate 2 one: Python 3.11 or later, FastAPI with uvicorn, PostgreSQL 16 through psycopg 3 with a pool, pydantic v2, bcrypt, pytest. Docker Engine runs the sandboxes. Pages are plain HTML, CSS and JavaScript with no build step.

### 3.1 Dependencies

Slice A-1 installs these minimum versions, then pins every top-level package with `==` from `pip freeze`. No other library is added without a decision line.

```text
# requirements.txt
fastapi>=0.115
uvicorn[standard]>=0.30        # includes the websockets package used by bench/
psycopg[binary,pool]>=3.2
pydantic>=2.7
bcrypt>=4.1
python-multipart>=0.0.9        # multipart upload of the source file

# requirements-dev.txt
-r requirements.txt
pytest>=8.2
httpx>=0.27                    # FastAPI TestClient
```

Host software: PostgreSQL 16 (a Docker container in development), Docker Engine with cgroup v2, Linux or WSL2 with Ubuntu 22.04 or 24.04.

### 3.2 Development database

```yaml
# docker-compose.yml  (database only; the app runs on the host because it drives Docker)
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: postgres
    ports: ["5432:5432"]
    command: ["postgres", "-c", "log_lock_waits=on", "-c", "deadlock_timeout=1s",
              "-c", "max_connections=200"]
    volumes: ["pgdata:/var/lib/postgresql/data"]
volumes:
  pgdata: {}
```

### 3.3 Configuration (`app/config.py`)

One frozen dataclass read from environment variables, with `.env.example` listing all of them. Baseline numbers are fixed defaults; proposals can be changed by a decision line.

| Variable | Default | Source |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://orch_app:orch@localhost:5432/orch` | proposal |
| `ADMIN_DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/postgres` (tests and scripts only) | proposal |
| `DATA_DIR` | `./data` | proposal |
| `SANDBOX_BACKEND` | `docker` (`fake` in tests) | proposal |
| `SANDBOX_IMAGE` | `judge-c:1` | proposal |
| `GPU_MODE` | `none` (`none`, `simulated`, `nvidia`) | proposal |
| `CAPACITY_PROFILE` | `s0` (`s0` or `host`) | S0 for `s0`; proposal for `host` |
| `ADMISSION_CYCLE_S` | 0.5 | baseline |
| `STARVATION_GUARD_S` | 60 | baseline (FR-01) |
| `HEARTBEAT_S` | 2 | baseline |
| `LOST_AFTER_S` | 6 | baseline (FR-12) |
| `SWEEP_S` | 1 | baseline |
| `MAX_RETRIES` | 2 | baseline (FR-14) |
| `STOP_GRACE_S` | 2 | baseline (SIGTERM, then SIGKILL after 2 s) |
| `PIDS_LIMIT` | 64 | baseline |
| `DEFAULT_POLICY` | `fcfs` | baseline (FCFS is the default) |
| `TOKEN_TTL_S` | 28800 (8 h) | proposal |
| `SOURCE_MAX_BYTES` | 65536 | proposal |
| `OUTPUT_CAP_BYTES` | 65536 per test case | proposal |
| `COMPILE_TIMEOUT_S` | 10 | proposal |
| `STOP_CONFIRM_TIMEOUT_S` | 10 | proposal |
| `DB_POOL_MIN`, `DB_POOL_MAX` | 4, 40 | proposal |
| `HEALTH_MIN_AVAILABLE_PCT` | 10 (host MemAvailable as a share of MemTotal) | proposal, mirrors the 10% reserve |

### 3.4 Repository tree

Every file the finished product has. The slice that creates each file is in brackets.

```text
.
├── CLAUDE.md                           [A-1]
├── README.md                           [A-1, C-4]
├── requirements.txt  requirements-dev.txt  pyproject.toml   [A-1]
├── docker-compose.yml  .env.example  .gitignore            [A-1]
├── db/
│   ├── roles.sql                       [A-2] database roles, once per cluster
│   ├── schema.sql                      [A-2] 15 tables, constraints, indexes, triggers, grants
│   ├── seed.sql                        [A-3] workload types, demo users, session 7, problem 12
│   └── invariants.sql                  [A-3] the invariant and leak queries
├── seed_data/problems/12/              [A-3] 01.in, 01.out ... 10.in, 10.out
├── app/
│   ├── main.py                         [C-1] app factory, lifespan, startup order
│   ├── config.py                       [A-1]
│   ├── common/
│   │   ├── db.py                       [A-4] pool, tx(), after_commit, retry
│   │   ├── clock.py                    [A-4] Clock, RealClock, FakeClock
│   │   ├── events.py                   [A-4] Hub interface, event builders
│   │   ├── ports.py                    [A-4] E1Port..E4Port protocols, Engines registry
│   │   ├── types.py                    [A-4] Vector, Limits, Snapshot, result types
│   │   ├── errors.py                   [A-4] ApiError, IllegalTransition, StopNotConfirmed
│   │   └── logging.py                  [A-4] JSON lines to stdout and data/logs/
│   ├── e3_allocation/
│   │   ├── __init__.py                 public: snapshot, allocate, release
│   │   ├── placement.py                [E3-1] fit check, best-fit GPU (pure functions)
│   │   ├── queries.py                  [E3-1] every SQL statement of E3
│   │   └── service.py                  [E3-1..E3-3]
│   ├── e2_scheduling/
│   │   ├── __init__.py                 public: submit, requeue, cancel_queued, capacity_changed,
│   │   │                               pause_admission, resume_admission, set_policy, queue_view
│   │   ├── validation.py               [E2-1]
│   │   ├── queue.py                    [E2-2] JobQueue: FIFO list, min-heap, hash map
│   │   ├── policy.py                   [E2-3] FCFS and priority by need
│   │   ├── loop.py                     [E2-3] AdmissionLoop thread
│   │   └── service.py                  [E2-1..E2-4]
│   ├── e1_execution/
│   │   ├── __init__.py                 public: dispatch, stop, cancel
│   │   ├── files.py                    [E1-1] job folders, path checks
│   │   ├── judge.py                    [E1-1] output comparison, verdict rules
│   │   ├── sandbox.py                  [E1-1] SandboxRunner protocol, ExecResult
│   │   ├── fake_runner.py              [E1-1]
│   │   ├── docker_runner.py            [E1-3]
│   │   ├── worker.py                   [E1-2, E1-3] child process: python -m app.e1_execution.worker
│   │   ├── launcher.py                 [E1-2] WorkerLauncher: real subprocess or fake thread
│   │   ├── supervisor.py               [E1-2] one thread per attempt
│   │   └── service.py                  [E1-2]
│   └── e4_monitoring/
│       ├── __init__.py                 public: transition, heartbeat, register_worker, close_worker
│       ├── state.py                    [E4-1] transition(), set_actor()
│       ├── auth.py                     [E4-2] bcrypt, tokens, current_user, require_role
│       ├── schemas.py                  [E4-3] pydantic request and response models
│       ├── api.py                      [E4-3, X-1, X-3] REST routers
│       ├── hub.py                      [E4-4] WebSocket hub, /ws/events
│       ├── liveness.py                 [E4-5] register, heartbeat, close, sweep thread
│       ├── recovery.py                 [E4-6, E4-7] recover(), reconcile()
│       └── health.py                   [X-2] host health thread (FR-04)
├── web/                                [C-2, C-3]
│   ├── index.html  submit.html  jobs.html  job.html  monitor.html  admin.html
│   ├── css/app.css
│   └── js/api.js  js/live.js  js/login.js  js/submit.js  js/jobs.js  js/job.js
│       js/monitor.js  js/admin.js
├── sandbox/
│   ├── Dockerfile                      [E1-3] judge-c:1
│   └── bench/ cpu_burn.c mem_touch.c mixed.c gpu_sim.c   [E1-3]
├── bench/                              [E1-4, X-4]
│   ├── client.py                       API client used by every script
│   ├── v1_validation.py  r1_reference.py  b1_normal.py  t1_timeouts.py  b2_saturation.py
│   ├── f1_worker_kill.py  f2_server_crash.py  ws_latency.py
│   ├── sampler.py                      utilisation sampler, 1 s
│   ├── kpi.sql                         KPI queries per NFR
│   ├── report.py                       KPI table against the NFR targets
│   └── r1/  01_ac.c ... 20_mle.c  expected.json  tests/
├── scripts/
│   ├── reset_db.sh                     [A-3] roles, schema, seed, copy seed_data
│   ├── capacity.py                     [A-3] capacity profile s0 or host
│   ├── add_users.py                    [E4-2] users from CSV, bcrypt
│   ├── add_problem.py                  [E1-4] problem and test files from a folder
│   └── setup_host.sh                   [X-6] demo machine setup
├── tests/
│   ├── conftest.py  helpers.py  stubs.py   [A-4]
│   ├── schema/  e1/  e2/  e3/  e4/  system/
└── docs/
    ├── PROJECT_CONTEXT.md  IMPLEMENTATION_PLAN.md  decisions.md
    └── kpi_report.md                   [X-6]
```

Runtime data under `DATA_DIR` (git-ignored): `payloads/<uuid>/prog.c`, `runs/<job_id>/a<n>/` (binary, outputs, compile log), `problems/<problem_id>/NN.in` and `NN.out`, `logs/`.

## 4. Database

The four files below are complete and were run on PostgreSQL 16.15 before this plan was written: the schema builds 15 tables and 17 foreign keys, and 25 behaviour checks passed, including 100 runs of the last-unit test with exactly one commit each. Slice A-2 commits them and turns those checks into `tests/schema/`.

Three things the baseline asks for are enforced by the database itself, not only by code: the transition list (trigger), the 10% reserve (`resources_reserve_ck`) and the table ownership of §7 (one role per engine, column-level grant on `jobs.state` for `transition()`).

### 4.1 db/roles.sql (once per PostgreSQL cluster, as a superuser)

```sql
-- db/roles.sql
-- Run once per PostgreSQL cluster as a superuser, before schema.sql.
-- One NOLOGIN role per engine. The application logs in as orch_app, which
-- inherits nothing: every engine transaction starts with SET LOCAL ROLE eN_role,
-- so the database itself refuses a write to another engine's table.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'e1_role') THEN CREATE ROLE e1_role NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'e2_role') THEN CREATE ROLE e2_role NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'e3_role') THEN CREATE ROLE e3_role NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'e4_role') THEN CREATE ROLE e4_role NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'orch_app') THEN
        CREATE ROLE orch_app LOGIN NOINHERIT PASSWORD 'orch';   -- change on the demo machine
    END IF;
END $$;
GRANT e1_role, e2_role, e3_role, e4_role TO orch_app;
```

### 4.2 db/schema.sql

```sql
-- db/schema.sql
-- PostgreSQL 16. Single source for tables, constraints, indexes, triggers and grants.
-- Run on an empty database, as its owner, after db/roles.sql:
--   psql -v ON_ERROR_STOP=1 -d orch -f db/schema.sql
-- 15 tables, 17 foreign keys. Integer units: millicores and MiB.

BEGIN;

CREATE DOMAIN job_state_t AS TEXT
    CHECK (VALUE IN ('queued','admitted','allocated','running',
                     'completed','failed','cancelled','released'));

-- ======================================================================
-- E4 Job Monitoring & Recovery: users, login_sessions (worker_liveness and
-- job_state_history follow their parent tables below)
-- ======================================================================
CREATE TABLE users (
    user_id       TEXT PRIMARY KEY CHECK (user_id ~ '^[A-Za-z0-9._-]{3,32}$'),
    role          TEXT NOT NULL CHECK (role IN ('student','faculty','administrator')),
    password_hash TEXT NOT NULL CHECK (password_hash LIKE '$2%')
);

CREATE TABLE login_sessions (
    token_hash TEXT PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    user_id    TEXT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX login_sessions_user_idx ON login_sessions (user_id);

-- ======================================================================
-- E2 Workload Scheduling: workload_types, lab_sessions (jobs below)
-- ======================================================================
CREATE TABLE workload_types (
    type_code          TEXT PRIMARY KEY CHECK (type_code ~ '^[a-z_]{3,20}$'),
    default_need_class SMALLINT NOT NULL CHECK (default_need_class BETWEEN 1 AND 3),
    max_time_limit_s   INTEGER  NOT NULL CHECK (max_time_limit_s BETWEEN 1 AND 3600)
);

CREATE TABLE lab_sessions (
    lab_session_id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    faculty_id     TEXT NOT NULL REFERENCES users (user_id) ON DELETE RESTRICT,
    kind           TEXT NOT NULL CHECK (kind IN ('exam','lab')),
    opens_at       TIMESTAMPTZ NOT NULL,
    closes_at      TIMESTAMPTZ NOT NULL,
    CONSTRAINT lab_sessions_window_ck CHECK (closes_at > opens_at)
);
CREATE INDEX lab_sessions_faculty_idx ON lab_sessions (faculty_id);

-- ======================================================================
-- E1 Process Execution & Control: problems, test_cases (attempts and
-- test_results below)
-- ======================================================================
CREATE TABLE problems (
    problem_id    BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    created_by    TEXT NOT NULL REFERENCES users (user_id) ON DELETE RESTRICT,
    title         TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    time_limit_ms INTEGER NOT NULL CHECK (time_limit_ms BETWEEN 100 AND 60000)
);
CREATE INDEX problems_created_by_idx ON problems (created_by);

CREATE TABLE test_cases (
    problem_id    BIGINT  NOT NULL REFERENCES problems (problem_id) ON DELETE CASCADE,
    case_no       INTEGER NOT NULL CHECK (case_no BETWEEN 1 AND 100),
    input_path    TEXT    NOT NULL,
    expected_path TEXT    NOT NULL,
    PRIMARY KEY (problem_id, case_no)
);

-- ======================================================================
-- E2: jobs
-- ======================================================================
CREATE TABLE jobs (
    job_id         BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id        TEXT        NOT NULL REFERENCES users (user_id) ON DELETE RESTRICT,
    type_code      TEXT        NOT NULL REFERENCES workload_types (type_code) ON DELETE RESTRICT,
    lab_session_id BIGINT               REFERENCES lab_sessions (lab_session_id) ON DELETE SET NULL,
    problem_id     BIGINT               REFERENCES problems (problem_id) ON DELETE RESTRICT,
    cpu_millicores INTEGER     NOT NULL CHECK (cpu_millicores > 0),
    ram_mib        INTEGER     NOT NULL CHECK (ram_mib >= 64),
    vram_mib       INTEGER     NOT NULL DEFAULT 0 CHECK (vram_mib >= 0),
    time_limit_s   INTEGER     NOT NULL CHECK (time_limit_s > 0),
    need_class     SMALLINT    NOT NULL CHECK (need_class BETWEEN 1 AND 3),
    state          job_state_t NOT NULL DEFAULT 'queued',
    retry_count    SMALLINT    NOT NULL DEFAULT 0 CHECK (retry_count BETWEEN 0 AND 2),
    submitted_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    payload_path   TEXT        NOT NULL,
    CONSTRAINT jobs_code_eval_problem_ck CHECK (type_code <> 'code_eval' OR problem_id IS NOT NULL)
);
CREATE INDEX jobs_state_idx  ON jobs (state);
CREATE INDEX jobs_user_idx   ON jobs (user_id, job_id DESC);
CREATE INDEX jobs_queued_idx ON jobs (need_class, submitted_at, job_id) WHERE state = 'queued';

-- ======================================================================
-- E4: job_state_history (append-only, written only by the state trigger)
-- ======================================================================
CREATE TABLE job_state_history (
    event_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id     BIGINT      NOT NULL REFERENCES jobs (job_id) ON DELETE RESTRICT,
    from_state job_state_t,
    to_state   job_state_t NOT NULL,
    reason     TEXT        NOT NULL,
    actor      TEXT        NOT NULL CHECK (actor IN ('E1','E2','E3','E4')),
    at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX history_job_idx ON job_state_history (job_id, event_id);
CREATE INDEX history_at_idx  ON job_state_history (at);

-- ======================================================================
-- E1: attempts, test_results;  E4: worker_liveness
-- ======================================================================
CREATE TABLE attempts (
    attempt_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id       BIGINT   NOT NULL REFERENCES jobs (job_id) ON DELETE RESTRICT,
    attempt_no   SMALLINT NOT NULL CHECK (attempt_no BETWEEN 1 AND 3),
    sandbox_name TEXT,
    started_at   TIMESTAMPTZ,
    ended_at     TIMESTAMPTZ,
    end_reason   TEXT CHECK (end_reason IN ('completed','timeout','memory_limit','crash',
                                            'cancelled','worker_lost','not_started')),
    verdict      TEXT,
    CONSTRAINT attempts_job_no_uk UNIQUE (job_id, attempt_no),
    CONSTRAINT attempts_end_ck CHECK ((ended_at IS NULL) = (end_reason IS NULL))
);
CREATE UNIQUE INDEX attempts_one_open_uk ON attempts (job_id) WHERE ended_at IS NULL;

CREATE TABLE worker_liveness (
    attempt_id   BIGINT PRIMARY KEY REFERENCES attempts (attempt_id) ON DELETE CASCADE,
    last_beat_at TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('alive','lost','closed'))
);
CREATE INDEX liveness_alive_idx ON worker_liveness (last_beat_at) WHERE status = 'alive';

CREATE TABLE test_results (
    attempt_id BIGINT  NOT NULL REFERENCES attempts (attempt_id) ON DELETE CASCADE,
    case_no    INTEGER NOT NULL,
    verdict    TEXT    NOT NULL CHECK (verdict IN ('AC','WA','TLE','RE')),
    time_ms    INTEGER NOT NULL CHECK (time_ms >= 0),
    PRIMARY KEY (attempt_id, case_no)
);

-- ======================================================================
-- E3 Transaction-Safe Resource Allocation
-- ======================================================================
CREATE TABLE resources (
    resource_id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    kind        TEXT     NOT NULL CHECK (kind IN ('cpu','ram','gpu')),
    gpu_index   SMALLINT CHECK (gpu_index >= 0),
    total       INTEGER  NOT NULL CHECK (total > 0),     -- cpu: millicores; ram, gpu: MiB
    reserved    INTEGER  NOT NULL,
    allocated   INTEGER  NOT NULL DEFAULT 0,             -- stored derived value (M3)
    CONSTRAINT resources_gpu_index_ck CHECK ((kind = 'gpu') = (gpu_index IS NOT NULL)),
    CONSTRAINT resources_reserve_ck   CHECK (reserved = (total + 9) / 10),   -- ceil(10% of total)
    CONSTRAINT resources_allocated_ck CHECK (allocated >= 0 AND allocated <= total - reserved),
    CONSTRAINT resources_kind_gpu_uk  UNIQUE NULLS NOT DISTINCT (kind, gpu_index)
);

CREATE TABLE allocations (
    allocation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id        BIGINT NOT NULL REFERENCES jobs (job_id) ON DELETE RESTRICT,
    status        TEXT   NOT NULL DEFAULT 'open' CHECK (status IN ('open','released')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    released_at   TIMESTAMPTZ,
    CONSTRAINT allocations_released_ck CHECK ((status = 'released') = (released_at IS NOT NULL)),
    CONSTRAINT allocations_id_job_uk UNIQUE (allocation_id, job_id)
);
CREATE UNIQUE INDEX allocations_one_open_uk ON allocations (job_id) WHERE status = 'open';

CREATE TABLE allocation_items (
    allocation_id BIGINT  NOT NULL REFERENCES allocations (allocation_id) ON DELETE CASCADE,
    resource_id   INTEGER NOT NULL REFERENCES resources (resource_id) ON DELETE RESTRICT,
    amount        INTEGER NOT NULL CHECK (amount > 0),
    PRIMARY KEY (allocation_id, resource_id)
);
CREATE INDEX allocation_items_resource_idx ON allocation_items (resource_id);

CREATE TABLE allocation_audit (
    audit_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id        BIGINT NOT NULL REFERENCES jobs (job_id) ON DELETE RESTRICT,
    allocation_id BIGINT,
    action        TEXT   NOT NULL CHECK (action IN ('allocate','release')),
    outcome       TEXT   NOT NULL CHECK (outcome IN ('committed','refused')),
    at            TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    -- M2: an audit row can never name a different job than its allocation
    CONSTRAINT audit_allocation_fk FOREIGN KEY (allocation_id, job_id)
        REFERENCES allocations (allocation_id, job_id) ON DELETE RESTRICT,
    CONSTRAINT audit_refused_ck CHECK ((outcome = 'refused') = (allocation_id IS NULL)),
    CONSTRAINT audit_refused_allocate_ck CHECK (outcome = 'committed' OR action = 'allocate')
);
CREATE INDEX audit_job_idx ON allocation_audit (job_id, audit_id);
CREATE INDEX audit_allocation_idx ON allocation_audit (allocation_id);

-- ======================================================================
-- Triggers
-- ======================================================================

-- 1. Append-only audit and history
CREATE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'AO001';
END $$;
CREATE TRIGGER job_state_history_append_only BEFORE UPDATE OR DELETE ON job_state_history
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER allocation_audit_append_only BEFORE UPDATE OR DELETE ON allocation_audit
    FOR EACH ROW EXECUTE FUNCTION forbid_change();

-- 2. CONDUCTS and AUTHORS link only users whose role is faculty
CREATE FUNCTION require_faculty() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    uid TEXT := to_jsonb(NEW) ->> TG_ARGV[0];
BEGIN
    IF NOT EXISTS (SELECT 1 FROM users WHERE user_id = uid AND role = 'faculty') THEN
        RAISE EXCEPTION '%.% must be a faculty user, got %', TG_TABLE_NAME, TG_ARGV[0], uid
            USING ERRCODE = 'FA001';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER lab_sessions_faculty BEFORE INSERT OR UPDATE OF faculty_id ON lab_sessions
    FOR EACH ROW EXECUTE FUNCTION require_faculty('faculty_id');
CREATE TRIGGER problems_faculty BEFORE INSERT OR UPDATE OF created_by ON problems
    FOR EACH ROW EXECUTE FUNCTION require_faculty('created_by');

-- 3. A faculty member's role stays fixed while they own sessions or problems
CREATE FUNCTION keep_faculty_role() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.role = 'faculty' AND NEW.role <> 'faculty' AND (
           EXISTS (SELECT 1 FROM lab_sessions WHERE faculty_id = OLD.user_id)
        OR EXISTS (SELECT 1 FROM problems     WHERE created_by = OLD.user_id)) THEN
        RAISE EXCEPTION 'user % owns sessions or problems and must stay faculty', OLD.user_id
            USING ERRCODE = 'FA002';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER users_keep_faculty BEFORE UPDATE OF role ON users
    FOR EACH ROW EXECUTE FUNCTION keep_faculty_role();

-- 4. State tracker: allowed transitions (section 9 of the baseline) and one history row per change
CREATE FUNCTION job_transition_allowed(from_s TEXT, to_s TEXT) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT (from_s, to_s) IN (
        ('queued','admitted'),     ('queued','cancelled'),
        ('admitted','allocated'),  ('admitted','queued'),
        ('allocated','running'),   ('allocated','cancelled'),
        ('running','completed'),   ('running','failed'),   ('running','cancelled'),
        ('completed','released'),  ('failed','released'),  ('cancelled','released'),
        ('released','queued'))
$$;

CREATE FUNCTION job_state_track() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
    v_actor  TEXT := nullif(current_setting('app.actor',  true), '');
    v_reason TEXT := nullif(current_setting('app.reason', true), '');
    v_from   TEXT := NULL;
BEGIN
    IF v_actor IS NULL OR v_reason IS NULL THEN
        RAISE EXCEPTION 'job %: state written without transition()', NEW.job_id
            USING ERRCODE = 'JT002';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'queued' THEN
            RAISE EXCEPTION 'job % must start as queued, not %', NEW.job_id, NEW.state
                USING ERRCODE = 'JT001';
        END IF;
    ELSE
        v_from := OLD.state;
        IF NOT job_transition_allowed(OLD.state, NEW.state) THEN
            RAISE EXCEPTION 'illegal transition % to % for job %', OLD.state, NEW.state, NEW.job_id
                USING ERRCODE = 'JT001';
        END IF;
    END IF;
    INSERT INTO job_state_history (job_id, from_state, to_state, reason, actor, at)
    VALUES (NEW.job_id, v_from, NEW.state, v_reason, v_actor, clock_timestamp());
    RETURN NULL;
END $$;
CREATE TRIGGER jobs_state_track AFTER INSERT OR UPDATE OF state ON jobs
    FOR EACH ROW EXECUTE FUNCTION job_state_track();

-- 5. M1: a test result's case must belong to the problem of its attempt's job
CREATE FUNCTION test_result_case_check() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM attempts a
        JOIN jobs j       ON j.job_id = a.job_id
        JOIN test_cases t ON t.problem_id = j.problem_id AND t.case_no = NEW.case_no
        WHERE a.attempt_id = NEW.attempt_id) THEN
        RAISE EXCEPTION 'case % is not a case of the problem of attempt %', NEW.case_no, NEW.attempt_id
            USING ERRCODE = 'TR001';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER test_results_case BEFORE INSERT OR UPDATE ON test_results
    FOR EACH ROW EXECUTE FUNCTION test_result_case_check();

-- ======================================================================
-- Grants: reads are open to every engine; writes only to the owner (section 7)
-- ======================================================================
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO orch_app, e1_role, e2_role, e3_role, e4_role;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO orch_app, e1_role, e2_role, e3_role, e4_role;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO e1_role, e2_role, e3_role, e4_role;

GRANT INSERT, UPDATE, DELETE ON attempts, problems, test_cases, test_results       TO e1_role;
GRANT INSERT, UPDATE, DELETE ON jobs, workload_types, lab_sessions                  TO e2_role;
GRANT INSERT, UPDATE, DELETE ON resources, allocations, allocation_items           TO e3_role;
GRANT INSERT                 ON allocation_audit                                    TO e3_role;
GRANT INSERT, UPDATE, DELETE ON users, login_sessions, worker_liveness             TO e4_role;
-- transition(): the one cross-table write. Column-level, so nothing else on jobs.
GRANT UPDATE (state) ON jobs TO e1_role, e3_role, e4_role;
-- job_state_history: no grant at all. Only the SECURITY DEFINER trigger writes it.

COMMIT;
```

### 4.3 db/seed.sql

```sql
-- db/seed.sql
-- Development and demo data. Run as the database owner after schema.sql.
-- Capacity rows here are the S0 benchmark profile (32 vCPU, 128 GB RAM, 2 GPUs of 16 GB).
-- On the demo machine, scripts/capacity.py --profile host rewrites the resources rows.
-- Demo passwords (change before the demo): demo-student, demo-faculty, demo-admin.

BEGIN;

-- Workload types (proposal: names from the five S0 workload kinds; need classes and limits are proposals)
INSERT INTO workload_types (type_code, default_need_class, max_time_limit_s) VALUES
    ('code_eval', 2,   60),
    ('cpu_bound', 3,  600),
    ('mem_bound', 3,  600),
    ('gpu_bound', 3, 1800),
    ('mixed',     3,  600);

-- Demo users (bcrypt, cost 12)
INSERT INTO users (user_id, role, password_hash) VALUES
    ('01FE24BCS901', 'student',       '$2b$12$Atgu5qfd5zIyj8ZhilQB/Op8JC8JhNjdyTVe3sMXwnSDtkiZkheZ2'),
    ('01FE24BCS902', 'student',       '$2b$12$Atgu5qfd5zIyj8ZhilQB/Op8JC8JhNjdyTVe3sMXwnSDtkiZkheZ2'),
    ('FAC001',       'faculty',       '$2b$12$30Bt1m1CzAu1qSL/fNxCw..qDZsPJFNNEhOz76/zJbDu8yVPpnw92'),
    ('ADM001',       'administrator', '$2b$12$TUETnZzHa5R42Q7tc7jUeuLXKm7u1eKgZlRNTnrDC062NIzr6HDXK');

-- Lab session 7 (exam, open now) and 8 (lab), matching the API sample in the baseline
INSERT INTO lab_sessions (lab_session_id, faculty_id, kind, opens_at, closes_at) VALUES
    (7, 'FAC001', 'exam', now() - interval '1 hour', now() + interval '12 hours'),
    (8, 'FAC001', 'lab',  now() - interval '1 hour', now() + interval '12 hours');

-- Problem 12 with 10 test cases; files live under DATA_DIR/problems/12/
INSERT INTO problems (problem_id, created_by, title, time_limit_ms) VALUES
    (12, 'FAC001', 'Sum of N integers', 1000);
INSERT INTO test_cases (problem_id, case_no, input_path, expected_path)
SELECT 12, n, format('problems/12/%s.in', lpad(n::text, 2, '0')),
              format('problems/12/%s.out', lpad(n::text, 2, '0'))
FROM generate_series(1, 10) AS n;

-- Capacity, S0 profile. Lock order follows resource_id: 1 CPU, 2 RAM, 3 and 4 GPUs.
INSERT INTO resources (resource_id, kind, gpu_index, total, reserved) VALUES
    (1, 'cpu', NULL,  32000,  3200),    -- millicores
    (2, 'ram', NULL, 131072, 13108),    -- MiB; allocatable 117964
    (3, 'gpu', 0,     16384,  1639),    -- VRAM MiB; allocatable 14745
    (4, 'gpu', 1,     16384,  1639);
-- Move the identity counters past the fixed ids used above
DO $$
BEGIN
    PERFORM setval(pg_get_serial_sequence('lab_sessions', 'lab_session_id'), 100);
    PERFORM setval(pg_get_serial_sequence('problems', 'problem_id'), 100);
    PERFORM setval(pg_get_serial_sequence('resources', 'resource_id'), 4);
END $$;

-- The first submitted job gets Job ID 1042, as in the baseline scenario
ALTER TABLE jobs ALTER COLUMN job_id RESTART WITH 1042;

COMMIT;
```

`seed_data/problems/12/` holds the ten case files (`01.in`, `01.out` to `10.in`, `10.out`). Input: a count N on the first line, then N integers; expected output: their sum. Slice A-3 writes them, including edge cases (N = 1, negative numbers, a sum that needs 64 bits).

### 4.4 db/invariants.sql

```sql
-- db/invariants.sql
-- Each query returns one row per violation. Empty results mean the database is consistent.
-- tests/helpers.py runs "invariants" after every system test, and "leaks" once the run has settled.

-- name: invariants
-- 1. allocated equals the sum of open allocation items and stays in 0 .. total - reserved (NFR-06)
SELECT 'capacity' AS check_name, r.resource_id::text AS subject,
       format('allocated=%s held=%s limit=%s', r.allocated, coalesce(h.held, 0), r.total - r.reserved) AS detail
FROM resources r
LEFT JOIN (SELECT i.resource_id, sum(i.amount) AS held
           FROM allocation_items i
           JOIN allocations a ON a.allocation_id = i.allocation_id AND a.status = 'open'
           GROUP BY i.resource_id) h ON h.resource_id = r.resource_id
WHERE r.allocated <> coalesce(h.held, 0) OR r.allocated < 0 OR r.allocated > r.total - r.reserved
UNION ALL
-- 2. at most one open allocation and one open attempt per job
SELECT 'open_allocations', job_id::text, count(*)::text
FROM allocations WHERE status = 'open' GROUP BY job_id HAVING count(*) > 1
UNION ALL
SELECT 'open_attempts', job_id::text, count(*)::text
FROM attempts WHERE ended_at IS NULL GROUP BY job_id HAVING count(*) > 1
UNION ALL
-- 3. queued, admitted and released jobs hold no open allocation
SELECT 'holds_while_idle', j.job_id::text, j.state
FROM jobs j JOIN allocations a ON a.job_id = j.job_id AND a.status = 'open'
WHERE j.state IN ('queued', 'admitted', 'released')
UNION ALL
-- 4. allocated and running jobs hold exactly one open allocation
SELECT 'missing_allocation', j.job_id::text, j.state
FROM jobs j
WHERE j.state IN ('allocated', 'running')
  AND NOT EXISTS (SELECT 1 FROM allocations a WHERE a.job_id = j.job_id AND a.status = 'open')
UNION ALL
-- 5. every allocation has exactly one committed allocate audit row (NFR-05)
SELECT 'audit_allocate', a.allocation_id::text, count(au.audit_id)::text
FROM allocations a
LEFT JOIN allocation_audit au
       ON au.allocation_id = a.allocation_id AND au.action = 'allocate' AND au.outcome = 'committed'
GROUP BY a.allocation_id HAVING count(au.audit_id) <> 1
UNION ALL
-- 6. a released allocation has exactly one release audit row, an open one has none (NFR-05)
SELECT 'audit_release', a.allocation_id::text, a.status || ':' || count(au.audit_id)
FROM allocations a
LEFT JOIN allocation_audit au ON au.allocation_id = a.allocation_id AND au.action = 'release'
GROUP BY a.allocation_id, a.status
HAVING count(au.audit_id) <> CASE WHEN a.status = 'released' THEN 1 ELSE 0 END
UNION ALL
-- 7. the recorded state equals the last history row, and every job has history (NFR-05)
SELECT 'history_tail', j.job_id::text, j.state || ' vs ' || coalesce(h.to_state, 'none')
FROM jobs j
LEFT JOIN LATERAL (SELECT to_state FROM job_state_history
                   WHERE job_id = j.job_id ORDER BY event_id DESC LIMIT 1) h ON true
WHERE h.to_state IS DISTINCT FROM j.state
UNION ALL
-- 8. a worker marked alive belongs to a running job with an open attempt
SELECT 'alive_without_run', w.attempt_id::text, j.state
FROM worker_liveness w
JOIN attempts a ON a.attempt_id = w.attempt_id
JOIN jobs j     ON j.job_id = a.job_id
WHERE w.status = 'alive' AND (j.state <> 'running' OR a.ended_at IS NOT NULL);

-- name: leaks
-- NFR-07: once a run has settled, no completed, failed or cancelled job still holds resources,
-- and no attempt is left open on a job that is not running.
SELECT 'leak' AS check_name, j.job_id::text AS subject, j.state AS detail
FROM jobs j JOIN allocations a ON a.job_id = j.job_id AND a.status = 'open'
WHERE j.state IN ('completed', 'failed', 'cancelled')
UNION ALL
SELECT 'open_attempt_not_running', a.job_id::text, j.state
FROM attempts a JOIN jobs j ON j.job_id = a.job_id
WHERE a.ended_at IS NULL AND j.state NOT IN ('allocated', 'running');
```

### 4.5 scripts/reset\_db.sh

```bash
#!/usr/bin/env bash
# Drop and rebuild the development database. Never run it during a measured run.
set -euo pipefail
ADMIN_URL=${ADMIN_DATABASE_URL:-postgresql://postgres:postgres@localhost:5432/postgres}
DB=${DB_NAME:-orch}
DATA=${DATA_DIR:-./data}
psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -f db/roles.sql
psql "$ADMIN_URL" -c "DROP DATABASE IF EXISTS $DB WITH (FORCE)" -c "CREATE DATABASE $DB"
psql "${ADMIN_URL%/*}/$DB" -v ON_ERROR_STOP=1 -f db/schema.sql -f db/seed.sql
mkdir -p "$DATA/problems" "$DATA/payloads" "$DATA/runs" "$DATA/logs"
cp -r seed_data/problems/. "$DATA/problems/"
python -m scripts.capacity --profile "${CAPACITY_PROFILE:-s0}"
```

### 4.6 What each rule protects

| Rule in the database | Protects | Checked by |
| --- | --- | --- |
| `resources_allocated_ck` | No over-allocation even if code is wrong (NFR-06) | `tests/schema/test_constraints.py` |
| `resources_reserve_ck` | reserved is always ceil(10% of total) (NFR-06) | same |
| `allocations_one_open_uk`, `attempts_one_open_uk` | One open allocation and one open attempt per job | same |
| `audit_allocation_fk` (M2) | An audit row cannot name another job | same |
| `jobs_state_track` with `job_transition_allowed` | Only the 13 allowed transitions; one history row per change (FR-11, NFR-05) | `tests/schema/test_state_trigger.py` |
| Append-only triggers | Audit and history never change (FR-06) | same |
| `require_faculty`, `keep_faculty_role` | CONDUCTS and AUTHORS link only faculty | `tests/schema/test_triggers.py` |
| `test_result_case_check` (M1) | A verdict always belongs to the attempt's problem | same |
| Engine roles and grants | Each engine writes only its own tables (§7) | `tests/schema/test_grants.py` |

## 5. Common layer (slice A-4)

`app/common` gives every engine the same four tools: a transaction that runs as the engine's role and defers outside work until after COMMIT, an injectable clock, the ports that engines call each other through, and the shared types. The `tx()` below is the prototype that passed the database checks of section 4.

### 5.1 app/common/db.py

```python
"""Pool, tx(), after-commit hooks, deadlock retry."""
from __future__ import annotations
import threading
from contextlib import contextmanager
from typing import Callable, Iterator
import psycopg
from psycopg_pool import ConnectionPool

ROLE = {"E1": "e1_role", "E2": "e2_role", "E3": "e3_role", "E4": "e4_role"}
RETRYABLE = {"40P01", "40001"}            # deadlock, serialisation failure
_state = threading.local()
_pool: ConnectionPool | None = None


class Tx:
    """Cursor wrapper. after_commit() work runs only after a successful COMMIT."""
    def __init__(self, cur: psycopg.Cursor):
        self.cur = cur
        self.hooks: list[Callable[[], None]] = []
    def execute(self, sql: str, params=None):
        return self.cur.execute(sql, params)
    def fetchone(self):
        return self.cur.fetchone()
    def fetchall(self):
        return self.cur.fetchall()
    def after_commit(self, fn: Callable[[], None]) -> None:
        self.hooks.append(fn)


def init_pool(dsn: str, min_size: int, max_size: int) -> None:
    global _pool
    _pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size, open=True)


def close_pool() -> None:
    if _pool is not None:
        _pool.close()


def in_transaction() -> bool:
    return getattr(_state, "depth", 0) > 0


def assert_outside_tx(what: str) -> None:
    """Called by dispatch, hub.publish, capacity_changed and every docker call (invariant 3)."""
    if in_transaction():
        raise RuntimeError(f"{what} called inside an open transaction")


@contextmanager
def tx(engine: str) -> Iterator[Tx]:
    """One transaction as one engine's database role. Rolls back on any exception.
    Nested tx() on the same thread is refused: an engine call that opens its own
    transaction can never run while the caller still holds locks."""
    assert _pool is not None, "init_pool() first"
    if in_transaction():
        raise RuntimeError("nested tx(): call other engines after COMMIT")
    _state.depth = 1
    hooks: list[Callable[[], None]] = []
    try:
        with _pool.connection() as conn:              # commit on clean exit, rollback on exception
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL ROLE {ROLE[engine]}")
                t = Tx(cur)
                yield t
                hooks = t.hooks
    finally:
        _state.depth = 0
    for fn in hooks:                                    # connection already back in the pool
        fn()


def retrying(fn: Callable, attempts: int = 3):
    """Retry a whole transaction function on 40P01 or 40001 only, then fail loudly."""
    for i in range(attempts):
        try:
            return fn()
        except psycopg.Error as e:
            if e.sqlstate in RETRYABLE and i < attempts - 1:
                continue
            raise
```

`SET LOCAL ROLE` lasts only for the transaction, so a pooled connection never keeps an engine's role. With READ COMMITTED (PostgreSQL's default, the isolation level the team assumes in §16 and §17) a statement that waits for a row lock re-reads the latest committed row, which is why the re-check inside the lock is enough.

### 5.2 app/common/clock.py

```python
class Clock(Protocol):
    def monotonic(self) -> float: ...                    # seconds; deadlines and timers
    def time(self) -> float: ...                         # wall seconds; the 60 s guard
    def wait(self, event: threading.Event, timeout: float) -> bool: ...   # loop pacing

class RealClock:            # time.monotonic, event.wait
class FakeClock:            # advance(seconds) moves time and wakes every waiter whose timeout passed
```

Every loop paces itself with `clock.wait(event, period)`, so tests drive the admission cycle, the deadline timer and the 60 s guard with `FakeClock.advance()` and no real sleeping. Database times always come from `clock_timestamp()`; the liveness sweep compares database times only.

### 5.3 app/common/types.py

```python
@dataclass(frozen=True)
class Vector:            # resource vector, integer units
    cpu_millicores: int
    ram_mib: int
    vram_mib: int = 0

@dataclass(frozen=True)
class Snapshot:          # free capacity per resource (message 3)
    cpu_free: int
    ram_free: int
    gpu_free: dict[int, int]          # gpu_index -> free VRAM MiB
    def fits(self, v: Vector) -> bool:
        return (v.cpu_millicores <= self.cpu_free and v.ram_mib <= self.ram_free and
                (v.vram_mib == 0 or any(f >= v.vram_mib for f in self.gpu_free.values())))

@dataclass(frozen=True)
class Limits:            # what E1 enforces (message 5)
    cpu_millicores: int
    ram_mib: int
    gpu_index: int | None
    time_limit_s: int

@dataclass(frozen=True)
class Committed:  allocation_id: int; gpu_index: int | None
@dataclass(frozen=True)
class Refused:    reason: str
@dataclass(frozen=True)
class Released:   allocation_id: int
@dataclass(frozen=True)
class AlreadyReleased: pass

@dataclass(frozen=True)
class User:       user_id: str; role: Literal["student", "faculty", "administrator"]
```

### 5.4 app/common/ports.py

The only way one engine reaches another. Names follow §8 and the Stage 2 API table (`snapshot`, `allocate`, `dispatch`, `heartbeat`, `release`, `stop`, `requeue`, `pause_admission`, `resume_admission`); the rest are marked proposal.

```python
class E3Port(Protocol):
    def snapshot(self) -> Snapshot: ...                                    # message 3
    def allocate(self, job_id: int, vector: Vector) -> Committed | Refused: ...   # message 4
    def release(self, job_id: int) -> Released | AlreadyReleased: ...     # messages 7, 10

class E2Port(Protocol):
    def submit(self, user: User, req: SubmitRequest, source: bytes) -> SubmitResult: ...  # message 2
    def capacity_changed(self) -> None: ...                                # message 8
    def requeue(self, job_id: int) -> bool: ...                            # message 11; False = retries used up
    def cancel_queued(self, job_id: int, by: str) -> bool: ...             # message 11; proposal name
    def pause_admission(self, reason: str) -> None: ...                    # message 11
    def resume_admission(self) -> None: ...                                # message 11
    def set_policy(self, policy: Literal["fcfs", "need"]) -> None: ...     # proposal name
    def queue_view(self) -> list[QueueEntry]: ...                          # proposal name

class E1Port(Protocol):
    def dispatch(self, job_id: int, allocation_id: int, limits: Limits, file_ref: str) -> int: ...  # message 5
    def stop(self, job_id: int) -> None: ...      # message 9: stop and confirm; raises StopNotConfirmed
    def cancel(self, job_id: int, by: str) -> None: ...                    # message 9; proposal name

class E4Port(Protocol):
    def heartbeat(self, attempt_id: int) -> bool: ...        # message 6; False = ignored (worker not alive)
    def register_worker(self, attempt_id: int) -> None: ...  # proposal name
    def close_worker(self, attempt_id: int) -> bool: ...     # proposal name; True = caller owns the ending
    def publish(self, event: dict) -> None: ...              # message 12; outside transactions only

@dataclass
class Engines:
    e1: E1Port
    e2: E2Port
    e3: E3Port
    e4: E4Port

def install(e: Engines) -> None: ...      # main.py installs the real engines; tests install stubs
def engines() -> Engines: ...
```

`transition()` is not a port: it runs inside the caller's transaction, so engines import it directly from `app.e4_monitoring.state`.

### 5.5 Errors and logging

- `app/common/errors.py`: `ApiError(status, error, field=None)`; `IllegalTransition` (raised when psycopg reports SQLSTATE `JT001`); `StopNotConfirmed`; `NotFound`.
- `app/common/logging.py`: one JSON object per line to stdout and `DATA_DIR/logs/server.log`, with `event`, `job_id`, `engine` and `ms`. Every allocate, refuse and release logs its transaction time (the §17 proposal for B2), and every rejected transition is logged, which is how "rejected by the trigger and logged" in §7 is met.

### 5.6 Test harness (tests/conftest.py, helpers.py, stubs.py)

- **Session fixture:** run `db/roles.sql`, then build a template database `orch_template` from `db/schema.sql` once.
- **Per-test fixture `db`:** `CREATE DATABASE t_<random> TEMPLATE orch_template`, open the pool on it as `orch_app`, install the stubs, yield, then close the pool and drop the database. Every test starts from an empty, real schema.
- **Factories in helpers.py:** `make_user(role)`, `make_problem(cases=10)`, `make_job(state="queued", vector=...)`. A factory reaches a later state only by walking legal transitions, so the trigger also checks the test data.
- **`assert_invariants(settled=False)`:** runs the `invariants` query and, with `settled=True`, the `leaks` query too; fails with every violating row printed.
- **stubs.py:** `StubE1` (records dispatch, stop and cancel calls), `StubE2`, `StubE3` (scripted Committed or Refused), `StubE4` (records heartbeats), `RecordingHub` (keeps every published event).
- **Markers in pyproject.toml:** `docker` (Linux with cgroup v2 and Docker), `slow` (real waits over 5 s), `system`. Default `addopts = "-m 'not docker'"`.

Slice A-4 is done when tests show: rollback on any exception; hooks skipped on rollback and run after the connection is returned; nested `tx()` refused; `assert_outside_tx` raises inside a transaction; a write to another engine's table fails with SQLSTATE 42501; `retrying` retries 40P01 twice and then raises.

## 6. E3 Transaction-Safe Resource Allocation (Sagar; slices E3-1 to E3-3)

E3 is three functions and one script. `snapshot()` is a plain read, `allocate()` and `release()` are one transaction each, and everything that leaves the process runs after COMMIT. The code below is the prototype that passed the last-unit, concurrent-release and best-fit checks on PostgreSQL 16.

### 6.1 Public interface (`app/e3_allocation/__init__.py`)

| Function | Returns | Transaction |
| --- | --- | --- |
| `snapshot()` | `Snapshot` (free CPU millicores, RAM MiB, VRAM MiB per GPU) | Read only, no lock; may be stale, the re-check inside the lock makes that harmless |
| `allocate(job_id, vector)` | `Committed(allocation_id, gpu_index)` or `Refused(reason)` | One `tx("E3")`; dispatch and capacity push after COMMIT |
| `release(job_id)` | `Released(allocation_id)` or `AlreadyReleased()` | One `tx("E3")`; capacity changed to E2 and capacity push after COMMIT |

Resource ids are cached at startup by kind (`{"cpu": 1, "ram": 2, "gpu": [3, 4]}`), so the lock statement names its rows by id as invariant 2 requires. `scripts/capacity.py` reloads the cache.

### 6.2 placement.py (pure functions, slice E3-1)

```python
Row = tuple[int, str, int | None, int, int, int]   # resource_id, kind, gpu_index, total, reserved, allocated

def free(r: Row) -> int:
    return r[3] - r[4] - r[5]                       # total - reserved - allocated

def fit(rows: list[Row], need: Vector) -> tuple[list[tuple[int, int]], int | None] | None:
    """rows are the locked rows. Returns ([(resource_id, amount)] in id order, gpu_index) or None."""
    cpu = next(r for r in rows if r[1] == "cpu")
    ram = next(r for r in rows if r[1] == "ram")
    if need.cpu_millicores > free(cpu) or need.ram_mib > free(ram):
        return None
    items = [(cpu[0], need.cpu_millicores), (ram[0], need.ram_mib)]
    gpu_index = None
    if need.vram_mib > 0:
        gpus = [r for r in rows if r[1] == "gpu" and free(r) >= need.vram_mib]
        if not gpus:
            return None
        best = min(gpus, key=lambda r: (free(r), r[0]))     # best fit: least free VRAM that fits
        items.append((best[0], need.vram_mib))
        gpu_index = best[2]
    return sorted(items), gpu_index
```

### 6.3 allocate() (slice E3-2)

```python
def allocate(job_id: int, need: Vector) -> Committed | Refused:
    ids = [IDS["cpu"], IDS["ram"]] + (IDS["gpu"] if need.vram_mib > 0 else [])
    with tx("E3") as t:
        t.execute("SELECT resource_id, kind, gpu_index, total, reserved, allocated "
                  "FROM resources WHERE resource_id = ANY(%s) "
                  "ORDER BY resource_id FOR UPDATE", (ids,))          # the one lock statement
        plan = fit(t.fetchall(), need)                                 # re-check inside the lock
        if plan is None:
            t.execute("INSERT INTO allocation_audit (job_id, allocation_id, action, outcome) "
                      "VALUES (%s, NULL, 'allocate', 'refused')", (job_id,))
            transition(t, job_id, "queued", "E3", "refused: capacity taken first")
            return Refused("insufficient capacity")
        items, gpu_index = plan
        for rid, amount in items:
            t.execute("UPDATE resources SET allocated = allocated + %s WHERE resource_id = %s",
                      (amount, rid))
        _test_hook("after_update")                                     # forced-failure test
        t.execute("INSERT INTO allocations (job_id) VALUES (%s) RETURNING allocation_id", (job_id,))
        allocation_id = t.fetchone()[0]
        for rid, amount in items:
            t.execute("INSERT INTO allocation_items (allocation_id, resource_id, amount) "
                      "VALUES (%s, %s, %s)", (allocation_id, rid, amount))
        t.execute("INSERT INTO allocation_audit (job_id, allocation_id, action, outcome) "
                  "VALUES (%s, %s, 'allocate', 'committed')", (job_id, allocation_id))
        transition(t, job_id, "allocated", "E3", "allocation committed")
        t.execute("SELECT time_limit_s, payload_path FROM jobs WHERE job_id = %s", (job_id,))
        time_limit_s, payload_path = t.fetchone()
        limits = Limits(need.cpu_millicores, need.ram_mib, gpu_index, time_limit_s)
        t.after_commit(lambda: engines().e1.dispatch(job_id, allocation_id, limits, payload_path))
        t.after_commit(publish_capacity)
    return Committed(allocation_id, gpu_index)
```

**Refusal path (decision D-01, needs team confirmation).** The §10 pseudocode reads "ROLLBACK; audit refused". A rolled-back transaction cannot keep the audit row, and a second transaction would break "allocate is a single transaction". At the refusal point nothing has been written yet, so the plan writes the refused audit row and the admitted to queued transition, then commits. Any real error still rolls back everything. The job keeps its `submitted_at`, so it keeps its place.

### 6.4 release() (slice E3-3)

```python
def release(job_id: int) -> Released | AlreadyReleased:
    with tx("E3") as t:
        t.execute("SELECT allocation_id FROM allocations "
                  "WHERE job_id = %s AND status = 'open' FOR UPDATE", (job_id,))  # lock 1: the allocation
        row = t.fetchone()
        if row is None:
            return AlreadyReleased()                                   # a repeat changes nothing
        allocation_id = row[0]
        t.execute("SELECT resource_id, amount FROM allocation_items "
                  "WHERE allocation_id = %s ORDER BY resource_id", (allocation_id,))
        items = t.fetchall()
        t.execute("SELECT resource_id FROM resources WHERE resource_id = ANY(%s) "
                  "ORDER BY resource_id FOR UPDATE", ([rid for rid, _ in items],))  # lock 2: rows in id order
        for rid, amount in items:
            t.execute("UPDATE resources SET allocated = allocated - %s WHERE resource_id = %s",
                      (amount, rid))
        t.execute("UPDATE allocations SET status = 'released', released_at = clock_timestamp() "
                  "WHERE allocation_id = %s", (allocation_id,))
        t.execute("INSERT INTO allocation_audit (job_id, allocation_id, action, outcome) "
                  "VALUES (%s, %s, 'release', 'committed')", (job_id, allocation_id))
        transition(t, job_id, "released", "E3", "release committed")
        t.after_commit(engines().e2.capacity_changed)                 # message 8
        t.after_commit(publish_capacity)
    return Released(allocation_id)
```

Callers guarantee the stop is confirmed before they call `release()` (E1 after its own stop, E4 after `e1.stop()`). E3 does not check processes; the transition rule does: only completed, failed or cancelled can move to released.

### 6.5 scripts/capacity.py (slice A-3)

- `--profile s0`: CPU 32,000 millicores, RAM 131,072 MiB, two GPUs of 16,384 MiB (the S0 configuration).
- `--profile host` (proposal): CPU = `os.cpu_count() * 1000`; RAM = `MemTotal` from `/proc/meminfo` in MiB; GPUs from `nvidia-smi --query-gpu=index,memory.total --format=csv,noheader,nounits` when `GPU_MODE=nvidia`, two logical 16,384 MiB GPUs when `GPU_MODE=simulated`, none when `GPU_MODE=none`.
- `reserved = (total + 9) // 10` for every row; the CHECK refuses anything else.
- Runs in `tx("E3")`, refuses to run while any allocation is open, updates existing rows and adds or removes GPU rows (a used GPU row cannot be deleted: the foreign key is RESTRICT).

### 6.6 Tests

| File | What it proves |
| --- | --- |
| `tests/e3/test_placement.py` | fit with and without VRAM; best fit picks the least free VRAM that fits; ties go to the lower `resource_id`; the reserve is never offered |
| `tests/e3/test_snapshot.py` | free = total minus reserved minus allocated per row; S0 seed gives 28,800, 117,964 and 14,745 |
| `tests/e3/test_allocate.py` | commit writes allocation, items, audit and state together; refusal writes one refused audit row and moves the job back to queued; dispatch runs after COMMIT with the right `Limits` and GPU (the stub asserts `not in_transaction()`); forced failure after the update leaves nothing; a job that is not admitted is rejected by the trigger (JT001) and nothing is written |
| `tests/e3/test_e3_allocation.py` (the file named in §13) | last unit: two threads, a Barrier, 100 runs, exactly one Committed; stress: 32 threads with random vectors for 20 rounds, invariant query clean after each round, zero 40P01 errors |
| `tests/e3/test_release.py` | RELEASED then ALREADY\_RELEASED; two concurrent releases of one job give one of each; release racing an allocate on the same rows never deadlocks (50 runs); capacity\_changed is called exactly once per release, after COMMIT |

Run command for E3 slices: `pytest -q tests/schema tests/e3`.

## 7. E4 state model (Aniruddh; slice E4-1)

The state model is built before any engine moves a job, because every engine reports states through it. The trigger in section 4 does the checking and the history; `transition()` is the only door to it and also queues the live push.

### 7.1 app/e4\_monitoring/state.py

```python
def set_actor(t: Tx, actor: str, reason: str) -> None:
    """Used by E2's INSERT of a new job (the first history row) and by transition()."""
    t.execute("SELECT set_config('app.actor', %s, true), set_config('app.reason', %s, true)",
              (actor, reason))

def clear_actor(t: Tx) -> None:
    t.execute("SELECT set_config('app.actor', '', true), set_config('app.reason', '', true)")

def transition(t: Tx, job_id: int, to_state: str, actor: str, reason: str,
               extra: dict | None = None) -> datetime:
    """Change jobs.state inside the caller's transaction. The trigger rejects illegal moves
    (SQLSTATE JT001, raised here as IllegalTransition) and writes the history row."""
    set_actor(t, actor, reason)
    try:
        t.execute("UPDATE jobs SET state = %s WHERE job_id = %s "
                  "RETURNING user_id, clock_timestamp()", (to_state, job_id))
    except psycopg.Error as e:
        if e.sqlstate == "JT001":
            log.warning("illegal_transition", job_id=job_id, to=to_state, actor=actor, error=str(e))
            raise IllegalTransition(str(e)) from e
        raise
    row = t.fetchone()
    if row is None:
        raise NotFound(f"job {job_id}")
    clear_actor(t)                       # a later raw UPDATE in this transaction fails with JT002
    user_id, at = row
    event = {"type": "job_state", "job_id": job_id, "user_id": user_id, "state": to_state,
             "reason": reason, "at": at.isoformat(), **(extra or {})}
    t.after_commit(lambda: engines().e4.publish(event))   # message 12, only if the transaction commits
    return at
```

The `at` in the event comes from the database clock, a few microseconds before the history row's `at`. NFR-09 is measured from it (section 13).

### 7.2 Who makes each transition

| From | To | Actor | Where |
| --- | --- | --- | --- |
| (insert) | queued | E2 | `e2.submit()` |
| queued | admitted | E2 | admission loop |
| queued | cancelled | E2 | `e2.cancel_queued()` |
| admitted | allocated | E3 | `e3.allocate()` commit |
| admitted | queued | E3 | `e3.allocate()` refusal; E4 at startup reconciliation |
| allocated | running | E1 | supervisor, after the container starts |
| allocated | cancelled | E1 | `e1.cancel()` before start; E4 when recovering a job that never started (D-04) |
| running | completed | E1 | supervisor, after stop and confirm |
| running | failed | E1 | time limit, memory limit, sandbox crash |
| running | failed | E4 | worker lost (no heartbeat for 6 s), startup reconciliation |
| running | cancelled | E1 | `e1.cancel()` after stop and confirm |
| completed, failed, cancelled | released | E3 | `e3.release()` |
| released | queued | E2 | `e2.requeue()` after a lost worker, retry\_count below 2 |

### 7.3 Tests (`tests/e4/test_transition.py`)

- All 64 ordered pairs of the 8 states: the 13 allowed pairs succeed and write one history row; the other 51 raise `IllegalTransition` and write nothing.
- The history row has `from_state`, `to_state`, `actor` and `reason` as passed; `from_state` is empty only in a job's first row.
- A raw `UPDATE jobs SET state` without `transition()` fails with JT002, including right after a `transition()` in the same transaction.
- The `job_state` event is published only after COMMIT, never after a rollback, and carries `user_id` for role filtering.
- Run command: `pytest -q tests/schema tests/e4/test_transition.py`.

## 8. E2 Workload Scheduling (Sudhanv; slices E2-1 to E2-4)

E2 validates and stores each request, keeps the queue in three structures that never disagree, and runs one admission loop. The `JobQueue` below passed a property test of 20 seeds by 10,000 random operations against a plain sorted model.

Lock rule for E2: the in-process `RLock` is always taken before any database transaction and never inside one. Every queue change (admit, requeue, cancel, rebuild) holds it, so the admission cycle and a cancel can never interleave.

### 8.1 Validation and submit (slice E2-1, `validation.py`, `service.py`)

Fields are checked in this order; the first failure returns `400 {field, error}` and no job is created (FR-02).

| Field | Rule | Source of the limit |
| --- | --- | --- |
| `workload_type` | One of the `workload_types` rows | seed |
| `problem_id` | Required and existing for `code_eval`; must be empty for other types | schema CHECK |
| `lab_session_id` | Optional; if given it must exist and be open now | proposal |
| `time_limit_s` | Integer, 1 to `max_time_limit_s` of the workload type | seed |
| `ram_mib` | Integer, 64 to RAM allocatable maximum (117,964 on S0) | baseline (§12) |
| `vram_mib` | Integer, 0 to the largest GPU's allocatable VRAM (14,745 on S0); 0 when the host has no GPU rows | total minus reserved |
| `cpu_millicores` | Integer, 100 to CPU allocatable maximum (28,800 on S0) | lower bound proposal; upper bound total minus reserved |
| `source` | File, 1 byte to 64 KiB, UTF-8, name ends in `.c` or `.cpp` | proposal |

Error text follows the §12 sample, for example `{"field": "ram_mib", "error": "must be 64 to 117964 (allocatable maximum)"}`. The allocatable maxima are read from `resources` (`total - reserved`) when E2 starts and after `scripts/capacity.py`.

**Need class** (FR-01, fixed at submission): 1 when `lab_session_id` names an exam session that is open now; otherwise the workload type's `default_need_class`. Who submits never matters.

**submit() steps:**

1. Validate.
2. Write the source to `DATA_DIR/payloads/<uuid4>/prog.c` (or `prog.cpp`); the folder is what E1 later mounts read-only.
3. In `tx("E2")`: `set_actor(t, "E2", "submitted")`, `INSERT INTO jobs ... RETURNING job_id, submitted_at`, `clear_actor(t)`. The trigger writes the first history row.
4. After COMMIT: push the job into the queue, publish its `job_state` event, wake the loop (proposal: a new job can start without waiting for the next tick).
5. If anything fails before COMMIT, delete the payload folder.

### 8.2 JobQueue (slice E2-2, `queue.py`)

```python
@dataclass(frozen=True)
class Entry:
    job_id: int
    need_class: int
    submitted_at: float          # epoch seconds of jobs.submitted_at; a requeue keeps it
    cpu_millicores: int
    ram_mib: int
    vram_mib: int
    version: int                 # changes on every (re)insert, so stale heap and FIFO items are skipped

class JobQueue:
    """map: job_id -> live Entry (O(1) lookup, cancel, requeue)
    heap: (need_class, submitted_at, job_id, version) min-heap for priority by need, lazy deletion
    fifo: (submitted_at, job_id, version) list kept sorted for FCFS, lazy deletion with a head index"""

    def __init__(self) -> None:
        self._map: dict[int, Entry] = {}
        self._heap: list[tuple[int, float, int, int]] = []
        self._fifo: list[tuple[float, int, int]] = []
        self._head = 0
        self._dead = 0
        self._versions = itertools.count(1)

    def __len__(self) -> int:
        return len(self._map)

    def __contains__(self, job_id: int) -> bool:
        return job_id in self._map

    def push(self, job_id: int, need_class: int, submitted_at: float,
             cpu_millicores: int, ram_mib: int, vram_mib: int) -> Entry:
        if job_id in self._map:
            raise ValueError(f"job {job_id} already queued")
        e = Entry(job_id, need_class, submitted_at, cpu_millicores, ram_mib, vram_mib, next(self._versions))
        self._map[job_id] = e
        heapq.heappush(self._heap, (need_class, submitted_at, job_id, e.version))       # O(log n)
        key = (submitted_at, job_id, e.version)
        if not self._fifo or key >= self._fifo[-1]:
            self._fifo.append(key)                                                       # new job: O(1)
        else:
            bisect.insort(self._fifo, key, lo=self._head)                                # requeue keeps its place
        return e

    def remove(self, job_id: int) -> Entry | None:
        e = self._map.pop(job_id, None)                                                  # O(1); items go stale
        if e is not None:
            self._dead += 1
            if self._dead > 64 and self._dead > len(self._map):
                self._compact()
        return e

    def _live(self, job_id: int, version: int) -> bool:
        e = self._map.get(job_id)
        return e is not None and e.version == version

    def fifo_head(self) -> Entry | None:
        """Oldest live job (FCFS head). Amortised O(1)."""
        while self._head < len(self._fifo):
            _, job_id, version = self._fifo[self._head]
            if self._live(job_id, version):
                return self._map[job_id]
            self._head += 1
        return None

    def first_fit(self, fits: Callable[[Entry], bool]) -> Entry | None:
        """Priority by need: first live job in (need_class, submitted_at, job_id) order that fits.
        Pops past the k jobs that do not fit and pushes them back: O(k log n)."""
        popped: list[tuple[int, float, int, int]] = []
        found = None
        while self._heap:
            item = heapq.heappop(self._heap)
            if not self._live(item[2], item[3]):
                continue                                                                 # lazy deletion
            popped.append(item)
            entry = self._map[item[2]]
            if fits(entry):
                found = entry
                break
        for item in popped:
            heapq.heappush(self._heap, item)
        return found

    def in_fifo_order(self) -> Iterator[Entry]:
        for _, job_id, version in self._fifo[self._head:]:
            if self._live(job_id, version):
                yield self._map[job_id]

    def in_need_order(self) -> list[Entry]:
        return sorted(self._map.values(), key=lambda e: (e.need_class, e.submitted_at, e.job_id))

    def _compact(self) -> None:
        self._fifo = [k for k in self._fifo[self._head:] if self._live(k[1], k[2])]
        self._heap = [i for i in self._heap if self._live(i[2], i[3])]
        heapq.heapify(self._heap)
        self._head = 0
        self._dead = 0
```

The FIFO list is sorted by `(submitted_at, job_id)` rather than by insertion, because a requeued job must return to its original place (invariant 8). New jobs still append in O(1); only a requeue pays O(n) for `insort`, at most twice per job.

### 8.3 Policies and the admission loop (slice E2-3, `policy.py`, `loop.py`)

```python
def select_fcfs(q: JobQueue, snap: Snapshot) -> Entry | None:
    head = q.fifo_head()
    return head if head is not None and snap.fits(vector(head)) else None    # the head waits; no overtaking

def select_need(q: JobQueue, snap: Snapshot, now: float, guard_s: float) -> Entry | None:
    oldest = q.fifo_head()
    if oldest is None:
        return None
    if now - oldest.submitted_at >= guard_s:                                 # 60 s no-overtaking guard
        return oldest if snap.fits(vector(oldest)) else None
    return q.first_fit(lambda e: snap.fits(vector(e)))

class AdmissionLoop(threading.Thread):
    """One instance. Wakes every ADMISSION_CYCLE_S or at once on capacity changed.
    A wake during a cycle leaves the event set, so the next cycle starts right after: cycles never overlap."""
    def run(self) -> None:
        while not self._stop.is_set():
            self.clock.wait(self._wake, self.period)
            self._wake.clear()
            try:
                self.service.run_cycle()
            except Exception:
                log.exception("admission_cycle_failed")           # the loop never dies

# service.py
def run_cycle(self) -> None:
    with self.lock:
        if self.paused:
            return
        while True:                                              # proposal D-03: admit until nothing fits
            snap = engines().e3.snapshot()
            pick = (select_fcfs(self.queue, snap) if self.policy == "fcfs"
                    else select_need(self.queue, snap, self.clock.time(), self.guard_s))
            if pick is None:
                return
            with tx("E2") as t:
                transition(t, pick.job_id, "admitted", "E2", f"picked by {self.policy}")
            result = engines().e3.allocate(pick.job_id, vector(pick))
            if not isinstance(result, Committed):
                return                                           # refused: E3 put it back to queued; it keeps its place
            self.queue.remove(pick.job_id)                       # leaves the queue only after COMMITTED
```

The `Clock` of section 5.2 also offers `time()` (wall seconds) for the 60 s guard, because `submitted_at` is a wall-clock value from the database. `FakeClock` drives both.

**D-03 (proposal).** The §10 pseudocode admits one job per cycle. At 0.5 s per cycle, the 60th job of a lab-exam burst would wait 30 s with capacity free, which breaks NFR-01 (start within 5 s). The cycle therefore repeats the selection until nothing fits or a refusal happens; each admission is still its own single allocate transaction.

### 8.4 Requeue, cancel, pause, rebuild (slice E2-4)

| Function | Behaviour |
| --- | --- |
| `requeue(job_id)` | Under the lock, in `tx("E2")`: lock the job row; it must be `released`; if `retry_count` is 2 return False (the job ends as it is); else `transition(released to queued, reason "requeue n of 2")` and `retry_count = retry_count + 1`. After COMMIT push it back with the same `submitted_at` and wake the loop. |
| `cancel_queued(job_id, by)` | Under the lock: if the job is not in the queue return False (E4 then tries E1). Else `transition(queued to cancelled, reason "cancelled by <user_id>")` and remove it. Nothing was held, so there is no release. |
| `pause_admission(reason)`, `resume_admission()` | Set or clear `paused` under the lock; publish `{"type": "admission", "paused": ..., "reason": ...}`. Running jobs are not touched (FR-04). |
| `set_policy(policy)` | `fcfs` or `need`, under the lock; kept in memory, default from `DEFAULT_POLICY`; publish an `admission` event. |
| `capacity_changed()` | Sets the wake event. Never blocks, never takes the lock. |
| `rebuild()` | At startup, after E4's reconciliation: push every job whose state is `queued`, in `(submitted_at, job_id)` order. |
| `queue_view()` | Live entries in the active policy's order with position, need class, wait in seconds and resource vector. |

### 8.5 Tests

| File | What it proves |
| --- | --- |
| `tests/e2/test_validation.py` | Every rule and both edges of every range (63 and 64, 117,964 and 117,965 MiB on S0); the first failing field is reported; need class 1 only for an open exam session; a closed session is rejected; a failed submit leaves no job and no payload folder |
| `tests/e2/test_v1_batch.py` | V1: 1,000 generated requests (seeded: 800 well-formed, 200 each breaking one field); accuracy of accept and reject decisions is at least 99% (NFR-03) |
| `tests/e2/test_queue.py` | Unit cases plus the property test: 20 seeds by 10,000 random push, remove, requeue and first-fit operations; FIFO list, heap and map always hold the same live jobs in the right orders |
| `tests/e2/test_policy.py` | FCFS keeps arrival order and the head blocks; under need a higher class that fits goes first and the same class keeps arrival order; with `FakeClock` the oldest job is never overtaken after 60 s |
| `tests/e2/test_loop.py` | With `StubE3`: a job leaves the queue only after Committed; a refusal keeps its place and ends the cycle; pause stops admission; a wake during a cycle triggers exactly one more cycle; 1,000 concurrent wakes never run two cycles at once |
| `tests/e2/test_requeue_cancel.py` | Requeue keeps `submitted_at` and the FIFO position; a third requeue returns False; cancel of a queued job writes cancelled with no allocation; cancel of a non-queued job returns False; rebuild restores the exact order |

Run command for E2 slices: `pytest -q tests/e2`.

## 9. E1 Process Execution & Control (Abhishek; slices E1-1 to E1-4)

E1 runs each attempt as three layers: a **supervisor** thread in the server that owns the container, the clock and every database write; a **worker** child process that runs the compiler and the test cases and prints one JSON line per event; and the **sandbox** container where the untrusted code runs. Expected outputs never leave the host, and the worker never touches the database.

### 9.1 Files (`files.py`, slice E1-1)

| Path under `DATA_DIR` | Contents | Mounted into the sandbox |
| --- | --- | --- |
| `payloads/<uuid>/prog.c` | The submitted source (written by E2) | `/job`, read-only |
| `runs/<job_id>/a<n>/` | Binary, `compile.txt`, one `NN.txt` per test case (capped) | `/out`, read-write; mode 0777 so user 65534 can write (proposal) |
| `runs/<job_id>/a<n>.json` | The worker's spec (container name, case inputs, timeouts) | No |
| `problems/<problem_id>/NN.in`, `NN.out` | Test inputs and expected outputs | No. Inputs are piped to stdin; outputs are compared on the host |

`safe_path(rel)` resolves `DATA_DIR / rel` and refuses anything that is not inside `DATA_DIR` after resolving symlinks. Every path from the database or a request goes through it.

### 9.2 SandboxRunner (`sandbox.py`, slice E1-1)

```python
@dataclass(frozen=True)
class SandboxSpec:
    name: str                    # "job-<job_id>-a<attempt_no>"
    limits: Limits
    job_dir: Path                # mounted read-only at /job
    out_dir: Path                # mounted read-write at /out
    labels: dict[str, str]       # {"orch.job_id": "1042", "orch.attempt_id": "77"}

@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    wall_ms: int
    timed_out: bool              # the per-command timeout fired
    oom_killed: bool             # memory.events oom_kill rose during this command
    output_truncated: bool

class SandboxRunner(Protocol):
    def start(self, spec: SandboxSpec) -> None: ...      # container up, limits applied, idle
    def exec(self, name: str, argv: list[str], stdin: Path | None, stdout: Path,
             timeout_s: float, cap_bytes: int, merge_stderr: bool = False) -> ExecResult: ...
    def stop(self, name: str, grace_s: float) -> None: ...  # SIGTERM to every job process, SIGKILL after grace_s
    def remove(self, name: str) -> None: ...                # idempotent
    def exists(self, name: str) -> bool: ...
    def list_sandboxes(self, job_id: int | None = None) -> list[str]: ...   # by label, for stop and reconcile
```

`FakeRunner` (any OS) scripts each program by a marker comment in the source, for example `// fake: sleep 30`, `// fake: oom`, `// fake: exit 3`, `// fake: print-expected`, so the supervisor, deadline and verdict logic are tested without Docker.

### 9.3 DockerRunner (`docker_runner.py`, slice E1-3)

Every call is `subprocess.run([...])` with an argument list, never `shell=True`.

```bash
# start: one container per attempt, idle until the worker runs commands in it
docker run -d --init --name job-1042-a1 \
  --label orch.job_id=1042 --label orch.attempt_id=77 \
  --cpus 1.000 --memory 512m --memory-swap 512m --pids-limit 64 \
  --network none --read-only --tmpfs /tmp:rw,nosuid,size=64m \
  --cap-drop ALL --security-opt no-new-privileges --user 65534:65534 --ulimit core=0 \
  -v DATA_DIR/payloads/<uuid>:/job:ro -v DATA_DIR/runs/1042/a1:/out:rw -w /out \
  judge-c:1 sleep infinity
#   GPU_MODE=nvidia and a GPU was allocated: add --gpus device=<gpu_index>

# exec: one command, with its own timeout inside the container
docker exec -i job-1042-a1 timeout -k 1 1.0 /out/prog < problems/12/01.in > runs/1042/a1/01.txt

# stop: SIGTERM to every job process, then SIGKILL after 2 s
docker exec job-1042-a1 kill -TERM -1        # user 65534 signals all its processes; PID 1 is spared
#   ... poll up to STOP_GRACE_S until the container has stopped ...
docker kill job-1042-a1                      # SIGKILL; PID 1 exits and the kernel kills the rest

# remove and confirm
docker rm -f job-1042-a1
docker inspect job-1042-a1                    # must fail with "No such object": confirmed
```

| Flag | Effect under cgroups v2 | Baseline item |
| --- | --- | --- |
| `--cpus` (`cpu_millicores / 1000`) | `cpu.max` quota per 100 ms period | CPU limit (FR-08) |
| `--memory` and `--memory-swap` equal | `memory.max` set, `memory.swap.max` 0 | Memory limit, swap off |
| `--pids-limit 64` | `pids.max` 64 | pids limit 64 |
| `--network none` | Only the loopback interface | No network |
| `--read-only`, `--tmpfs /tmp` | Root file system read-only; `/tmp` for the compiler | Read-only root |
| `--user 65534:65534`, `--cap-drop ALL`, `no-new-privileges` | Unprivileged, no capabilities | Unprivileged user |
| `--init` | A small init as PID 1 reaps zombies | Clean stop |

Two details that matter:

- **Why `kill -TERM -1` and not `docker stop`.** `docker stop` signals only PID 1. The judged program is started by `docker exec`, so it is not a child of PID 1 and would get SIGKILL without the SIGTERM first. Sending `kill -TERM -1` as user 65534 reaches every job process; the baseline order (SIGTERM, 2 s, SIGKILL) then holds.
- **Memory verdict.** At `start`, read the container's main PID (`docker inspect -f '{{.State.Pid}}'`), take its cgroup path from `/proc/<pid>/cgroup`, and keep `/sys/fs/cgroup/<path>/memory.events`. `exec` reads `oom_kill` before and after each command; a rise means the memory limit was hit. Exit code 137 alone is never used to decide.

Image (`sandbox/Dockerfile`, built once with `docker build -t judge-c:1 sandbox/`):

```dockerfile
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc g++ libc6-dev coreutils procps \
 && rm -rf /var/lib/apt/lists/*
USER 65534:65534
WORKDIR /out
```

### 9.4 Worker child process (`worker.py`, slices E1-2 and E1-3)

Started by the supervisor as `python -m app.e1_execution.worker runs/<job_id>/a<n>.json`. It prints one JSON object per line to stdout and flushes after each line.

| Line | When |
| --- | --- |
| `{"t": "beat"}` | Every 2 s, from its own thread, from start to exit |
| `{"t": "compiled", "ok": true}` | After `gcc -O2 -std=c17 -o /out/prog /job/prog.c -lm` (or `g++ -O2 -std=c++17`), timeout `COMPILE_TIMEOUT_S`, stderr merged into `compile.txt` capped at 8 KiB |
| `{"t": "case", "case_no": 3, "exit": 0, "ms": 41, "timed_out": false, "oom": false, "truncated": false}` | After each test case; per-case timeout is `problems.time_limit_ms` (D-05) |
| `{"t": "ran", "exit": 0, "ms": 12034, "oom": false}` | Non-judged workload types: one run of `/out/prog` with no input |
| `{"t": "oom", "stage": "compile"}` | The memory limit was hit; the worker stops at once |
| `{"t": "error", "detail": "..."}` | A Docker or file error; exit code 1 |
| `{"t": "done"}` | Last line; exit code 0 |

The worker is a separate process on purpose: killing it with `kill -9` is the real lost-worker case of FR-12 and the F1 run.

### 9.5 Supervisor (`supervisor.py`, slice E1-2)

One thread per attempt. A reader thread moves the worker's lines into a queue and sets an event; the supervisor waits with `clock.wait(event, min(0.2, time_left))`, so `FakeClock` drives every timing test.

1. **Check.** In `tx("E1")`, lock the job row (`SELECT state FROM jobs WHERE job_id = %s FOR UPDATE`). If it is no longer `allocated` (a cancel won the race), close the attempt as `not_started` and exit. If the cancel flag is set: close the attempt, `transition(allocated to cancelled)`, then `e3.release()` after COMMIT.
2. **Start.** `runner.start(spec)`. If it fails: close the attempt as `crash`, `transition(allocated to cancelled, "sandbox failed to start")`, release; the job ends (D-06).
3. **Running.** In `tx("E1")`: set `sandbox_name` and `started_at`, `transition(allocated to running)`. After COMMIT, `e4.register_worker(attempt_id)`, start the deadline (`clock.monotonic() + time_limit_s`), launch the worker.
4. **Watch.** Relay every beat to `e4.heartbeat(attempt_id)`. If it returns False, E4 has declared the worker lost and owns the ending: kill the child and exit. If the worker's stdout ends without `done`, exit quietly; E4 detects the silence after 6 s (FR-12).
5. **Finish.** On `done`, deadline, `oom`, `error` or cancel, first call `e4.close_worker(attempt_id)`. If it returns False, E4 owns the ending: kill the child and exit. Otherwise:

| Cause | Order of steps | Terminal state and reason | attempts.end\_reason |
| --- | --- | --- | --- |
| `done` | stop and confirm, judge, write `test_results`, then transition, then release | completed, verdict | completed |
| Deadline | set the timeout flag, kill the child, stop and confirm, transition, release | failed, "time limit exceeded" | timeout |
| `oom` | stop and confirm, transition, release | failed, "memory limit exceeded" | memory\_limit |
| `error` | stop and confirm, transition, release | failed, "sandbox error" | crash |
| Cancel | stop and confirm, transition, release | cancelled, "cancelled by \<user\_id>" | cancelled |

The terminal state is written only after the stop is confirmed, so "completed" and "failed" always mean no process of the job is left (FR-08, FR-13). The timeout flag is set in memory before anything is killed, which is why exit 137 is never needed to tell a timeout from an out-of-memory kill. If the stop cannot be confirmed within `STOP_CONFIRM_TIMEOUT_S`, the supervisor logs it, publishes an `alert` event and does not release: the allocation stays held and the job is not requeued (§7, E1 failure behaviour).

### 9.6 Judge (`judge.py`, slice E1-1)

- **Comparison** (on the host): split into lines, strip trailing spaces from each line, drop trailing empty lines, then compare exactly (proposal).
- **Case verdict:** TLE if `timed_out`; RE if the exit code is not 0; WA if the output was truncated or differs; AC otherwise.
- **Overall verdict:** `Compilation Error` if compiling failed; `Accepted 10/10` if every case is AC; otherwise the first non-AC case's name with the count passed, for example `Wrong Answer 7/10`. Names: Accepted, Wrong Answer, Time Limit Exceeded, Runtime Error.
- **Non-judged types:** `Exited 0` or `Exited <code>`.
- Compile errors, wrong answers, per-case time limits and runtime errors are verdicts: the job still completes (§7). Only the job's own time limit and the memory limit fail the job.

### 9.7 dispatch, stop, cancel (`service.py`)

```python
def dispatch(job_id: int, allocation_id: int, limits: Limits, file_ref: str) -> int:
    assert_outside_tx("e1.dispatch")
    with tx("E1") as t:
        t.execute("SELECT count(*) FROM attempts WHERE job_id = %s", (job_id,))
        attempt_no = t.fetchone()[0] + 1                      # 1 to 3 (CHECK)
        t.execute("INSERT INTO attempts (job_id, attempt_no) VALUES (%s, %s) RETURNING attempt_id",
                  (job_id, attempt_no))
        attempt_id = t.fetchone()[0]
    sup = Supervisor(job_id, attempt_id, attempt_no, limits, file_ref, runner, launcher, clock)
    with _registry_lock:
        _supervisors[job_id] = sup
    sup.start()
    return attempt_id

def stop(job_id: int, end_reason: str = "worker_lost") -> None:
    """Message 9: stop and confirm. Returns only when no container of the job exists."""
    assert_outside_tx("e1.stop")
    with _registry_lock:
        sup = _supervisors.pop(job_id, None)
    if sup:
        sup.abandon()                                         # kill the worker child; the thread exits
    for name in runner.list_sandboxes(job_id):
        runner.stop(name, STOP_GRACE_S)
        runner.remove(name)
    if not wait_until(lambda: not runner.list_sandboxes(job_id), STOP_CONFIRM_TIMEOUT_S):
        raise StopNotConfirmed(job_id)
    with tx("E1") as t:
        t.execute("UPDATE attempts SET ended_at = clock_timestamp(), "
                  "end_reason = CASE WHEN started_at IS NULL THEN 'not_started' ELSE %s END "
                  "WHERE job_id = %s AND ended_at IS NULL", (end_reason, job_id))
```

`cancel(job_id, by)` (FR-10, slice X-1): under `_registry_lock`, if a supervisor exists set its cancel flag (it handles both before-start and running cases as in 9.5); if none exists yet, lock the job row in `tx("E1")` and, if it is still `allocated`, `transition(allocated to cancelled)` and release after COMMIT. The supervisor's step 1 then finds the job no longer allocated and exits.

### 9.8 GPU

E3 reserves VRAM as a number; E1 passes only the allocated device into the container (`--gpus device=<index>`) when `GPU_MODE=nvidia`. The kernel cannot cap GPU memory, so VRAM is a reservation, not an enforced limit. With `GPU_MODE=simulated` the GPU rows exist, `gpu_bound` jobs are admitted and accounted, and they run on the CPU (S0 allows simulation when GPUs are limited).

### 9.9 Tests

With `FakeRunner`, a fake launcher and `FakeClock` (any OS):

| File | What it proves |
| --- | --- |
| `tests/e1/test_files.py` | `../` and symlink escapes are refused; folder layout and permissions |
| `tests/e1/test_judge.py` | Normalisation; every case verdict; the overall verdict string; compile error |
| `tests/e1/test_dispatch.py` | `attempt_no` counts 1 to 3; dispatch inside a transaction raises |
| `tests/e1/test_supervisor.py` | Happy path order (running, beats every 2 s, stop confirmed, completed with `Accepted 10/10`, then release); deadline sets the flag before the stop and ends failed with timeout; oom ends failed with memory\_limit; cancel before start starts no container; cancel while running stops first, then cancelled, then release; `close_worker` False leaves the state alone; worker EOF without `done` changes nothing; stop not confirmed means no release |

On Linux with Docker (`-m docker`):

| File | What it proves |
| --- | --- |
| `tests/e1/test_docker_limits.py` | Inside a 1,000-millicore, 512 MiB container: `cpu.max` is `100000 100000`, `memory.max` is 536870912, `memory.swap.max` is 0, `pids.max` is 64, only `lo` in `/sys/class/net`, writing to `/` fails, `id -u` is 65534 |
| `tests/e1/test_docker_timeout.py` | A 30 s job with a 5 s limit ends failed with timeout, stopped by 5 + 2 s plus a 1 s margin, container gone (FR-08, NFR-10) |
| `tests/e1/test_docker_memory.py` | A program that writes twice its memory: `oom_kill` rises, the job ends failed with memory\_limit |
| `tests/e1/test_docker_pids.py` | A fork bomb is capped at 64 processes and the host is unaffected |
| `tests/e1/test_docker_stop.py` | A program that traps SIGTERM writes a marker before it exits; one that ignores it is killed after 2 s; the container is gone |
| `tests/e1/test_r1_reference.py` | R1: 20 reference programs for problem 12 give the expected verdict, 20 of 20 (NFR-03 needs at least 99%) |

R1 composition (proposal, `bench/r1/`): 8 Accepted (including N = 1, negative numbers, a 64-bit sum), 4 Wrong Answer, 3 Time Limit Exceeded, 2 Runtime Error (a segmentation fault and `exit(3)`), 2 Compilation Error, 1 that exceeds its memory limit (job failed with memory\_limit). `expected.json` holds the verdict of each.

Run commands: `pytest -q tests/e1` and, on Linux, `pytest -q -m docker tests/e1`.

## 10. E4 server side (Aniruddh; slices E4-2 to E4-7, X-2)

E4 is the entry point and the recovery owner: login and roles, the REST API, the WebSocket hub, the heartbeat table and its sweep, recovery of lost workers, reconciliation at startup and, in Sprint 3, the host health guard.

### 10.1 Login and sessions (`auth.py`, slice E4-2)

- `POST /api/auth/login {user_id, password}`: read `password_hash` and `role`; check with `bcrypt.checkpw`. Unknown user and wrong password give the same `401 {"error": "login failed"}`; for an unknown user a dummy bcrypt check still runs so the timing does not reveal which (proposal).
- On success: `token = secrets.token_urlsafe(32)`; store only `sha256(token).hexdigest()` in `login_sessions` with `expires_at = clock_timestamp() + TOKEN_TTL_S`; delete that user's expired rows; return `200 {token, role, user_id}`.
- `current_user` dependency: `Authorization: Bearer <token>`, hash it, `SELECT u.user_id, u.role FROM login_sessions s JOIN users u USING (user_id) WHERE s.token_hash = %s AND s.expires_at > clock_timestamp()`; no row means 401.
- `require_role(*roles)` dependency gives 403. `POST /api/auth/logout` deletes the session row (proposal).
- `scripts/add_users.py users.csv` (columns `user_id,role,password`) creates accounts in `tx("E4")` with bcrypt cost 12.

### 10.2 REST API (`api.py`, `schemas.py`, slice E4-3)

Endpoints are plain `def`. A custom handler turns FastAPI's validation errors into `400 {field, error}` (first error, `field` is the last element of its location); `ApiError` maps to its status; `IllegalTransition` maps to 409.

| Request | Roles | Engine call | Responses |
| --- | --- | --- | --- |
| `POST /api/auth/login` | public | E4 | 200 `{token, role, user_id}`; 401 |
| `POST /api/auth/logout` (proposal) | any | E4 | 204 |
| `GET /api/me` (proposal) | any | E4 | 200 `{user_id, role}` |
| `GET /api/workload_types` (proposal) | any | read | 200 list |
| `GET /api/problems` (Stage 2 API table) | any | read | 200 `[{problem_id, title, time_limit_ms}]` |
| `GET /api/lab_sessions?open=true` (proposal) | any | read | 200 list |
| `POST /api/jobs` (multipart: the §12 fields plus file part `source`) | any | `e2.submit` | 201 `{job_id, state: "queued", need_class}`; 400 `{field, error}`; 401; 403 |
| `GET /api/jobs?owner=me` | any; `owner=all` for faculty and administrator | read | 200 list, newest first, 50 per page (`?before=<job_id>`) |
| `GET /api/jobs/{id}` | owner, faculty, administrator | read | 200 job with history, attempts, case verdicts and the compile log; 403; 404 |
| `POST /api/jobs/{id}/cancel` (FR-10, slice X-1) | owner (student or faculty), administrator | `e2.cancel_queued`, else `e1.cancel` | 202 `{state: "cancelling"}`; 409 already finished; 403; 404 |
| `GET /api/resources` | any | read | 200 per resource: kind, gpu\_index, total, reserved, allocated, free |
| `GET /api/queue` | any | `e2.queue_view` | 200 entries in policy order; another user's `user_id` is hidden from students (proposal) |
| `PUT /api/admin/policy {policy: fcfs or need}` | administrator | `e2.set_policy` | 200; 400; 403 |
| `GET /api/admin/status` (proposal) | faculty, administrator | read | 200 `{policy, admission_paused, reason, queued, running}` |
| `POST /api/lab_sessions` (proposal, X-3) | faculty | E2 service | 201 |
| `POST /api/problems` (proposal, X-3; multipart with `NN.in` and `NN.out` files) | faculty | E1 service | 201 |
| `WS /ws/events` | any, after login | hub | pushes within 2 s |

Static pages are served by `app.mount("/", StaticFiles(directory="web", html=True))`, mounted after the API routes.

`GET /api/jobs/{id}` response:

```json
{
  "job_id": 1042, "user_id": "01FE24BCS901", "type_code": "code_eval", "problem_id": 12,
  "lab_session_id": 7, "cpu_millicores": 1000, "ram_mib": 512, "vram_mib": 0,
  "time_limit_s": 5, "need_class": 1, "state": "released", "retry_count": 0,
  "submitted_at": "2026-11-30T10:00:00.120+05:30",
  "verdict": "Accepted 10/10",
  "history": [{"from": null, "to": "queued", "actor": "E2", "reason": "submitted", "at": "..."}],
  "attempts": [{"attempt_no": 1, "started_at": "...", "ended_at": "...", "end_reason": "completed",
                "cases": [{"case_no": 1, "verdict": "AC", "time_ms": 41}]}],
  "compile_log": ""
}
```

### 10.3 Role matrix (`tests/e4/test_roles.py`)

| Action | Student | Faculty | Administrator | No token |
| --- | --- | --- | --- | --- |
| Submit a job | 201 | 201 | 201 | 401 |
| View own job | 200 | 200 | 200 | 401 |
| View another user's job | 403 | 200 | 200 | 401 |
| List all jobs (`owner=all`) | 403 | 200 | 200 | 401 |
| Cancel own job | 202 | 202 | 202 | 401 |
| Cancel another user's job | 403 | 403 | 202 | 401 |
| Cancel a finished job | 409 | 409 | 409 | 401 |
| Change the policy | 403 | 403 | 200 | 401 |
| Create a lab session or problem (X-3) | 403 | 201 | 403 | 401 |

### 10.4 WebSocket hub (`hub.py`, slice E4-4)

- The hub is created in the app's lifespan and keeps the running event loop. `publish(event)` may be called from any thread, only after COMMIT (`assert_outside_tx`); it calls `asyncio.run_coroutine_threadsafe(self._broadcast(event), loop)` and returns without waiting.
- A client opens `/ws/events` and must send `{"type": "auth", "token": "..."}` within 5 s, otherwise the server closes with code 4401 (browsers cannot set headers on a WebSocket). The server answers `{"type": "hello", "user_id", "role"}`.
- Filtering: a student receives `job_state` events only for their own jobs; everyone receives `capacity` and `admission`; faculty and administrators also receive `alert`.
- Each connection has a bounded `asyncio.Queue` (1,000 events, proposal). A client that falls behind is closed with code 4408; on reconnect it reloads state over REST, then applies events again.
- Every pushed event carries `sent_at` (server wall time) next to the commit time `at`, which is what NFR-09 is measured from.

Events: `job_state {job_id, user_id, state, reason, at, verdict?}`, `capacity {resources: [...], at}`, `admission {paused, reason, policy}`, `alert {kind, job_id, detail}`.

### 10.5 Liveness (`liveness.py`, slice E4-5)

```sql
-- register_worker(attempt_id): called by E1 right after running is committed
INSERT INTO worker_liveness (attempt_id, last_beat_at, status)
VALUES (%(a)s, clock_timestamp(), 'alive') ON CONFLICT (attempt_id) DO NOTHING;

-- heartbeat(attempt_id) -> bool: a late beat from a lost worker matches no row and is ignored
UPDATE worker_liveness SET last_beat_at = clock_timestamp()
WHERE attempt_id = %(a)s AND status = 'alive' RETURNING 1;

-- close_worker(attempt_id) -> bool: the supervisor claims the ending (section 2.3)
UPDATE worker_liveness SET status = 'closed'
WHERE attempt_id = %(a)s AND status = 'alive' RETURNING 1;

-- sweep, every SWEEP_S: marks each lost worker exactly once
UPDATE worker_liveness SET status = 'lost'
WHERE status = 'alive' AND last_beat_at < clock_timestamp() - make_interval(secs => %(lost_after)s)
RETURNING attempt_id;
```

Each returned `attempt_id` becomes one `recover(job_id, lost=True)` task on the recovery executor, so the sweep itself never waits on Docker. With a 2 s heartbeat, a 6 s limit and a 1 s sweep, detection takes 6 to 7 s (FR-12).

### 10.6 Recovery (`recovery.py`, slice E4-6)

```python
def recover(job_id: int, reason: str = "worker lost", lost: bool = True) -> None:
    """Runs on the recovery executor, never inside a transaction. Order: stop, confirm, release, requeue."""
    t0 = time.monotonic()
    try:
        engines().e1.stop(job_id)                               # message 9: returns only when confirmed
    except StopNotConfirmed:
        log.error("stop_not_confirmed", job_id=job_id)
        engines().e4.publish({"type": "alert", "kind": "stop_not_confirmed", "job_id": job_id})
        return                                                  # nothing released, nothing requeued
    with tx("E4") as t:
        t.execute("SELECT state FROM jobs WHERE job_id = %s FOR UPDATE", (job_id,))
        state = t.fetchone()[0]
        if state == "running":
            transition(t, job_id, "failed", "E4", reason)
        elif state == "allocated":
            transition(t, job_id, "cancelled", "E4", f"not started: {reason}")   # D-04
        elif state not in ("completed", "failed", "cancelled"):
            return                                              # queued, admitted, released: nothing held
    engines().e3.release(job_id)                                # message 10
    if lost and not engines().e2.requeue(job_id):               # message 11
        log.info("retries_used_up", job_id=job_id)              # the job ends; its last outcome is failed
    log.info("recovered", job_id=job_id, ms=int((time.monotonic() - t0) * 1000))
```

Time budget for NFR-08 (10 s): detection 6 to 7 s, stop up to 2 s of SIGTERM grace plus about 0.5 s for `docker kill` and `rm`, release and requeue a few milliseconds. The F1 run measures it (section 13).

### 10.7 Reconciliation at startup (slice E4-7)

Runs in `main.py` before E2 rebuilds its queue and before any loop starts, so admission resumes only after every job is accounted for (FR-14).

| Found at start | Action |
| --- | --- |
| `queued` | Nothing; E2's rebuild picks it up |
| `admitted` | `transition(admitted to queued, "reconcile: restart")`. No allocation can exist, because allocate is one transaction |
| `allocated` | `recover(job_id, "restart before start", lost=True)`: stop removes any container and closes the attempt as not\_started, then cancelled, released, requeued if retries remain |
| `running` | Mark its liveness row lost, then `recover(job_id, "worker lost at restart", lost=True)` |
| `completed`, `failed` or `cancelled` with an open allocation | `recover(job_id, "restart before release", lost=False)`: stop for safety, release, no requeue |
| `released`, last terminal row was `failed` by E4 for a lost worker, retries below 2 | `e2.requeue(job_id)` (the crash came between release and requeue) |
| Containers labelled `orch.job_id` whose job is not allocated or running | Remove them |
| Anything else | Nothing |

After it runs, the `invariants` and `leaks` queries must both return no rows; `main.py` logs any row as an error.

### 10.8 Host health guard (`health.py`, slice X-2, FR-04 Challenge)

- A `HealthProbe` read every 1 s (proposal): `MemAvailable` and `MemTotal` from `/proc/meminfo`; `docker info` every 5 s for the execution backend (proposal).
- Unhealthy when `MemAvailable` is below `HEALTH_MIN_AVAILABLE_PCT` (10%, mirroring the reserve) of `MemTotal`, or Docker does not answer. On the change to unhealthy: `e2.pause_admission(reason)`. After 3 healthy reads in a row: `e2.resume_admission()` (proposal).
- Running jobs are never touched.

### 10.9 Tests

| File | What it proves |
| --- | --- |
| `tests/e4/test_auth.py` | Correct login; same 401 for unknown user and wrong password; only the SHA-256 of the token is stored; an expired token gives 401; logout ends the session |
| `tests/e4/test_api_jobs.py` | The §12 sample returns 201 with job 1042, state queued, need class 1; `ram_mib` 117965 returns the §12 400 body; list and detail; 404; validation errors become 400 |
| `tests/e4/test_roles.py` | Every cell of the role matrix (FR-15) |
| `tests/e4/test_hub.py` | Auth within 5 s or close 4401; a student receives only their own job events; capacity goes to all; an event published from a thread after COMMIT arrives; none arrives after a rollback; a slow client is closed with 4408 |
| `tests/e4/test_liveness.py` | A heartbeat moves `last_beat_at` only while alive; a late beat after lost returns False and changes nothing; 4 concurrent sweeps mark a worker lost once; `close_worker` racing the sweep: exactly one wins, 100 runs |
| `tests/e4/test_recovery.py` | Order stop, failed, release, requeue (stubs record it); StopNotConfirmed means no release and no requeue; the third loss ends the job; an allocated job goes the cancelled path |
| `tests/e4/test_reconcile.py` | One job left in each state before a simulated restart ends as the table says; both invariant queries are clean afterwards |
| `tests/e4/test_health.py` (X-2) | Low memory pauses admission and leaves running jobs alone; 3 healthy reads resume it |

Run command for E4 slices: `pytest -q tests/e4`.

## 11. Web pages (slices C-2 and C-3)

Six static pages in plain HTML, CSS and JavaScript, served by the same FastAPI process. Every page loads its state over REST, then keeps it current from `/ws/events`; nothing polls.

| Page | Users | What it does | Calls |
| --- | --- | --- | --- |
| `index.html` Login | everyone | user\_id and password; stores the token in `sessionStorage`; students go to Submit, faculty and administrators to Monitor | `POST /api/auth/login` |
| `submit.html` Submit job | everyone | Workload type; problem (shown only for `code_eval`); open lab session (exam sessions marked); CPU in vCPU (step 0.1, sent as millicores); RAM MiB; VRAM MiB (hidden when the host has no GPU rows); time limit; `.c` or `.cpp` file checked for size before upload. A 400 shows its `error` under the named field; a 201 opens the job page | `GET /api/workload_types`, `/api/problems`, `/api/lab_sessions?open=true`, `/api/resources`; `POST /api/jobs` |
| `jobs.html` My jobs | everyone; "all jobs" switch for faculty and administrators | Table of Job ID, type, problem, state, verdict, need class, submitted; rows change live | `GET /api/jobs`; WebSocket `job_state` |
| `job.html?id=` Job detail | owner, faculty, administrators | State, timeline of every history row, per-case verdicts and times, compile log, Cancel button while the job is not finished | `GET /api/jobs/{id}`; `POST /api/jobs/{id}/cancel`; WebSocket |
| `monitor.html` Live monitor | everyone (students see only their own jobs in lists) | One bar per resource: allocated, allocatable and the hatched 10% reserve; queue in policy order with wait times; running jobs; admission paused or active; event log showing each event's delay (`now - at`) | `GET /api/resources`, `/api/queue`, `/api/jobs?owner=all`; WebSocket `capacity`, `admission`, `job_state` |
| `admin.html` Admin console | administrators: policy; faculty: lab sessions and problems (X-3) | FCFS or priority by need switch; admission status and reason; alerts; forms to create a lab session and upload a problem with its test files | `PUT /api/admin/policy`, `GET /api/admin/status`, `POST /api/lab_sessions`, `POST /api/problems` |

Shared scripts:

- `js/api.js`: `api(path, {method, body, form})` adds the Bearer token, sends JSON or `FormData`, turns 401 into a redirect to login, and returns `{status, data}` so pages can show `{field, error}` next to the right input.
- `js/live.js`: `connectLive({onEvent, onResync})` opens `ws://` or `wss://` to `/ws/events`, sends the auth message, reconnects with backoff from 0.5 s to 8 s, and calls `onResync()` after each reconnect so the page reloads state over REST before applying new events.
- `css/app.css`: one stylesheet; state badges use one colour per state; monitor bars use the engine colours of the design (E1 green, E2 blue, E3 amber, E4 purple).
- Every input has a `<label>`; state changes are announced through an `aria-live` region (proposal).

Done when: the lab-exam scenario can be clicked through end to end; a student sees only their own jobs; a state change appears on an open page within 2 s (checked by the event log's delay column and by `bench/ws_latency.py`).

## 12. Integration and startup (slices C-1 and C-4)

Integration swaps one stub for the real engine at a time, in message order, and runs the system test after each swap. The startup order guarantees that no job is admitted before every old job has been reconciled.

### 12.1 app/main.py

```python
def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    app = FastAPI(title="Computing Resource Allocation & Utilization Orchestration System",
                  lifespan=lifespan)
    app.state.cfg = cfg
    install_error_handlers(app)                 # 400 {field, error}, ApiError, IllegalTransition -> 409
    app.include_router(api_router, prefix="/api")
    app.add_api_websocket_route("/ws/events", ws_events)
    app.mount("/", StaticFiles(directory="web", html=True), name="web")
    return app

@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = app.state.cfg
    init_pool(cfg.database_url, cfg.db_pool_min, cfg.db_pool_max)
    check_schema()                               # exactly the 15 tables, or refuse to start
    hub = Hub(asyncio.get_running_loop())
    clock = RealClock()
    e3 = E3Service()
    e2 = E2Service(clock, cfg)
    e1 = E1Service(make_runner(cfg), make_launcher(cfg), clock, cfg)
    e4 = E4Service(hub, cfg)
    install(Engines(e1=e1, e2=e2, e3=e3, e4=e4))
    e3.load_ids()
    await run_in_threadpool(e4.reconcile)        # FR-14: before any admission
    e2.rebuild()
    loops = [LivenessSweep(clock, cfg), AdmissionLoop(e2, clock, cfg)]
    if cfg.health_guard:
        loops.append(HealthGuard(clock, cfg))    # X-2
    for loop in loops:
        loop.start()
    try:
        yield
    finally:
        for loop in loops:
            loop.stop()
        e1.shutdown()          # supervisors stop; containers keep running; the next start reconciles them
        close_pool()
```

Run it with `uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --workers 1` in development. Section 16 adds TLS for the LAN.

### 12.2 Order of stub replacement (slice C-1)

| Step | Real call switched on | Message | System test that must pass |
| --- | --- | --- | --- |
| 1 | E2 calls the real `e3.snapshot()` and `e3.allocate()` | 3, 4 | Jobs reach `allocated`; invariant query clean |
| 2 | E3 calls the real `e1.dispatch()` after COMMIT (`FakeRunner`) | 5 | Jobs reach `running` |
| 3 | E1 calls the real `e3.release()`; E3 calls `e2.capacity_changed()` | 7, 8 | Jobs reach `released`; the next job is admitted at once |
| 4 | E1 calls the real `e4.register_worker()`, `heartbeat()`, `close_worker()` | 6 | Liveness rows follow each attempt |
| 5 | The API calls the real `e2.submit()` | 1, 2 | Lab-exam scenario through HTTP |
| 6 | The hub pushes to pages | 12 | Events arrive in order |
| 7 | Recovery calls the real `e1.stop()`, `e3.release()`, `e2.requeue()` | 9, 10, 11 | Lost-worker test passes |
| 8 | `SANDBOX_BACKEND=docker` on Linux (slice C-4) | 5 | Lab-exam scenario on Docker |

### 12.3 System tests (`tests/system/`)

| File | Scenario | Passes when |
| --- | --- | --- |
| `test_lab_exam_fake.py` | The §15 scenario through HTTP and the WebSocket with `FakeRunner`: login, submit the §12 sample, judge 10 cases | Events queued, admitted, allocated, running, completed, released arrive in order; verdict `Accepted 10/10`; start within 5 s; release within 2 s of completed; both invariant queries clean |
| `test_lab_exam_docker.py` (`-m docker`) | The same scenario on Docker with the real compiler | Same checks; this is the Sprint 2 exit |
| `test_burst.py` | 60 submissions at once into an open exam session (FakeRunner) | Every job completes; each starts within 5 s while capacity is free; invariant query clean |
| `test_lost_worker.py` | Kill the worker of a running job; repeat on the same job | Lost after 6 to 7 s, requeued within 10 s (NFR-08); after 2 retries the job ends; invariants clean |
| `test_restart.py` | Stop the app with jobs in every state, start it again | Reconciliation table of 10.7 holds; queue order restored; invariants and leaks clean |
| `test_cancel.py` (X-1) | Cancel queued, allocated and running jobs; cancel a finished one | Queued: cancelled, nothing held; running: stop confirmed, then cancelled, then released; finished: 409 |

Every system test ends with `assert_invariants(settled=True)` after waiting for all jobs to settle.

## 13. Testing, benchmarks and KPI measurement (slices X-4 and X-6)

Tests are written with the code in every slice. Sprint 3 adds the measured runs that compare the system with each NFR target, using one API client, one sampler and one set of SQL queries over the history table.

### 13.1 Test levels and commands

| Level | Where | Needs | Command |
| --- | --- | --- | --- |
| Schema | `tests/schema/` | PostgreSQL 16 | `pytest -q tests/schema` |
| Engine unit and integration | `tests/e1` to `tests/e4` | PostgreSQL 16; stubs for other engines | `pytest -q tests/e3` (and so on) |
| Sandbox | `tests/e1/test_docker_*.py` | Linux, cgroup v2, Docker, image `judge-c:1` | `pytest -q -m docker tests/e1` |
| System | `tests/system/` | PostgreSQL 16; FakeRunner, or Docker with `-m docker` | `pytest -q tests/system` |
| Measured runs | `bench/` | The demo machine, server running | `python -m bench.b1_normal` (and so on) |

Rules from the project instructions hold everywhere: real PostgreSQL for locks, triggers and constraints; concurrency tests with real threads, separate connections, a Barrier and repeated runs; a flaky test is a race to fix, never a sleep to add; every system test and every measured run ends with the invariant query.

### 13.2 Measured runs

The baseline names V1 (1,000 requests), R1 (20 programs), B2 (saturation, both policies), and B1, F1 and F2 whose exact definitions sit in the S0 and S1 material (§13). The definitions below are proposals that fill that gap; if the S0 or S1 documents define a run differently, theirs wins.

| Run | Script | Workload (proposal unless stated) | Measures |
| --- | --- | --- | --- |
| V1 | `bench/v1_validation.py` | 1,000 requests through the API (baseline count): 800 well-formed, 200 each breaking one field, fixed seed | NFR-03: share of correct accept or reject decisions, at least 99% |
| R1 | `bench/r1_reference.py` | The 20 reference programs of 9.9 (baseline count) | NFR-03: verdict accuracy, at least 99% (all 20) |
| B1 normal load | `bench/b1_normal.py` | 10 minutes of arrivals at 60 per minute: 70% `code_eval` (problem 12, 1,000 millicores, 256 MiB, 5 s), 10% each `cpu_bound` (2,000 millicores, 512 MiB, 20 s), `mem_bound` (1,000 millicores, 4,096 MiB, 20 s), `mixed` | NFR-01 wait and start, NFR-02 completed per minute, NFR-07 release latency, NFR-09 push delay |
| T1 timeouts | `bench/t1_timeouts.py` | 100 jobs that run 30 s with a 5 s limit (the §13 E1 test, repeated) | NFR-10: share stopped and classified as timeout, at least 99% |
| B2 saturation | `bench/b2_saturation.py` | 300 jobs in 60 s whose total demand is at least 3 times the allocatable CPU, RAM and VRAM, including `gpu_bound` jobs of 4,000 to 8,000 MiB; run once with FCFS and once with priority by need (baseline) | NFR-04 average utilisation over the active window; waiting time per need class; NFR-06 peak allocated |
| F1 worker failure | `bench/f1_worker_kill.py` | B1 at 30 per minute; `kill -9` the worker child of 20 running jobs at random times; kill one job's worker 3 times | NFR-08 detection and requeue time; the third kill ends that job |
| F2 server crash | `bench/f2_server_crash.py` | B1 load; `kill -9` the server with jobs in every state; restart | FR-14 reconciliation; invariant and leak queries clean; no duplicate release audit rows |

Every run ends with `assert_invariants(settled=True)` and stores its raw output under `bench/results/<run>-<timestamp>/`.

### 13.3 Samplers and clients

- `bench/client.py`: login, submit (multipart), wait for a job's terminal state, read events. Used by every script, so measured runs go through the same API as users.
- `bench/sampler.py`: every 1 s writes `resources.allocated` per row to CSV (NFR-04, NFR-06), and every 5 s compares the jobs recorded as `running` with the containers Docker reports (NFR-05 state accuracy, at least 99% of samples matching).
- `bench/ws_latency.py`: one WebSocket client per role; for each event records receive time minus `at` (NFR-09, at most 2 s). Run it on the server machine so both clocks are the same.
- PostgreSQL runs with `log_lock_waits = on`; after each run, count `deadlock detected` lines in its log. The expected count is 0 (the §17 proposal).

### 13.4 KPI queries (`bench/kpi.sql`)

All six queries below were run on PostgreSQL 16 against the schema of section 4.

```sql
-- name: nfr01_wait_and_start
WITH h AS (
    SELECT job_id,
           min(at) FILTER (WHERE to_state = 'queued')   AS queued_at,
           min(at) FILTER (WHERE to_state = 'admitted') AS admitted_at,
           min(at) FILTER (WHERE to_state = 'running')  AS running_at
    FROM job_state_history
    WHERE job_id BETWEEN %(first_job)s AND %(last_job)s
    GROUP BY job_id)
SELECT count(*) AS jobs,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM admitted_at - queued_at)) AS p95_wait_s,
       max(extract(epoch FROM admitted_at - queued_at))                                          AS max_wait_s,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM running_at - queued_at))  AS p95_start_s,
       max(extract(epoch FROM running_at - queued_at))                                           AS max_start_s
FROM h WHERE admitted_at IS NOT NULL;

-- name: nfr02_completed_per_minute
SELECT date_trunc('minute', at) AS minute, count(*) AS completed
FROM job_state_history
WHERE to_state = 'completed' AND at >= %(from)s AND at < %(to)s
GROUP BY 1 ORDER BY 1;

-- name: nfr07_release_latency
SELECT count(*)                                                    AS terminal_events,
       max(extract(epoch FROM r.at - t.at))                        AS max_release_s,
       count(*) FILTER (WHERE r.at - t.at > interval '2 seconds')  AS over_2_s,
       count(*) FILTER (WHERE r.at IS NULL)                        AS never_released
FROM job_state_history t
LEFT JOIN LATERAL (
    SELECT at FROM job_state_history r
    WHERE r.job_id = t.job_id AND r.event_id > t.event_id AND r.to_state = 'released'
    ORDER BY r.event_id LIMIT 1) r ON true
WHERE t.to_state IN ('completed', 'failed', 'cancelled')
  AND EXISTS (SELECT 1 FROM allocations a WHERE a.job_id = t.job_id)
  AND t.at >= %(from)s AND t.at < %(to)s;

-- name: nfr08_recovery   (kill times come from the F1 script; detection time from the server log)
SELECT k.job_id,
       extract(epoch FROM f.at - k.killed_at) AS failed_s,
       extract(epoch FROM q.at - k.killed_at) AS recover_s
FROM unnest(%(job_ids)s::bigint[], %(killed_at)s::timestamptz[]) AS k(job_id, killed_at)
JOIN LATERAL (SELECT at FROM job_state_history
              WHERE job_id = k.job_id AND to_state = 'failed' AND actor = 'E4' AND at > k.killed_at
              ORDER BY event_id LIMIT 1) f ON true
LEFT JOIN LATERAL (SELECT at FROM job_state_history
                   WHERE job_id = k.job_id AND to_state = 'queued' AND at > f.at
                   ORDER BY event_id LIMIT 1) q ON true;

-- name: nfr10_timeouts
SELECT count(*) AS jobs,
       count(*) FILTER (WHERE a.end_reason = 'timeout') AS classified_timeout,
       round(100.0 * count(*) FILTER (WHERE a.end_reason = 'timeout') / greatest(count(*), 1), 2) AS pct
FROM attempts a
WHERE a.job_id = ANY(%(job_ids)s);

-- name: nfr04_utilisation_sample   (run by the sampler every 1 s)
SELECT kind, gpu_index, allocated, total - reserved AS allocatable
FROM resources ORDER BY resource_id;
```

NFR-01 note: during B1 capacity is mostly free, so `queued` to `admitted` is the scheduling wait the baseline defines (eligible to decision). During B2 a long wait is expected and is reported per need class, not against NFR-01.

### 13.5 KPI report (`bench/report.py`, slice X-6)

Reads every run's results and writes `docs/kpi_report.md`: one row per NFR with the target, the measured value, the run it came from, and pass or fail. That table is the Sprint 3 evidence that "measured KPIs are compared with the NFR targets" (§15).

## 14. Build sequence: every slice from line 1 to the final product

33 slices in six weeks, following the baseline's start order: E3 and the E4 state model first, E2 and E1 in parallel on stubs, then the E4 API and pages, then integration. Week numbers follow the baseline's proposed Sprint 2 (Weeks 10 to 12) and Sprint 3 (Weeks 13 to 15); the split inside each week is a proposal.

### 14.1 Sprint 2: build and integrate (Weeks 10 to 12)

| Slice | Owner | Week | Builds | Needs | Done when | Covers |
| --- | --- | --- | --- | --- | --- | --- |
| A-1 | Abhishek; every member sets up their machine | 10 | Repository, `CLAUDE.md`, `config.py`, requirements, `pyproject.toml`, `docker-compose.yml`, docs folder | nothing | `docker compose up -d db` works; `pytest -q` runs; each member's machine prints `cgroup2fs` | setup |
| A-2 | Sagar; each owner reviews their own tables | 10 | `db/roles.sql`, `db/schema.sql`, `tests/schema/` | A-1 | 15 tables, 17 foreign keys; the 25 checks of section 4 pass as tests | FR-05, FR-06, FR-11, NFR-05, NFR-06 |
| A-3 | Sagar | 10 | `db/seed.sql`, `db/invariants.sql`, `seed_data/problems/12/`, `scripts/reset_db.sh`, `scripts/capacity.py` | A-2 | Reset from a clean checkout; S0 free capacity 28,800, 117,964 and 14,745; invariant query empty | NFR-06 |
| A-4 | Sagar | 10 | `app/common/*`, `tests/conftest.py`, `helpers.py`, `stubs.py` | A-2 | The six checks at the end of 5.6 | invariants 2, 3, 6 |
| E4-1 | Aniruddh | 10 | `state.py` | A-4 | All 64 state pairs tested | FR-11, NFR-05 |
| E3-1 | Sagar | 10 | `placement.py`, `queries.py`, `snapshot()` | A-4 | `test_placement`, `test_snapshot` | FR-03 |
| E3-2 | Sagar | 10 | `allocate()` | E3-1, E4-1 | `test_allocate`; `test_e3_allocation` last unit, 100 runs | FR-05, FR-06, NFR-06 |
| E2-1 | Sudhanv | 10 | `validation.py`, `submit()` | A-4, E4-1 | `test_validation`, `test_v1_batch` | FR-02, NFR-03 |
| E2-2 | Sudhanv | 10 | `queue.py` | A-1 | Property test, 20 seeds by 10,000 operations | FR-01 |
| E1-1 | Abhishek | 10 | `files.py`, `judge.py`, `sandbox.py`, `fake_runner.py` | A-4 | `test_files`, `test_judge` | FR-09 |
| E3-3 | Sagar | 11 | `release()` | E3-2 | `test_release` | FR-13, NFR-07 |
| E2-3 | Sudhanv | 11 | `policy.py`, `loop.py`, `run_cycle()` | E2-2, StubE3 | `test_policy`, `test_loop` | FR-01, FR-03, NFR-01 |
| E2-4 | Sudhanv | 11 | requeue, cancel\_queued, pause and resume, set\_policy, rebuild, queue\_view | E2-3 | `test_requeue_cancel` | FR-14 (requeue) |
| E1-2 | Abhishek | 11 | `supervisor.py`, `worker.py`, `launcher.py`, `service.py` | E1-1, StubE3, StubE4 | `test_dispatch`, `test_supervisor` | FR-07, FR-08 |
| E1-3 | Abhishek | 11 | `sandbox/Dockerfile`, `docker_runner.py` | E1-2 | The five `test_docker_*` files | FR-08, NFR-10 |
| E4-2 | Aniruddh | 11 | `auth.py`, `scripts/add_users.py` | A-4 | `test_auth` | FR-15 |
| E4-3 | Aniruddh | 11 | `api.py`, `schemas.py`, error handlers | E4-2, StubE2 | `test_api_jobs`, `test_roles` | FR-02, FR-15 |
| E4-4 | Aniruddh | 11 | `hub.py`, `/ws/events` | E4-1 | `test_hub` | FR-11, NFR-09 |
| E4-5 | Aniruddh | 11 | `liveness.py` and the sweep | E4-1 | `test_liveness` | FR-12 |
| E1-4 | Abhishek | 12 | `bench/r1/`, `scripts/add_problem.py` | E1-3 | `test_r1_reference`, 20 of 20 | FR-09, NFR-03 |
| E4-6 | Aniruddh | 12 | `recover()` | E4-5 | `test_recovery` | FR-14, NFR-08 |
| E4-7 | Aniruddh | 12 | `reconcile()` | E4-6 | `test_reconcile` | FR-14 |
| C-1 | Aniruddh leads, all four | 12 | `app/main.py`; stub swaps 1 to 7 of 12.2 | every B slice | `test_lab_exam_fake`, `test_burst`, `test_lost_worker`, `test_restart` | all Mandatory FRs |
| C-2 | Aniruddh | 12 | Login, Submit, My jobs, Job detail pages | E4-3, E4-4 | Click through the scenario | FR-11, FR-15 |
| C-3 | Sudhanv | 12 | Live monitor and Admin console pages | E4-3, E4-4 | Bars, queue and policy switch update live | FR-01, FR-11 |
| C-4 | all four | 12 | Docker run of the scenario, README | C-1, E1-3 | `test_lab_exam_docker` passes: **Sprint 2 exit** | Sprint 2 exit |

### 14.2 Sprint 3: Challenge, measurement, demo (Weeks 13 to 15)

| Slice | Owner | Week | Builds | Needs | Done when | Covers |
| --- | --- | --- | --- | --- | --- | --- |
| X-1 | Abhishek, with Sudhanv and Aniruddh | 13 | Cancel endpoint, `e1.cancel`, wiring to `e2.cancel_queued`, Cancel button | C-4 | `test_cancel`; cancel rows of the role matrix | FR-10 |
| X-2 | Aniruddh | 13 | `health.py` | C-4 | `test_health` | FR-04 |
| X-3 | Sudhanv (lab sessions), Abhishek (problems) | 13 | `POST /api/lab_sessions`, `POST /api/problems`, admin page forms | C-4 | Faculty rows of the role matrix | faculty role (§7), proposal |
| X-4 | V1, B1, B2: Sudhanv; R1, T1, F1: Abhishek; F2, `ws_latency`: Aniruddh; sampler, `kpi.sql`, `report.py`: Sagar | 14 | `bench/` | C-4 | Each script runs end to end on a laptop at reduced scale | NFR-01 to NFR-10 |
| X-5 | Sagar | 14 | TLS, log rotation, transaction timing logs, pool sizing, `EXPLAIN` of the hot queries, deadlock count | C-4 | B1 at full scale with 0 deadlocks | NFR-02, NFR-05 |
| X-6 | all four | 15 | All runs on the demo machine, `docs/kpi_report.md`, fixes, rehearsal | X-1 to X-5 | Every Mandatory acceptance criterion passes; the KPI table is complete: **Sprint 3 exit** | Sprint 3 exit |
| X-7 (optional) | Sagar, Abhishek | 15 | Capacity profile endpoint; NVIDIA GPU passthrough | X-6 | Only if the demo machine has GPUs | proposal |

### 14.3 Rules for the sequence

- A slice starts only when everything in its Needs column is merged.
- Every owner keeps one slice in progress at a time; nobody waits, because every cross-engine call has a stub from A-4.
- If a slice finds a flaw in the design, it stops and reports in 3 lines (what, evidence, fix); the four owners decide before anyone patches around it.

## 15. Prompts to paste into Claude Code

One template covers every slice; the table says which sections to cite. Six slices carry most of the risk, so their prompts are written out in full. Start each slice in a fresh session on its own branch.

### 15.1 Template

```text
Slice <ID>: <title from section 14>. Owner: <name>.

Read CLAUDE.md. Then read only these parts of docs/IMPLEMENTATION_PLAN.md: <plan sections>,
and of docs/PROJECT_CONTEXT.md: <context sections>.

Build exactly what the slice row in section 14 lists: <files>.
Do not change db/schema.sql or another engine's package. Cross-engine calls go through
app/common/ports.py; use the stubs in tests/stubs.py for engines not built yet.

Tests: <test files from the plan>. Real PostgreSQL 16 from db/schema.sql; no SQLite,
no mocks for the database.

First reply with a plan of at most 3 lines (files, interface, test) and wait for my OK.
If the plan or the baseline looks wrong, report it in 3 lines (what, evidence, fix) and wait.
When done, show: the pytest summary line, the invariant query result, the run command,
and the lines you added to docs/decisions.md.
```

| Slice | Plan sections | Context sections |
| --- | --- | --- |
| A-1 | 0, 3 | §6, §18 |
| A-2, A-3 | 4, 6.5 | §3, §11, §12 |
| A-4 | 5 | §8 |
| E4-1 | 4.2, 7 | §9 |
| E3-1 to E3-3 | 6 | §7 (E3), §10, §17 |
| E2-1 to E2-4 | 8 | §7 (E2), §10, §12 |
| E1-1 to E1-4 | 9 | §7 (E1), §10 |
| E4-2 to E4-7 | 10 | §7 (E4), §9, §10, §12 |
| C-1 to C-4 | 2, 11, 12 | §8, §15 |
| X-1 | 9.7, 10.2, 10.3, 12.3 | §4 (FR-10) |
| X-2 | 10.8 | §4 (FR-04) |
| X-3 | 10.2, 11 | §7 (roles) |
| X-4 to X-6 | 13 | §4 (NFRs), §13, §17 |

### 15.2 A-2 and A-3: the database

```text
Slice A-2 then A-3: database. Owner: Sagar.
Read CLAUDE.md and docs/IMPLEMENTATION_PLAN.md section 4.
Create db/roles.sql, db/schema.sql, db/seed.sql, db/invariants.sql and scripts/reset_db.sh
exactly as written in section 4 (they were already run on PostgreSQL 16). Write
seed_data/problems/12/01.in .. 10.out for "Sum of N integers" (N on line 1, then N
integers; output their sum), including N = 1, negatives and a sum needing 64 bits.
Write scripts/capacity.py as in section 6.5.
Turn the 25 checks of section 4 into tests/schema/test_constraints.py,
test_state_trigger.py, test_triggers.py and test_grants.py, each running as orch_app
through SET LOCAL ROLE, against a fresh database cloned from a template.
Done: 15 tables, 17 foreign keys, all schema tests pass, reset_db.sh works from a clean
checkout, the invariant query returns no rows.
```

### 15.3 E3-2: allocate

```text
Slice E3-2: allocate(). Owner: Sagar.
Read CLAUDE.md, plan sections 5 and 6, context §7 (E3), §10 and §17.
Implement app/e3_allocation/service.py allocate() as in section 6.3: one lock statement on
CPU, RAM and (when vram_mib > 0) every GPU row by cached id, ORDER BY resource_id FOR UPDATE;
re-check with placement.fit(); refusal writes one refused audit row and moves the job back
to queued (decision D-01); commit path writes allocation, items, audit and transition in the
same transaction; dispatch and the capacity push only through t.after_commit().
Tests: tests/e3/test_allocate.py and tests/e3/test_e3_allocation.py as listed in 6.6. The
last-unit test uses two threads, separate connections, a Barrier and 100 runs, and asserts
exactly one Committed per run and a clean invariant query. The stress test counts 40P01
errors and expects 0.
```

### 15.4 E2-3: admission loop and policies

```text
Slice E2-3: admission loop. Owner: Sudhanv.
Read CLAUDE.md, plan sections 5.2 and 8, context §7 (E2) and §10.
Implement policy.py (select_fcfs, select_need with the 60 s guard), loop.py (AdmissionLoop:
one thread, clock.wait(event, 0.5), never overlapping cycles) and run_cycle() in service.py
as in 8.3, including decision D-03 (repeat until nothing fits or a refusal). Take the E2 RLock
before any transaction and never inside one. A job leaves the queue only after Committed.
Tests: tests/e2/test_policy.py and tests/e2/test_loop.py with StubE3 and FakeClock, as in 8.5.
No real sleeps in tests.
```

### 15.5 E1-2 and E1-3: supervisor, worker and Docker

```text
Slice E1-2: supervisor and worker on fakes. Owner: Abhishek.
Read CLAUDE.md, plan sections 2.3 and 9, context §7 (E1) and §10.
Implement worker.py (the JSON-line protocol of 9.4), launcher.py (real subprocess launcher
and a fake thread launcher for tests), supervisor.py (steps 1 to 5 of 9.5, including the
close_worker ownership rule) and service.py dispatch() and stop() as in 9.7. The deadline uses
clock.monotonic(); set the timeout flag before killing anything. The terminal state is written
only after the stop is confirmed, then e3.release() is called.
Tests: test_dispatch.py and test_supervisor.py with FakeRunner, the fake launcher, FakeClock,
StubE3 and StubE4, covering every row of the table in 9.5.

Slice E1-3: Docker backend. Owner: Abhishek.
Implement sandbox/Dockerfile and docker_runner.py exactly as in 9.3: subprocess argument
lists, never shell=True; stop = kill -TERM -1 inside the container, wait up to 2 s, docker kill,
docker rm -f, confirm with docker inspect; memory verdict from the oom_kill counter in the
container's memory.events, never from exit 137 alone.
Tests (marked docker): the five test_docker_* files of 9.9.
```

### 15.6 E4-5 to E4-7: liveness, recovery, reconciliation

```text
Slices E4-5, E4-6, E4-7. Owner: Aniruddh. One slice per session, in this order.
Read CLAUDE.md, plan sections 2.3 and 10.5 to 10.7, context §7 (E4), §9 and §10.
E4-5: register_worker, heartbeat, close_worker and the 1 s sweep with the SQL of 10.5; each
lost attempt becomes one recover() task on a 4-thread executor.
E4-6: recover() exactly as in 10.6. Order: e1.stop() (confirmed), transition, e3.release(),
e2.requeue(). StopNotConfirmed means no release and no requeue.
E4-7: reconcile() following the table in 10.7, then assert both invariant queries are empty.
Tests: test_liveness.py (late beat ignored; marked lost once under 4 concurrent sweeps;
close_worker racing the sweep, exactly one winner, 100 runs), test_recovery.py,
test_reconcile.py. In tests, move last_beat_at back in time instead of sleeping 6 s.
```

### 15.7 C-1: integration

```text
Slice C-1: integration. Owners: all four, Aniruddh leads.
Read CLAUDE.md, plan sections 2 and 12, context §8 and §15.
Write app/main.py as in 12.1 (startup order: pool, schema check, hub, engines, reconcile,
E2 rebuild, loops). Replace stubs one step at a time in the order of table 12.2 and run the
system tests after each step. Stop at the first failing step and report it in 3 lines.
Tests: tests/system/test_lab_exam_fake.py, test_burst.py, test_lost_worker.py, test_restart.py.
```

### 15.8 Review, debugging and viva prompts

```text
Review: Read CLAUDE.md. Review the diff of this branch against main for: the nine hard
invariants, writes to another engine's table, anything done inside a transaction that leaves
the process, lock order, missing tests for a rule in the slice row. List findings only, most
serious first, each with file, line and a one-line fix.

Debug: <paste the failing test output>. Find the cause before changing code. If it is a race,
explain the interleaving step by step and fix the race; do not add sleeps or retries.

Viva: Explain app/<engine>/<file>.py to me as my examiner would ask it: what each function
does, which requirement it serves (FR or NFR ID), which OS, DBMS or DSA concept it uses, and
the two questions I am most likely to be asked about it. Plain words, short, nothing invented.
```

## 16. Deployment on the demo machine and the final demo (slices X-5, X-6)

The demo machine runs one server process behind TLS on the LAN, PostgreSQL 16 and Docker on the same host. The same steps work on a team laptop with Linux as the fallback.

### 16.1 scripts/setup\_host.sh (run once, as root)

1. **Check the kernel:** `stat -fc %T /sys/fs/cgroup` must print `cgroup2fs`; stop if not.
2. **Install:** PostgreSQL 16, Docker Engine, Python 3.11 or later with `venv`, `openssl`.
3. **Service user:** create `orch`, add it to the `docker` group (this is root-equivalent on the host, so the machine is dedicated to the demo), clone the repository to `/opt/orch`, create the venv, install the pinned requirements.
4. **Database:** run `db/roles.sql` with a new `orch_app` password, create database `orch`, run `db/schema.sql` and `db/seed.sql`, replace the demo passwords with `scripts/add_users.py`, then `python -m scripts.capacity --profile host`.
5. **Sandbox image:** `docker build -t judge-c:1 sandbox/`, then pre-pull nothing else; the demo needs no internet.
6. **TLS for the LAN (proposal):** a self-signed certificate for the machine's IP address: `openssl req -x509 -newkey rsa:2048 -nodes -days 90 -keyout key.pem -out cert.pem -subj "/CN=<ip>" -addext "subjectAltName=IP:<ip>"`.
7. **Service:** a systemd unit running `uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8443 --workers 1 --ssl-keyfile key.pem --ssl-certfile cert.pem` as user `orch`, `Restart=on-failure`, environment from `/opt/orch/.env`.
8. **Firewall:** allow TCP 8443 from the lab subnet only.
9. **Smoke test:** `python -m bench.client smoke --url https://<ip>:8443` logs in, submits one job and waits for `Accepted 10/10`.

Laptop fallback: identical, with `CAPACITY_PROFILE=host` and `GPU_MODE=simulated`.

### 16.2 The demo (the lab-exam scenario of §15)

| Step | Action | What the room sees | Requirement |
| --- | --- | --- | --- |
| 1 | A student logs in | Token and role; the Submit page opens | FR-15 |
| 2 | Submits `prog.c`: 1,000 millicores, 512 MiB, 0 VRAM, 5 s, exam session 7 open | 201 with Job ID 1042, state queued, need class 1 | FR-02 |
| 3 | The next cycle admits it | `admitted` on the monitor | FR-01, FR-03 |
| 4 | E3 allocates in one locked transaction | `allocated`; CPU and RAM bars move; one audit row | FR-05, FR-06 |
| 5 | E1 starts the sandbox | `running`; heartbeats every 2 s | FR-07, FR-08 |
| 6 | E1 judges 10 test cases | Verdict `Accepted 10/10` | FR-09 |
| 7 | E1 asks for release; E3 releases and wakes E2 | `completed`, then `released`; bars return | FR-13, NFR-07 |
| 8 | Every change is pushed | Each state appears within 2 s; the monitor's delay column shows it | FR-11, NFR-09 |
| 9 (variation) | `kill -9` the worker of a running job | Lost after 6 s, stopped, released, requeued within 10 s | FR-12, FR-14, NFR-08 |
| 10 (Challenge) | Cancel a running job; switch the policy during a burst | Stop, then cancelled, then released; an exam job overtakes practice work | FR-10, FR-01 |

Close with three queries on screen: the job's `job_state_history` rows, its `allocation_audit` rows, and the invariant query returning no rows. Then show `docs/kpi_report.md`.

Before the demo:

- [ ] Rehearse on the demo machine itself, twice, with a fresh `reset_db.sh`.
- [ ] Confirm the image `judge-c:1` exists and the machine works without internet.
- [ ] Change all demo passwords; keep one student, one faculty and one administrator account ready.
- [ ] Keep the fallback laptop set up the same way, with the same seed.

## 17. Decisions, proposals and open items

Nothing below changes the baseline. Decisions fill places where the baseline is silent or cannot be built literally; proposals are numbers or features the baseline does not state. Copy the decision lines into `docs/decisions.md` in slice A-1.

### 17.1 Decisions (one line each for docs/decisions.md)

```text
D-01 Refused allocate writes the refused audit row and admitted->queued in the same transaction, then commits: the §10 "ROLLBACK; audit" cannot keep the audit row in one transaction. NEEDS TEAM CONFIRMATION.
D-02 One database role per engine (SET LOCAL ROLE), column grant UPDATE(state) for transition(), SECURITY DEFINER trigger for history: §7 ownership enforced by the database.
D-03 The admission cycle repeats selection until nothing fits or a refusal: one admission per 0.5 s would break NFR-01 in a lab-exam burst.
D-04 Recovering or reconciling a job that never started uses allocated->cancelled with actor E4: the only legal path from allocated that is not running.
D-05 Per test case limit = problems.time_limit_ms, a verdict (TLE); job limit = jobs.time_limit_s, a failure (timeout): both columns exist in §11.
D-06 A sandbox that fails to start ends the job as cancelled ("sandbox failed to start"), no requeue: §9 requeues only lost workers.
D-07 "Worker" = one child process per attempt that runs the commands and prints a heartbeat every 2 s; the supervisor thread relays beats and does all database writes.
D-08 Whoever moves worker_liveness from alive first (closed by the supervisor, lost by the sweep) owns the end of the attempt: no double release or double verdict.
D-09 Docker Engine CLI behind SandboxRunner; stop = kill -TERM -1 in the container, 2 s, docker kill, docker rm, inspect to confirm: docker stop alone would skip SIGTERM for exec'd programs.
D-10 Allowed transitions live inside the trigger function: the schema stays at 15 tables.
D-11 Actor and reason reach the trigger as transaction-local settings, cleared after each transition(): application code never writes history.
D-12 READ COMMITTED isolation (the §16 working assumption) with row locks and the re-check inside the lock.
D-13 E2's FIFO list is sorted by (submitted_at, job_id): a requeued job returns to its original place.
D-14 The source file is a multipart part named source; payloads live in DATA_DIR/payloads/<uuid>/.
D-15 WebSocket clients authenticate with their first message: browsers cannot set headers on a WebSocket.
D-16 uvicorn runs with exactly one worker process: the queue and the loops are in-process singletons.
D-17 Demo seed reproduces the §12 sample: problem 12, exam session 7, first Job ID 1042.
```

### 17.2 Proposals (numbers and features not in the baseline)

| Area | Proposal |
| --- | --- |
| Workload types | `code_eval`, `cpu_bound`, `mem_bound`, `gpu_bound`, `mixed` (from the five S0 workload kinds); default need classes 2, 3, 3, 3, 3; maximum limits 60, 600, 600, 1,800, 600 s |
| Validation | CPU at least 100 millicores; source at most 64 KiB, `.c` or `.cpp`; a given lab session must be open |
| Execution | Output cap 64 KiB per case; compile timeout 10 s; stop-confirm timeout 10 s; `/tmp` tmpfs of 64 MiB; output comparison ignores trailing spaces and trailing empty lines |
| Server | Token lifetime 8 h; pool 4 to 40 connections; recovery executor of 4 threads; WebSocket queue of 1,000 events per client |
| Health guard | Probe every 1 s; unhealthy below 10% available memory or when Docker does not answer; resume after 3 healthy reads |
| Endpoints | `POST /api/auth/logout`, `GET /api/me`, `GET /api/workload_types`, `GET /api/lab_sessions`, `GET /api/admin/status`, `POST /api/lab_sessions`, `POST /api/problems` |
| Measurement | Workloads of B1, T1, B2, F1, F2 and the R1 composition in section 13 |
| Deployment | Self-signed TLS on port 8443; capacity profile `host` |

### 17.3 Open items for the team

1. **Confirm D-01**, the refusal path of allocate. It is the only place where the plan departs from the letter of the §10 pseudocode.
2. **B1, F1 and F2 definitions.** The context file says they are in the S0 and S1 material; they were not found in this project's files. Send them if they exist, and section 13.2 will be corrected.
3. **Demo machine.** Confirm it runs Linux with cgroup v2 and allows Docker, and whether its GPUs are available (decides `GPU_MODE`).
4. **Faculty endpoints (X-3).** Keep them, or create sessions and problems only with scripts.
5. **Languages.** C only, or C and C++ (same image either way).
6. **Outdated text to avoid.** The Stage 2 document still shows the login body as `{usn, password}` and `cpu_milli`; the code follows §11 and §12 (`user_id`, `cpu_millicores`).

## 18. Traceability: every requirement to code, test and slice

Each of the 15 FRs and 10 NFRs has at least one module that builds it, one test or run that proves it, and one slice that delivers it.

| ID | Built in | Proven by | Slice |
| --- | --- | --- | --- |
| FR-01 | `e2_scheduling/queue.py`, `policy.py`, `loop.py` | `test_queue`, `test_policy`; B2 under both policies | E2-2, E2-3 |
| FR-02 | `e2_scheduling/validation.py`, `submit()`; `POST /api/jobs` | `test_validation`, `test_api_jobs`; V1 | E2-1, E4-3 |
| FR-03 | Fit check in `policy.py`; re-check in `e3 allocate()` | `test_policy`, `test_loop`, refusal case of `test_allocate` | E2-3, E3-2 |
| FR-04 | `e4_monitoring/health.py`; `pause_admission`, `resume_admission` | `test_health` | X-2 |
| FR-05 | `e3 allocate()`; `resources_allocated_ck`; one-open-allocation index | `test_allocate`, `test_e3_allocation` | E3-2 |
| FR-06 | Audit inserts in `allocate()` and `release()`; append-only trigger | `test_allocate`, `test_release`; invariant checks 5 and 6 | E3-2, E3-3 |
| FR-07 | `e1 dispatch()`, `supervisor.py` | `test_dispatch`; lab-exam system tests | E1-2 |
| FR-08 | `docker_runner.py`; deadline and finish order in `supervisor.py` | `test_supervisor`; the five `test_docker_*` files | E1-2, E1-3 |
| FR-09 | `worker.py` compile and run; `judge.py` | `test_judge`, `test_r1_reference`; R1 | E1-1, E1-4 |
| FR-10 | Cancel endpoint; `e2.cancel_queued`; `e1.cancel` | `test_cancel`; cancel rows of `test_roles` | X-1 |
| FR-11 | State trigger and `transition()`; hub; pages | `test_transition`, `test_hub`, `test_lab_exam_fake` | E4-1, E4-4, C-2, C-3 |
| FR-12 | Worker heartbeats; `liveness.py` sweep | `test_liveness`, `test_lost_worker` | E1-2, E4-5 |
| FR-13 | `e3 release()`; stop-before-release order in E1 and E4 | `test_release`, `test_supervisor`; leaks query | E3-3, E1-2 |
| FR-14 | `recover()`, `reconcile()`; `e2.requeue()` | `test_recovery`, `test_reconcile`, `test_restart`; F1, F2 | E4-6, E4-7, E2-4 |
| FR-15 | `auth.py`; role checks in `api.py`; filtering in `hub.py` | `test_auth`, `test_roles`, `test_hub` | E4-2, E4-3, E4-4 |
| NFR-01 | Admission loop with D-03; wake on submit and release | `test_burst`; B1 `nfr01_wait_and_start` | E2-3, X-4 |
| NFR-02 | The whole pipeline | B1 `nfr02_completed_per_minute` | X-4, X-6 |
| NFR-03 | Validation; judge | `test_v1_batch`; V1; R1 | E2-1, E1-4 |
| NFR-04 | Both policies; best-fit GPU | B2 with `sampler.py` | E2-3, E3-1, X-4 |
| NFR-05 | Trigger; audit; invariant queries | Invariant queries after every test and run; sampler state check | A-2, X-4 |
| NFR-06 | CHECK, row locks, re-check inside the lock, reserve CHECK | `test_e3_allocation`; sampler peak in B2 | A-2, E3-2 |
| NFR-07 | Release right after the confirmed stop; after-commit notice | `test_release`; `nfr07_release_latency`; leaks query | E3-3, X-4 |
| NFR-08 | Liveness sweep and `recover()` | `test_lost_worker`; F1 `nfr08_recovery` | E4-5, E4-6, X-4 |
| NFR-09 | Hub publishes after COMMIT | `test_hub`; `ws_latency.py` during B1 | E4-4, X-4 |
| NFR-10 | Supervisor deadline, timeout flag before kill | `test_docker_timeout`; T1 `nfr10_timeouts` | E1-3, X-4 |
