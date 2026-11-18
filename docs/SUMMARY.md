# Handoff summary

Current state of the build. Rewrite a section when it changes; this is not a log. Read it first, then only the files it points to.

Last updated: 2026-11-19, end of slice A-4.

## Status

| Slice | Status | Branch or commit |
| --- | --- | --- |
| A-1 | done; the cgroup2fs check is still open (see Gotchas) | merged to main (a0d9d2d) |
| A-2 | done: roles, schema (with the D-40 fix), seed, invariants, `tests/schema/` (25 tests), `scripts/reset_db.ps1` | merged to main (524d85f) |
| A-3 | done: `seed_data/problems/12/`, `scripts/capacity.py`, `scripts/reset_db.sh`, data folders, `.gitattributes`, the DB_NAME guard (D-46), 44 tests | merged to main (15cd962) |
| A-4 | done: `app/common/*`, `tests/conftest.py`, `helpers.py`, `stubs.py`, `tests/common/` (43 tests); capacity.py writes through tx("E3") (D-41 done); reviewed | slice/A-4, commit "A-4: shared common layer", not pushed or merged |
| all others | not started: E4-1, E3-1, E3-2, E2-1, E2-2, E1-1, E3-3, E2-3, E2-4, E1-2, E1-3, E4-2 to E4-7, E1-4, C-1 to C-4, X-1 to X-6, X-7 (optional); order in plan 14.1 | none |

## Changes in the last session (2026-11-19, slice A-4)

- New `app/common/` (db, clock, types, ports, events, errors, logging), `tests/conftest.py` (the A-2 fixtures moved here, D-42), `tests/helpers.py`, `tests/stubs.py`, `tests/common/` (43 tests, each written and failing first); `scripts/capacity.py` writes through tx("E3") (D-57); `pyproject.toml`: markers slow and system, pyright venv (D-59).
- Verified: 23 injected bugs, each caught by the test named for it; full suite 128 passed; pyright 0 errors on the A-4 files; invariants 0 rows on orch.
- Separate code review and an incident-thinker pass on tx(), retrying() and after_commit led to: the guard against a hand-sent ROLLBACK, the pool's connection check, the per-thread nesting flag, the `log.<level>()` API, at most 3 tries, a give-up log line, hook names in logs, and a stronger FakeClock sync.
- `docs/decisions.md`: D-47 to D-60 added.

## Code map

