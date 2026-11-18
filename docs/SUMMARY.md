# Handoff summary

Current state of the build. Rewrite a section when it changes; this is not a log. Read it first, then only the files it points to.

Last updated: 2026-11-19, end of slice A-2.

## Status

| Slice | Status | Branch or commit |
| --- | --- | --- |
| A-1 | done; the cgroup2fs check is still open (see Gotchas) | merged to main (a0d9d2d) |
| A-2 | done: roles, schema (with the D-40 fix), seed, invariants, `tests/schema/`, `scripts/reset_db.ps1`; reviewed | slice/A-2, commit "A-2: roles, schema, seed, invariants", not pushed or merged |
| A-3 | not started; scope is now `seed_data/problems/12/`, `scripts/reset_db.sh`, `scripts/capacity.py` (D-39) | none |
| A-4 | not started | none |
| E4-1 | not started | none |
| E3-1 | not started | none |
| E3-2 | not started | none |
| E2-1 | not started | none |
| E2-2 | not started | none |
| E1-1 | not started | none |
| E3-3 | not started | none |
| E2-3 | not started | none |
| E2-4 | not started | none |
| E1-2 | not started | none |
| E1-3 | not started | none |
| E4-2 | not started | none |
| E4-3 | not started | none |
| E4-4 | not started | none |
| E4-5 | not started | none |
| E1-4 | not started | none |
| E4-6 | not started | none |
| E4-7 | not started | none |
| C-1 | not started | none |
| C-2 | not started | none |
| C-3 | not started | none |
| C-4 | not started | none |
| X-1 | not started | none |
| X-2 | not started | none |
| X-3 | not started | none |
| X-4 | not started | none |
| X-5 | not started | none |
| X-6 | not started | none |
| X-7 (optional) | not started | none |

## Changes in the last session (2026-11-18 to 2026-11-19, slice A-2)

Replaced at the end of every session; the sections below hold the current state.

- `docker-compose.yml`: the database port is published on loopback only, `127.0.0.1:5432` and `[::1]:5432` (D-37). The container was recreated; the data volume was kept.
- `docs/decisions.md`: D-34 rejected (verdicts stay as the baseline defines them, no schema change); D-01 accepted; D-37, D-38, D-39 and D-40 added.
- New `db/roles.sql`, `db/seed.sql` and `db/invariants.sql` (as plan 4.1, 4.3 and 4.4) and `db/schema.sql` (plan 4.2 plus the D-40 security fix).
- New `tests/schema/` (25 tests) and `scripts/reset_db.ps1` (D-38), which waits up to 60 s for the server before it runs psql.
- `docs/IMPLEMENTATION_PLAN.md`: the 4.2 block, the section 4 intro and table 4.6 updated for D-40; the 3.4 tree and the 14.1 rows A-2 and A-3 updated to the new scope (D-39); the per-owner review instruction removed from the A-2 row.
- This file: rewritten for A-2; owner names and the per-owner review instruction removed. `CLAUDE.md` had no owner names, so it is unchanged.
- PostgreSQL state: the cluster roles e1_role to e4_role and orch_app exist; the database orch is rebuilt by `scripts/reset_db.ps1`; orch_test is rebuilt by every test session.
- Code review by a separate reviewer: mandatory checks 1, 2, 3 and 5 passed. Check 4 failed on a hole in the plan's SQL: orch_app could attach the SECURITY DEFINER `job_state_track()` to its own temp table and forge history rows, and a temp table named `job_state_history` captured real history rows (and could run SQL as superuser). Fixed by D-40 after your OK.
- Also fixed from the review: the privilege matrix test now sees column grants and checks TRIGGER, EXECUTE and sequence privileges; temp tables can no longer fool the faculty and M1 checks (tested); the clock test now tells `clock_timestamp()` from `statement_timestamp()`; the reserve test also refuses a reserve that is too large; `reset_db.ps1` waits for the server.
- Not changed from the review: the JT002 guard with stale settings belongs to `transition()` in E4-1, and `[::1]` publishing on hosts without IPv6 needs a check on the VM (both in Gotchas). The reviewer's out-of-scope list was accepted as out of scope.
- Evidence: against empty SQL files the 25 tests gave 23 failed and 2 errors, then 25 passed; against the plan's SQL the review's regression checks gave 5 failed, then 25 passed after D-40; nine deliberate schema breaks were each caught; `db/invariants.sql` returns 0 rows on orch.

