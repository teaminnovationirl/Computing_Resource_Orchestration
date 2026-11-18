# Handoff summary

Current state of the build. Rewrite a section when it changes; this is not a log. Read it first, then only the files it points to.

Last updated: 2026-11-18, end of slice A-1.

## Status

| Slice | Status | Branch or commit |
| --- | --- | --- |
| A-1 | done; Docker and cgroup2fs checks deferred (see Gotchas) | slice/A-1, commit "A-1: repository setup", not pushed or merged |
| A-2 | not started | none |
| A-3 | not started | none |
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

## Code map

- `app/config.py`: settings from environment variables (plan 3.3). Public: `Config` (frozen dataclass, one lower case field per variable), `Config.from_env(env=os.environ)`, `CHOICES`.
- `tests/test_config.py`: 16 tests: defaults match the 3.3 table, `.env.example` in sync, overrides, frozen, bad values rejected.
- `.env.example`: all 24 variables with their defaults.
- `pyproject.toml`: pytest settings only: `testpaths`, `pythonpath = ["."]`, `--strict-markers`, `-m "not docker"` by default.
- `requirements.txt`, `requirements-dev.txt`: `==` pins (D-36).
- `docker-compose.yml`: PostgreSQL 16 for development (plan 3.2).
- `CLAUDE.md`: rules for every session.
- `docs/`: `PROJECT_CONTEXT.md` (baseline, wins every conflict), `IMPLEMENTATION_PLAN.md` (build plan; 3.4 has the full file tree), `LAN_BUILD_GUIDE.md` (LAN slices only), `decisions.md`, `SUMMARY.md`.

## Run and test

Windows (PowerShell), from the repository root:

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\pytest -q
```

Linux or WSL2:

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                     # everything that needs no Docker
pytest -q -m docker           # sandbox tests, needs cgroup v2
docker compose up -d db       # PostgreSQL 16 for development
stat -fc %T /sys/fs/cgroup    # must print cgroup2fs
```

Last known results (2026-11-18, Windows 11, Python 3.13.5):
- `pytest -q`: `16 passed in 0.04s`
- `pytest -q -m docker`: `16 deselected` (exit code 5 until docker tests exist)
- `docker compose up -d db` and the cgroup2fs check: not run yet.

## Decisions

- Accepted: D-31, D-32, D-33, D-36.
- Pending: D-34, D-35, D-01 confirmation.
- D-01 to D-17: plan section 17. D-18 to D-30: LAN guide proposals.

## Gotchas and open items

- bcrypt 5.0.0 raises ValueError for passwords longer than 72 bytes instead of truncating them (checked in the venv). Slice E4-2 must handle it.
- The pins were generated on Python 3.13.5 (Windows) and must be re-checked on the VM's Python (D-36). When re-pinning, update `psycopg-pool` too: the pool extra does not pin it.
- Docker and WSL2 are not installed on this machine yet (planned before E3-1), so `docker compose up -d db` and the cgroup2fs check from A-1 have not run. Every member's machine must still print cgroup2fs.
- A-2 and A-3 tests need a real PostgreSQL 16 (CLAUDE.md, Tests), which the plan runs through Docker, so the machine that builds A-2 needs Docker or a native PostgreSQL 16 first.
- `app/` has no `__init__.py` (the 3.4 tree lists none). Tests import it through `pythonpath = ["."]` in `pyproject.toml`; keep that line.

## Next

- Slice A-2 (Sagar; each owner reviews their own tables): `db/roles.sql`, `db/schema.sql`, `tests/schema/`. Done when 15 tables and 17 foreign keys exist and the 25 checks of plan section 4 pass as tests.
- First: settle D-34 with the whole team (it changes the schema and is due before A-2), then start PostgreSQL 16.
- Prompt: plan 15.2. Sections cited for A-2 and A-3: plan 4 and 6.5; context 3, 11 and 12.
- In parallel: E2-2 (Sudhanv, `queue.py`) needs only A-1 and no database.