- `app/config.py`: settings from environment variables (plan 3.3). Public: `Config` (frozen dataclass, one lower case field per variable), `Config.from_env(env=os.environ)`, `CHOICES`.
- `app/common/db.py` (plan 5.1): `Engine`, `Tx` (`execute`, `fetchone`, `fetchall`, `after_commit`), `init_pool(dsn, min_size, max_size)`, `close_pool()`, `in_transaction()`, `assert_outside_tx(what)`, `tx(engine)`, `retrying(fn, attempts=3)`. tx() runs SET LOCAL ROLE eN_role, commits, and runs the hooks only after COMMIT with the connection back in the pool; a failing hook is logged and the next one runs (D-47); a caught statement error or a hand-sent COMMIT or ROLLBACK makes tx() raise and skip the hooks (D-48); nested tx() on one thread is refused (D-60). retrying() retries only 40P01 and 40001, 3 tries in all (D-55). init_pool() probes first and checks pooled connections (D-49).
- `app/common/clock.py`: `Clock` (`monotonic`, `time`, `wait(event, timeout)`), `RealClock`, `FakeClock(start=0.0)` with `advance(seconds)` and `wait_for_waiters(count)` (D-50).
- `app/common/types.py`: `Vector`, `Snapshot.fits`, `Limits`, `Committed`, `Refused`, `Released`, `AlreadyReleased`, `User` (plan 5.3), `SubmitRequest`, `SubmitResult` (D-52), `QueueEntry` (D-53).
- `app/common/ports.py`: `E1Port` to `E4Port` exactly as plan 5.4, `Engines`, `install(e)`, `engines()` (RuntimeError before install). `events.py`: the `Hub` protocol only (D-54). `errors.py`: `ApiError(status, error, field=None)`, `IllegalTransition`, `StopNotConfirmed`, `NotFound`. `logging.py`: `log.info/warning/error/exception(event, **fields)` and `configure(data_dir)`, JSON lines (D-58).
- `db/roles.sql`: roles e1_role to e4_role (NOLOGIN) and the login orch_app (NOINHERIT, password `orch`, to change on the demo machine). Run once per cluster as a superuser (plan 4.1).
- `db/schema.sql`: 15 tables, 17 foreign keys, CHECKs, triggers and grants (plan 4.2 with the D-40 fix). Custom error codes: JT001 illegal transition or a new job not in queued; JT002 jobs.state written (INSERT too) without the settings `app.actor` and `app.reason`; AO001 update or delete on an append-only table; FA001 session or problem owner who is not faculty; FA002 role change of a faculty member who owns sessions or problems; TR001 test result for a case outside the attempt's problem.
- `db/seed.sql`: workload types, demo users, lab sessions 7 and 8, problem 12 with 10 cases, the S0 capacity (ids 1 CPU, 2 RAM, 3 and 4 GPUs); job ids start at 1042 (plan 4.3). `db/invariants.sql`: two queries, `-- name: invariants` and `-- name: leaks`; each returned row is one violation (plan 4.4).
- `seed_data/problems/12/`: 10 cases of "Sum of N integers": N on line 1, the N integers on line 2, `.out` is the sum. LF only.
- `scripts/capacity.py`: writes the capacity profile (plan 6.5). Public: `Row`, `Free`, `reserve(total)`, `s0_rows()`, `host_rows(...)`, `apply(conn, rows)` (a psycopg connection or a Tx already acting as e3_role), `write(database_url, rows)` (a pool of one connection and tx("E3"), D-57), `main(argv=None, env=None)`, `CapacityError`. Locks every resources row in id order, refuses while an allocation is open, updates rows in place. `--check-database NAME` writes nothing (D-46). Exit 0; 1 on a refusal or a database error; 2 on a bad argument or setting; each error is one line on stderr.
- `scripts/reset_db.sh` (Linux or WSL2) and `scripts/reset_db.ps1` (Windows, D-38): stop first unless DATABASE_URL names DB_NAME (D-46), then roles, schema, seed, the DATA_DIR folders, the seed problems and capacity.py.
- `tests/conftest.py`: each session runs roles.sql once, drops leftover `t_<12 hex>` databases, builds `orch_test` (schema and seed; fixtures `owner_tx`, `app_tx`, class `OpenTx` with `refused(sqlstate)`) and `orch_template` (schema only). Fixture `db` copies the template into a fresh database per test, opens the pool as orch_app (1 to 4 connections), installs the stubs and yields `TempDb(name, owner_url, app_url, stubs)`. Fixture `json_log` configures logging into tmp_path.
- `tests/helpers.py`: `TempDb`, `assert_invariants(settled=False)`, `invariant_queries()`, `make_user(role)`, `make_problem(cases=10)`, `make_job(state="queued", vector=...)` (queued only, D-51). `tests/stubs.py`: `StubE1` to `StubE4`, `RecordingHub`, `install_stubs() -> Stubs`; each stub refuses calls inside a transaction (D-56).
- Tests: `tests/test_config.py` (16), `tests/schema/` (69: the 25 of A-2, 12 seed data, 32 capacity), `tests/common/` (43: test_tx 24, test_clock 4, test_ports 8, test_harness 6, test_logging 1).
- `.env.example` (all 24 variables), `pyproject.toml` (pytest and pyright settings), `requirements*.txt` (`==` pins, D-36), `docker-compose.yml` (PostgreSQL 16, loopback only, D-37), `.gitattributes` (D-43).
- `docs/`: `PROJECT_CONTEXT.md` (baseline, wins every conflict), `IMPLEMENTATION_PLAN.md` (build plan; 3.4 has the full file tree), `LAN_BUILD_GUIDE.md` (LAN slices only), `decisions.md`, `SUMMARY.md`.

## Run and test

Windows (PowerShell), from the repository root:

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
docker compose up -d db                                          # PostgreSQL 16, loopback only
powershell -ExecutionPolicy Bypass -File scripts\reset_db.ps1    # database orch, data folders, capacity
.venv\Scripts\pytest -q                                          # needs the db service
.venv\Scripts\pyright.exe app/common scripts/capacity.py tests/conftest.py tests/helpers.py tests/stubs.py tests/common
Get-Content -Raw db\invariants.sql | docker compose exec -T db psql -U postgres -d orch
```

Linux or WSL2 (needs `psql`, for example from postgresql-client):

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
docker compose up -d db && ./scripts/reset_db.sh
pytest -q                     # everything except the sandbox tests; needs the db service
pytest -q -m docker           # sandbox tests, needs cgroup v2 (stat -fc %T /sys/fs/cgroup prints cgroup2fs)
```

Last known results (2026-11-19, Windows 11, Python 3.13.5, PostgreSQL 16.16):
- `pytest -q`: `128 passed` in about 35 s (16 config, 25 A-2 schema, 12 seed data, 32 capacity, 43 common).
- pyright 1.1.414: 0 errors on the A-4 files above. `db/invariants.sql` on orch: 0 rows for both queries.
- `pytest -q -m docker`: exit code 5 until docker tests exist. With the database down, the database tests fail (none skip) with: Start it with "docker compose up -d db".

## Decisions

