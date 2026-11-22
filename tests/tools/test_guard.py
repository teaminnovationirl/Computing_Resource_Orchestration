"""The PreToolUse guard .claude/hooks/guard.py (slice T-1, D-110). Each docstring names the break the
test catches: a destructive command that gets through, or a near miss that the guard blocks by mistake.
The process list is the only fake; the git checks run on a real repository in tmp_path."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARD_PATH = ROOT / ".claude" / "hooks" / "guard.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("guard", GUARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load()

Process = tuple[int, str]
RUNNING = (4242, r"D:\Mini Project\CROsystem\.venv\Scripts\python.exe -m pytest -q tests/e2")
DISCOVERY = (5151, r"c:\Python313\python.exe -m pytest -p vscode_pytest --collect-only D:\Mini Project\CROsystem")


def run(command: str, cwd: Path, tool: str = "Bash", processes: tuple[Process, ...] = ()) -> tuple[int, str]:
    """One PreToolUse payload through guard.main(); returns (exit code, stderr)."""
    payload = {"session_id": "test", "hook_event_name": "PreToolUse", "tool_name": tool, "cwd": str(cwd),
               "tool_input": {"command": command, "description": "test"}}
    err = io.StringIO()
    code = guard.main(io.StringIO(json.dumps(payload)), err, processes=lambda: list(processes))
    return code, err.getvalue()


def assert_blocked(code: int, err: str) -> None:
    assert code == 2, err
    assert err.strip() and err.endswith("\n") and err.count("\n") == 1, repr(err)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty project folder, which the guard takes from CLAUDE_PROJECT_DIR."""
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    return root


@pytest.fixture
def repo(project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repository in the project: clean.py committed and unchanged, dirty.py changed in the work
    tree, staged.py changed in the index. The user's own git configuration is not read."""
    empty = tmp_path / "gitconfig"
    empty.write_bytes(b"")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    def git(*args: str) -> None:
        subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
                       cwd=project, check=True, capture_output=True)

    for name in ("clean.py", "dirty.py", "staged.py"):
        (project / name).write_bytes(b"x = 1\n")
    git("init", "-q")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    (project / "dirty.py").write_bytes(b"x = 2\n")
    (project / "staged.py").write_bytes(b"x = 3\n")
    git("add", "staged.py")
    return project


@pytest.mark.parametrize("command, tool", [
    ("pytest -q", "Bash"),
    (".venv/Scripts/pytest -k guard", "Bash"),
    ("cd tests && pytest -x", "Bash"),
    ('bash -c "pytest -q"', "Bash"),
    (r".venv\Scripts\python.exe -m pytest tests/e2", "PowerShell"),
    (r"& .venv\Scripts\pytest.exe -q", "PowerShell"),
])
def test_pytest_while_another_pytest_runs_is_blocked_naming_its_pid(project: Path, command: str, tool: str):
    """Catches a second pytest let through while one runs (CLAUDE.md Machine limits), in any spelling of
    the call, and a reason that names the wrong process: VS Code's discovery is running too and must not
    be named."""
    code, err = run(command, project, tool, processes=(RUNNING, DISCOVERY))
    assert_blocked(code, err)
    assert "4242" in err and "5151" not in err


@pytest.mark.parametrize("processes", [(), (DISCOVERY,), ((os.getpid(), "python -m pytest -q"),)],
                         ids=["nothing-else", "vscode-discovery-only", "own-pid-only"])
def test_pytest_passes_when_no_other_pytest_runs(project: Path, processes: tuple[Process, ...]):
    """Catches a guard that counts VS Code's --collect-only discovery or its own process as a running
    pytest, and so blocks every test run."""
    assert run("pytest -k guard -q", project, processes=processes) == (0, "")


def test_other_commands_pass_while_pytest_runs(project: Path):
    """Catches a guard that blocks any command naming pytest, not only a pytest run."""
    for command in ("git status", "grep -n pytest pyproject.toml"):
        assert run(command, project, processes=(RUNNING,)) == (0, ""), command


def test_only_a_pytest_command_lists_the_processes(project: Path):
    """Catches a guard that lists every process for every command (a PowerShell start on each call)."""
    def boom() -> list[Process]:
        raise AssertionError("listed the processes for a command that is not pytest")

    err = io.StringIO()
    payload = {"tool_name": "Bash", "cwd": str(project), "tool_input": {"command": "git status"}}
    assert guard.main(io.StringIO(json.dumps(payload)), err, processes=boom) == 0


