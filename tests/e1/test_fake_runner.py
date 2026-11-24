"""Slice E1-1: sandbox.py and FakeRunner (plan 9.2 with R2, D-149 and D-152).

The oracle is plan 9.2 and the owner's plan review: output is kept up to cap_bytes and flagged past it, every
captured file is made new and never follows what is already at its path, stderr is merged, kept apart or
dropped, and the two together are refused. FakeRunner scripts a program by marker comments; a run reads its
markers from what the compile step wrote, so a missing or stale binary behaves as it would.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.common.types import Limits
from app.e1_execution.fake_runner import FakeRunner
from app.e1_execution.judge import LANGUAGES, main_check_command, run_command
from app.e1_execution.sandbox import CappedFile, ExecResult, SandboxError, SandboxRunner, SandboxSpec

Advancing = Callable[..., ExecResult]

NAME = "job-1042-a1"
LIMITS = Limits(cpu_millicores=1000, ram_mib=512, gpu_index=None, time_limit_s=5)
RUN_C = run_command("prog.c", 512)


class Box:
    """A started sandbox on runner, with its folders in tmp_path, and the files its commands captured."""

    def __init__(self, runner: SandboxRunner, tmp_path: Path, name: str = NAME, job_id: int = 1042) -> None:
        self.runner, self.name = runner, name
        self.job, self.out, self.kept = (tmp_path / name / part for part in ("job", "out", "kept"))
        for folder in (self.job, self.out, self.kept):
            folder.mkdir(parents=True)
        self.spec = SandboxSpec(name, LIMITS, self.job, self.out,
                                {"orch.job_id": str(job_id), "orch.attempt_id": "77"})
        runner.start(self.spec)

    def exec(self, argv: list[str], stdout: str = "stdout", *, stdin: Path | None = None, timeout_s: float = 1.0,
             cap: int = 65536, **rest: Any) -> ExecResult:
        return self.runner.exec(self.name, argv, stdin, self.kept / stdout, timeout_s, cap, **rest)

    def compile(self, source: str, text: str, **rest: Any) -> ExecResult:
        (self.job / source).write_bytes(text.encode())
        return self.exec(list(LANGUAGES[source].compile), "compile.txt", merge_stderr=True, **rest)

    def c(self, *markers: str) -> None:
        """Compile a C program that holds these markers, one per line."""
        assert self.compile("prog.c", "".join(f"// fake: {marker}\n" for marker in markers)).exit_code == 0

    def text(self, name: str = "stdout") -> bytes:
        return (self.kept / name).read_bytes()


@pytest.fixture
def box(runner: FakeRunner, tmp_path: Path) -> Box:
    return Box(runner, tmp_path)


# ---- what a run does ------------------------------------------------------------------------------------

@pytest.mark.parametrize("markers, output, expected", [
    ((), b"", ExecResult(0, 0, False, False, False)),
    (("print 42",), b"42\n", ExecResult(0, 0, False, False, False)),
    (("print a", "print b"), b"a\nb\n", ExecResult(0, 0, False, False, False)),
    (("print-hex ff00fe",), b"\xff\x00\xfe", ExecResult(0, 0, False, False, False)),
    (("print 1", "exit 3", "print 2"), b"1\n", ExecResult(3, 0, False, False, False)),
    (("print 1", "oom", "print 2"), b"1\n", ExecResult(137, 0, False, True, False)),
    (("print 1", "peak 12"), b"1\n", ExecResult(0, 0, False, False, False, False, 12)),
])
def test_markers_script_a_run(box: Box, markers: tuple[str, ...], output: bytes, expected: ExecResult):
    """Catches a marker read out of order (what follows exit or oom must not run) and a result field that is
    not the marker's."""
    box.c(*markers)
    assert box.exec(RUN_C) == expected
    assert box.text() == output


def test_the_expected_output_is_the_out_file_beside_the_input(box: Box, tmp_path: Path):
    (tmp_path / "03.in").write_bytes(b"1\n7\n")
    (tmp_path / "03.out").write_bytes(b"7\n")
    box.c("print-expected")
    assert box.exec(RUN_C, stdin=tmp_path / "03.in").exit_code == 0
    assert box.text() == b"7\n"


