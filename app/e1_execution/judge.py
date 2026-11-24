"""The judge (plan 9.4 and 9.6, D-142): runs a program through any SandboxRunner and turns the results into
verdicts. E1-2 uses the two halves in two places.

The worker builds a Program and calls compile(), then run_case() for every test case or run_once(); it reports raw
results and never sees an expected output. OutOfMemory from any of them ends the attempt: a memory kill fails the
job and is no verdict. The supervisor, once the stop is confirmed, calls judge_cases() on those results, which
compares the captured outputs with the expected ones on the host, or write_result() for a practice run.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, NamedTuple

from app.common.logging import log
from app.e1_execution.files import CaseFiles, RunFolder
from app.e1_execution.sandbox import ExecResult, SandboxError, SandboxRunner

CaseVerdict = Literal["AC", "WA", "TLE", "RE"]  # test_results.verdict
_NAMES: Final[Mapping[CaseVerdict, str]] = {
    "AC": "Accepted", "WA": "Wrong Answer", "TLE": "Time Limit Exceeded", "RE": "Runtime Error"}
COMPILATION_ERROR: Final = "Compilation Error"
JAVA_CLASS_MESSAGE: Final = "Java submissions must declare class Main"  # PRODUCT_SPEC section 2
COMPILE_CAP_BYTES: Final = 8192  # of compile.txt (plan 9.4)
_TRAILING: Final = b" \t\r"  # blanks that do not count at the end of a line (D-147)

JAVA: Final = "Main.java"
PYTHON: Final = "prog.py"
# The Python syntax check (D-145): compiles the file without running it and prints only the error; 2 for a file it
# cannot read, as Python itself ends with for an option it does not know.
PYTHON_CHECK: Final = """\
import sys, traceback
try:
    source = open(sys.argv[1], "rb").read()
except OSError as error:
    print(error, file=sys.stderr)
    sys.exit(2)
try:
    compile(source, sys.argv[1], "exec")
except Exception as error:
    sys.exit("".join(traceback.format_exception_only(error)))