## Code map

- `app/config.py`: settings from environment variables (plan 3.3). Public: `Config` (frozen dataclass, one lower case field per variable), `Config.from_env(env=os.environ)`, `CHOICES`.
- `tests/test_config.py`: 16 tests: defaults match the 3.3 table, `.env.example` in sync, overrides, frozen, bad values rejected.
- `db/roles.sql`: roles e1_role to e4_role (NOLOGIN) and the login orch_app (NOINHERIT, password `orch`, to change on the demo machine). Run once per cluster as a superuser (plan 4.1).
- `db/schema.sql`: 15 tables, 17 foreign keys, CHECKs, triggers and grants (plan 4.2 with the D-40 fix: the trigger functions pin search_path with pg_temp last, only the trigger runs `job_state_track()`, no sequence grants). Custom error codes: JT001 illegal transition or a new job not in queued; JT002 jobs.state written without the settings `app.actor` and `app.reason` that transition() sets; AO001 update or delete on an append-only table; FA001 session or problem owner who is not faculty; FA002 role change of a faculty member who owns sessions or problems; TR001 test result for a case outside the attempt's problem.
- `db/seed.sql`: workload types, demo users, lab sessions 7 and 8, problem 12 with 10 cases, the S0 capacity; job ids start at 1042 (plan 4.3).
- `db/invariants.sql`: two queries, `-- name: invariants` and `-- name: leaks`; each returned row is one violation (plan 4.4).
- `scripts/reset_db.ps1`: drops and rebuilds the database orch from roles, schema and seed inside the compose service db (D-38); it first waits up to 60 s for the server to accept TCP connections.
- `tests/schema/`: 25 tests in the four files of plan 4.6. `conftest.py` rebuilds orch_test each session; `owner_tx` (superuser) and `app_tx` (orch_app) give each test one rolled-back transaction; `Tx.refused(sqlstate, constraint)` asserts the exact error code. The race test commits on two connections of its own and deletes its row afterwards.
- `.env.example`: all 24 variables with their defaults.
- `pyproject.toml`: pytest settings only: `testpaths`, `pythonpath = ["."]`, `--strict-markers`, `-m "not docker"` by default.
- `requirements.txt`, `requirements-dev.txt`: `==` pins (D-36).
- `docker-compose.yml`: PostgreSQL 16 for development (plan 3.2), published on loopback only (D-37).
- `CLAUDE.md`: rules for every session.
- `docs/`: `PROJECT_CONTEXT.md` (baseline, wins every conflict), `IMPLEMENTATION_PLAN.md` (build plan; 3.4 has the full file tree), `LAN_BUILD_GUIDE.md` (LAN slices only), `decisions.md`, `SUMMARY.md`.

## Run and test

Windows (PowerShell), from the repository root:

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
docker compose up -d db                                          # PostgreSQL 16, loopback only
powershell -ExecutionPolicy Bypass -File scripts\reset_db.ps1    # rebuild the database orch
.venv\Scripts\pytest -q                                          # needs the db service
Get-Content -Raw db\invariants.sql | docker compose exec -T db psql -U postgres -d orch
```

Linux or WSL2 (`scripts/reset_db.sh` arrives with A-3):

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
docker compose up -d db       # PostgreSQL 16 for development
pytest -q                     # everything except the sandbox tests; needs the db service
pytest -q -m docker           # sandbox tests, needs cgroup v2
stat -fc %T /sys/fs/cgroup    # must print cgroup2fs
```