def test_echo_copies_stdin_and_no_stdin_is_empty(box: Box, tmp_path: Path):
    (tmp_path / "input.txt").write_bytes(b"\xff line\n")
    box.c("echo")
    box.exec(RUN_C, "with", stdin=tmp_path / "input.txt")
    box.exec(RUN_C, "without")
    assert box.text("with") == b"\xff line\n" and box.text("without") == b""


def test_an_input_that_is_not_there_is_an_error(box: Box, tmp_path: Path):
    """Catches a wrong input path that a program which reads nothing never shows: a real runner opens it."""
    box.c("print 1")
    with pytest.raises(FileNotFoundError):
        box.exec(RUN_C, stdin=tmp_path / "nowhere" / "01.in")


def test_a_case_marker_speaks_for_that_case_alone(box: Box, tmp_path: Path):
    """Catches a case marker that also runs for the other cases, or beside the unscoped ones in its own."""
    for case in ("02", "03"):
        (tmp_path / f"{case}.in").write_bytes(b"")
    box.c("print every", "case 3 exit 9")
    assert box.exec(RUN_C, "two", stdin=tmp_path / "02.in").exit_code == 0
    assert box.exec(RUN_C, "three", stdin=tmp_path / "03.in").exit_code == 9
    assert box.text("two") == b"every\n" and box.text("three") == b""


@pytest.mark.parametrize("marker", [
    "pritn 42", "print42", "PRINT 42", "", "case 3", "sleep", "sleep abc", "sleep -1", "sleep nan", "sleep inf",
    "compile-sleep x", "exit x", "exit 256", "exit -1", "print-hex zz", "print-hex 1", "flood x", "flood -1",
    "flood 99999999999", "peak x", "oom now", "echo x", "print-expected x", "no-main x", "compile-oom x", "classes",
    "case " + "9" * 5000 + " exit 1", "classes " + "x" * 300, "classes a/b", "classes ../../escaped", "classes ..\\escaped", "classes /escaped", "classes caf\u00e9",
    "case 3 compile-error x", "case 3 compile-oom", "case 3 classes Main", "case 3 no-main",
])
def test_a_marker_that_means_nothing_raises(box: Box, tmp_path: Path, marker: str):
    """Catches a misspelt marker that silently scripts nothing, one that fails with another error, only when it
    runs, or for one case alone where it speaks for the whole program, and a class name that leaves /out. It is
    found when the program is compiled."""
    with pytest.raises(SandboxError):
        box.c("print 1", marker)
    assert list(box.out.iterdir()) == [] and not list(tmp_path.rglob("*escaped*"))


def test_a_program_cannot_script_the_fake_to_exhaust_the_server(box: Box, tmp_path: Path):
    """C-1 runs the server on this runner, with what students write. Catches a class file per name of a list of
    17,000, and gigabytes of flood built a line at a time."""
    names = " ".join(f"C{n}" for n in range(101))
    with pytest.raises(SandboxError):
        box.compile("Main.java", f"// fake: classes {names}\n")
    assert list(box.out.iterdir()) == []
    (box.kept / "compile.txt").unlink()
    box.c("flood 16777216", "flood 1")
    with pytest.raises(SandboxError, match="flood"):
        box.exec(RUN_C, cap=10)


def test_an_expected_output_that_is_not_there_raises(box: Box, tmp_path: Path):
    """Catches a plain FileNotFoundError, which E1-2 would take for a disk error."""
    (tmp_path / "input.txt").write_bytes(b"")
    box.c("print-expected")
    for name, stdin in (("none", None), ("no out file", tmp_path / "input.txt")):
        with pytest.raises(SandboxError):
            box.exec(RUN_C, name, stdin=stdin)


def test_a_long_run_of_blanks_is_read_at_once(box: Box):
    """Catches a marker pattern that takes quadratic time: 32,000 blanks took 5 s, with every thread of the server
    waiting. The bound is far above what this takes."""
    start = time.monotonic()
    box.c("print a" + " " * 32_000 + "b")
    assert box.exec(RUN_C).exit_code == 0 and time.monotonic() - start < 2
    assert box.text() == b"a" + b" " * 32_000 + b"b\n"


# ---- the cap and the captured files ---------------------------------------------------------------------