- Accepted: D-01, D-31, D-32, D-33, D-36 to D-60 (D-53 is a proposal). Rejected: D-34. Pending: D-35.
- D-01 to D-17: plan section 17. D-18 to D-30: LAN guide proposals.

## Gotchas and open items

- E4-1: extend make_job to every state through transition(); E3-2: allocated and running jobs get a real allocation.
- E2-1: make_job inserts through submit().
- For E4-1 and every engine: a database error the body of tx() must survive goes in a savepoint (`with cur.cur.connection.transaction():`); a caught error without one makes tx() raise at exit (D-48). E4-1's illegal pairs and JT002 checks need this.
- For E4-1: `transition()` must clear `app.actor` and `app.reason` after its UPDATE (`set_config(name, '', true)`) and never set them for the whole session; otherwise a later direct write of jobs.state in the same transaction passes the JT002 guard with stale values.
- For E3-2 and E1: tx() only logs a failing after_commit hook (D-47). Dispatch must handle its own failure (end the attempt, release, requeue or fail the job), or the job stays allocated until a restart; every network call in a hook needs its own timeout.
- For E3-1: plan 6.1 says capacity.py reloads E3's resource id cache, but capacity.py is a separate process. Run it only while the server is stopped, or E3 must re-read the ids.
- For E2-3: FakeClock starts at 0.0; a test that compares `clock.time()` with database times (the 60 s guard reads submitted_at) starts it at the database's now. Tests sync with loops through `fake.wait_for_waiters(n)`.
- For E2-1: the API's RAM bound 117,964 MiB is S0's (context §3). Under `--profile host` the real allocatable RAM differs.
- For E1-1: a reset restarts job ids at 1042 but keeps `DATA_DIR/runs` and `payloads`, so a `runs/1042/` from an older database may exist. E1 must not trust an existing run folder.
- For E4-2: bcrypt 5.0.0 raises ValueError for passwords longer than 72 bytes instead of truncating them.
- Open from the A-4 incident pass: no lock_timeout, statement_timeout or idle_in_transaction_session_timeout is set, so a stuck lock holds its pool connection and other callers wait up to 30 s for one; a proposal for the X slices. Committed but undispatched jobs after a crash are left to the startup reconciliation (E4-7).
- Tests: there is no `__init__.py` under tests/, so every test file name must be unique across tests/. Import `tests.helpers` and `tests.stubs` by that name only (another path loads a second copy). The fixture `db` shadows a module named db: import from `app.common.db` by name.
- Every test session drops and rebuilds orch_test and orch_template and drops leftover `t_<12 hex>` databases, so never run two test sessions against the same server at once. Database tests need the db service running; they never skip.
- pyright 1.1.414 is installed in `.venv` by hand and is not in requirements-dev.txt (D-59).
- The reset scripts rebuild DB_NAME, but capacity.py writes to the database in DATABASE_URL, so both scripts stop first unless the two name the same database (D-46). To reset another database, set both.
- `--profile host` reads `/proc/meminfo`, so it works only on Linux; on Windows it stops with a clear error and `reset_db.ps1` fails.
- The pins were generated on Python 3.13.5 (Windows) and must be re-checked on the VM's Python (D-36). When re-pinning, update `psycopg-pool` too: the pool extra does not pin it.
- A terminal or VS Code window opened before Docker was installed has no `docker` on its PATH: open a new one, or prepend `C:\Program Files\Docker\Docker\resources\bin`. The cgroup2fs check from A-1 has not run yet; the machine that runs the sandbox tests must print cgroup2fs.
- `localhost` resolves to `::1` first on Windows, so the compose file publishes both loopback addresses (D-37). Where IPv6 is disabled the container may not start: drop the `[::1]` entry there and use 127.0.0.1 in the database URLs.
- If recreating the db container fails with "ports are not available", run `docker compose up -d db` again. To run `reset_db.ps1` from a copy of the repository in another folder, set `COMPOSE_PROJECT_NAME=crosystem`.
- Claude Code on this machine: a double backslash typed into a Bash command arrives as one, and a heredoc of about 7.5 KB failed once. Windows PowerShell 5.1 strips double quotes inside arguments to native programs, so put code in a script file instead of `python -c "..."`.
- `app/`, `app/common/` and `scripts/` have no `__init__.py` (the 3.4 tree lists none). Tests and `python -m scripts.capacity` import them as namespace packages from the repository root (`pythonpath = ["."]` in `pyproject.toml`); keep that line.

## Next

- Slice E4-1: `app/e4_monitoring/state.py`, transition() (all 64 state pairs tested). Prompt: plan 15.1 template; sections cited: plan 4.2 and 7; context §9. Needs only A-4.
- In parallel: E2-2 (`queue.py`) needs only A-1 and no database.
