# Handoff summary

Current state of the build. Rewrite a section when it changes; this is not a log. Read it first, then only the files it points to.

Last updated: 2026-11-19, end of slice E3-2.

## Status

| Slice | Status | Branch or commit |
| --- | --- | --- |
| A-1 | done; the cgroup2fs check is still open (see Gotchas) | merged to main (a0d9d2d) |
| A-2 | done: roles, schema (with the D-40 fix), seed, invariants, `tests/schema/` (25 tests), `scripts/reset_db.ps1` | merged to main (524d85f) |
| A-3 | done: `seed_data/problems/12/`, `scripts/capacity.py`, `scripts/reset_db.sh`, data folders, `.gitattributes`, the DB_NAME guard (D-46), 44 tests | merged to main (15cd962) |
| A-4 | done: `app/common/*`, `tests/conftest.py`, `helpers.py`, `stubs.py`, `tests/common/`; reviewed | merged to main (f0130cc) |
| E4-1 | done: `app/e4_monitoring/state.py`, `Tx.savepoint()`, `tests/e4/test_transition.py` (86), make_job for every state; reviewed | merged to main (1503b5b) |
| E3-1 | done: `app/e3_allocation/` (`__init__`, `placement`, `queries`, `service`), `tests/e3/` (14); reviewed | merged to main (3b4b3a1) |
| E3-2 | done: `allocate()` in `app/e3_allocation/` (`service`, `queries`), `tests/e3/` (+19), make_job with real allocations (D-73); reviewed | slice/E3-2: "E3-2: allocate()"; not pushed or merged |
| all others | not started: E2-1, E2-2, E1-1, E3-3, E2-3, E2-4, E1-2, E1-3, E4-2 to E4-7, E1-4, C-1 to C-4, X-1 to X-6, X-7 (optional); order in plan 14.1 | none |

## Changes in the last session (2026-11-19, slice E3-2)

- `allocate()` (plan 6.3 with D-68): one tx("E3") in retrying(): read_resources, the one lock statement in id order, the job read after it (D-70), fit() on the locked rows, then the commit or the refusal (D-01); the allocate log line, dispatch and capacity event after COMMIT (D-71, D-72).
- Tests: `tests/e3/test_allocate.py` (14) and `tests/e3/test_e3_allocation.py` (5: last unit, best fit, same job twice, lock order, stress; 100 gated runs each, no sleeps); the `s0` fixture moved to `tests/e3/conftest.py`.
- Harness (D-73): make_job's allocated and running jobs hold a real allocation; `assert_invariants(unallocated=...)` is gone; `tests/e4/test_transition.py` and `tests/common/test_harness.py` follow.
- Verified: 9 planted bugs each caught by its own file; a code review and an incident pass added the same-job race, the audit-failure rollback and the log ordering.
- `docs/decisions.md`: D-70 to D-73 added; `scripts/capacity.py` docstring no longer claims an id cache.

## Code map

