"""Slice E1-1: a practice run (code_run; D-96, D-150): one run with input.txt as stdin, and the result file that
E4-3 serves from the attempt's folder.

The oracle is PRODUCT_SPEC section 7 (output, stderr, exit code, time and memory of one run in any of the four
languages, no verdict) and the fields of the approved plan. The file is read back as JSON, never through the code.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.common.clock import FakeClock
from app.e1_execution import judge
from app.e1_execution.judge import COMPILATION_ERROR, OutOfMemory, exited, write_result
from app.e1_execution.sandbox import ExecResult
from tests.e1.support import Attempt, Recorder
from tests.helpers import logged

COMMENT = {"prog.c": "//", "prog.cpp": "//", "Main.java": "//", "prog.py": "#"}
FIELDS = ["compiled", "compile_output", "exit_code", "time_ms", "peak_ram_mib", "stdout", "stdout_truncated", "stderr",
          "stderr_truncated"]


@pytest.fixture
def data(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def recorder(clock: FakeClock) -> Recorder:
    return Recorder(clock)


def practice(data: Path, recorder: Recorder, source: str, *markers: str, stdin: bytes = b"", cap: int = 65536) -> Attempt:
    """An attempt of a code_run job whose program holds these markers, and whose input is stdin."""
    text = "".join(f"{COMMENT[source]} fake: {marker}\n" for marker in markers)
    return Attempt(data, recorder, source, text, stdin=stdin, cap=cap)


def finish(attempt: Attempt) -> dict[str, Any]:
    """The worker's half and then the supervisor's, as E1-2 will call them; the result file's content."""
    compiled = attempt.program.compile()
    ran = attempt.program.run_once(attempt.payload.stdin, 10.0) if compiled else None
    write(attempt, compiled, ran)
    return read(attempt)


def write(attempt: Attempt, compiled: bool | None, ran: ExecResult | None) -> None:
    write_result(attempt.run, compiled, ran, attempt.program.output_cap_bytes)


def read(attempt: Attempt) -> dict[str, Any]:
    content = json.loads(attempt.run.result.read_bytes())
    assert list(content) == FIELDS
    return content


@pytest.mark.parametrize("source", COMMENT)
def test_one_run_with_the_users_input(data: Path, recorder: Recorder, source: str):
    """Catches an input that never reaches the program, a second run, and a field that is missing or swapped."""
    attempt = practice(data, recorder, source, "echo", "stderr careful", "peak 12", "exit 3", stdin=b"5 7\n\xc3\xa9\n")
    assert finish(attempt) == {
        "compiled": True, "compile_output": "", "exit_code": 3, "time_ms": 0, "peak_ram_mib": 12,
        "stdout": "5 7\né\n", "stdout_truncated": False, "stderr": "careful\n", "stderr_truncated": False}
    runs = [call for call in recorder.calls if call.stdin is not None]
    assert [call.stdin for call in runs] == [attempt.payload.folder / "input.txt"]
    assert runs[0].timeout_s == 10.0 and runs[0].cap_bytes == 65536


def test_the_result_lies_where_e4_looks_for_it(data: Path, recorder: Recorder):
    """The path is a contract with E4-3, and it is outside the mounted folder (R1)."""
    attempt = practice(data, recorder, "prog.c", "print 1")
    finish(attempt)
    assert attempt.run.result == data.resolve() / "runs" / "1042" / "a1" / "result.json"
    assert sorted(p.name for p in attempt.run.folder.iterdir()) == ["compile.txt", "out", "result.json", "stderr.txt",
                                                                    "stdout.txt"]


def test_an_empty_input_is_still_an_input(data: Path, recorder: Recorder):
    """Every code_run payload holds input.txt, empty when none was given (D-129)."""
    attempt = practice(data, recorder, "prog.c", "echo", "print end", stdin=b"")
    assert finish(attempt)["stdout"] == "end\n"
    assert recorder.calls[-1].stdin == attempt.payload.folder / "input.txt"


def test_time_is_the_runs_own(data: Path, recorder: Recorder, advancing: Callable[..., Any]):
    attempt = practice(data, recorder, "prog.c", "compile-sleep 2", "sleep 0.25", "print done")
    result = advancing(lambda: finish(attempt), 2.0, 0.25)
    assert (result["time_ms"], result["stdout"], result["peak_ram_mib"]) == (250, "done\n", None)


@pytest.mark.parametrize("source", COMMENT)
def test_a_compile_error_is_in_the_result(data: Path, recorder: Recorder, source: str):
    """Catches a program that did not compile shown as one that ran, with an exit code or a time."""
    attempt = practice(data, recorder, source, "compile-error line 3: what is this", "print 1")
    assert finish(attempt) == {
        "compiled": False, "compile_output": "line 3: what is this\n", "exit_code": None, "time_ms": None,
        "peak_ram_mib": None, "stdout": "", "stdout_truncated": False, "stderr": "", "stderr_truncated": False}
    assert len(recorder.calls) == 1


