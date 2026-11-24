"""What the E1 tests share: one attempt, from its payload to its Program, on a SandboxRunner that records every
command. Import this module as tests.e1.support only.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.common.clock import Clock
from app.common.types import Limits
from app.e1_execution.fake_runner import FakeRunner
from app.e1_execution.files import CaseFiles, Files
from app.e1_execution.judge import COMPILATION_ERROR, Judged, Program, judge_cases
from app.e1_execution.sandbox import ExecResult, SandboxSpec, captured


@dataclass(frozen=True)
class Call:
    """One exec()."""
    argv: list[str]
    stdin: Path | None
    stdout: Path
    timeout_s: float
    cap_bytes: int
    merge_stderr: bool
    stderr: Path | None


class Recorder(FakeRunner):
    """A FakeRunner that records every exec(); answer(call), when it returns a result, speaks for the program, and
    with it for what the program printed."""

    def __init__(self, clock: Clock) -> None:
        super().__init__(clock)
        self.calls: list[Call] = []
        self.answer: Callable[[Call], ExecResult | tuple[ExecResult, bytes] | None] = lambda call: None

    def exec(self, name: str, argv: list[str], stdin: Path | None, stdout: Path, timeout_s: float, cap_bytes: int,
             merge_stderr: bool = False, stderr: Path | None = None) -> ExecResult:
        call = Call(argv, stdin, stdout, timeout_s, cap_bytes, merge_stderr, stderr)
        self.calls.append(call)
        answer = self.answer(call)
        if answer is None:
            return super().exec(name, argv, stdin, stdout, timeout_s, cap_bytes, merge_stderr, stderr)
        result, said = answer if isinstance(answer, tuple) else (answer, b"")
        with captured(stdout, stderr, cap_bytes, merge_stderr) as (out, _):
            out.write(said)
        return result


class Attempt:
    """Attempt attempt_no of job job_id under DATA_DIR data, its sandbox started; text is the source."""

    def __init__(self, data: Path, runner: Recorder, source: str, text: str = "", *, stdin: bytes | None = None,
                 job_id: int = 1042, attempt_no: int = 1, ram_mib: int = 512, cap: int = 65536) -> None:
        ref = f"payloads/{uuid.uuid4().hex}"
        (data / ref).mkdir(parents=True)
        (data / ref / source).write_bytes(text.encode())
        if stdin is not None:
            (data / ref / "input.txt").write_bytes(stdin)
        self._data = data
        self.runner = runner
        self.files = Files(data)
        self.payload = self.files.payload(ref, with_input=stdin is not None)
        self.run = self.files.new_run(job_id, attempt_no)
        name = f"job-{job_id}-a{attempt_no}"
        runner.start(SandboxSpec(name, Limits(1000, ram_mib, None, 5), self.payload.folder, self.run.out,
                                 {"orch.job_id": str(job_id), "orch.attempt_id": "77"}))
        self.program = Program(runner, name, source, ram_mib, self.run, compile_timeout_s=10.0, output_cap_bytes=cap)

    def cases(self, *expected: bytes) -> list[CaseFiles]:
        """Test cases 1, 2, ... of problem 12 with these expected outputs; the input of case n is its number."""
        folder = self._data / "problems" / "12"
        folder.mkdir(parents=True, exist_ok=True)
        for n, output in enumerate(expected, 1):
            (folder / f"{n:02d}.in").write_bytes(b"%d\n" % n)
            (folder / f"{n:02d}.out").write_bytes(output)
        return [self.files.case(n, f"problems/12/{n:02d}.in", f"problems/12/{n:02d}.out")
                for n in range(1, len(expected) + 1)]

    def judge(self, cases: Sequence[CaseFiles], time_limit_ms: int = 1000) -> Judged:
        """The worker's half and then the supervisor's, as E1-2 will call them."""
        if not self.program.compile():
            return Judged(COMPILATION_ERROR, ())
        ran = {case.case_no: self.program.run_case(case.case_no, case.input, time_limit_ms) for case in cases}
        return judge_cases(self.run, cases, ran)
