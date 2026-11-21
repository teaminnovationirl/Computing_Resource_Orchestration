"""Fixtures of the E2 tests (plan 8.5)."""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from app.config import Config
from app.e2_scheduling import E2Service
from tests.helpers import ROOT, TempDb


@pytest.fixture
def seeded(db: TempDb) -> TempDb:
    """A fresh database with db/seed.sql loaded: the six workload types, students 01FE24BCS901 and 902,
    FAC001, ADM001, lab sessions 7 (exam) and 8 (lab), both open, problem 12 and the S0 capacity; job
    ids start at 1042."""
    with psycopg.connect(db.owner_url, autocommit=True) as conn:
        conn.execute((ROOT / "db" / "seed.sql").read_bytes())
    return db


@pytest.fixture
def e2(seeded: TempDb, tmp_path: Path) -> E2Service:
    """E2 on the seeded database, with tmp_path as DATA_DIR and every other setting at its default."""
    return E2Service(Config(data_dir=tmp_path))