Last known results (2026-11-19, Windows 11, Python 3.13.5, PostgreSQL 16.16):
- `pytest -q`: `41 passed in 3.99s` (16 config, 25 schema)
- `pytest -q -m docker`: `41 deselected in 0.23s` (exit code 5 until docker tests exist)
- `db/invariants.sql` on orch after `scripts/reset_db.ps1`: 0 rows for both queries.
- With the database down, the 25 schema tests fail (none skip) with: Start it with "docker compose up -d db", then run the tests again.
- `docker compose up -d db`: works (Docker 29.9.3, PostgreSQL 16.16). The cgroup2fs check: not run yet.

## Decisions

- Accepted: D-01, D-31, D-32, D-33, D-36, D-37, D-38, D-39, D-40. Rejected: D-34.
- Pending: D-35.
- D-01 to D-17: plan section 17. D-18 to D-30: LAN guide proposals.

## Gotchas and open items

- bcrypt 5.0.0 raises ValueError for passwords longer than 72 bytes instead of truncating them (checked in the venv). Slice E4-2 must handle it.
- The pins were generated on Python 3.13.5 (Windows) and must be re-checked on the VM's Python (D-36). When re-pinning, update `psycopg-pool` too: the pool extra does not pin it.
- Docker Desktop 29.9.3 and PostgreSQL 16.16 (compose service db) run on this machine. A terminal or VS Code window opened before Docker was installed has no `docker` on its PATH: open a new one. The cgroup2fs check from A-1 has not run yet; the machine that runs the sandbox tests must print cgroup2fs.
- `localhost` resolves to `::1` first on Windows. With port 5432 published on 127.0.0.1 alone, psycopg waits for its connect timeout (130 s by default) before it tries 127.0.0.1, so the compose file publishes both loopback addresses (D-37).
- Publishing on `[::1]` needs IPv6 on the host. Where IPv6 is disabled (some Linux VMs) the container may not start: drop the `[::1]` entry there and use 127.0.0.1 in the database URLs. Check it on the VM (D-36).
- If recreating the db container fails with "ports are not available", the old container had not released the port yet; run `docker compose up -d db` again.
- Database tests need the db service running: `docker compose up -d db`. When it is down they fail with that hint; they never skip.
- Every test session drops and rebuilds orch_test, so never run two test sessions against the same server at once.
- For E4-1: `transition()` must clear `app.actor` and `app.reason` after its UPDATE (`set_config(name, '', true)`) and must never set them for the whole session; otherwise a later direct write of jobs.state in the same transaction, or on the same pooled connection, passes the JT002 guard with stale values (review finding, left to E4-1).
- Claude Code on this machine: a double backslash typed into a Bash command arrives as one, and a heredoc of about 7.5 KB failed once with a quoting error. Avoid double backslashes in commands and write large files in parts.
- `db/invariants.sql` has no automated test yet; A-4's `tests/helpers.py` runs it after every system test. `tests/schema/conftest.py` holds the database fixtures A-2 needed; A-4 may move them into `tests/conftest.py`.
- Windows PowerShell 5.1 strips double quotes inside arguments to native programs, so `python -c "..."` with quoted strings breaks; put the code in a script file.
- `app/` has no `__init__.py` (the 3.4 tree lists none). Tests import it through `pythonpath = ["."]` in `pyproject.toml`; keep that line.

## Next

- Slice A-3: `seed_data/problems/12/` (plan 4.3, last paragraph), `scripts/reset_db.sh` (plan 4.5) and `scripts/capacity.py` (plan 6.5); also add the data folders and `capacity.py` to `scripts/reset_db.ps1` (D-38). Done when a reset from a clean checkout works, the S0 free capacity is 28,800, 117,964 and 14,745, and the invariant query is empty.
- Prompt: plan 15.2. Sections cited for A-2 and A-3: plan 4 and 6.5; context 3, 11 and 12.
- In parallel: E2-2 (`queue.py`) needs only A-1 and no database.
