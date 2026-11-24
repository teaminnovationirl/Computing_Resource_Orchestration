"""FakeRunner: a SandboxRunner for any OS that runs no code (plan 9.2, D-152). A program is scripted by marker
comments, one per line, `// fake: <action>` or `# fake: <action>`; `fake: case 3 <action>` speaks for test case 3
alone, whose other markers are then not used. Only results and files are simulated here: every verdict is made
in judge.py.

Compile step: compile-error TEXT, compile-sleep SECONDS, compile-oom, and for Java classes NAME ... (what javac
writes; Main when not given). It copies the source to /out/prog, or to /out/<name>.class; Python writes nothing.
Launcher check (Java): no-main. Run, in the order of the lines: print TEXT, print-hex HEX, print-expected (the
.out file beside the input), echo (stdin), flood BYTES, stderr TEXT, peak MIB, sleep SECONDS, exit CODE, oom. A
run reads its markers from what the compile step wrote, so a missing or stale binary behaves as it would. Any
other marker, or one with a bad argument, is SandboxError when the program is compiled.

Its sandboxes live in this process only: the launcher of E1-2 runs the worker as a thread with it.
"""
from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, NamedTuple

from app.common.clock import Clock
from app.e1_execution.judge import JAVA, LANGUAGES, PYTHON
from app.e1_execution.sandbox import CappedFile, ExecResult, SandboxError, SandboxSpec, captured

TIMED_OUT, KILLED, NOT_FOUND = 124, 137, 127  # exit codes as Docker and timeout report them
FLOOD_MAX_BYTES: Final = 1 << 24
_MARKER: Final = re.compile(rb"(?://|#)[ \t]*fake:([^\n]*)")
_BLANKS: Final = b" \t\r"
_COMPILES: Final[Mapping[tuple[str, ...], str]] = {lang.compile: source for source, lang in LANGUAGES.items()}
_NO_CLASS: Final = b"Error: Could not find or load main class Main\nCaused by: java.lang.ClassNotFoundException: Main\n"
_NO_MAIN: Final = (b"Error: Main method not found in class Main, please define the main method as:\n"
                   b"   public static void main(String[] args)\n")


def _text(arg: bytes) -> bytes:
    return arg


def _nothing(arg: bytes) -> None:
    if arg:
        raise ValueError("takes no argument")


def _seconds(arg: bytes) -> float:
    if not re.fullmatch(rb"\d+(\.\d+)?", arg):
        raise ValueError("not a number of seconds")
    return float(arg)


def _whole(most: int) -> Callable[[bytes], int]:
    def parse(arg: bytes) -> int:
        if not arg.isdigit() or int(arg) > most:
            raise ValueError(f"not a whole number up to {most}")
        return int(arg)
    return parse


def _class_names(arg: bytes) -> list[str]:
    names = arg.decode("ascii").split()
    if not 1 <= len(names) <= 100 or not all(re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$.]{0,99}", name) for name in names):
        raise ValueError("not a list of class names")  # each becomes the name of a file in /out
    return names


# How the argument of each action is read: a ValueError is a marker that means nothing.
_RUN: Final[Mapping[str, Callable[[bytes], Any]]] = {
    "print": _text, "print-hex": lambda arg: bytes.fromhex(arg.decode("ascii")), "print-expected": _nothing,
    "echo": _nothing, "flood": _whole(FLOOD_MAX_BYTES), "stderr": _text, "peak": _whole(1 << 20), "sleep": _seconds,
    "exit": _whole(255), "oom": _nothing}
_COMPILE: Final[Mapping[str, Callable[[bytes], Any]]] = {
    "compile-error": _text, "compile-sleep": _seconds, "compile-oom": _nothing, "classes": _class_names,
    "no-main": _nothing}
_ACTIONS: Final = {**_RUN, **_COMPILE}


class _Marker(NamedTuple):
    case: int | None
    action: str
    arg: Any  # as _ACTIONS[action] reads it


class _Ended(NamedTuple):
    exit_code: int
    timed_out: bool = False
    oom_killed: bool = False
    peak_ram_mib: int | None = None