def test_a_java_program_without_main_is_a_compile_error(data: Path, recorder: Recorder):
    result = finish(practice(data, recorder, "Main.java", "no-main"))
    assert result["compiled"] is False
    assert result["compile_output"].startswith("Java submissions must declare class Main\nError: Main method not found")


def test_the_compilers_warnings_are_shown_too(data: Path, recorder: Recorder):
    attempt = practice(data, recorder, "prog.c", "print 1")
    assert attempt.program.compile()
    attempt.run.compile_log.write_bytes(b"prog.c:1: warning: unused\n")
    write(attempt, True, attempt.program.run_once(attempt.payload.stdin, 10.0))
    assert read(attempt)["compile_output"] == "prog.c:1: warning: unused\n"


@pytest.mark.parametrize("markers, truncated", [
    (("flood 10", "stderr 123456789"), (False, False)), (("flood 11", "stderr 123456789"), (True, False)),
    (("flood 10", "stderr 1234567890"), (False, True)),
])
def test_each_stream_is_cut_at_the_cap_and_says_so(data: Path, recorder: Recorder, markers: tuple[str, ...],
                                                   truncated: tuple[bool, bool]):
    result = finish(practice(data, recorder, "prog.c", *markers, cap=10))
    assert (result["stdout_truncated"], result["stderr_truncated"]) == truncated
    assert result["stdout"] == "x" * 10 and len(result["stderr"]) == 10


def test_output_that_is_no_text_is_shown_as_text(data: Path, recorder: Recorder):
    """Invalid UTF-8 is replaced, and everything else, a NUL and markup included, is kept as it is: the page shows
    it as text (PRODUCT_SPEC section 8)."""
    attempt = practice(data, recorder, "prog.c", "print-hex ff41000a", "print <script>alert(1)</script>")
    assert finish(attempt)["stdout"] == "�A\x00\n<script>alert(1)</script>\n"


def test_a_run_stopped_by_the_memory_limit_still_shows_its_output(data: Path, recorder: Recorder):
    """ran=None: the job fails with memory_limit, and the page still shows what the program printed."""
    attempt = practice(data, recorder, "prog.c", "print before", "stderr and here", "oom")
    assert attempt.program.compile()
    with pytest.raises(OutOfMemory):
        attempt.program.run_once(attempt.payload.stdin, 10.0)
    write(attempt, True, None)
    assert read(attempt) == {
        "compiled": True, "compile_output": "", "exit_code": None, "time_ms": None, "peak_ram_mib": None,
        "stdout": "before\n", "stdout_truncated": False, "stderr": "and here\n", "stderr_truncated": False}


def test_a_flood_stopped_by_a_limit_is_shown_as_cut(data: Path, recorder: Recorder):
    """Catches a full file shown as the whole output: an endless print loop ends by the deadline, so no result says
    that it was cut, but a file of exactly the cap does."""
    attempt = practice(data, recorder, "prog.c", "flood 11", "stderr 12345678", "oom", cap=10)
    assert attempt.program.compile()
    with pytest.raises(OutOfMemory):
        attempt.program.run_once(attempt.payload.stdin, 10.0)
    write(attempt, True, None)
    result = read(attempt)
    assert (result["stdout"], result["stdout_truncated"], result["stderr_truncated"]) == ("x" * 10, True, False)


@pytest.mark.parametrize("log", [True, False])
def test_an_attempt_stopped_before_it_compiled_does_not_say_it_failed_to(data: Path, recorder: Recorder, log: bool):
    """The deadline or the memory limit can end the attempt while the program compiles, or before: compiled is null,
    no output was captured, and none is invented."""
    attempt = practice(data, recorder, "prog.c", "compile-oom")
    with pytest.raises(OutOfMemory):
        attempt.program.compile()
    if not log:
        attempt.run.compile_log.unlink()
    write(attempt, None, None)
    assert read(attempt) == {
        "compiled": None, "compile_output": "", "exit_code": None, "time_ms": None, "peak_ram_mib": None,
        "stdout": "", "stdout_truncated": False, "stderr": "", "stderr_truncated": False}


def test_a_compile_error_needs_its_reason(data: Path, recorder: Recorder):
    """Catches a lost compile.txt shown as a compile error that said nothing."""
    attempt = practice(data, recorder, "prog.c", "compile-error line 3: what is this")
    assert attempt.program.compile() is False
    attempt.run.compile_log.unlink()
    with pytest.raises(FileNotFoundError):
        write(attempt, False, None)
    assert not attempt.run.result.exists() and not list(attempt.run.folder.glob("*.part"))


