"""Slice E2-1, V1 (plan 8.5, NFR-03): 1,000 seeded requests through submit(), 800 well formed and 200
that each break exactly one field. NFR-03 asks for at least 99% right accept and reject decisions; the
rules are deterministic, so every decision must be right, every 201 must carry the baseline's need class
and every 400 must name the broken field.

The oracle is written here from the seed and S0, never computed by the code under test.
"""
from __future__ import annotations

import dataclasses
import random
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.common.db import tx
from app.common.errors import ApiError
from app.common.types import SubmitRequest, User
from app.e2_scheduling import E2Service
from tests.helpers import TempDb, assert_invariants, zip_of

# db/seed.sql: type -> (default need class, max_time_limit_s); S0 allocatable maxima (context §3).
TYPES = {"code_eval": (2, 60), "cpu_bound": (3, 600), "mem_bound": (3, 600), "gpu_bound": (3, 1800),
         "mixed": (3, 600), "python_ml": (3, 3600)}
CPU_MAX, RAM_MAX, VRAM_MAX = 28800, 117964, 14745
SOURCE_MAX = 65536
FIELDS = ["workload_type", "problem_id", "lab_session_id", "time_limit_s", "ram_mib", "vram_mib",
          "cpu_millicores", "source_name", "source"]
USERS = [User("01FE24BCS901", "student"), User("FAC001", "faculty"), User("ADM001", "administrator")]
NOT_UTF8 = b"x = 1  # \xff\n"
TOTAL, BROKEN = 1000, 200


@dataclass(frozen=True)
class Case:
    req: SubmitRequest
    source: Any
    broken: str | None   # the field this case breaks; None when it is well formed
    need_class: int      # the class a well-formed case must get


def text(rng: random.Random) -> bytes:
    """A short UTF-8 source, with characters of 1, 2 and 3 bytes."""
    return ("// " + "".join(rng.choice("ab c\u00e9\u20ac;{}") for _ in range(rng.randint(1, 2000))) + "\n").encode()


def well_formed(rng: random.Random) -> tuple[SubmitRequest, bytes]:
    type_code = rng.choice(sorted(TYPES))
    extensions = ["py", "zip"] if type_code == "python_ml" else ["c", "cpp", "java", "py"]
    extension = rng.choice(extensions)
    stem = rng.choice(["prog", "Solution", "my prog (1)", "../../x", "..\\up", "C:\\work\\main"])
    if extension == "zip":
        source = zip_of({"main.py": text(rng), "data/train.csv": b"x,y\n1,2\n"})
    else:
        source = text(rng)
    req = SubmitRequest(type_code, rng.randint(100, CPU_MAX), rng.randint(64, RAM_MAX),
                        rng.choice([0, rng.randint(0, VRAM_MAX)]), rng.randint(1, TYPES[type_code][1]),
                        problem_id=12 if type_code == "code_eval" else None,
                        lab_session_id=rng.choice([None, 7, 8]), source_name=f"{stem}.{extension}")
    return req, source


