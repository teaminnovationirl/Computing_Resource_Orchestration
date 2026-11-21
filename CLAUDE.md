# CLAUDE.md

Product: Computing Resource Allocation & Utilization Orchestration System
(Team 10 Mini Project, Theme 11). A LAN server where students submit C, C++,
Java and Python programs that are judged in a LeetCode-style workspace or run
with their own input, and Python ML/DL jobs from a Colab-style notebook on CPU
or GPU, all competing for limited CPU, RAM and GPU memory. The clients are the
browser version (desktop and phone) and two Windows programs (client and
server), as docs/PRODUCT_SPEC.md defines. One FastAPI process with four engine
modules, one PostgreSQL 16 database of 15 tables, one Docker container per job
under cgroups v2.

Read before writing code, in this order:
1. docs/PROJECT_CONTEXT.md  baseline (FR-01..FR-15, NFR-01..NFR-10, schema,
   messages, states). It wins over everything else, except where
   docs/PRODUCT_SPEC.md records a scope decision of mine (see Addendum).
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
- Inside tx(), take savepoints only with t.savepoint(), never connection.transaction() directly:
  t.savepoint() also drops the after_commit hooks of work it rolls back (D-67).
- Never shell=True. Every file path must resolve inside DATA_DIR.
- No new tables, columns, endpoints or libraries unless the plan or
  docs/PRODUCT_SPEC.md lists them.
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

## Addendum (overrides anything above that conflicts)

- Package names follow docs/IMPLEMENTATION_PLAN.md section 3.4: app/e1_execution, app/e2_scheduling, app/e3_allocation, app/e4_monitoring. Ignore the folder names in docs/LAN_BUILD_GUIDE.md.
- Never read a whole document except docs/PRODUCT_SPEC.md. Read only the sections the slice prompt names. docs/PROJECT_CONTEXT.md is the baseline and wins every conflict, except where docs/PRODUCT_SPEC.md records a scope decision of mine.
- Read docs/PRODUCT_SPEC.md before any slice it names. It records scope I decided beyond the baseline; where it differs from the plan or the baseline it wins, and each difference is logged in decisions.md.
- docs/LAN_BUILD_GUIDE.md is only for LAN, agent, discovery, isolate and deployment slices, and only the section the prompt names. Where it differs from the plan, docs/decisions.md decides.
- Liveness and heartbeat SQL uses clock_timestamp(), never now().
- Live updates are published by transition() after commit. Do not add pg_notify triggers unless docs/decisions.md says so.
- At the start of every session read docs/SUMMARY.md first. Do not explore the codebase to rebuild context. Read only the files it points to.
- Before every commit, update docs/SUMMARY.md and commit it with the code. A slice is not done until it is updated.
- If a Superpowers skill conflicts with this file, this file wins. Use the 3 line plan and wait for my OK. Do not write design or plan documents unless asked.

## Machine limits (permanent; the development laptop hung under load on 2026-11-19)

- One pytest process at a time. Never run subagents in parallel: run them one after another, and a subagent never runs tests while another test run is active.
- During development run only the affected test files; run the full suite once, just before the commit. For a planted bug, run only the test file that should catch it.
- Do not pull or build extra Docker images for verification. Linux verification happens at C-4 or when asked.
- Before a command that creates many databases or may run longer than 5 minutes, say so first.
- The development PostgreSQL (docker-compose.yml) runs with fsync, synchronous_commit and full_page_writes off to cut disk writes. Development and tests only, never the demo machine. After a crash, rebuild it with the reset script.