- `app/config.py`: settings from environment variables (plan 3.3). Public: `Config` (frozen dataclass, one lower case field per variable), `Config.from_env(env=os.environ)`, `CHOICES`.
- `app/common/db.py` (plan 5.1): `Engine`, `Tx` (`execute`, `fetchone`, `fetchall`, `after_commit`, `savepoint()`), `init_pool(dsn, min_size, max_size)`, `close_pool()`, `in_transaction()`, `assert_outside_tx(what)`, `tx(engine)`, `retrying(fn, attempts=3)`. tx() runs SET LOCAL ROLE eN_role, commits, and runs the hooks only after COMMIT with the connection back in the pool; a failing hook is logged and the next one runs (D-47); a caught statement error or a hand-sent COMMIT or ROLLBACK makes tx() raise and skip the hooks (D-48); nested tx() on one thread is refused (D-60). `Tx.savepoint()` is the only way to take a savepoint inside tx(); when it rolls back it also drops the hooks registered inside it, inner released savepoints included (D-67). retrying() retries only 40P01 and 40001, 3 tries in all (D-55). init_pool() probes first and checks pooled connections (D-49).
- `app/common/clock.py`: `Clock` (`monotonic`, `time`, `wait(event, timeout)`), `RealClock`, `FakeClock(start=0.0)` with `advance(seconds)` and `wait_for_waiters(count)` (D-50).
- `app/common/types.py`: `Vector`, `Snapshot.fits`, `Limits`, `Committed`, `Refused`, `Released`, `AlreadyReleased`, `User` (plan 5.3), `SubmitRequest`, `SubmitResult` (D-52), `QueueEntry` (D-53).
- `app/common/ports.py`: `E1Port` to `E4Port` exactly as plan 5.4, `Engines`, `install(e)`, `engines()` (RuntimeError before install). `events.py`: the `Hub` protocol only (D-54). `errors.py`: `ApiError(status, error, field=None)`, `IllegalTransition`, `StopNotConfirmed`, `NotFound`. `logging.py`: `log.info/warning/error/exception(event, **fields)` and `configure(data_dir)`, JSON lines (D-58).
- `app/e4_monitoring/state.py` (plan 7.1; no `__init__.py`, D-64): `JobState`, `set_actor(t, actor, reason)`, `clear_actor(t)`, `transition(t, job_id, to_state, actor, reason, extra=None, *, expected=None) -> datetime`. RuntimeError unless t is in an open transaction; its statements run on a cursor of its own inside `t.savepoint()`; JT001 or a job no longer in `expected` raise IllegalTransition (logged `illegal_transition`, transaction usable, nothing written); a missing job raises NotFound; every other error is re-raised unchanged. The event `{extra..., type, job_id, user_id, state, reason, at}` goes to `engines().e4.publish` after COMMIT; the six core keys win over extra (D-62).
- `app/e3_allocation/` (plan 6.1 to 6.3): `__init__.py` exports `snapshot` and `allocate` (release joins in E3-3). `placement.py`, pure: `Kind`, `Row` (NamedTuple: resource_id, kind, gpu_index, total, reserved, allocated), `free(r)`, `fit(rows, need) -> ([(resource_id, amount)] in id order, gpu_index) | None` (best fit: least free VRAM that fits, ties to the lower resource_id; no GPU when vram_mib is 0), `snapshot_of(rows) -> Snapshot` (gpu_free by gpu_index); both raise ValueError on a missing cpu or ram row. `queries.py`, one statement each, resources rows always through the one `_ROWS` column list: `read_resources(t)` (no lock), `lock_resources(t, ids)` (the lock statement), `read_job(t, job_id) -> Job | None`, `add_allocated`, `insert_allocation`, `insert_items`, `insert_audit`. `service.py`: `snapshot()` (one tx, no lock, no retry; D-68, D-69) and `allocate(job_id, vector) -> Committed | Refused`: raises NotFound, IllegalTransition (not admitted, a repeat included; D-70) or ValueError (no capacity) with nothing written; hooks after COMMIT in this order: allocate log line, job_state, dispatch, capacity event (D-71, D-72).
- `db/roles.sql`: roles e1_role to e4_role (NOLOGIN) and the login orch_app (NOINHERIT, password `orch`, to change on the demo machine). Run once per cluster as a superuser (plan 4.1).
- `db/schema.sql`: 15 tables, 17 foreign keys, CHECKs, triggers and grants (plan 4.2 with the D-40 fix). Custom error codes: JT001 illegal transition or a new job not in queued; JT002 jobs.state written (INSERT too) without the settings `app.actor` and `app.reason`; AO001 update or delete on an append-only table; FA001 session or problem owner who is not faculty; FA002 role change of a faculty member who owns sessions or problems; TR001 test result for a case outside the attempt's problem.
- `db/seed.sql`: workload types, demo users, lab sessions 7 and 8, problem 12 with 10 cases, the S0 capacity (ids 1 CPU, 2 RAM, 3 and 4 GPUs); job ids start at 1042 (plan 4.3). `db/invariants.sql`: two queries, `-- name: invariants` and `-- name: leaks`; each returned row is one violation (plan 4.4).
- `seed_data/problems/12/`: 10 cases of "Sum of N integers": N on line 1, the N integers on line 2, `.out` is the sum. LF only.
- `scripts/capacity.py`: writes the capacity profile (plan 6.5). Public: `Row`, `Free`, `reserve(total)`, `s0_rows()`, `host_rows(...)`, `apply(conn, rows)`, `write(database_url, rows)` (D-57), `main(argv=None, env=None)`, `CapacityError`. Locks every resources row in id order, refuses while an allocation is open, updates rows in place. `--check-database NAME` writes nothing (D-46). Exit 0; 1 on a refusal or a database error; 2 on a bad argument or setting.
- `scripts/reset_db.sh` (Linux or WSL2) and `scripts/reset_db.ps1` (Windows, D-38): stop first unless DATABASE_URL names DB_NAME (D-46), then roles, schema, seed, the DATA_DIR folders, the seed problems and capacity.py.
- `tests/conftest.py`: each session runs roles.sql once, drops leftover `t_<12 hex>` databases, builds `orch_test` (schema and seed; fixtures `owner_tx`, `app_tx`, class `OpenTx` with `refused(sqlstate)`) and `orch_template` (schema only). Fixture `db` copies the template into a fresh database per test, opens the pool as orch_app (1 to 4 connections), installs the stubs and yields `TempDb(name, owner_url, app_url, stubs)`. Fixture `json_log` configures logging into tmp_path.
- `tests/helpers.py`: `TempDb`, `invariant_violations(settled=False)` (the rows), `assert_invariants(settled=False)`, `invariant_queries()`, `make_user(role)`, `make_problem(cases=10)`, `make_job(state="queued", vector=...)` (every state on `PATH`, one tx() per step in the engine of plan 7.2; allocated and running jobs through e3 allocate() on S0, loaded when resources is empty, D-73); the test oracle `STATES`, `ACTOR` (the 13 transitions with their engine), `ALLOWED`. `tests/stubs.py`: `StubE1` to `StubE4`, `RecordingHub`, `install_stubs() -> Stubs`; each stub refuses calls inside a transaction (D-56).
- Tests: `tests/test_config.py` (16), `tests/schema/` (69), `tests/common/` (46: test_tx 25, test_clock 4, test_ports 8, test_harness 8, test_logging 1), `tests/e4/` (86: test_transition), `tests/e3/` (33: test_placement 10, test_snapshot 4, test_allocate 14, test_e3_allocation 5; the fixture `s0` in `tests/e3/conftest.py` loads db/seed.sql into a `db` copy).
- `.env.example` (all 24 variables), `pyproject.toml` (pytest and pyright settings), `requirements*.txt` (`==` pins, D-36), `docker-compose.yml` (PostgreSQL 16, loopback only, D-37; fsync, synchronous_commit and full_page_writes off, D-66), `.gitattributes` (D-43).
- `docs/`: `PROJECT_CONTEXT.md` (baseline, wins every conflict), `IMPLEMENTATION_PLAN.md` (build plan; 3.4 has the full file tree), `LAN_BUILD_GUIDE.md` (LAN slices only), `decisions.md`, `SUMMARY.md`.