def broken(rng: random.Random, req: SubmitRequest, source: bytes, field: str,
           closed: int, future: int) -> tuple[SubmitRequest, Any]:
    """req and source with field made wrong in one of the ways a client can get it wrong."""
    ml = req.workload_type == "python_ml"
    if field == "source":
        extension = req.source_name.rpartition(".")[2]
        if extension == "zip":
            choices: list[Any] = [None, b"", b"PK\x03\x04 broken", zip_of({"train.py": b"1"}),
                                     zip_of({"main.py": b"1", "../x.py": b"1"}), zip_of({"main.py": b"1", "/x": b"1"}),
                                     zip_of({"main.py": b"1"}, ("data/link",)), zip_of({"pkg/main.py": b"1"})]
        elif extension == "java":
            choices = [None, b"", b"x" * (SOURCE_MAX + 1)]
        elif ml:
            choices = [None, b"", NOT_UTF8]
        else:
            choices = [None, b"", b"x" * (SOURCE_MAX + 1), NOT_UTF8]
        return req, rng.choice(choices)
    values: dict[str, list[Any]] = {
        "workload_type": [None, "", "no_such_type", "CODE_EVAL", 7],
        "problem_id": [None, 999999, "12", True, 12.0] if req.workload_type == "code_eval" else [12, 999999],
        "lab_session_id": [999999, closed, future, "7", True, 7.0],
        "time_limit_s": [0, -1, TYPES[req.workload_type][1] + 1, None, 5.0, True],
        "ram_mib": [63, RAM_MAX + 1, None, 512.0, "512"],
        "vram_mib": [-1, VRAM_MAX + 1, None, 0.0, True],
        "cpu_millicores": [99, CPU_MAX + 1, None, 1000.0, False],
        "source_name": (["", None, "x.c", "x.java", "x.ZIP", "x.tar.gz"] if ml
                        else ["", None, "x.C", "x.h", "x.zip", "x.c.exe", "x", "x.Py", 5]),
    }
    return dataclasses.replace(req, **{field: rng.choice(values[field])}), source


def need_class(req: SubmitRequest) -> int:
    """The baseline rule (context §7): 1 in an open exam session (7), else the type's default."""
    return 1 if req.lab_session_id == 7 else TYPES[req.workload_type][0]


def new_session(opens_in: str, closes_in: str) -> int:
    with tx("E2") as cur:
        row = cur.execute("INSERT INTO lab_sessions (faculty_id, kind, opens_at, closes_at) VALUES ('FAC001', 'exam', "
                          "clock_timestamp() + %s::interval, clock_timestamp() + %s::interval) "
                          "RETURNING lab_session_id", (opens_in, closes_in)).fetchone()
    assert row is not None
    return row[0]


def test_v1_every_decision_of_1000_requests_is_right(e2: E2Service, seeded: TempDb, tmp_path: Path,
                                                     spool: Callable[[bytes], Path]):
    """Each source travels as a spooled file (D-90); at the end every accepted upload has left the spool for its
    payload folder and every refused one is still there, untouched, for E4-3 to delete."""
    closed, future = new_session("-2 hours", "-1 hour"), new_session("1 hour", "2 hours")
    rng = random.Random(1042)
    broken_at = set(rng.sample(range(TOTAL), BROKEN))
    cases: list[Case] = []
    for i in range(TOTAL):
        req, source = well_formed(rng)
        if i in broken_at:
            field = rng.choice(FIELDS)
            bad_req, bad_source = broken(rng, req, source, field, closed, future)
            cases.append(Case(bad_req, bad_source, field, 0))
        else:
            cases.append(Case(req, source, None, need_class(req)))
    assert {case.broken for case in cases} == {None, *FIELDS}  # the batch breaks every field

    wrong: list[tuple[int, str | None, str]] = []
    left: dict[Path, bytes] = {}  # the refused uploads, which submit() must leave in the spool
    for i, case in enumerate(cases):
        source = spool(case.source) if isinstance(case.source, bytes) else case.source
        try:
            result = e2.submit(USERS[i % len(USERS)], case.req, source)
        except ApiError as refusal:
            if isinstance(source, Path):
                left[source] = case.source
            if refusal.field != case.broken:
                wrong.append((i, case.broken, f"400 {refusal.field}: {refusal.error}"))
            continue
        if case.broken is not None or result.need_class != case.need_class:
            wrong.append((i, case.broken, f"201 need_class {result.need_class}, expected {case.need_class}"))
    accuracy = 1 - len(wrong) / TOTAL
    assert wrong == [], f"accuracy {accuracy:.1%} (NFR-03: at least 99%); first wrong: {wrong[:5]}"

    with tx("E4") as cur:
        row = cur.execute("SELECT count(*) FROM jobs").fetchone()
    assert row == (TOTAL - BROKEN,)
    assert len(list((tmp_path / "payloads").iterdir())) == TOTAL - BROKEN
    assert {path: path.read_bytes() for path in (tmp_path / "spool").iterdir()} == left
    assert_invariants(settled=True)