@pytest.mark.parametrize("flood, kept, truncated", [(9, 9, False), (10, 10, False), (11, 10, True), (500, 10, True)])
def test_output_is_kept_up_to_the_cap(box: Box, flood: int, kept: int, truncated: bool):
    """Catches cap_bytes ignored, off by one at the limit, and output_truncated set when all was kept."""
    box.c(f"flood {flood}")
    assert box.exec(RUN_C, cap=10).output_truncated is truncated
    assert box.text() == b"x" * kept


def test_the_cap_counts_every_write(box: Box):
    """Catches a cap held per write instead of per file: three lines of 4 bytes against a cap of 10."""
    box.c("print aaa", "print bbb", "print ccc")
    assert box.exec(RUN_C, cap=10).output_truncated is True
    assert box.text() == b"aaa\nbbb\ncc"


def test_stderr_is_merged_kept_apart_or_dropped(box: Box):
    """Catches stderr written into the output when neither asked for, and a merge out of order."""
    box.c("print out", "stderr err", "print end")
    assert box.exec(RUN_C, "merged", merge_stderr=True) == ExecResult(0, 0, False, False, False)
    assert box.exec(RUN_C, "apart", stderr=box.kept / "err") == ExecResult(0, 0, False, False, False)
    assert box.exec(RUN_C, "dropped") == ExecResult(0, 0, False, False, False)
    assert box.text("merged") == b"out\nerr\nend\n"
    assert box.text("apart") == b"out\nend\n" and box.text("err") == b"err\n"
    assert box.text("dropped") == b"out\nend\n"
    assert sorted(p.name for p in box.kept.iterdir()) == ["apart", "compile.txt", "dropped", "err", "merged"]


def test_stderr_has_a_cap_and_a_flag_of_its_own(box: Box):
    """Catches one cap shared by the two files, and the two truncation flags swapped."""
    box.c("print 123456789", "stderr 1234567890")
    result = box.exec(RUN_C, cap=10, stderr=box.kept / "err")
    assert (result.output_truncated, result.stderr_truncated) == (False, True)
    assert box.text() == b"123456789\n" and box.text("err") == b"1234567890"


def test_merged_stderr_with_a_file_of_its_own_is_refused(box: Box):
    """Catches either one silently winning: ValueError, with nothing run and no file made (plan review)."""
    (box.job / "prog.c").write_bytes(b"// fake: print 1\n")
    with pytest.raises(ValueError, match="merge_stderr"):
        box.exec(list(LANGUAGES["prog.c"].compile), merge_stderr=True, stderr=box.kept / "err")
    assert list(box.kept.iterdir()) == [] and list(box.out.iterdir()) == []


def test_what_was_captured_is_on_the_disk_at_once(tmp_path: Path):
    """Catches output that waits in a buffer: a worker that is killed must lose nothing it captured, and the job's
    log is read while it runs."""
    with CappedFile(tmp_path / "out", 10) as out:
        out.write(b"abc")
        assert (tmp_path / "out").read_bytes() == b"abc"
        out.write(b"defghijkl")
        assert (tmp_path / "out").read_bytes() == b"abcdefghij" and out.truncated


@pytest.mark.parametrize("which", ["stdout", "stderr"])
def test_a_captured_file_is_always_made_new(box: Box, tmp_path: Path, which: str):
    """Catches "wb": a second name (a hard link) of a file elsewhere, left at the path of a captured file, must
    not be opened and emptied. FileExistsError, and the file elsewhere is untouched."""
    victim = tmp_path / "02.out"
    victim.write_bytes(b"expected\n")
    os.link(victim, box.kept / which)
    box.c("print 1", "stderr 2")
    with pytest.raises(FileExistsError):
        box.exec(RUN_C, "stdout", stderr=box.kept / "stderr")
    assert victim.read_bytes() == b"expected\n"


def test_a_captured_file_never_follows_a_link(box: Box, tmp_path: Path):
    """The symbolic link a program could leave for the next case (R1): never followed, even to a path that does
    not exist yet. Skipped where the OS refuses to create one (Windows without the privilege)."""
    victim = tmp_path / "new.out"
    try:
        (box.kept / "stdout").symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"this OS refuses a file symlink: {exc}")
    box.c("print 1")
    with pytest.raises(FileExistsError):
        box.exec(RUN_C)
    assert not victim.exists()