@dataclass
class _Box:
    spec: SandboxSpec
    stopped: threading.Event = field(default_factory=threading.Event)


class FakeRunner:
    """The one instance of a process; its methods may be called from any thread."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._boxes: dict[str, _Box] = {}

    def start(self, spec: SandboxSpec) -> None:
        for folder in (spec.job_dir, spec.out_dir):
            if not folder.is_dir():
                raise SandboxError(f"{spec.name}: {folder} is not a folder")
        with self._lock:
            if spec.name in self._boxes:
                raise SandboxError(f"{spec.name} already exists")
            self._boxes[spec.name] = _Box(spec)

    def exec(self, name: str, argv: list[str], stdin: Path | None, stdout: Path, timeout_s: float, cap_bytes: int,
             merge_stderr: bool = False, stderr: Path | None = None) -> ExecResult:
        with self._lock:
            box = self._boxes.get(name)
        if box is None or box.stopped.is_set():
            raise SandboxError(f"{name} is not running")
        start = self._clock.monotonic()
        with captured(stdout, stderr, cap_bytes, merge_stderr) as (out, err):
            source = _COMPILES.get(tuple(argv))
            if source is not None:
                ended = self._compile(box, source, out, start + timeout_s)
            elif "--dry-run" in argv:
                ended = self._check(box, err)
            else:
                ended = self._run(box, argv, stdin, out, err, start + timeout_s)
        wall_ms = round((self._clock.monotonic() - start) * 1000)
        return ExecResult(ended.exit_code, wall_ms, ended.timed_out, ended.oom_killed, out.truncated,
                          err is not None and err is not out and err.truncated, ended.peak_ram_mib)

    def stop(self, name: str, grace_s: float) -> None:
        with self._lock:
            box = self._boxes.get(name)
        if box is not None:
            box.stopped.set()

    def remove(self, name: str) -> None:
        with self._lock:
            box = self._boxes.pop(name, None)
        if box is not None:
            box.stopped.set()

    def exists(self, name: str) -> bool:
        with self._lock:
            return name in self._boxes

    def list_sandboxes(self, job_id: int | None = None) -> list[str]:
        with self._lock:
            return [name for name, box in self._boxes.items()
                    if job_id is None or box.spec.labels.get("orch.job_id") == str(job_id)]

    def _compile(self, box: _Box, source: str, out: CappedFile, deadline: float) -> _Ended:
        program = (box.spec.job_dir / source).read_bytes()
        classes = ["Main"]
        for marker in _markers(program):
            match marker.action:
                case "compile-error":
                    out.write(marker.arg + b"\n")
                    return _Ended(1)
                case "compile-oom":
                    return _Ended(KILLED, oom_killed=True)
                case "compile-sleep":
                    ended = self._sleep(box, marker.arg, deadline)
                    if ended is not None:
                        return ended
                case "classes":
                    classes = marker.arg
        if source == JAVA:
            for name in classes:
                (box.spec.out_dir / f"{name}.class").write_bytes(program)
        elif source != PYTHON:
            (box.spec.out_dir / "prog").write_bytes(program)
        return _Ended(0)

    def _check(self, box: _Box, err: CappedFile | None) -> _Ended:
        main = box.spec.out_dir / "Main.class"
        said = _NO_CLASS
        if main.is_file():
            if not any(marker.action == "no-main" for marker in _markers(main.read_bytes())):
                return _Ended(0)
            said = _NO_MAIN
        if err is not None:
            err.write(said)
        return _Ended(1)

    def _run(self, box: _Box, argv: list[str], stdin: Path | None, out: CappedFile, err: CappedFile | None,
             deadline: float) -> _Ended:
        given = b"" if stdin is None else stdin.read_bytes()  # opened whatever the program does, as a real one is
        program = _program(box.spec, argv)
        if not program.is_file():
            if err is not None:
                err.write(_NO_CLASS if argv[0] == "java" else f"{argv[0]}: No such file or directory\n".encode())
            return _Ended(1 if argv[0] == "java" else NOT_FOUND)
        peak: int | None = None
        flooded = 0
        for marker in _for_case(_markers(program.read_bytes()), stdin):
            if self._clock.monotonic() > deadline:
                return _Ended(TIMED_OUT, timed_out=True, peak_ram_mib=peak)
            match marker.action:
                case "print":
                    out.write(marker.arg + b"\n")
                case "print-hex":
                    out.write(marker.arg)
                case "print-expected":
                    out.write(_expected(stdin))
                case "echo":
                    out.write(given)
                case "flood":
                    flooded += marker.arg
                    if flooded > FLOOD_MAX_BYTES:
                        raise SandboxError(f"FakeRunner: flood of more than {FLOOD_MAX_BYTES} bytes")
                    out.write(b"x" * marker.arg)
                case "stderr":
                    if err is not None:
                        err.write(marker.arg + b"\n")
                case "peak":
                    peak = marker.arg
                case "sleep":
                    ended = self._sleep(box, marker.arg, deadline)
                    if ended is not None:
                        return ended._replace(peak_ram_mib=peak)
                case "exit":
                    return _Ended(marker.arg, peak_ram_mib=peak)
                case "oom":
                    return _Ended(KILLED, oom_killed=True, peak_ram_mib=peak)
        return _Ended(0, peak_ram_mib=peak)

    def _sleep(self, box: _Box, seconds: float, deadline: float) -> _Ended | None:
        """None when the sleep ended by itself, a timeout when the command's time did. SandboxError when the
        sandbox was stopped or removed under it."""
        left = deadline - self._clock.monotonic()
        if self._clock.wait(box.stopped, min(seconds, left)):
            raise SandboxError(f"{box.spec.name} was stopped before the command ended")
        return _Ended(TIMED_OUT, timed_out=True) if seconds > left else None


def _markers(program: bytes) -> list[_Marker]:
    return [_marker(text) for text in _MARKER.findall(program)]


def _marker(text: bytes) -> _Marker:
    case: int | None = None
    words = text.split(None, 1)
    if words[:1] == [b"case"]:
        number, *rest = words[1].split(None, 1) if len(words) == 2 else [b""]
        if not number.isdigit() or len(number) > 3:  # test_cases.case_no is 1 to 100
            raise SandboxError("FakeRunner: case needs a number")
        case, words = int(number), rest[0].split(None, 1) if rest else []
    action = words[0].decode("utf-8", "replace") if words else ""
    parse = (_ACTIONS if case is None else _RUN).get(action)  # what is compiled is the same for every case
    if parse is None:
        raise SandboxError(f"FakeRunner: no marker {action!r}" + (" for one case" if case is not None else ""))
    try:
        return _Marker(case, action, parse(words[1].rstrip(_BLANKS) if len(words) == 2 else b""))
    except ValueError as exc:
        raise SandboxError(f"FakeRunner: {action}: {exc}") from exc


def _for_case(markers: list[_Marker], stdin: Path | None) -> list[_Marker]:
    """The run markers of the case that stdin belongs to (03.in), or the unscoped ones when it has none."""
    ours = [marker for marker in markers if marker.action in _RUN]
    case_no = int(stdin.stem) if stdin is not None and stdin.stem.isdecimal() else None
    scoped = [marker for marker in ours if marker.case is not None and marker.case == case_no]
    return scoped or [marker for marker in ours if marker.case is None]


def _expected(stdin: Path | None) -> bytes:
    """The fake program's oracle; a real one is never near it."""
    if stdin is None or not stdin.with_suffix(".out").is_file():
        raise SandboxError("FakeRunner: print-expected needs an input with an .out file beside it")
    return stdin.with_suffix(".out").read_bytes()


def _program(spec: SandboxSpec, argv: list[str]) -> Path:
    """The file on the host that argv runs: a class in /out for java, else the last argument in /out or /job."""
    if argv[0] == "java":
        return spec.out_dir / f"{argv[-1]}.class"
    last = argv[-1]
    for mount, folder in (("/out/", spec.out_dir), ("/job/", spec.job_dir)):
        if last.startswith(mount):
            return folder / last.removeprefix(mount)
    raise SandboxError(f"FakeRunner: cannot tell what {argv!r} runs")
