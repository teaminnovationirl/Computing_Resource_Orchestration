# Handoff summary

Current state of the build. Rewrite a section when it changes; this is not a log. Read it first, then only the files it points to.

Last updated: 2026-11-19, end of slice A-3.

## Status

| Slice | Status | Branch or commit |
| --- | --- | --- |
| A-1 | done; the cgroup2fs check is still open (see Gotchas) | merged to main (a0d9d2d) |
| A-2 | done: roles, schema (with the D-40 fix), seed, invariants, `tests/schema/` (25 tests), `scripts/reset_db.ps1` | merged to main (524d85f) |
| A-3 | done: `seed_data/problems/12/`, `scripts/capacity.py`, `scripts/reset_db.sh`, data folders and capacity in `scripts/reset_db.ps1`, `.gitattributes`, the DB_NAME guard (D-46), 44 tests; reviewed | slice/A-3, commits "A-3: seed data, reset_db.sh, capacity.py" and "A-3: guard DB_NAME and DATABASE_URL", not pushed or merged |
| all others | not started: A-4, E4-1, E3-1, E3-2, E2-1, E2-2, E1-1, E3-3, E2-3, E2-4, E1-2, E1-3, E4-2 to E4-7, E1-4, C-1 to C-4, X-1 to X-6, X-7 (optional); order in plan 14.1 | none |

## Changes in the last session (2026-11-19, slice A-3)

- New `seed_data/problems/12/`, `scripts/capacity.py` (D-41, D-44), `scripts/reset_db.sh` (plan 4.5, mode 100755), `.gitattributes` (D-43); `scripts/reset_db.ps1` now makes the DATA_DIR folders, copies the problems and runs capacity.py.
- New `tests/schema/test_seed_data.py` (12) and `tests/schema/test_capacity.py` (32) (D-42), each written and failing first; four injected bugs (check before lock, no SET LOCAL ROLE, autocommit, stale rows after a lock wait) were each caught.
- Verified from a clean checkout: Windows (new venv, `reset_db.ps1`, full suite) and Linux (`reset_db.sh` and the full suite in a python:3.13-slim container on the compose network); invariants 0 rows; free 28,800, 117,964, 14,745, 14,745.
- Separate code review, no critical issue. Fixed: one-line CLI errors (bad setting exit 2, database error exit 1), nvidia-smi's own message kept, refusal wording, a 100-run test of two concurrent runs, D-45. Then, after your OK, both reset scripts stop first when DB_NAME and DATABASE_URL name different databases (D-46; plan 4.5 and 6.5 updated).
- `docs/decisions.md`: D-41 to D-46 added (D-45 accepted). A-2 had already been merged to main (524d85f).

## Code map

