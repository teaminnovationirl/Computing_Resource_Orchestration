"""The sandbox that untrusted code runs in (plan 9.2, D-149): its protocol, its value types and the one way a
runner keeps what a command prints. FakeRunner (fake_runner.py) and DockerRunner (E1-3) implement it."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self

from app.common.types import Limits


class SandboxError(Exception):
    """The sandbox itself failed (it is not there, Docker refused, a folder is missing): never a verdict."""


@dataclass(frozen=True)
class SandboxSpec:
    name: str                # "job-<job_id>-a<attempt_no>"
    limits: Limits
    job_dir: Path            # mounted read-only at /job
    out_dir: Path            # mounted read-write at /out: RunFolder.out, never the attempt's folder (D-143)
    labels: dict[str, str]   # {"orch.job_id": "1042", "orch.attempt_id": "77"}


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    wall_ms: int
    timed_out: bool                   # the per-command timeout fired
    oom_killed: bool                  # memory.events oom_kill rose during this command
    output_truncated: bool            # stdout (with stderr, when merged) went past cap_bytes
    stderr_truncated: bool = False    # the stderr file went past cap_bytes; False when there is none
    peak_ram_mib: int | None = None   # peak memory of this command; None when not measured


class SandboxRunner(Protocol):
    def start(self, spec: SandboxSpec) -> None:
        """Sandbox up, limits applied, idle. SandboxError when the name is taken or a folder is missing."""
        ...

    def exec(self, name: str, argv: list[str], stdin: Path | None, stdout: Path, timeout_s: float, cap_bytes: int,
             merge_stderr: bool = False, stderr: Path | None = None) -> ExecResult:
        """Run one command to its end and keep what it prints, through captured(): stdout in the new file
        stdout; stderr in that same file when merge_stderr, in the new file stderr when a path is given, and
        dropped when neither is. merge_stderr with a stderr path is ValueError, before anything runs or is
        made. Each file keeps its first cap_bytes; what follows is read and dropped, so a program that floods
        its output still ends by itself. A runner may end a flood that goes on and on: the result is then exit
        code 0 with the output truncated, which is WA, never RE or TLE. stdin None is an empty input, never the
        caller's own; a file that cannot be read is its OSError.

        SandboxError when the sandbox is not running, or when stop() or remove() ended the command: what we cut
        has no result, so it can never become a verdict. A sandbox that ends during the command without either
        is the program's doing (it may kill every process of its user), and that is its result, a non-zero exit
        code: a program must not be able to turn its verdict into a failure of the server."""
        ...

    def stop(self, name: str, grace_s: float) -> None:
        """SIGTERM to every job process, SIGKILL after grace_s."""
        ...

    def remove(self, name: str) -> None:
        """Idempotent."""
        ...

    def exists(self, name: str) -> bool:
        """SandboxError when it cannot tell: False confirms a stop (invariant 4)."""
        ...

    def list_sandboxes(self, job_id: int | None = None) -> list[str]:
        """Names, by label, for stop and reconcile. SandboxError when it cannot tell: an empty list confirms a
        stop."""
        ...


class CappedFile:
    """A file that keeps the first cap_bytes written to it. It is made new ("xb"), so whatever is already at
    its path, a link included, is never opened or followed; every write is flushed, so a worker that is
    killed loses nothing it captured."""

    def __init__(self, path: Path, cap_bytes: int) -> None:
        self._file = open(path, "xb")
        self._left = cap_bytes
        self.truncated = False

    def write(self, data: bytes) -> None:
        kept = data[:self._left]
        self._file.write(kept)
        self._file.flush()
        self._left -= len(kept)
        self.truncated = self.truncated or len(kept) < len(data)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None,
                 trace: TracebackType | None) -> None:
        self._file.close()


@contextmanager
def captured(stdout: Path, stderr: Path | None, cap_bytes: int,
             merge_stderr: bool) -> Iterator[tuple[CappedFile, CappedFile | None]]:
    """The two sinks of one exec(), the same for every runner: stdout's file, and for stderr that file
    (merged), a file of its own, or None (dropped)."""
    if merge_stderr and stderr is not None:
        raise ValueError("merge_stderr sends stderr to the output file: it cannot have a file of its own too")
    with CappedFile(stdout, cap_bytes) as out:
        if stderr is None:
            yield out, out if merge_stderr else None
        else:
            with CappedFile(stderr, cap_bytes) as err:
                yield out, err