# ---- time and stop --------------------------------------------------------------------------------------

def test_a_sleep_within_the_limit_takes_its_time(box: Box, advancing: Advancing):
    box.c("sleep 0.5", "print done")
    assert advancing(lambda: box.exec(RUN_C, timeout_s=1.0), 0.5) == ExecResult(0, 500, False, False, False)
    assert box.text() == b"done\n"


def test_a_sleep_past_the_limit_times_out_and_keeps_the_output_so_far(box: Box, advancing: Advancing):
    """Catches a timeout that is not the command's own, or one that loses what was printed before it."""
    box.c("print before", "sleep 30", "print after")
    result = advancing(lambda: box.exec(RUN_C, timeout_s=1.5), 1.5)
    assert result.timed_out is True and result.wall_ms == 1500 and result.oom_killed is False
    assert box.text() == b"before\n"


def test_two_sleeps_share_one_limit(box: Box, advancing: Advancing):
    """Catches a limit held per sleep instead of per command."""
    box.c("sleep 0.6", "sleep 0.6")
    result = advancing(lambda: box.exec(RUN_C, timeout_s=1.0), 0.6, 0.4)
    assert result.timed_out is True and result.wall_ms == 1000


def test_nothing_runs_once_the_time_is_up(box: Box, advancing: Advancing):
    """Catches a limit that only a sleep looks at."""
    box.c("sleep 0.5", "print late")
    result = advancing(lambda: box.exec(RUN_C, timeout_s=1.0), 5.0)
    assert result.timed_out is True and box.text() == b""


@pytest.mark.parametrize("how", ["stop", "remove"])
@pytest.mark.parametrize("stage", ["run", "compile"])
def test_a_command_that_is_cut_has_no_result(box: Box, advancing: Advancing, how: str, stage: str):
    """Catches a stop that waits for the program, and one that hands back an ordinary result: exit code 143 would
    read Runtime Error, Compilation Error or "Exited 143". What was cut is SandboxError, at once."""
    end = {"stop": lambda: box.runner.stop(NAME, 2.0), "remove": lambda: box.runner.remove(NAME)}[how]
    if stage == "run":
        box.c("print before", "sleep 30")
        command = lambda: box.exec(RUN_C, timeout_s=60.0)  # noqa: E731 (one of two)
    else:
        command = lambda: box.compile("prog.c", "// fake: compile-sleep 30\n", timeout_s=60.0)  # noqa: E731
    with pytest.raises(SandboxError, match="before"):
        advancing(command, end)
    if stage == "run":
        assert box.text() == b"before\n"


# ---- compiling ------------------------------------------------------------------------------------------

def test_a_compile_writes_what_the_compiler_writes(box: Box):
    for source in LANGUAGES:
        assert box.compile(source, "// fake: print 1\n").exit_code == 0
        (box.kept / "compile.txt").unlink()
    assert sorted(p.name for p in box.out.iterdir()) == ["Main.class", "prog"]  # Python writes nothing


def test_a_compile_error_prints_and_writes_no_binary(box: Box):
    result = box.compile("prog.c", "// fake: compile-error prog.c:1: expected ';'\n")
    assert result == ExecResult(1, 0, False, False, False)
    assert box.text("compile.txt") == b"prog.c:1: expected ';'\n" and list(box.out.iterdir()) == []


def test_a_compile_can_run_out_of_memory_or_time(box: Box, advancing: Advancing):
    assert box.compile("prog.c", "// fake: compile-oom\n") == ExecResult(137, 0, False, True, False)
    (box.kept / "compile.txt").unlink()
    result = advancing(lambda: box.compile("prog.c", "// fake: compile-sleep 30\n", timeout_s=10.0), 10.0)
    assert result.timed_out is True and result.wall_ms == 10000
    assert list(box.out.iterdir()) == []


def test_two_compile_sleeps_share_one_limit(box: Box, advancing: Advancing):
    """Catches a limit held per sleep instead of per compile step."""
    text = "// fake: compile-sleep 6\n// fake: compile-sleep 6\n"
    result = advancing(lambda: box.compile("prog.c", text, timeout_s=10.0), 6.0, 4.0)
    assert result.timed_out is True and result.wall_ms == 10000 and list(box.out.iterdir()) == []