def test_a_run_that_timed_out_has_no_exit_code(data: Path, recorder: Recorder, advancing: Callable[..., Any]):
    """The run's own timeout is the backstop behind the deadline: 124 is the timeout's code, not the program's."""
    attempt = practice(data, recorder, "prog.c", "print before", "peak 12", "sleep 30")
    result = advancing(lambda: finish(attempt), 10.0)
    assert (result["exit_code"], result["time_ms"], result["peak_ram_mib"]) == (None, None, None)
    assert (result["stdout"], result["stdout_truncated"]) == ("before\n", False)


@pytest.mark.parametrize("gone", ["compile.txt", "stdout.txt", "stderr.txt"])
def test_a_result_needs_what_the_run_captured(data: Path, recorder: Recorder, gone: str):
    """Catches a captured file that is gone shown as an empty output, and a half-written result left behind."""
    attempt = practice(data, recorder, "prog.c", "print 1")
    assert attempt.program.compile()
    ran = attempt.program.run_once(attempt.payload.stdin, 10.0)
    (attempt.run.folder / gone).unlink()
    with pytest.raises(FileNotFoundError):
        write(attempt, True, ran)
    assert not attempt.run.result.exists() and not list(attempt.run.folder.glob("*.part"))


@pytest.mark.parametrize("compiled, ran", [
    (False, ExecResult(0, 1, False, False, False)), (None, ExecResult(0, 1, False, False, False)),
    (True, ExecResult(137, 1, False, True, False)), (True, ExecResult(0, 1, False, True, False)),
])
def test_a_result_that_cannot_be_is_refused(data: Path, recorder: Recorder, compiled: bool | None, ran: ExecResult):
    """Nothing runs that did not compile, and a memory kill is not a run that ended (plan 9.6)."""
    attempt = practice(data, recorder, "prog.c", "print 1")
    attempt.program.compile()
    attempt.program.run_once(None, 10.0)
    with pytest.raises(ValueError):
        write(attempt, compiled, ran)
    assert not attempt.run.result.exists()


def test_a_failed_write_leaves_no_partial_result(data: Path, recorder: Recorder, monkeypatch: pytest.MonkeyPatch):
    """Catches a result written in place, which E4 could read half of: it is written beside and then renamed."""
    attempt = practice(data, recorder, "prog.c", "print 1")
    assert attempt.program.compile()
    ran = attempt.program.run_once(attempt.payload.stdin, 10.0)

    def fail(source: Any, target: Any) -> None:
        assert json.loads(Path(source).read_bytes())["stdout"] == "1\n" and not os.path.lexists(target)
        raise OSError("disk")

    with monkeypatch.context() as patch:
        patch.setattr(judge.os, "replace", fail)
        with pytest.raises(OSError, match="disk"):
            write(attempt, True, ran)
    assert not attempt.run.result.exists() and not list(attempt.run.folder.glob("*.part"))
    write(attempt, True, ran)
    assert read(attempt)["stdout"] == "1\n"


def test_a_cleanup_that_fails_is_logged_and_hides_nothing(data: Path, recorder: Recorder, json_log: Path,
                                                          monkeypatch: pytest.MonkeyPatch):
    """Catches the cleanup's own error replacing the first one, and a file left behind with no trace."""
    attempt = practice(data, recorder, "prog.c", "print 1")
    assert attempt.program.compile()
    ran = attempt.program.run_once(attempt.payload.stdin, 10.0)

    def fail(*args: Any, **rest: Any) -> None:
        raise OSError("disk full")

    def in_use(*args: Any, **rest: Any) -> None:
        raise PermissionError("in use")

    with monkeypatch.context() as patch:
        patch.setattr(judge.os, "replace", fail)
        patch.setattr(Path, "unlink", in_use)
        with pytest.raises(OSError, match="disk full"):
            write(attempt, True, ran)
    line, = logged(json_log, "result_cleanup_failed")
    assert line["level"] == "ERROR" and line["path"].endswith("result.part")
    assert "in use" in line["error"] and "disk full" in line["traceback"]


def test_the_part_of_another_call_is_left_alone(data: Path, recorder: Recorder):
    """Catches a cleanup that deletes a file it did not make."""
    attempt = practice(data, recorder, "prog.c", "print 1")
    assert attempt.program.compile()
    ran = attempt.program.run_once(attempt.payload.stdin, 10.0)
    part = attempt.run.folder / "result.part"
    part.write_bytes(b"another call")
    with pytest.raises(FileExistsError):
        write(attempt, True, ran)
    assert part.read_bytes() == b"another call" and not attempt.run.result.exists()


def test_the_verdict_text_of_a_run_without_cases(data: Path, recorder: Recorder):
    """Plan 9.6 for every type without test cases, code_run included: it has no verdict of its own."""
    attempt = practice(data, recorder, "prog.c", "exit 3")
    assert attempt.program.compile()
    assert exited(attempt.program.run_once(attempt.payload.stdin, 10.0)) == "Exited 3"
    assert COMPILATION_ERROR == "Compilation Error"
