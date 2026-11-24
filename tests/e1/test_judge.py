"""Slice E1-1: the judge (plan 9.4 and 9.6, PRODUCT_SPEC section 2, D-142 and D-145 to D-148).

The oracle is written here from the plan and the spec, never computed from the code: the commands of each
language, the order TLE, RE, WA, AC, the verdict strings, and the rules of the plan review: bytes are compared,
an output cut at the cap is WA, a memory kill is no verdict, and an attempt reads only what it wrote itself.
"""
from __future__ import annotations

import dataclasses
import itertools
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.common.clock import FakeClock
from app.e1_execution.files import CaseFiles, RunFolder
from app.e1_execution.judge import (JAVA_CLASS_MESSAGE, LANGUAGES, PYTHON_CHECK, CaseResult, CaseVerdict, Judged,
                                    OutOfMemory, Program, case_verdict, exited, judge_cases, normalise)
from app.e1_execution.sandbox import ExecResult, SandboxError
from tests.e1.support import Attempt, Call, Recorder

Advancing = Callable[..., Any]
SOURCES = {"prog.c": "//", "prog.cpp": "//", "Main.java": "//", "prog.py": "#"}  # each with its comment sign
OK = ExecResult(0, 12, False, False, False)
CODE = {"prog.c": "int main(void) { return 0; }\n", "prog.cpp": "int main() { return 0; }\n",
        "Main.java": "public class Main { public static void main(String[] args) {} }\n", "prog.py": "pass\n"}