def test_a_run_reads_its_markers_from_the_binary(box: Box):
    """Catches a fake that reads the source: the binary in /out is what runs, stale or not, and a missing one
    cannot run (127, as a command that is not there)."""
    (box.job / "prog.c").write_bytes(b"// fake: print new\n")
    assert box.exec(RUN_C, "missing").exit_code == 127
    (box.out / "prog").write_bytes(b"// fake: print stale\n")
    assert box.exec(RUN_C, "stale").exit_code == 0
    assert box.text("stale") == b"stale\n" and box.text("missing") == b""


def test_a_python_program_runs_from_the_job_folder(box: Box):
    (box.job / "prog.py").write_bytes(b"# fake: print 42\n")
    assert box.exec(run_command("prog.py", 512)).exit_code == 0
    assert box.text() == b"42\n"


@pytest.mark.parametrize("markers, check, said", [
    ("", 0, b""),
    ("// fake: no-main\n", 1, b"Error: Main method not found in class Main"),
    ("// fake: classes Other Helper\n", 1, b"Error: Could not find or load main class Main"),
    ("// fake: classes Other Main\n", 0, b""),
])
def test_the_launcher_check_reads_what_javac_wrote(box: Box, markers: str, check: int, said: bytes):
    """The dry run of the JVM launcher (D-146): it looks at the class files in /out and runs nothing."""
    assert box.compile("Main.java", markers + "// fake: print ran\n").exit_code == 0
    result = box.exec(main_check_command(512), "check", merge_stderr=True)
    assert result.exit_code == check and box.text("check").startswith(said) and b"ran" not in box.text("check")


def test_a_java_run_needs_the_class_main(box: Box):
    assert box.compile("Main.java", "// fake: classes Other\n// fake: print ran\n").exit_code == 0
    assert box.exec(run_command("Main.java", 512)).exit_code == 1
    assert box.text() == b""


# ---- the life of a sandbox ------------------------------------------------------------------------------

def test_the_life_of_a_sandbox(runner: FakeRunner, tmp_path: Path):
    """Catches a name that can be started twice, a command run in a sandbox that is stopped or was never
    started, a sandbox that is gone before it is removed, and a remove that is not idempotent."""
    box = Box(runner, tmp_path)
    box.c("print 1")
    with pytest.raises(SandboxError):
        runner.start(box.spec)
    with pytest.raises(SandboxError):
        runner.exec("job-9-a1", RUN_C, None, tmp_path / "x", 1.0, 10)
    runner.stop(NAME, 2.0)
    assert runner.exists(NAME)  # stopped, as a container that exited: there until it is removed
    with pytest.raises(SandboxError):
        box.exec(RUN_C)
    runner.remove(NAME)
    runner.remove(NAME)
    assert not runner.exists(NAME) and not (tmp_path / "x").exists()
    runner.stop(NAME, 2.0)  # nothing to stop: no error, as in service.stop()'s loop (plan 9.7)
    runner.start(box.spec)
    assert runner.exists(NAME)


def test_sandboxes_are_listed_by_job(runner: FakeRunner, tmp_path: Path):
    for name, job_id in (("job-7-a1", 7), ("job-7-a2", 7), ("job-70-a1", 70)):
        Box(runner, tmp_path, name, job_id)
    assert sorted(runner.list_sandboxes(7)) == ["job-7-a1", "job-7-a2"]
    assert sorted(runner.list_sandboxes()) == ["job-7-a1", "job-7-a2", "job-70-a1"]
    runner.remove("job-7-a1")
    assert runner.list_sandboxes(7) == ["job-7-a2"] and runner.list_sandboxes(8) == []


@pytest.mark.parametrize("missing", ["job_dir", "out_dir"])
def test_a_sandbox_needs_its_folders(runner: FakeRunner, tmp_path: Path, missing: str):
    """Catches a mount of a folder that is not there, which Docker would create, owned by root."""
    folders = {"job_dir": tmp_path / "job", "out_dir": tmp_path / "out"}
    for part, folder in folders.items():
        if part != missing:
            folder.mkdir()
    with pytest.raises(SandboxError):
        runner.start(SandboxSpec(NAME, LIMITS, labels={}, **folders))
    assert not runner.exists(NAME)