def test_pytest_is_blocked_when_the_process_list_fails(project: Path):
    """Catches a guard that lets pytest through when it cannot see the other processes: the one-pytest
    rule would then hold only while the lister works."""
    def broken() -> list[Process]:
        raise OSError("powershell.exe not found")

    err = io.StringIO()
    payload = {"tool_name": "Bash", "cwd": str(project), "tool_input": {"command": "pytest -q"}}
    assert_blocked(guard.main(io.StringIO(json.dumps(payload)), err, processes=broken), err.getvalue())


BLOCKED_BASH = [
    "docker compose down -v",
    "docker compose -f docker-compose.yml down --volumes",
    "docker-compose down -v",
    "docker volume rm crosystem_pgdata",
    "docker volume remove crosystem_pgdata",
    "docker volume prune -f",
    "docker system prune -a",
    "wsl -d Ubuntu -- docker compose down -v",
    "git push --force",
    "git push -f origin main",
    "git push -uf origin slice/T-1",
    "git push --force-with-lease origin main",
    "git push origin +main",
    "git push --force --dry-run",
    "git -C ../other push --force",
    "git reset --hard",
    "git reset --hard HEAD~1",
    "git clean -f",
    "git clean -fdx",
    "git clean -d --force",
    "git checkout -- .",
    "git checkout .",
    "git restore .",
    "git restore -- .",
    "git restore :/",
    "git checkout -f",
    "git checkout --force main",
    "git switch -f main",
    "git switch --discard-changes main",
    'bash -c "git reset --hard"',
    "git status; git reset --hard",
    "rm -rf app",
    "rm -r docs",
    "rm -R tests",
    "rm --recursive data",
    "rm -fr app",
    "rm -rf .pytest_cache app",
    "rm -rf build/../app",
    'rm -rf "$DIR"',
    "rm -rf /tmp/build",
    "rm -rf *",
    'bash -c "rm -rf app"',
    'cmd /c "rd /s /q app"',
    "cmd /c rmdir /s app",
]

BLOCKED_POWERSHELL = [
    "Remove-Item -Recurse -Force app",
    "Remove-Item app -r",
    "rm -r app",
    "ri -Recurse app",
    "Remove-Item -Path app -Recurse",
    "Get-ChildItem -Filter __pycache__ -Recurse | Remove-Item -Recurse -Force",
    'powershell -Command "Remove-Item -Recurse -Force app"',
    "git status; if ($?) { git clean -fd }",
    "docker compose down -v 2>&1",
]


@pytest.mark.parametrize("command, tool", [(c, "Bash") for c in BLOCKED_BASH]
                         + [(c, "PowerShell") for c in BLOCKED_POWERSHELL])
def test_destructive_commands_are_blocked_with_one_line(project: Path, command: str, tool: str):
    """Catches a destructive form that gets through: Docker volume deletes, force pushes, hard resets,
    git clean -f, whole-tree discards, and recursive deletes of anything but caches and build output,
    also when nested in bash -c, cmd /c, powershell -Command or wsl, or after a ; or inside { }."""
    assert_blocked(*run(command, project, tool))


ALLOWED_BASH = [
    "./scripts/reset_db.sh",
    "docker compose down",
    "docker compose up -d db",
    "docker compose exec -T db psql -U postgres -d orch",
    "docker volume ls",
    "docker system df",
    "docker run --rm -v data:/data alpine ls",
    "git push",
    "git push --dry-run",
    "git push -u origin slice/T-1",
    "git clean -n",
    "git clean -dn",
    "git log --oneline -3",
    'git commit -m "rm -rf app"',
    'git commit -m "git reset --hard is blocked now"',
    "grep -rn 'reset --hard' docs",
    "echo rm -rf app",
    "rm notes.txt",
    "rm -f notes.txt",
    "rmdir emptydir",
    "rm -rf .pytest_cache",
    "rm -rf app/__pycache__ tests/__pycache__",
    "rm -rf .ruff_cache .mypy_cache",
    "rm -rf build dist",
    "rm -rf build/lib",
    "rm -rf crosystem.egg-info",
    "rm -rf .pytest_cache > /dev/null 2>&1",
]

ALLOWED_POWERSHELL = [
    r"scripts\reset_db.ps1",
    r"powershell -ExecutionPolicy Bypass -File scripts\reset_db.ps1",
    "Remove-Item notes.txt",
    "Remove-Item -Force notes.txt",
    "rm -Force notes.txt",
    "Remove-Item -Recurse -Force .ruff_cache",
    r"Remove-Item -Recurse -Force app\__pycache__",
    r".venv\Scripts\ruff.exe check .",
]