## Run and test

Windows (PowerShell), from the repository root (CLAUDE.md "Machine limits": one pytest process at a time; the full suite once, before a commit):

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
docker compose up -d db                                          # PostgreSQL 16, loopback only
powershell -ExecutionPolicy Bypass -File scripts\reset_db.ps1    # database orch, data folders, capacity
.venv\Scripts\pytest -q tests/schema tests/e3                   # E3 slices (plan 6.6)
.venv\Scripts\pytest -q                                          # full suite; needs the db service
.venv\Scripts\pyright.exe app/common app/e3_allocation app/e4_monitoring scripts/capacity.py tests/conftest.py tests/helpers.py tests/stubs.py tests/common tests/e3 tests/e4
Get-Content -Raw db\invariants.sql | docker compose exec -T db psql -U postgres -d orch
```

Linux or WSL2: `python3 -m venv .venv && . .venv/bin/activate`, `pip install -r requirements-dev.txt`, `docker compose up -d db && ./scripts/reset_db.sh`, `pytest -q`, and `pytest -q -m docker` for the sandbox tests (needs cgroup v2: `stat -fc %T /sys/fs/cgroup` prints cgroup2fs).

Last known results (2026-11-19, Windows 11, Python 3.13.5, PostgreSQL 16.16):
- `pytest -q`: `250 passed` in about 150 s (16 config, 69 schema, 46 common, 86 e4, 33 e3; `tests/e3/test_e3_allocation.py` alone takes about 80 s).
- pyright 1.1.414: 0 errors on every path of the command above. `db/invariants.sql` on orch: 0 rows for both queries.
- `pytest -q -m docker`: exit code 5 until docker tests exist. With the database down, the database tests fail (none skip) with: Start it with "docker compose up -d db".

## Decisions

- Accepted: D-01, D-31, D-32, D-33, D-36 to D-73 (D-53 is a proposal). Rejected: D-34. Pending: D-35.
- D-01 to D-17: plan section 17. D-18 to D-30: LAN guide proposals.

## Gotchas and open items

- For E2-1: make_job inserts its job through submit() once it exists.
- For E2-3: allocate() raises IllegalTransition for a job not in admitted (a repeat; nothing written), NotFound, ValueError (no capacity) or a psycopg error after 3 tries. After any raise the job stays admitted: call allocate() for it again on a later cycle (safe, D-70) instead of admitting it anew. IllegalTransition naming allocated right after a lost connection means the first call committed.
- For E1-2, E4-6 and X-1: pass `expected=` to transition() whenever the decision was based on a state read earlier (D-65). Without it, a cancel decided while the job was allocated applies as running to cancelled once E1 has started it.
- For every engine: inside tx(), take savepoints only with `with cur.savepoint():` (CLAUDE.md, D-67), never connection.transaction(); a caught database error without a savepoint makes tx() raise at exit (D-48).
- For E3-3 and E1-2: transition() locks the job row; call it after the allocation and resource locks of invariant 2, never before, or concurrent callers deadlock (40P01, retried 3 times by retrying()). allocate() does so; its lock-order probe in `tests/e3/test_e3_allocation.py` shows how to test it.
- For E4-7 and any bulk move: one transaction per job. Each transition() takes a savepoint, and more than 64 in one transaction overflow PostgreSQL's subtransaction cache, which slows every snapshot.
- For E4-4: a job_state event can be lost (a failed push or a crash after COMMIT, D-47) or arrive out of order (hooks of different transactions run on different threads). Pages refetch on (re)connect and ignore an event whose `at` is older than the last one seen for that job. Values passed in `extra` must be JSON-serialisable.
- For E2-1: submit() sets the settings with set_actor() for its INSERT; if its transaction goes on after the INSERT, it calls clear_actor().
- Tests that count hub events or E1 calls take a baseline after make_job: each make_job step publishes one job_state event, and the allocate step of an allocated or running job also records one dispatch and one capacity event. make_job allocates on S0 for real: 28 jobs of the default vector use up the CPU, so loops pass a small vector (Vector(100, 64)).
- For E1-2: dispatch runs right after allocate()'s COMMIT on E2's admission thread: return at once (the supervisor starts the container) and end a failed start yourself (stop, confirm, release, requeue). A raise only logs after_commit_failed and leaves the job allocated (D-47, D-72); every network call in a hook needs its own timeout.
- For E4-7: an allocated job with no open attempt is undispatched (a crash after COMMIT, or a dispatch that raised): dispatch it again or recover it (D-72). For E4-4: keep the newest capacity event by `at` across all capacity events, not per job (D-71).
- For E3-3: release() builds its capacity event as allocate() does (D-71). make_job("completed") holds no allocation, so release tests start from make_job("running") and move the job to completed.
- Concurrency tests: in pg_blocking_pids a queued locker names the locker ahead of it, not the holder. Wait until the number of waiting sessions in the test database reaches the number of racers (`wait_until_waiting` in `tests/e3/test_e3_allocation.py`).
- For C-1: plan 12.1's main.py sketch builds E3Service() and calls e3.load_ids(), but plan 6.1 and E3-1 give module functions in app/e3_allocation and no cache (D-68): install the module, or a thin class around it, as the E3Port, and drop load_ids().
- For E2-3: snapshot() raises ValueError when no capacity is loaded (D-69), and psycopg or pool errors when the database is down. The admission loop must catch them, log a lasting failure once per change rather than every 0.5 s, and keep looping.
- For E2-3: FakeClock starts at 0.0; a test that compares `clock.time()` with database times (the 60 s guard reads submitted_at) starts it at the database's now. Tests sync with loops through `fake.wait_for_waiters(n)`.
- For E2-1: the API's RAM bound 117,964 MiB is S0's (context §3). Under `--profile host` the real allocatable RAM differs.
- For E1-1: a reset restarts job ids at 1042 but keeps `DATA_DIR/runs` and `payloads`, so a `runs/1042/` from an older database may exist. E1 must not trust an existing run folder.
- For E4-2: bcrypt 5.0.0 raises ValueError for passwords longer than 72 bytes instead of truncating them.
- Open (A-4 and E4-1 incident passes): no lock_timeout, statement_timeout or idle_in_transaction_session_timeout is set, so a stuck lock holds its pool connection, other callers wait up to 30 s for one, and transition() waits on a locked job row the same way; a lock_timeout would raise 55P03, which retrying() does not retry. A proposal for the X slices. Committed but undispatched jobs after a crash are left to the startup reconciliation (E4-7).
- The development database runs with fsync, synchronous_commit and full_page_writes off (D-66): after a crash or power loss rebuild it with `docker compose up -d db` and `reset_db.ps1`. Never use these settings on the demo machine.
- Tests: there is no `__init__.py` under tests/, so every test file name must be unique across tests/. Import `tests.helpers` and `tests.stubs` by that name only (another path loads a second copy). The fixture `db` shadows a module named db: import from `app.common.db` by name.
- Every test session drops and rebuilds orch_test and orch_template and drops leftover `t_<12 hex>` databases, so never run two test sessions against the same server at once. Database tests need the db service running; they never skip.
- pyright 1.1.414 is installed in `.venv` by hand and is not in requirements-dev.txt (D-59).
- The reset scripts rebuild DB_NAME, but capacity.py writes to the database in DATABASE_URL, so both scripts stop first unless the two name the same database (D-46). To reset another database, set both.
- `--profile host` reads `/proc/meminfo`, so it works only on Linux; on Windows it stops with a clear error and `reset_db.ps1` fails.
- The pins were generated on Python 3.13.5 (Windows) and must be re-checked on the VM's Python (D-36). When re-pinning, update `psycopg-pool` too: the pool extra does not pin it.
- A terminal or VS Code window opened before Docker was installed has no `docker` on its PATH: open a new one, or prepend `C:\Program Files\Docker\Docker\resources\bin`. Docker Desktop may need starting after a reboot. The cgroup2fs check from A-1 has not run yet; the machine that runs the sandbox tests must print cgroup2fs.
- `localhost` resolves to `::1` first on Windows, so the compose file publishes both loopback addresses (D-37). Where IPv6 is disabled the container may not start: drop the `[::1]` entry there and use 127.0.0.1 in the database URLs.
- If recreating the db container fails with "ports are not available", run `docker compose up -d db` again. To run `reset_db.ps1` from a copy of the repository in another folder, set `COMPOSE_PROJECT_NAME=crosystem`.
- Claude Code on this machine: a double backslash typed into a Bash command arrives as one, and a heredoc of about 7.5 KB failed once. Windows PowerShell 5.1 strips double quotes inside arguments to native programs, so put code in a script file instead of `python -c "..."`, and commit with `git commit -F <message file>`. Python's `write_text()` on Windows writes CRLF; use bytes when a file must keep LF.
- `app/`, `app/common/`, `app/e4_monitoring/` and `scripts/` have no `__init__.py` (the 3.4 tree lists none for E4-1). Tests and `python -m scripts.capacity` import them as namespace packages from the repository root (`pythonpath = ["."]` in `pyproject.toml`); keep that line.

## Next

- Slice E3-3 (`release()`, plan 14.1 order; needs E3-2). E2-1, E4-4 and E4-5 also need only what is built now (A-4, E4-1, E3-1 and E3-2).
- In parallel: E2-2 (`queue.py`) needs only A-1 and no database.
