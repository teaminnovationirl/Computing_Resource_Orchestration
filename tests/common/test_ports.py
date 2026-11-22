"""ports.py, the shared types and the stubs of tests/stubs.py (plan 5.3, 5.4, 5.6 and 12.2).
Each docstring names the bug the test catches."""
from __future__ import annotations

import inspect
import typing
from pathlib import Path

import pytest

from app.common import ports
from app.common.db import tx
from app.common.ports import E1Port, E2Port, E3Port, E4Port, engines
from app.common.types import (AlreadyReleased, Committed, Limits, Refused, Released, Snapshot, SubmitRequest, User,
                              Vector)
from tests.helpers import TempDb
from tests.stubs import StubE1, StubE2, StubE3, StubE4

PAIRS = [(E1Port, StubE1), (E2Port, StubE2), (E3Port, StubE3), (E4Port, StubE4)]
LIMITS = Limits(cpu_millicores=1000, ram_mib=512, gpu_index=None, time_limit_s=5)
CALLS: dict[str, tuple[object, ...]] = {  # one valid argument list for every port method
    "dispatch": (1042, 5, LIMITS, "payloads/1042"), "stop": (1042,), "cancel": (1042, "01FE24BCS901"),
    "submit": (User("01FE24BCS901", "student"), SubmitRequest("cpu_bound", 1000, 512, 0, 5), Path("spool/prog")),
    "capacity_changed": (), "requeue": (1042,), "cancel_queued": (1042, "01FE24BCS901"),
    "pause_admission": ("host health",), "resume_admission": (), "set_policy": ("need",), "queue_view": (),
    "snapshot": (), "allocate": (1042, Vector(1000, 512)), "release": (1042,),
    "heartbeat": (7,), "register_worker": (7,), "close_worker": (7,), "publish": ({"type": "job_state"},),
}


def _methods(protocol: type) -> list[str]:
    return sorted(name for name, value in vars(protocol).items()
                  if inspect.isfunction(value) and not name.startswith("_"))


@pytest.mark.parametrize("protocol, stub", PAIRS)
def test_every_stub_matches_its_port(protocol: type, stub: type):
    """Catches a stub that drifts from its port (a renamed parameter, another return type): the
    engine tests would pass against a contract that the real engine does not have."""
    names = _methods(protocol)
    assert names and set(names) <= set(CALLS), names
    for name in names:
        expected, actual = inspect.signature(getattr(protocol, name)), inspect.signature(getattr(stub, name))
        assert [(p.name, p.kind, p.default) for p in expected.parameters.values()] == \
               [(p.name, p.kind, p.default) for p in actual.parameters.values()], name
        assert typing.get_type_hints(getattr(protocol, name)) == typing.get_type_hints(getattr(stub, name)), name


def test_every_stub_call_inside_a_transaction_is_refused(db: TempDb):
    """Catches an engine that calls another engine or pushes an event while its transaction holds
    locks (invariant 3): the stubs refuse it as the real engines will, long before slice C-1."""
    stubs = db.stubs
    for (protocol, _), target in zip(PAIRS, (stubs.e1, stubs.e2, stubs.e3, stubs.e4), strict=True):
        for name in _methods(protocol):
            with tx("E2"):
                with pytest.raises(RuntimeError, match="inside an open transaction"):
                    getattr(target, name)(*CALLS[name])
        assert target.calls == [], target
    with tx("E2"):
        with pytest.raises(RuntimeError, match="inside an open transaction"):
            stubs.hub.publish({"type": "job_state"})
    assert stubs.hub.events == []


def test_stubs_keep_the_port_contracts(db: TempDb):
    """Catches stubs that hide cases the engines must handle: a second release is ALREADY_RELEASED
    (invariant 4), allocate returns only what was scripted, close_worker is True once per attempt,
    a published event reaches the hub, and install_stubs() installs what it returns."""
    e1, e3, e4 = db.stubs.e1, db.stubs.e3, db.stubs.e4
    assert engines().e3 is e3
    assert e3.snapshot() == Snapshot(cpu_free=28800, ram_free=117964, gpu_free={0: 14745, 1: 14745})
    e3.allocate_results += [Committed(allocation_id=5, gpu_index=None), Refused(reason="no capacity")]
    assert e3.allocate(1042, Vector(1000, 512)) == Committed(5, None)
    assert e3.allocate(1043, Vector(1000, 512)) == Refused("no capacity")
    with pytest.raises(AssertionError, match="no result scripted"):
        e3.allocate(1044, Vector(1000, 512))
    assert [e3.release(1042), e3.release(1042)] == [Released(5), AlreadyReleased()]
    assert [e4.close_worker(7), e4.close_worker(7)] == [True, False]
    e4.publish({"type": "job_state", "job_id": 1042})
    assert db.stubs.hub.events == [{"type": "job_state", "job_id": 1042}]
    assert [e1.dispatch(1042, 5, LIMITS, "payloads/1042"), e1.dispatch(1043, 6, LIMITS, "payloads/1043")] == [1, 2]
    assert e1.calls[0] == ("dispatch", (1042, 5, LIMITS, "payloads/1042"))


def test_engines_before_install_fails_loudly(monkeypatch: pytest.MonkeyPatch):
    """Catches engines() handing out None before main.py or a test installed the engines."""
    monkeypatch.setattr(ports, "_installed", None)
    with pytest.raises(RuntimeError, match="install"):
        engines()


def test_snapshot_fits_needs_one_gpu_with_all_the_vram():
    """Catches VRAM summed across GPUs (a job runs on one GPU), and an off-by-one on CPU or RAM."""
    free = Snapshot(cpu_free=1000, ram_free=512, gpu_free={0: 8000, 1: 8000})
    assert free.fits(Vector(1000, 512)) and free.fits(Vector(1000, 512, 8000))
    assert not free.fits(Vector(1000, 512, 10000))
    assert not free.fits(Vector(1001, 512)) and not free.fits(Vector(1000, 513))
    assert Snapshot(1000, 512, {}).fits(Vector(1000, 512)) and not Snapshot(1000, 512, {}).fits(Vector(1, 64, 1))