@pytest.mark.parametrize("command, tool", [(c, "Bash") for c in ALLOWED_BASH]
                         + [(c, "PowerShell") for c in ALLOWED_POWERSHELL])
def test_near_misses_pass_silently(project: Path, command: str, tool: str):
    """Catches a guard that blocks too much: the reset scripts, a docker compose down that keeps the
    volumes, plain pushes, dry-run cleans, quoted text that only mentions a dangerous command, plain
    file deletes, and recursive deletes of caches and build output."""
    assert run(command, project, tool) == (0, "")


@pytest.mark.parametrize("command", [
    "git checkout -- dirty.py",
    "git checkout dirty.py",
    "git checkout HEAD -- dirty.py",
    "git restore dirty.py",
    "git restore --staged --worktree dirty.py",
    "git checkout -- staged.py",
    "git restore --source=HEAD clean.py dirty.py",
])
def test_discarding_a_path_with_uncommitted_changes_is_blocked(repo: Path, command: str):
    """Catches a checkout or restore that silently drops uncommitted work in a file (planted bugs are
    restored with Edit, byte for byte); the reason names the file."""
    code, err = run(command, repo)
    assert_blocked(code, err)
    assert "dirty.py" in err or "staged.py" in err


@pytest.mark.parametrize("command", [
    "git checkout -- clean.py",
    "git restore clean.py",
    "git restore --staged dirty.py staged.py",
    "git checkout main",
    "git checkout -b slice/T-2",
    "git reset HEAD staged.py",
    "git reset --soft HEAD~1",
])
def test_git_near_misses_pass(repo: Path, command: str):
    """Catches a guard that blocks a checkout or restore that loses nothing: a file without changes, an
    unstage, a branch switch, a soft or mixed reset."""
    assert run(command, repo) == (0, "")


def test_git_dash_c_is_followed_to_its_repository(repo: Path):
    """Catches a dirty check that runs in the hook's cwd instead of the folder given to git -C."""
    assert_blocked(*run(f'git -C "{repo}" checkout -- dirty.py', repo.parent))


def test_an_absolute_path_is_judged_by_where_it_points(project: Path):
    """Catches a guard that judges absolute paths by their text rather than their place in the project."""
    assert run(f'rm -rf "{project / ".pytest_cache"}"', project) == (0, "")
    assert_blocked(*run(f'Remove-Item -Recurse -Force "{project / "app"}"', project, "PowerShell"))


@pytest.mark.skipif(os.name != "nt", reason="Git Bash paths (/d/...) exist only on Windows")
def test_a_git_bash_path_is_read_as_its_windows_path(project: Path):
    """Catches a guard that reads /d/... (Git Bash) as a folder d on the current drive and so blocks a
    cache delete inside the project."""
    drive, rest = str(project).split(":", 1)
    msys = f"/{drive.lower()}{rest.replace(chr(92), '/')}"
    assert run(f'rm -rf "{msys}/.pytest_cache"', project) == (0, "")
    assert_blocked(*run(f'rm -rf "{msys}/app"', project))


@pytest.mark.parametrize("stdin", ["", "not json", "[]", '{"tool_input": {}}', '{"tool_input": {"command": 7}}'])
def test_malformed_input_passes(stdin: str):
    """Catches a guard that blocks every tool call when a payload is not the one it expects."""
    err = io.StringIO()
    assert guard.main(io.StringIO(stdin), err, processes=list) == 0 and err.getvalue() == ""


def run_script(command: str, project: Path) -> subprocess.CompletedProcess[str]:
    payload = {"tool_name": "Bash", "cwd": str(project), "tool_input": {"command": command}}
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
    return subprocess.run([sys.executable, str(GUARD_PATH)], input=json.dumps(payload), capture_output=True,
                          text=True, env=env, timeout=60)


def test_the_script_blocks_with_exit_2_and_one_stderr_line(project: Path):
    """Catches a script that decides right but exits 1 or writes to stdout (Claude Code blocks on exit 2
    only, and shows Claude the stderr)."""
    done = run_script("git push --force --dry-run", project)
    assert done.returncode == 2 and done.stdout == "" and done.stderr.count("\n") == 1, done


def test_the_script_passes_a_harmless_command_silently(project: Path):
    """Catches a script that prints or exits non-zero for a command it lets through."""
    done = run_script("git status", project)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_the_real_process_list_holds_this_test_run():
    """Catches a process lister that runs but sees nothing useful (bad PowerShell quoting, an empty /proc
    read): this pytest process must be listed with pytest in its command line."""
    listed = dict(guard.list_processes())
    assert "pytest" in listed.get(os.getpid(), "").lower()
