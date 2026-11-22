"""PreToolUse guard for the Bash and PowerShell tools (slice T-1, D-110). Standard library only.

Claude Code passes the tool call as JSON on stdin. A blocked command exits 2 with one line on stderr,
which Claude Code shows to Claude; any other command exits 0, and so does input that is not a tool call.
Blocked:
- a pytest run while another pytest process runs (CLAUDE.md Machine limits), naming its PIDs. VS Code's
  test discovery (--collect-only) and the guard's own processes do not count; a process list that
  cannot be read blocks the run;
- docker compose down -v, docker volume rm, remove or prune, docker system prune;
- git push --force, -f or a +refspec, git reset --hard, git clean -f, whole-tree discards (checkout or
  restore of ., checkout -f, switch -f), and a checkout or restore of a path with uncommitted changes:
  planted bugs are restored with Edit, byte for byte;
- recursive deletes of anything but caches and build output inside the project.
A command is split at ; & | ( ) { } and line breaks outside quotes, and the command handed to bash -c,
cmd /c, powershell -Command or wsl is checked the same way.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from typing import NamedTuple, TextIO

Process = tuple[int, str]
Processes = Callable[[], list[Process]]

CACHES = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".hypothesis", "htmlcov",
                    "build", "dist"})
SHELLS = {"bash": "bash", "sh": "bash", "zsh": "bash", "dash": "bash", "wsl": "bash",
          "powershell": "powershell", "pwsh": "powershell", "cmd": "cmd"}
DELETERS = frozenset({"rm", "remove-item", "ri", "del", "erase", "rd", "rmdir"})
PREFIXES = frozenset({"sudo", "env", "nohup", "time", "exec", "command", "nice", "xargs"})
WHOLE_TREE = frozenset({".", "./", ".\\", ":", ":/", ":/.", "*", ":(top)"})
PYTHON = re.compile(r"python[0-9.]*|py")
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
REDIRECT = re.compile(r"\d*(>>?|<)")
PS_RECURSE = re.compile(r"-r(e(c(u(r(s(e)?)?)?)?)?)?(:\$true)?", re.IGNORECASE)  # -Recurse or a prefix
PS_VALUE = re.compile(r"-(include|exclude|filter|credential|stream)", re.IGNORECASE)  # value is no target
RM_CLUSTER = re.compile(r"-[rRfiIvd]+")
CMD_SWITCH = re.compile(r"/[A-Za-z]")
SEPARATORS = {"bash": ";&|(){}\n`", "powershell": ";&|(){}\n", "cmd": ";&|()\n"}
ESCAPE = {"bash": "\\", "powershell": "`", "cmd": "^"}
LIST_PROCESSES_PS = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                     "Get-CimInstance -ClassName Win32_Process -Property ProcessId, CommandLine | "
                     "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress")


class Token(NamedTuple):
    text: str
    quoted: bool  # some part of it was inside quotes


class Context(NamedTuple):
    cwd: str
    project: str
    processes: Processes


def split(command: str, dialect: str) -> list[list[Token]]:
    """The command's segments, each a list of tokens. bash: a backslash escapes the next character
    outside single quotes (inside double quotes only before " \\ $ ` or a line break); powershell: a
    backtick escapes and a backslash is a path separator; cmd: a caret escapes and ' is no quote."""
    segments: list[list[Token]] = []
    tokens: list[Token] = []
    chars: list[str] = []
    quoted = started = False
    separators, escape = SEPARATORS[dialect], ESCAPE[dialect]
    i, n = 0, len(command)

    def end_token() -> None:
        nonlocal chars, quoted, started
        if started:
            tokens.append(Token("".join(chars), quoted))
        chars, quoted, started = [], False, False

    def end_segment() -> None:
        nonlocal tokens
        end_token()
        if tokens:
            segments.append(tokens)
        tokens = []

    while i < n:
        c = command[i]
        if c == escape and i + 1 < n:
            chars.append(command[i + 1])
            started = True
            i += 2
        elif c == '"' or (c == "'" and dialect != "cmd"):
            j = i + 1
            while j < n:
                d = command[j]
                if d == c:
                    if dialect == "powershell" and j + 1 < n and command[j + 1] == c:  # "" or '' inside
                        chars.append(c)
                        j += 2
                        continue
                    break
                if c == '"' and j + 1 < n and (
                        (dialect == "bash" and d == "\\" and command[j + 1] in '"\\$`\n')
                        or (dialect == "powershell" and d == "`")):
                    chars.append(command[j + 1])
                    j += 2
                    continue
                chars.append(d)
                j += 1
            quoted = started = True
            i = j + 1
        elif c in " \t\r":
            end_token()
            i += 1
        elif c in separators:
            end_segment()
            i += 1
        else:
            chars.append(c)
            started = True
            i += 1
    end_segment()
    return segments


def name(word: str) -> str:
    """A program's name: no folder, lower case, no .exe."""
    base = re.split(r"[\\/]", word)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if base.endswith(suffix):
            return base[:-len(suffix)]
    return base


def known(word: str) -> bool:
    prog = name(word)
    return (prog in ("git", "docker", "docker-compose", "pytest", "py.test") or prog in DELETERS
            or prog in SHELLS or bool(PYTHON.fullmatch(prog)))


def check(command: str, dialect: str, ctx: Context, depth: int = 0) -> str | None:
    """The reason to block the command, or None."""
    if depth > 4:
        return None
    for tokens in split(command, dialect):
        reason = check_segment(tokens, dialect, ctx, depth)
        if reason:
            return reason
    return None


def check_segment(tokens: list[Token], dialect: str, ctx: Context, depth: int) -> str | None:
    i = 0
    while i < len(tokens):  # skip FOO=bar, sudo, env, nohup and the like, and their options
        prog = name(tokens[i].text)
        if ASSIGNMENT.fullmatch(tokens[i].text) or (i > 0 and name(tokens[i - 1].text) in PREFIXES
                                                    and tokens[i].text.startswith("-")):
            i += 1
        elif prog in PREFIXES:
            i += 1
        elif prog == "timeout" and i + 1 < len(tokens):
            i += 2
        else:
            break
    tokens = tokens[i:]
    if not tokens:
        return None
    prog, args = name(tokens[0].text), [t.text for t in tokens[1:]]
    if prog in ("pytest", "py.test") or (PYTHON.fullmatch(prog)
                                         and any(a == "-m" and b == "pytest" for a, b in pairwise(args))):
        return pytest_reason(ctx)
    if prog == "git":
        return git_reason(args, ctx)
    if prog in ("docker", "docker-compose"):
        return docker_reason(prog, args)
    if prog in DELETERS:
        return delete_reason(prog, tokens[1:], dialect, ctx)
    if prog in SHELLS:
        inner = SHELLS[prog]
        for token in tokens[1:]:
            if token.quoted:
                reason = check(token.text, inner, ctx, depth + 1)
                if reason:
                    return reason
        for k in range(1, len(tokens)):
            if not tokens[k].quoted and known(tokens[k].text):
                return check_segment(tokens[k:], inner, ctx, depth + 1)
    return None


def pytest_reason(ctx: Context) -> str | None:
    try:
        processes = ctx.processes()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        return f"the running processes cannot be listed ({exc}), so another pytest cannot be ruled out"
    own = os.getpid()
    pids = sorted({pid for pid, cmd in processes
                   if pid != own and "pytest" in cmd.lower() and "--collect-only" not in cmd.lower()})
    if pids:
        return (f"pytest is already running (PID {', '.join(map(str, pids))}); CLAUDE.md allows one pytest "
                "process at a time")
    return None


def git_reason(args: list[str], ctx: Context) -> str | None:
    cwd, i = ctx.cwd, 0
    while i < len(args) and args[i].startswith("-"):  # git's own options before the subcommand
        if args[i] == "-C" and i + 1 < len(args):
            cwd = os.path.join(cwd, args[i + 1])
            i += 2
        elif args[i] in ("-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(args):
            i += 2
        else:
            i += 1
    if i >= len(args):
        return None
    sub, rest = args[i], args[i + 1:]
    shorts = [a[1:] for a in rest if a.startswith("-") and not a.startswith("--")]
    if sub == "push":
        if (any(a.startswith("--force") for a in rest) or any("f" in s for s in shorts)
                or any(a.startswith("+") and len(a) > 1 for a in rest)):
            return "git push with --force, -f or a +refspec rewrites the remote's history"
    elif sub == "reset":
        if "--hard" in rest:
            return "git reset --hard discards uncommitted work"
    elif sub == "clean":
        if "--force" in rest or any("f" in s for s in shorts):
            return "git clean -f deletes untracked files"
    elif sub == "switch":
        if any(a in ("-f", "--force", "--discard-changes") for a in rest):
            return "git switch --force or --discard-changes discards uncommitted changes"
    elif sub in ("checkout", "restore"):
        if sub == "checkout" and any(a in ("-f", "--force") for a in rest):
            return "git checkout -f discards every uncommitted change in the tree"
        if (sub == "restore" and any(a in ("--staged", "-S") for a in rest)
                and not any(a in ("--worktree", "-W") for a in rest)):
            return None  # the index only: the work tree keeps its changes
        paths = rest[rest.index("--") + 1:] if "--" in rest else [a for a in rest if not a.startswith("-")]
        if any(p in WHOLE_TREE for p in paths):
            return f"git {sub} of the whole tree discards every uncommitted change"
        changed = dirty(paths, cwd)
        if changed:
            return f"{changed} has uncommitted changes; restore a planted bug with Edit, byte for byte"
    return None


def dirty(paths: list[str], cwd: str) -> str | None:
    """The first of paths with uncommitted changes (staged or not), or None; None also when git fails."""
    if not paths:
        return None
    specs = [p.replace("\\", "/") for p in paths] if os.name == "nt" else paths
    try:
        done = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no", "--", *specs],
                              cwd=cwd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    for line in done.stdout.splitlines():
        if len(line) > 3:
            return line[3:]
    return None


def docker_reason(prog: str, args: list[str]) -> str | None:
    rest = [a.lower() for a in args]
    if prog == "docker-compose" or "compose" in rest:
        start = rest.index("compose") + 1 if prog == "docker" else 0
        if "down" in rest[start:]:
            after = rest[rest.index("down", start) + 1:]
            if any(a in ("-v", "--volumes") or (a.startswith("--volumes=") and a != "--volumes=false")
                   for a in after):
                return "docker compose down -v deletes the volumes, the database's among them"
    if "volume" in rest and any(a in ("rm", "remove", "prune") for a in rest[rest.index("volume") + 1:]):
        return "docker volume rm and prune delete volumes, the database's among them"
    if "system" in rest and "prune" in rest[rest.index("system") + 1:]:
        return "docker system prune deletes containers, images and volumes"
    return None


def delete_reason(prog: str, tokens: list[Token], dialect: str, ctx: Context) -> str | None:
    recursive, skip = False, False
    targets: list[str] = []
    for token in tokens:
        word = token.text
        if skip:
            skip = False
            continue
        if not token.quoted:
            redirect = REDIRECT.match(word)
            if redirect:
                skip = redirect.end() == len(word)  # its target is the next token
                continue
            if word.startswith("--"):
                recursive |= len(word) >= 3 and "--recursive".startswith(word)
                continue
            if word.startswith("-"):
                recursive |= bool(PS_RECURSE.fullmatch(word)) or (
                    prog == "rm" and bool(RM_CLUSTER.fullmatch(word)) and "r" in word.lower())
                skip = bool(PS_VALUE.fullmatch(word))
                continue
            if CMD_SWITCH.fullmatch(word):
                recursive |= word.lower() == "/s"
                continue
        targets.append(word)
    if not recursive:
        return None
    if not targets:
        return f"{prog} would delete recursively what reaches it through the pipeline, which cannot be checked"
    for target in targets:
        if not in_caches(target, dialect, ctx):
            return (f"recursive delete of {target}: only caches and build output inside the project may be "
                    "deleted recursively")
    return None


def in_caches(target: str, dialect: str, ctx: Context) -> bool:
    """True if target lies inside the project, in a cache or build output folder."""
    if not target or any(c in target for c in "$%`~"):
        return False  # a variable or the home folder: where it points is unknown here
    path = target if dialect == "bash" else target.replace("\\", "/")
    if os.name == "nt":
        msys = re.fullmatch(r"/([A-Za-z])(/.*)?", path)  # Git Bash's /d/... is D:/...
        if msys:
            path = f"{msys.group(1)}:{msys.group(2) or '/'}"
    full = os.path.normpath(os.path.join(ctx.cwd, path))
    try:
        rel = os.path.relpath(full, os.path.normpath(ctx.project))
    except ValueError:  # another drive
        return False
    if rel == os.curdir or rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return False
    return any(part in CACHES or part.endswith(".egg-info") for part in Path(rel).parts)


def list_processes() -> list[Process]:
    """(pid, command line) of every process: Win32_Process through Windows PowerShell on Windows, /proc on
    Linux and WSL. The lister's own process is left out."""
    if os.name == "nt":
        script = base64.b64encode(LIST_PROCESSES_PS.encode("utf-16-le")).decode("ascii")
        with subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", script],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as lister:
            try:
                out, _ = lister.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                lister.kill()
                raise
        rows = json.loads(out.decode("utf-8-sig", "replace") or "[]")
        rows = [rows] if isinstance(rows, dict) else rows
        return [(int(row["ProcessId"]), row["CommandLine"] or "") for row in rows
                if int(row["ProcessId"]) != lister.pid]
    listed: list[Process] = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                raw = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            listed.append((int(entry.name), raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()))
    return listed


def main(stdin: TextIO | None = None, stderr: TextIO | None = None, processes: Processes | None = None) -> int:
    """Decide one PreToolUse call: 2 and one stderr line to block it, 0 to let it run."""
    raw = stdin.read() if stdin is not None else sys.stdin.buffer.read().decode("utf-8", "replace")
    try:
        payload = json.loads(raw)
        command = payload["tool_input"]["command"]
    except (ValueError, KeyError, TypeError):
        return 0
    if not isinstance(command, str):
        return 0
    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) and cwd else os.getcwd()
    project = os.environ.get("CLAUDE_PROJECT_DIR") or cwd
    dialect = "powershell" if payload.get("tool_name") == "PowerShell" else "bash"
    reason = check(command, dialect, Context(cwd, project, processes or list_processes))
    if reason is None:
        return 0
    line = "Blocked by .claude/hooks/guard.py: " + " ".join(reason.split()) + "\n"
    if stderr is not None:
        stderr.write(line)
    else:
        sys.stderr.buffer.write(line.encode("utf-8", "replace"))
        sys.stderr.buffer.flush()
    return 2


if __name__ == "__main__":
    sys.exit(main())
