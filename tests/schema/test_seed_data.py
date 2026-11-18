"""Test case files of problem 12 under seed_data/ (plan 4.3, last paragraph; slice A-3).

Input: a count N on the first line, then N integers. Expected output: their sum.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "seed_data"
CASES = SEED / "problems" / "12"
NAMES = [f"{n:02d}" for n in range(1, 11)]
INT32 = range(-2**31, 2**31)


def _numbers(name: str) -> list[int]:
    """The integers of NN.in after the count, once the count and the line endings check out."""
    data = (CASES / f"{name}.in").read_bytes()
    assert b"\r" not in data and data.endswith(b"\n"), f"{name}.in: LF line endings, newline at the end"
    count, _, rest = data.decode("ascii").partition("\n")
    numbers = [int(token) for token in rest.split()]
    assert int(count) == len(numbers) >= 1, f"{name}.in: N on line 1 must equal the count that follows"
    return numbers


@pytest.mark.parametrize("name", NAMES)
def test_expected_output_is_the_sum(name):
    """Catches a wrong expected answer, and a CRLF file that turns every right answer into WA."""
    assert (CASES / f"{name}.out").read_bytes() == f"{sum(_numbers(name))}\n".encode("ascii")


def test_cases_cover_the_edges():
    """Catches a case set that a program summing into an int, or never seeing N = 1 or a
    negative number, would pass. Every value fits in int32, so reading with %d stays correct."""
    cases = [_numbers(name) for name in NAMES]
    assert all(value in INT32 for numbers in cases for value in numbers), "a value outside int32"
    assert any(len(numbers) == 1 for numbers in cases), "no case with N = 1"
    assert any(value < 0 for numbers in cases for value in numbers), "no negative number"
    assert any(sum(numbers) not in INT32 for numbers in cases), "no sum that needs 64 bits"


def test_seed_paths_name_the_case_files(owner_tx):
    """Catches drift between db/seed.sql and the files, such as 1.in against 01.in, and stray files."""
    rows = owner_tx.all("SELECT input_path, expected_path FROM test_cases WHERE problem_id = 12")
    paths = sorted(path for row in rows for path in row)
    assert len(paths) == 20
    assert sorted(f.relative_to(SEED).as_posix() for f in CASES.iterdir()) == paths
