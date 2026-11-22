"""Fixtures of the E2 tests (plan 8.5)."""
from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path

import psycopg
import pytest

import app.e3_allocation as e3
from app.common.ports import Engines, install
from app.config import Config
from app.e2_scheduling import E2Service
from tests.helpers import ROOT, OffsetClock, TempDb, spooled


@pytest.fixture
def seeded(db: TempDb) -> TempDb:
    """A fresh database with db/seed.sql loaded: the six workload types, students 01FE24BCS901 and 902,
    FAC001, ADM001, lab sessions 7 (exam) and 8 (lab), both open, problem 12 and the S0 capacity; job
    ids start at 1042."""
    with psycopg.connect(db.owner_url, autocommit=True) as conn:
        conn.execute((ROOT / "db" / "seed.sql").read_bytes())
    return db


@pytest.fixture
def spool(tmp_path: Path) -> Callable[[bytes], Path]:
    """spool(data): data in a new spool file under tmp_path, the DATA_DIR of the E2 fixtures below, as E4-3
    hands an upload to submit() (D-90)."""
    return partial(spooled, tmp_path)


@pytest.fixture
def e2(seeded: TempDb, tmp_path: Path) -> E2Service:
    """E2 on the seeded database, with tmp_path as DATA_DIR and every other setting at its default."""
    return E2Service(Config(data_dir=tmp_path))


@pytest.fixture
def db_clock(seeded: TempDb) -> OffsetClock:
    """An OffsetClock whose time() starts at the database's now, which the 60 s rule compares with
    submitted_at; monotonic() starts at 0."""
    with psycopg.connect(seeded.owner_url, autocommit=True) as conn:
        row = conn.execute("SELECT extract(epoch FROM clock_timestamp())::float8").fetchone()
    assert row is not None
    return OffsetClock(row[0])


@pytest.fixture
def e2_stubbed(seeded: TempDb, tmp_path: Path, db_clock: OffsetClock) -> E2Service:
    """E2 on db_clock with the stubs behind the ports (StubE3 answers snapshot and allocate)."""
    return E2Service(Config(data_dir=tmp_path), clock=db_clock)


@pytest.fixture
def e2_real(seeded: TempDb, tmp_path: Path, db_clock: OffsetClock) -> E2Service:
    """E2 on db_clock with the real E3 behind the ports and this E2Service as e2, so a release() wakes it;
    E1 and E4 stay stubs. The db fixture uninstalls every engine afterwards."""
    service = E2Service(Config(data_dir=tmp_path), clock=db_clock)
    install(Engines(e1=seeded.stubs.e1, e2=service, e3=e3, e4=seeded.stubs.e4))  # pyright checks it is an E2Port
    return service