"""
# Exit codes of a compile step that no source causes: the command could not be started (125 to 127), or something
# outside killed it (137, 143). The step's own timeout and a memory kill are told apart before these are looked at.
_NO_SOURCE: Final = frozenset({125, 126, 127, 137, 143})
# What a JVM says that cannot start, so that its launcher never looked at class Main; none of it is translated.
_NO_JVM: Final = (b"initialization of VM", b"Could not create the Java Virtual Machine", b"UnsupportedClassVersionError")


@dataclass(frozen=True)
class Language:
    compile: tuple[str, ...]  # for Python a syntax check
    time_factor: int          # scales problems.time_limit_ms (D-85)
    faults: frozenset[int] = _NO_SOURCE  # exit codes of compile that are the sandbox's failure, not a compile error


# By the stored name of the source (D-76), mounted at /job; what the compile step writes goes to /out. Always
# lists of strings, never a shell.
LANGUAGES: Final[Mapping[str, Language]] = {
    "prog.c": Language(("gcc", "-O2", "-std=c17", "-o", "/out/prog", "/job/prog.c", "-lm"), 1),
    "prog.cpp": Language(("g++", "-O2", "-std=c++17", "-o", "/out/prog", "/job/prog.cpp"), 1),
    JAVA: Language(("javac", "-encoding", "UTF-8", "-d", "/out", "/job/Main.java"), 2, _NO_SOURCE | {2}),
    PYTHON: Language(("python3", "-I", "-B", "-c", PYTHON_CHECK, "/job/prog.py"), 3, _NO_SOURCE | {2}),
}


def run_command(source: str, ram_mib: int) -> list[str]:
    """The command that runs a compiled source. The JVM heap is three quarters of ram_mib; the serial collector
    keeps the JVM to few threads, which count under the pids limit (D-85, D-100, D-145)."""
    if source == JAVA:
        return ["java", f"-Xmx{ram_mib * 3 // 4}m", "-Xss64m", "-XX:+UseSerialGC", "-cp", "/out", "Main"]
    if source == PYTHON:
        return ["python3", "-I", "-B", "/job/prog.py"]
    return ["/out/prog"]


def main_check_command(ram_mib: int) -> list[str]:
    """The run command of a Java program, which the JVM launcher only checks: it loads Main from what javac wrote,
    looks for a main method it can start, and exits 0 without running anything, or not 0 (D-146)."""
    *options, main = run_command(JAVA, ram_mib)
    return [*options, "--dry-run", main]


class OutOfMemory(Exception):
    """The memory limit was hit in this stage: "compile", "case 3" or "run". E1-2 fails the job with memory_limit
    (plan 9.6)."""

    def __init__(self, stage: str) -> None:
        super().__init__(f"the memory limit was hit: {stage}")
        self.stage = stage


@dataclass(frozen=True)
class Program:
    """One attempt's program in its started sandbox: the worker's half. It knows no expected output."""
    runner: SandboxRunner
    name: str                  # of the sandbox
    source: str                # a key of LANGUAGES
    ram_mib: int
    run: RunFolder
    compile_timeout_s: float   # COMPILE_TIMEOUT_S, for each compile step
    output_cap_bytes: int      # OUTPUT_CAP_BYTES, for each captured output

    def compile(self) -> bool:
        """The compile step, and for Java the check that class Main can be run. False is a compile error, its
        reason in compile.txt, which is never empty then. SandboxError for what no source causes: a compiler or a
        JVM that cannot be started, or that something outside killed."""
        language = LANGUAGES[self.source]
        step = self._compile_step(list(language.compile), self.run.compile_log)
        if not step.timed_out:
            if step.exit_code in language.faults:
                raise _fault(language.compile[0], step, self.run.compile_log)
            if step.exit_code == 0 and self.source == JAVA:
                step = self._compile_step(main_check_command(self.ram_mib), self.run.main_check)
                if step.exit_code != 0 and not step.timed_out:
                    said = self.run.main_check.read_bytes()
                    if step.exit_code != 1 or any(text in said for text in _NO_JVM):
                        raise _fault("java --dry-run", step, self.run.main_check)
                    self.run.compile_log.write_bytes(JAVA_CLASS_MESSAGE.encode() + b"\n" + said)
                    return False
        if step.timed_out:
            self._note(f"The compiler was stopped after {self.compile_timeout_s:g} s")
            return False
        if step.output_truncated:
            self._note(f"The compiler's output was cut at {COMPILE_CAP_BYTES} bytes")
        if step.exit_code != 0 and self.run.compile_log.stat().st_size == 0:
            self._note(f"The compiler ended with exit code {step.exit_code}")
        return step.exit_code == 0

    def run_case(self, case_no: int, stdin: Path, time_limit_ms: int) -> ExecResult:
        """One test case, with the problem's limit scaled for the language. Its output is captured as NN.txt; its
        stderr is dropped."""
        timeout_s = time_limit_ms * LANGUAGES[self.source].time_factor / 1000
        return self._exec(f"case {case_no}", run_command(self.source, self.ram_mib), stdin,
                          self.run.case_output(case_no), timeout_s, self.output_cap_bytes)

    def run_once(self, stdin: Path | None, timeout_s: float) -> ExecResult:
        """The one run of a job without test cases; stdout.txt and stderr.txt keep what it prints. timeout_s is
        the job's time limit, behind the supervisor's deadline: a result that timed out ends the job as the
        deadline does."""
        return self._exec("run", run_command(self.source, self.ram_mib), stdin, self.run.stdout, timeout_s,
                          self.output_cap_bytes, stderr=self.run.stderr)

    def _compile_step(self, argv: list[str], output: Path) -> ExecResult:
        return self._exec("compile", argv, None, output, self.compile_timeout_s, COMPILE_CAP_BYTES, merge_stderr=True)

    def _note(self, text: str) -> None:
        """A line of ours at the end of compile.txt."""
        printed = self.run.compile_log.read_bytes()
        unfinished = b"\n" if printed and not printed.endswith(b"\n") else b""
        self.run.compile_log.write_bytes(printed + unfinished + text.encode() + b"\n")

    def _exec(self, stage: str, argv: list[str], stdin: Path | None, stdout: Path, timeout_s: float, cap_bytes: int,
              merge_stderr: bool = False, stderr: Path | None = None) -> ExecResult:
        result = self.runner.exec(self.name, argv, stdin, stdout, timeout_s, cap_bytes, merge_stderr, stderr)
        if result.oom_killed:
            raise OutOfMemory(stage)
        return result


def _fault(command: str, step: ExecResult, output: Path) -> SandboxError:
    said = output.read_bytes()[:500].decode("utf-8", errors="replace")
    return SandboxError(f"{command} ended with exit code {step.exit_code}, which no source causes: {said}")


class CaseResult(NamedTuple):
    """One test_results row of an attempt."""
    case_no: int
    verdict: CaseVerdict
    time_ms: int


@dataclass(frozen=True)
class Judged:
    verdict: str                      # attempts.verdict, for example "Wrong Answer 7/10"
    cases: tuple[CaseResult, ...]     # by case_no; none when nothing compiled


def normalise(data: bytes) -> list[bytes]:
    """The lines that are compared: bytes, never text, without the blanks at their ends and without the empty
    lines at the end (plan 9.6, D-147)."""
    lines = [line.rstrip(_TRAILING) for line in data.split(b"\n")]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def case_verdict(result: ExecResult, output: Path, expected: Path) -> CaseVerdict:
    """TLE, then RE, then WA (an output cut at the cap included), then AC (plan 9.6)."""
    if result.oom_killed:
        raise ValueError("a memory kill is no verdict: it fails the job with memory_limit")
    if result.timed_out:
        return "TLE"
    if result.exit_code != 0:
        return "RE"
    if result.output_truncated or normalise(output.read_bytes()) != normalise(expected.read_bytes()):
        return "WA"
    return "AC"


def judge_cases(run: RunFolder, cases: Sequence[CaseFiles], ran: Mapping[int, ExecResult]) -> Judged:
    """The verdict of every case, and of the job: Accepted, or the first other verdict by case number, with the
    count that passed. ran holds the worker's results by case number and must cover exactly cases, so no verdict
    comes from a case that this attempt did not run."""
    if not cases or len(ran) != len(cases) or ran.keys() != {case.case_no for case in cases}:
        raise ValueError(f"results for cases {sorted(ran)}, but the problem has {[case.case_no for case in cases]}")
    rows = tuple(CaseResult(case.case_no, case_verdict(ran[case.case_no], run.case_output(case.case_no), case.expected),
                            ran[case.case_no].wall_ms)
                 for case in sorted(cases, key=lambda case: case.case_no))
    failed: list[CaseVerdict] = [row.verdict for row in rows if row.verdict != "AC"]
    return Judged(f"{_NAMES[failed[0] if failed else 'AC']} {len(rows) - len(failed)}/{len(rows)}", rows)


def exited(ran: ExecResult) -> str:
    """attempts.verdict of a run without test cases, which ended by itself."""
    if ran.timed_out or ran.oom_killed:
        raise ValueError("a run that hit its time or memory limit fails the job: it has no exit code to show")
    return f"Exited {ran.exit_code}"


def write_result(run: RunFolder, compiled: bool | None, ran: ExecResult | None, output_cap_bytes: int) -> None:
    """The result file of a practice run (code_run; D-96, D-150): runs/<job_id>/a<n>/result.json, which E4-3 serves
    as text. The supervisor calls it after the confirmed stop and before the transition. compiled is None when the
    attempt was stopped before the compile step ended; ran is None when the program did not run to its end (a
    compile error, the deadline, the memory limit): what it had printed by then is still shown, as cut when the
    captured file is full. The file appears whole: it is written beside and renamed."""
    if ran is not None and (ran.oom_killed or compiled is not True):
        raise ValueError("no program runs that did not compile, and a memory kill is no run that ended")
    ended = ran if ran is not None and not ran.timed_out else None
    compile_output = _read(run.compile_log, compiled is not None)
    stdout, stderr = (_read(path, ran is not None) for path in (run.stdout, run.stderr))
    content = {
        "compiled": compiled,
        "compile_output": _text(compile_output),
        "exit_code": ended.exit_code if ended else None,
        "time_ms": ended.wall_ms if ended else None,
        "peak_ram_mib": ended.peak_ram_mib if ended else None,
        "stdout": _text(stdout),
        "stdout_truncated": ran.output_truncated if ran else len(stdout) >= output_cap_bytes,
        "stderr": _text(stderr),
        "stderr_truncated": ran.stderr_truncated if ran else len(stderr) >= output_cap_bytes,
    }
    part = run.result.with_suffix(".part")
    file = open(part, "xb")  # one that is there is another call's: FileExistsError, and it is left alone
    try:
        with file:
            file.write(json.dumps(content).encode())
        os.replace(part, run.result)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            log.exception("result_cleanup_failed", engine="E1", path=str(part))  # the first error is in its traceback
        raise


def _read(path: Path, needed: bool) -> bytes:
    """A captured file. One that is needed is an error when it is not there; any other may be missing, because the
    attempt ended before it was made."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        if needed:
            raise
        return b""


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")