- `app/config.py`: settings from environment variables (plan 3.3). Public: `Config` (frozen dataclass, one lower case field per variable), `Config.from_env(env=os.environ)`, `CHOICES`.
- `db/roles.sql`: roles e1_role to e4_role (NOLOGIN) and the login orch_app (NOINHERIT, password `orch`, to change on the demo machine). Run once per cluster as a superuser (plan 4.1).
- `db/schema.sql`: 15 tables, 17 foreign keys, CHECKs, triggers and grants (plan 4.2 with the D-40 fix). Custom error codes: JT001 illegal transition or a new job not in queued; JT002 jobs.state written without the settings `app.actor` and `app.reason` that transition() sets; AO001 update or delete on an append-only table; FA001 session or problem owner who is not faculty; FA002 role change of a faculty member who owns sessions or problems; TR001 test result for a case outside the attempt's problem.
- `db/seed.sql`: workload types, demo users, lab sessions 7 and 8, problem 12 with 10 cases, the S0 capacity (ids 1 CPU, 2 RAM, 3 and 4 GPUs); job ids start at 1042 (plan 4.3).
- `db/invariants.sql`: two queries, `-- name: invariants` and `-- name: leaks`; each returned row is one violation (plan 4.4).
- `seed_data/problems/12/`: 10 cases of "Sum of N integers": N on line 1, the N integers on line 2, `.out` is the sum. 01 and 02 have N = 1; 07, 08 and 10 need 64 bits; every value fits in int32. LF only.
- `scripts/capacity.py`: writes the capacity profile (plan 6.5). Public: `Row`, `Free`, `reserve(total)`, `s0_rows()`, `host_rows(gpu_mode, cpu_count=None, meminfo_path=Path("/proc/meminfo"), nvidia_smi=None)`, `apply(conn, rows)` (inside the caller's transaction, already e3_role), `write(database_url, rows)` (its own transaction, D-41), `main(argv=None, env=None)`, `CapacityError`. Locks every resources row in id order, refuses while an allocation is open or when a GPU to remove is named by allocation_items, updates rows in place. `--check-database NAME` writes nothing and exits 2 unless DATABASE_URL names NAME (D-46). Exit 0; 1 on a refusal or a database error; 2 on a bad argument or setting; each error is one line on stderr.
- `scripts/reset_db.sh`: Linux or WSL2 reset, the text of plan 4.5: stops first unless DATABASE_URL names DB_NAME (D-46); needs `psql` and `python` on PATH; run from the repository root.
- `scripts/reset_db.ps1`: Windows reset (D-38): stops first unless DATABASE_URL names DB_NAME (D-46), then roles, schema and seed through psql inside the compose service db (waits up to 60 s for it), then the DATA_DIR folders, the seed problems and `capacity.py` with `.venv\Scripts\python.exe` (else `python`).
- `tests/test_config.py` (16 tests). `tests/schema/`: the 25 A-2 tests in the four files of plan 4.6, `test_seed_data.py` (12) and `test_capacity.py` (32, one of them runs this platform's reset script). `conftest.py` rebuilds orch_test each session; `owner_tx` (superuser) and `app_tx` (orch_app) give each test one rolled-back transaction; `Tx.refused(sqlstate, constraint)` asserts the exact error code. The three race tests (100 runs each) commit on connections of their own and remove what they created.
- `.env.example` (all 24 variables), `pyproject.toml` (pytest settings only), `requirements*.txt` (`==` pins, D-36), `docker-compose.yml` (PostgreSQL 16, loopback only, D-37), `.gitattributes` (LF for `*.sh` and `seed_data/**`, D-43).
- `docs/`: `PROJECT_CONTEXT.md` (baseline, wins every conflict), `IMPLEMENTATION_PLAN.md` (build plan; 3.4 has the full file tree), `LAN_BUILD_GUIDE.md` (LAN slices only), `decisions.md`, `SUMMARY.md`.

## Run and test

Windows (PowerShell), from the repository root:

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
docker compose up -d db                                          # PostgreSQL 16, loopback only
powershell -ExecutionPolicy Bypass -File scripts\reset_db.ps1    # database orch, data folders, capacity
.venv\Scripts\pytest -q                                          # needs the db service
Get-Content -Raw db\invariants.sql | docker compose exec -T db psql -U postgres -d orch
```

Linux or WSL2 (needs `psql`, for example from postgresql-client):

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
docker compose up -d db       # PostgreSQL 16 for development
./scripts/reset_db.sh         # database orch, data folders, capacity
pytest -q                     # everything except the sandbox tests; needs the db service
pytest -q -m docker           # sandbox tests, needs cgroup v2
stat -fc %T /sys/fs/cgroup    # must print cgroup2fs
```

Last known results (2026-11-19, Windows 11, Python 3.13.5, PostgreSQL 16.16):
- `pytest -q`: `85 passed` (16 config, 25 A-2 schema, 12 seed data, 32 capacity), on Windows and in a python:3.13-slim container on the compose network.
- `scripts/reset_db.ps1`: works twice in a row and from a clean checkout; with DB_NAME=orch2 it stops before anything (exit 1); `db/invariants.sql` on orch then gives 0 rows for both queries.
- `scripts/reset_db.sh`: works from a clean checkout in a python:3.13-slim container on the compose network (`requirements.txt` installs there too).
- `pytest -q -m docker`: exit code 5 until docker tests exist. With the database down, the database tests fail (none skip) with: Start it with "docker compose up -d db".

## Decisions

- Accepted: D-01, D-31, D-32, D-33, D-36 to D-46. Rejected: D-34. Pending: D-35.
- D-01 to D-17: plan section 17. D-18 to D-30: LAN guide proposals.

## Gotchas and open items

- For A-4: Replace the own connection in scripts/capacity.py with tx("E3") (D-41).
- For E3-1: plan 6.1 says capacity.py reloads E3's resource id cache, but capacity.py is a separate process. Run it only while the server is stopped, or E3 must re-read the ids.
- For E1-1: a reset restarts job ids at 1042 but keeps `DATA_DIR/runs` and `payloads`, so a `runs/1042/` from an older database may exist. E1 must not trust an existing run folder.
- For E2-1: the API's RAM bound 117,964 MiB is S0's (context §3). Under `--profile host` the real allocatable RAM differs, so a job could ask for more than the machine can ever give.
- For E4-1: `transition()` must clear `app.actor` and `app.reason` after its UPDATE (`set_config(name, '', true)`) and must never set them for the whole session; otherwise a later direct write of jobs.state in the same transaction, or on the same pooled connection, passes the JT002 guard with stale values.
- For E4-2: bcrypt 5.0.0 raises ValueError for passwords longer than 72 bytes instead of truncating them.
- The reset scripts rebuild DB_NAME, but capacity.py writes to the database in DATABASE_URL, so both scripts stop first (exit 2 from `capacity.py --check-database`) unless the two name the same database (D-46). To reset another database, set both.
- `--profile host` reads `/proc/meminfo`, so it works only on Linux; on Windows it stops with a clear error and `reset_db.ps1` fails.
- The pins were generated on Python 3.13.5 (Windows) and must be re-checked on the VM's Python (D-36). When re-pinning, update `psycopg-pool` too: the pool extra does not pin it.
- A terminal or VS Code window opened before Docker was installed has no `docker` on its PATH: open a new one, or prepend `C:\Program Files\Docker\Docker\resources\bin`. The cgroup2fs check from A-1 has not run yet; the machine that runs the sandbox tests must print cgroup2fs.
- `localhost` resolves to `::1` first on Windows, so the compose file publishes both loopback addresses (D-37). Where IPv6 is disabled (some Linux VMs) the container may not start: drop the `[::1]` entry there and use 127.0.0.1 in the database URLs.
- If recreating the db container fails with "ports are not available", run `docker compose up -d db` again.
- Database tests need the db service running; they never skip. Every test session drops and rebuilds orch_test, so never run two test sessions against the same server at once.
- To run `reset_db.ps1` from a copy of the repository in another folder, set `COMPOSE_PROJECT_NAME=crosystem`, or `docker compose exec` looks for a project named after that folder.
- `db/invariants.sql` runs automatically only at the end of the two capacity race tests; A-4's `tests/helpers.py` runs it after every system test and may move the fixtures of `tests/schema/conftest.py` into `tests/conftest.py`.
- Claude Code on this machine: a double backslash typed into a Bash command arrives as one, and a heredoc of about 7.5 KB failed once. Windows PowerShell 5.1 strips double quotes inside arguments to native programs, so put code in a script file instead of `python -c "..."`.
- `app/` and `scripts/` have no `__init__.py` (the 3.4 tree lists none). Tests and `python -m scripts.capacity` import them as namespace packages from the repository root (`pythonpath = ["."]` in `pyproject.toml`); keep that line.

## Next

- Slice A-4: `app/common/*`, `tests/conftest.py`, `helpers.py`, `stubs.py` (plan 5); done when the six checks at the end of 5.6 pass. A-4 also replaces the own connection in `scripts/capacity.py` with tx("E3") (D-41).
- Prompt: plan 15.1 template. Sections cited for A-4: plan 5; context §8.
- In parallel: E2-2 (`queue.py`) needs only A-1 and no database.
