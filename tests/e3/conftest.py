"""Fixtures of the E3 tests (plan 6.6)."""
from __future__ import annotations

import psycopg
import pytest

from tests.helpers import ROOT, TempDb


@pytest.fixture
def s0(db: TempDb) -> TempDb:
    """A fresh database with db/seed.sql loaded: resources 1 cpu, 2 ram, 3 gpu 0 and 4 gpu 1 (S0); new
    resources rows get ids from 5 and job ids start at 1042."""
    with psycopg.connect(db.owner_url, autocommit=True) as conn:
        conn.execute((ROOT / "db" / "seed.sql").read_bytes())
    return db