@pytest.fixture
def data(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def recorder(clock: FakeClock) -> Recorder:
    return Recorder(clock)


def program(source: str, *markers: str) -> str:
    return CODE[source] + "".join(f"{SOURCES[source]} fake: {marker}\n" for marker in markers)


# ---- comparing outputs ----------------------------------------------------------------------------------

@pytest.mark.parametrize("output, expected", [
    (b"7\n", b"7"), (b"7", b"7\n"), (b"", b""), (b"", b"\n\n"), (b" \t\r\n", b""),
    (b"7 \n", b"7\n"), (b"7\t\n", b"7\n"), (b"7 \t \r\n", b"7\n"), (b"7\r\n8\r\n", b"7\n8\n"), (b"7\n8\n", b"7\r\n8"),
    (b"7\n\n\n", b"7\n"), (b"7\n \t\n\r\n", b"7"),
    (b"\xff\xfe 1\n", b"\xff\xfe 1"), (b"a\x00b\n", b"a\x00b"),
])
def test_outputs_that_are_the_same(output: bytes, expected: bytes):
    """Trailing spaces, tabs and carriage returns of a line and trailing empty lines do not count (D-147)."""
    assert normalise(output) == normalise(expected)


@pytest.mark.parametrize("output, expected", [
    (b"7\n", b"8\n"), (b"7\n", b""), (b"7\n8\n", b"7\n"), (b"7\n", b"7\n8\n"), (b"8\n7\n", b"7\n8\n"), (b"a", b"A"),
    (b" 7\n", b"7\n"), (b"\n7\n", b"7\n"), (b"1\n\n2\n", b"1\n2\n"), (b"1  2\n", b"1 2\n"), (b"1\t2\n", b"1 2\n"),
    (b"1\r2\n", b"1\n2\n"), (b"1\r2\n", b"12\n"), (b"1\x0b2", b"1\n2"), (b"1\x0c", b"1"), (b"1\x00", b"1"),
    (b"\xff\n", b"\xfe\n"), (b"\xff\n", b"\xef\xbf\xbd\n"), (b"1\xa0\n", b"1\n"), (b"1\xc2\xa0\n", b"1\n"),
    (b"1\xe2\x80\xa82", b"1\n2"), (b"\xef\xbb\xbf1\n", b"1\n"),
])
def test_outputs_that_differ(output: bytes, expected: bytes):
    """Catches text compared after decoding (two invalid bytes both become U+FFFD), lines split where only text
    splits (a lone \\r, \\x0b, U+2028), and other blanks stripped (no-break space, form feed, leading space)."""
    assert normalise(output) != normalise(expected)


# ---- the verdict of one case ----------------------------------------------------------------------------

VERDICTS = {  # (timed out, exit code is not 0, truncated, differs)
    (False, False, False, False): "AC", (False, False, False, True): "WA",
    (False, False, True, False): "WA", (False, False, True, True): "WA",
    (False, True, False, False): "RE", (False, True, False, True): "RE",
    (False, True, True, False): "RE", (False, True, True, True): "RE",
    (True, False, False, False): "TLE", (True, False, False, True): "TLE",
    (True, False, True, False): "TLE", (True, False, True, True): "TLE",
    (True, True, False, False): "TLE", (True, True, False, True): "TLE",
    (True, True, True, False): "TLE", (True, True, True, True): "TLE",
}


@pytest.mark.parametrize("timed_out, failed, truncated, differs", list(itertools.product((False, True), repeat=4)))
def test_every_verdict(tmp_path: Path, timed_out: bool, failed: bool, truncated: bool, differs: bool):
    """Catches any other order than TLE, RE, WA, AC, and a truncated output taken for the whole one: its kept
    bytes equal the expected output here."""
    (tmp_path / "out").write_bytes(b"8\n" if differs else b"7\n")
    (tmp_path / "expected").write_bytes(b"7\n")
    result = ExecResult(139 if failed else 0, 12, timed_out, False, truncated)
    assert case_verdict(result, tmp_path / "out", tmp_path / "expected") == VERDICTS[timed_out, failed, truncated,
                                                                                    differs]


@pytest.mark.parametrize("timed_out, exit_code", list(itertools.product((False, True), (0, 137))))
def test_a_memory_kill_is_no_verdict(tmp_path: Path, timed_out: bool, exit_code: int):
    """Catches a killed case read as RE, TLE or even AC (a child was killed and the program ended with 0): the
    job fails with memory_limit instead (plan 9.6), so asking for a verdict is a bug and says so."""
    for name in ("out", "expected"):
        (tmp_path / name).write_bytes(b"7\n")
    with pytest.raises(ValueError, match="memory"):
        case_verdict(ExecResult(exit_code, 12, timed_out, True, False), tmp_path / "out", tmp_path / "expected")


def test_the_expected_output_is_not_needed_for_tle_or_re(tmp_path: Path):
    """The order again: no output is read when the program did not end by itself with 0."""
    missing = tmp_path / "missing"
    assert case_verdict(ExecResult(124, 1000, True, False, False), missing, missing) == "TLE"
    assert case_verdict(ExecResult(1, 12, False, False, False), missing, missing) == "RE"


# ---- the verdict of a job -------------------------------------------------------------------------------

def judged(run: RunFolder, folder: Path, verdicts: list[CaseVerdict]) -> Judged:
    """judge_cases() over cases that end with these verdicts, the cases in reverse order."""
    cases: list[CaseFiles] = []
    ran: dict[int, ExecResult] = {}
    for n, verdict in enumerate(verdicts, 1):
        (folder / f"{n}.out").write_bytes(b"7\n")
        run.case_output(n).write_bytes(b"8\n" if verdict == "WA" else b"7\n")
        cases.append(CaseFiles(n, folder / f"{n}.in", folder / f"{n}.out"))
        ran[n] = ExecResult(3 if verdict == "RE" else 0, 10 * n, verdict == "TLE", False, False)
    return judge_cases(run, cases[::-1], ran)


@pytest.mark.parametrize("verdicts, overall", [
    (["AC"] * 10, "Accepted 10/10"), (["AC"], "Accepted 1/1"),
    (["AC"] * 7 + ["WA"] * 3, "Wrong Answer 7/10"), (["WA"] + ["AC"] * 9, "Wrong Answer 9/10"),
    (["AC", "TLE", "WA", "AC"], "Time Limit Exceeded 2/4"), (["AC", "WA", "TLE", "RE"], "Wrong Answer 1/4"),
    (["RE", "RE", "RE"], "Runtime Error 0/3"), (["AC", "AC", "RE"], "Runtime Error 2/3"),
])
def test_the_overall_verdict(tmp_path: Path, verdicts: list[CaseVerdict], overall: str):
    """Catches the count of failed cases, a count that stops at the first failure, and the first non-AC case
    taken in the order of the list instead of the case numbers (plan 9.6)."""
    run = RunFolder(tmp_path)
    result = judged(run, tmp_path, verdicts)
    assert result.verdict == overall
    assert result.cases == tuple(CaseResult(n, verdict, 10 * n) for n, verdict in enumerate(verdicts, 1))


def test_a_verdict_needs_exactly_the_cases_that_ran(tmp_path: Path):
    """Catches a case that was not run judged by a file that happens to be there, and a problem without cases
    accepted 0/0."""
    run = RunFolder(tmp_path)
    (tmp_path / "1.out").write_bytes(b"7\n")
    run.case_output(1).write_bytes(b"7\n")
    case = CaseFiles(1, tmp_path / "1.in", tmp_path / "1.out")
    other = CaseFiles(2, tmp_path / "1.in", tmp_path / "1.out")
    for cases, ran in (([case], {}), ([case, other], {1: OK}), ([case], {1: OK, 2: OK}), ([case, case], {1: OK}),
                       ([], {})):
        with pytest.raises(ValueError):
            judge_cases(run, cases, ran)
    assert judge_cases(run, [case], {1: OK}) == Judged("Accepted 1/1", (CaseResult(1, "AC", 12),))


def test_an_output_that_was_not_captured_is_an_error(tmp_path: Path):
    """Catches a missing file read as an empty output (WA, or AC for an empty expected one)."""
    (tmp_path / "1.out").write_bytes(b"")
    with pytest.raises(FileNotFoundError):
        judge_cases(RunFolder(tmp_path), [CaseFiles(1, tmp_path / "1.in", tmp_path / "1.out")], {1: OK})


@pytest.mark.parametrize("code, text", [(0, "Exited 0"), (3, "Exited 3"), (139, "Exited 139")])
def test_a_run_without_cases_names_its_exit_code(code: int, text: str):
    assert exited(ExecResult(code, 12, False, False, True)) == text


@pytest.mark.parametrize("ran", [ExecResult(124, 9, True, False, False), ExecResult(0, 9, True, False, False),
                                 ExecResult(137, 9, False, True, False), ExecResult(0, 9, False, True, False)])
def test_a_run_that_did_not_end_by_itself_has_no_exit_code(ran: ExecResult):
    """Catches "Exited 124" for a job that hit its limit, and "Exited 137" for one that hit its memory: both fail
    the job (plan 9.6), so asking for a text is a bug and says so, as in case_verdict()."""
    with pytest.raises(ValueError):
        exited(ran)


# ---- the commands ---------------------------------------------------------------------------------------

GCC = ["gcc", "-O2", "-std=c17", "-o", "/out/prog", "/job/prog.c", "-lm"]
GPP = ["g++", "-O2", "-std=c++17", "-o", "/out/prog", "/job/prog.cpp"]
JAVAC = ["javac", "-encoding", "UTF-8", "-d", "/out", "/job/Main.java"]
JAVA = ["java", "-Xmx384m", "-Xss64m", "-XX:+UseSerialGC", "-cp", "/out", "Main"]
DRY_RUN = ["java", "-Xmx384m", "-Xss64m", "-XX:+UseSerialGC", "-cp", "/out", "--dry-run", "Main"]
PYTHON = ["python3", "-I", "-B", "/job/prog.py"]
SYNTAX = ["python3", "-I", "-B", "-c", PYTHON_CHECK, "/job/prog.py"]


@pytest.mark.parametrize("source, compiles, run, factor", [
    ("prog.c", [GCC], ["/out/prog"], 1), ("prog.cpp", [GPP], ["/out/prog"], 1),
    ("Main.java", [JAVAC, DRY_RUN], JAVA, 2), ("prog.py", [SYNTAX], PYTHON, 3),
])
def test_the_commands_of_each_language(data: Path, recorder: Recorder, source: str, compiles: list[list[str]],
                                       run: list[str], factor: int):
    """Plan 9.4 and PRODUCT_SPEC section 2, every argument of every command: the argv, the input, the file it is
    captured in, the timeout (COMPILE_TIMEOUT_S; the problem's limit times the language's factor) and the cap
    (8 KiB for the compile log, OUTPUT_CAP_BYTES for an output). A case's stderr is dropped."""
    attempt = Attempt(data, recorder, source, program(source, "print-expected"), ram_mib=512)
    cases = attempt.cases(b"7\n", b"8\n")
    assert attempt.judge(cases, time_limit_ms=1500).verdict == "Accepted 2/2"
    logs = [attempt.run.compile_log, attempt.run.main_check]
    assert recorder.calls == [
        *(Call(argv, None, log, 10.0, 8192, True, None) for argv, log in zip(compiles, logs, strict=False)),
        *(Call(run, case.input, attempt.run.case_output(case.case_no), 1.5 * factor, 65536, False, None)
          for case in cases)]
    assert all(type(part) is str for call in recorder.calls for part in call.argv)


@pytest.mark.parametrize("ram_mib, heap", [(64, 48), (100, 75), (511, 383), (512, 384), (117964, 88473)])
def test_the_jvm_heap_is_three_quarters_of_the_memory(data: Path, recorder: Recorder, ram_mib: int, heap: int):
    """PRODUCT_SPEC section 2: in whole MiB, rounded down (invariant 7)."""
    attempt = Attempt(data, recorder, "Main.java", program("Main.java"), ram_mib=ram_mib)
    assert attempt.program.compile()
    attempt.program.run_once(None, 1.0)
    for call in recorder.calls[1:]:
        assert [part for part in call.argv if part.startswith("-Xmx")] == [f"-Xmx{heap}m"]


@pytest.mark.parametrize("text, ok, said", [
    ("print(int(input()) * 2)\n", True, ""), ("x = 'é'\n", True, ""), ("", True, ""), ("﻿x = 1\n", True, ""),
    ("def f(:\n    pass\n", False, "SyntaxError"), ("if True:\nx = 1\n", False, "IndentationError"),
    ("return 5\n", False, "SyntaxError"), ("# -*- coding: ascii -*-\nx = 'é'\n", False, "SyntaxError"),
    ("import os\nos.mkdir('ran')\n", True, ""), ("raise SystemExit(3)\n", True, ""),
])
def test_the_python_check_compiles_and_runs_nothing(tmp_path: Path, text: str, ok: bool, said: str):
    """The real check, on this Python: a syntax error is refused with the error alone (no frame of the check
    itself), everything else passes, and the program is never run and nothing is written."""
    path = tmp_path / "prog.py"
    path.write_bytes(text.encode())
    done = subprocess.run([sys.executable, *SYNTAX[1:-1], str(path)], cwd=tmp_path, capture_output=True, check=False)
    assert (done.returncode == 0) is ok and done.stdout == b""
    assert said.encode() in done.stderr and b"Traceback" not in done.stderr and (b"prog.py" in done.stderr) is not ok
    assert [p.name for p in tmp_path.iterdir()] == ["prog.py"]


@pytest.mark.parametrize("where", ["missing", "folder"])
def test_the_python_check_tells_a_file_it_cannot_read_apart(tmp_path: Path, where: str):
    """Catches a fault of the server (a mount, a permission) told to the student as a syntax error: 2, as for an
    option Python does not know, and a message that names the file."""
    path = tmp_path / "prog.py"
    if where == "folder":
        path.mkdir()
    done = subprocess.run([sys.executable, *SYNTAX[1:-1], str(path)], capture_output=True, check=False)
    assert done.returncode == 2 and done.stdout == b"" and b"prog.py" in done.stderr


def test_no_expected_output_reaches_the_sandbox(data: Path, recorder: Recorder):
    """Expected outputs stay on the host: no command, input or captured file is one, nothing holds one in the
    mounted folders, and a Program has nowhere to keep one."""
    attempt = Attempt(data, recorder, "prog.c", program("prog.c", "print 1"))
    cases = attempt.cases(b"the secret answer\n", b"the secret answer\n")
    attempt.judge(cases)
    expected = {str(case.expected) for case in cases}
    for call in recorder.calls:
        assert not expected & {*call.argv, str(call.stdin), str(call.stdout), str(call.stderr)}
    for folder in (attempt.run.out, attempt.payload.folder):
        assert not any(b"secret" in p.read_bytes() for p in folder.rglob("*") if p.is_file())
    assert {f.name for f in dataclasses.fields(Program)} == {"runner", "name", "source", "ram_mib", "run",
                                                             "compile_timeout_s", "output_cap_bytes"}


# ---- judging on the fake sandbox ------------------------------------------------------------------------

@pytest.mark.parametrize("source", SOURCES)
def test_accepted_in_every_language(data: Path, recorder: Recorder, source: str):
    attempt = Attempt(data, recorder, source, program(source, "print-expected"))
    result = attempt.judge(attempt.cases(*(b"%d\n" % n for n in range(10))))
    assert result.verdict == "Accepted 10/10" and [row.verdict for row in result.cases] == ["AC"] * 10


def test_every_case_runs_even_after_one_failed(data: Path, recorder: Recorder, advancing: Advancing):
    """Catches a judge that stops at the first failure: FR-09 wants a verdict for every case."""
    attempt = Attempt(data, recorder, "prog.c", program("prog.c", "print-expected", "case 2 print wrong",
                                                        "case 4 exit 3", "case 5 sleep 30", "case 6 flood 70000"))
    cases = attempt.cases(*[b"7\n"] * 7)
    result = advancing(lambda: attempt.judge(cases, time_limit_ms=1000), 1.0)
    assert result.verdict == "Wrong Answer 3/7"
    assert result.cases == (CaseResult(1, "AC", 0), CaseResult(2, "WA", 0), CaseResult(3, "AC", 0),
                            CaseResult(4, "RE", 0), CaseResult(5, "TLE", 1000), CaseResult(6, "WA", 0),
                            CaseResult(7, "AC", 0))


@pytest.mark.parametrize("source, limit_s", [("prog.c", 1.0), ("prog.cpp", 1.0), ("Main.java", 2.0), ("prog.py", 3.0)])
def test_the_limit_is_scaled_and_the_time_is_not(data: Path, recorder: Recorder, advancing: Advancing, source: str,
                                                 limit_s: float):
    """PRODUCT_SPEC section 2: x1, x1, x2, x3. Catches the stored time divided by the factor, and a factor that
    makes an integer of the limit."""
    attempt = Attempt(data, recorder, source, program(source, "print-expected", "case 2 sleep 30",
                                                      f"case 1 sleep {limit_s - 0.5}"))
    cases = attempt.cases(b"", b"7\n")
    result = advancing(lambda: attempt.judge(cases, time_limit_ms=1000), limit_s - 0.5, limit_s)
    assert result.cases == (CaseResult(1, "AC", round((limit_s - 0.5) * 1000)),
                            CaseResult(2, "TLE", round(limit_s * 1000)))


def test_an_output_that_is_not_utf8_is_compared_as_bytes(data: Path, recorder: Recorder):
    attempt = Attempt(data, recorder, "prog.c", program("prog.c", "print-hex fffe0a"))
    assert attempt.judge(attempt.cases(b"\xff\xfe\n", b"\xfe\xff\n")).verdict == "Wrong Answer 1/2"


def test_an_output_cut_at_the_cap_is_wrong(data: Path, recorder: Recorder):
    """Plan 9.6: the kept part equals the expected output here, and it is still WA."""
    attempt = Attempt(data, recorder, "prog.c", program("prog.c", "flood 11", "case 1 flood 10"), cap=10)
    result = attempt.judge(attempt.cases(b"x" * 10, b"x" * 10))
    assert [row.verdict for row in result.cases] == ["AC", "WA"]


# ---- compiling ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("source", SOURCES)
def test_a_compile_error_is_the_verdict_and_nothing_runs(data: Path, recorder: Recorder, source: str):
    attempt = Attempt(data, recorder, source, program(source, "compile-error line 1: what is this", "print-expected"))
    assert attempt.judge(attempt.cases(b"7\n")) == Judged("Compilation Error", ())
    assert len(recorder.calls) == 1
    assert attempt.run.compile_log.read_bytes() == b"line 1: what is this\n"


def test_a_compile_that_takes_too_long_is_a_compile_error(data: Path, recorder: Recorder, advancing: Advancing):
    """Catches a compiler that was stopped reported as a success or with an empty message."""
    attempt = Attempt(data, recorder, "prog.c", program("prog.c", "compile-sleep 30", "print-expected"))
    assert advancing(lambda: attempt.judge(attempt.cases(b"7\n")), 10.0) == Judged("Compilation Error", ())
    assert attempt.run.compile_log.read_bytes() == b"The compiler was stopped after 10 s\n"


@pytest.mark.parametrize("printed", [b"prog.c:1: warning: slow\n", b"prog.c:1: warning: sl"])
def test_the_note_of_a_stopped_compiler_follows_what_it_printed(data: Path, recorder: Recorder, printed: bytes):
    """Catches the compiler's own words lost, and the note glued to a line it had not finished."""
    recorder.answer = lambda call: (ExecResult(124, 10000, True, False, False), printed)
    attempt = Attempt(data, recorder, "prog.c", program("prog.c"))
    assert attempt.program.compile() is False
    assert attempt.run.compile_log.read_bytes().splitlines() == [printed.strip(), b"The compiler was stopped after 10 s"]


@pytest.mark.parametrize("code", [125, 126, 127, 137, 143])
@pytest.mark.parametrize("source", SOURCES)
def test_a_compiler_that_did_not_run_is_the_servers_error(data: Path, recorder: Recorder, source: str, code: int):
    """Catches a wrong image (126, 127), a broken exec (125) or a container killed from outside (137, 143) told to
    the student as Compilation Error with an empty reason: the sandbox failed, and that is no verdict."""
    recorder.answer = lambda call: (ExecResult(code, 5, False, False, False), b"exec failed: no such file")
    attempt = Attempt(data, recorder, source, program(source))
    with pytest.raises(SandboxError, match=rf"{LANGUAGES[source].compile[0].replace('+', '.')}.* {code}.*exec failed"):
        attempt.program.compile()
    assert len(recorder.calls) == 1


@pytest.mark.parametrize("source, code, fault", [
    ("Main.java", 2, True), ("prog.py", 2, True), ("prog.c", 2, False), ("prog.cpp", 2, False), ("prog.c", 1, False),
    ("prog.c", 4, False), ("prog.cpp", 139, False), ("Main.java", 1, False), ("Main.java", 3, False),
    ("Main.java", 4, False), ("prog.py", 1, False), ("prog.py", 139, False),
])
def test_what_the_exit_code_of_a_compile_step_says(data: Path, recorder: Recorder, source: str, code: int, fault: bool):
    """javac and the Python check end with 2 for an option or a file they cannot use (checked on JDK 17), which no
    source causes; every other code is about the source, a crash of the compiler on it included."""
    recorder.answer = lambda call: (ExecResult(code, 5, False, False, False), b"said")
    attempt = Attempt(data, recorder, source, program(source))
    if fault:
        with pytest.raises(SandboxError, match="said"):
            attempt.program.compile()
    else:
        assert attempt.program.compile() is False
        assert attempt.run.compile_log.read_bytes() == b"said"


def test_a_stopped_compiler_is_stopped_whatever_its_exit_code(data: Path, recorder: Recorder):
    """The timeout's own kill ends with 137: the timeout is told apart first."""
    recorder.answer = lambda call: ExecResult(137, 11000, True, False, False)
    attempt = Attempt(data, recorder, "prog.c", program("prog.c"))
    assert attempt.program.compile() is False
    assert attempt.run.compile_log.read_bytes() == b"The compiler was stopped after 10 s\n"


@pytest.mark.parametrize("code", [1, 139])
def test_a_compile_error_always_has_a_reason(data: Path, recorder: Recorder, code: int):
    """Catches a Compilation Error with an empty compile.txt: a compiler that crashed may have said nothing."""
    recorder.answer = lambda call: ExecResult(code, 5, False, False, False)
    attempt = Attempt(data, recorder, "prog.c", program("prog.c"))
    assert attempt.program.compile() is False
    assert attempt.run.compile_log.read_bytes() == f"The compiler ended with exit code {code}\n".encode()


@pytest.mark.parametrize("code, compiled", [(0, True), (1, False)])
def test_a_compile_log_that_was_cut_says_so(data: Path, recorder: Recorder, code: int, compiled: bool):
    """Catches a cut log shown as the whole one."""
    recorder.answer = lambda call: (ExecResult(code, 5, False, False, True), b"x" * 8192)
    attempt = Attempt(data, recorder, "prog.c", program("prog.c"))
    assert attempt.program.compile() is compiled
    assert attempt.run.compile_log.read_bytes() == b"x" * 8192 + b"\nThe compiler's output was cut at 8192 bytes\n"


def test_a_compiler_out_of_memory_is_no_compile_error(data: Path, recorder: Recorder):
    """Plan 9.6: the memory limit fails the job, in every step."""
    attempt = Attempt(data, recorder, "prog.c", program("prog.c", "compile-oom"))
    with pytest.raises(OutOfMemory) as caught:
        attempt.program.compile()
    assert caught.value.stage == "compile"


# ---- class Main (D-146) ---------------------------------------------------------------------------------

@pytest.mark.parametrize("declaration", [
    "public class Main {", "public final class Main {", "public\tclass\tMain {", "class Main extends Base {",
    "public class/* the one */Main{", "strictfp class\n  Main\n{", "/* class Other */ public record Main() {",
], ids=["plain", "final", "tabs", "not public", "comment", "lines", "record"])
def test_a_class_main_is_found_however_it_is_declared(data: Path, recorder: Recorder, declaration: str):
    """Catches a check on the source text: what javac wrote decides, and it wrote a Main that can be run."""
    attempt = Attempt(data, recorder, "Main.java", f"{declaration} }}\n// fake: print-expected\n")
    assert attempt.judge(attempt.cases(b"7\n")).verdict == "Accepted 1/1"


@pytest.mark.parametrize("text, launcher", [
    ("class Solution { } // public class Main\n// fake: classes Solution\n", b"Could not find or load main class Main"),
    ("class Solution { String s = \"public class Main {\"; }\n// fake: classes Solution\n", b"Could not find"),
    ("package app; public class Main { }\n// fake: classes app.Main\n", b"Could not find or load main class Main"),
    ("public class Main { }\n// fake: no-main\n", b"Main method not found in class Main"),
    ("public class Main { void main() { } } // public static void main(String[] args)\n// fake: no-main\n",
     b"Main method not found"),
], ids=["no Main", "Main in a string", "Main in a package", "no main()", "main() in a comment"])
def test_without_a_class_main_that_can_run_it_is_a_compile_error(data: Path, recorder: Recorder, text: str,
                                                                 launcher: bytes):
    """PRODUCT_SPEC section 2. Catches a check on the source text (the words are there, in a comment or a string)
    and one on the class file's name alone (Main without main()). The launcher's own words follow the message."""
    attempt = Attempt(data, recorder, "Main.java", text + "// fake: print-expected\n")
    assert attempt.judge(attempt.cases(b"7\n")) == Judged("Compilation Error", ())
    log = attempt.run.compile_log.read_bytes()
    assert log.startswith(b"Java submissions must declare class Main\n") and launcher in log
    assert [call.argv[0] for call in recorder.calls] == ["javac", "java"]  # compiled and checked, never run


def test_the_message_is_the_specs():
    assert JAVA_CLASS_MESSAGE == "Java submissions must declare class Main"


def test_a_failed_javac_is_not_checked_for_main(data: Path, recorder: Recorder):
    """Catches the class message put on top of javac's own errors."""
    attempt = Attempt(data, recorder, "Main.java", program("Main.java", "compile-error Main.java:1: error: ';' expected"))
    assert attempt.judge(attempt.cases(b"7\n")) == Judged("Compilation Error", ())
    assert attempt.run.compile_log.read_bytes() == b"Main.java:1: error: ';' expected\n"


def test_a_check_that_is_stopped_says_so(data: Path, recorder: Recorder):
    """Catches the class message for a launcher that only ran out of time."""
    attempt = Attempt(data, recorder, "Main.java", program("Main.java"))
    recorder.answer = lambda call: ExecResult(124, 10000, True, False, False) if "--dry-run" in call.argv else None
    assert attempt.program.compile() is False
    assert attempt.run.compile_log.read_bytes() == b"The compiler was stopped after 10 s\n"


def test_a_check_out_of_memory_is_no_compile_error(data: Path, recorder: Recorder):
    attempt = Attempt(data, recorder, "Main.java", program("Main.java"))
    recorder.answer = lambda call: ExecResult(1, 5, False, True, False) if "--dry-run" in call.argv else None
    with pytest.raises(OutOfMemory) as caught:
        attempt.program.compile()
    assert caught.value.stage == "compile"


@pytest.mark.parametrize("code, said", [
    (1, b"Error occurred during initialization of VM\nToo small maximum heap\n"),
    (1, b"Unrecognized option: --dry-run\nError: Could not create the Java Virtual Machine.\n"),
    (1, b"Error: LinkageError occurred while loading main class Main\n\tjava.lang.UnsupportedClassVersionError: Main"),
    (2, b""), (126, b""), (127, b"java: not found"), (134, b"# A fatal error has been detected"), (137, b""), (143, b""),
])
def test_a_jvm_that_cannot_start_is_not_a_missing_class(data: Path, recorder: Recorder, code: int, said: bytes):
    """Catches a bad image or JVM setting that tells every student to declare class Main (texts and exit codes of
    JDK 17, run here): the launcher never got to look at Main."""
    recorder.answer = lambda call: ((ExecResult(code, 5, False, False, False), said) if "--dry-run" in call.argv
                                    else None)
    attempt = Attempt(data, recorder, "Main.java", program("Main.java"))
    with pytest.raises(SandboxError, match=rf"{code}"):
        attempt.program.compile()


@pytest.mark.parametrize("source", ["prog.c", "prog.cpp", "prog.py"])
def test_only_java_is_checked_for_main(data: Path, recorder: Recorder, source: str):
    attempt = Attempt(data, recorder, source, program(source, "no-main"))
    assert attempt.program.compile() is True and len(recorder.calls) == 1


# ---- the memory limit -----------------------------------------------------------------------------------

@pytest.mark.parametrize("exit_code, timed_out", [(137, False), (0, False), (1, False), (124, True)])
def test_a_case_out_of_memory_stops_the_run(data: Path, recorder: Recorder, exit_code: int, timed_out: bool):
    """Plan 9.6: never RE, TLE or AC, also when the program itself survived (only a child was killed) or its time
    ran out too. The worker stops there: no later case runs."""
    attempt = Attempt(data, recorder, "prog.c", program("prog.c", "print-expected"))
    cases = attempt.cases(*[b"7\n"] * 5)
    recorder.answer = lambda call: (ExecResult(exit_code, 9, timed_out, True, False)
                                    if call.stdin == cases[2].input else None)
    with pytest.raises(OutOfMemory) as caught:
        attempt.judge(cases)
    assert caught.value.stage == "case 3"
    assert [call.stdin for call in recorder.calls[1:]] == [case.input for case in cases[:3]]


def test_a_single_run_out_of_memory_stops_too(data: Path, recorder: Recorder):
    attempt = Attempt(data, recorder, "prog.c", program("prog.c", "print before", "oom"))
    assert attempt.program.compile()
    with pytest.raises(OutOfMemory) as caught:
        attempt.program.run_once(None, 5.0)
    assert caught.value.stage == "run" and attempt.run.stdout.read_bytes() == b"before\n"


# ---- a run without test cases ---------------------------------------------------------------------------

def test_a_run_without_cases_is_captured_whole(data: Path, recorder: Recorder):
    """Plan 9.4: the non-judged types run once with no input; stdout and stderr are kept apart, each capped."""
    attempt = Attempt(data, recorder, "prog.c", program("prog.c", "print out", "stderr err", "peak 12", "exit 3"))
    assert attempt.program.compile()
    assert attempt.program.run_once(None, 60.0) == ExecResult(3, 0, False, False, False, False, 12)
    assert attempt.run.stdout.read_bytes() == b"out\n" and attempt.run.stderr.read_bytes() == b"err\n"
    assert recorder.calls[1] == Call(["/out/prog"], None, attempt.run.stdout, 60.0, 65536, False, attempt.run.stderr)


# ---- an existing run folder -----------------------------------------------------------------------------

@pytest.mark.parametrize("source, binary", [("prog.c", "prog"), ("Main.java", "Main.class")])
def test_a_stale_run_folder_never_produces_a_verdict(data: Path, recorder: Recorder, source: str, binary: str):
    """Catches an older database's run of the same job id that speaks for this one: its binary prints the right
    answer, its 01.txt holds it, and this attempt's program prints nothing (and, for Java, has no class Main)."""
    stale = data / "runs" / "1042" / "a1"
    (stale / "out").mkdir(parents=True)
    (stale / "out" / binary).write_bytes(b"// fake: print-expected\n")
    (stale / "01.txt").write_bytes(b"7\n")
    attempt = Attempt(data, recorder, source, program(source, *(["classes Other"] if source == "Main.java" else [])))
    result = attempt.judge(attempt.cases(b"7\n"))
    assert result.verdict == ("Compilation Error" if source == "Main.java" else "Wrong Answer 0/1")


def test_a_second_run_of_a_case_is_refused(data: Path, recorder: Recorder):
    """Catches an output file opened again: every captured file is made new, so what an attempt reads is what it
    wrote once."""
    attempt = Attempt(data, recorder, "prog.c", program("prog.c", "print-expected"))
    cases = attempt.cases(b"7\n")
    assert attempt.judge(cases).verdict == "Accepted 1/1"
    with pytest.raises(FileExistsError):
        attempt.program.run_case(1, cases[0].input, 1000)


def test_the_program_cannot_change_what_is_judged(data: Path, recorder: Recorder):
    """R1: whatever the program does in /out, the captured outputs lie outside it. Here it leaves a file named as
    the next case's output there, with the right answer."""
    attempt = Attempt(data, recorder, "prog.c", program("prog.c", "print wrong"))
    cases = attempt.cases(b"7\n", b"7\n")
    real = recorder.answer

    def answer(call: Call) -> Any:
        for n in (1, 2):
            (attempt.run.out / f"{n:02d}.txt").write_bytes(b"7\n")
        return real(call)

    recorder.answer = answer
    assert attempt.judge(cases).verdict == "Wrong Answer 0/2"


def test_every_language_has_a_stored_name_that_compiles():
    assert set(LANGUAGES) == set(SOURCES)
