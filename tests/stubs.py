"""Stub engines, so each engine can be built and tested alone (plan 5.6; replaced in the order of
plan 12.2). Every stub records its calls and, like the real engines, refuses to run inside a
transaction (invariant 3). Import this module as tests.stubs only.
"""
from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass
from typing import Any, Literal

from app.common.db import assert_outside_tx
from app.common.ports import Engines, install
from app.common.types import (AlreadyReleased, Committed, Limits, QueueEntry, Refused, Released, Snapshot,
                              SubmitRequest, SubmitResult, User, Vector)

def s0_free() -> Snapshot:
    """The free capacity of the S0 seed, a fresh copy each time (its gpu_free dict is mutable)."""
    return Snapshot(cpu_free=28800, ram_free=117964, gpu_free={0: 14745, 1: 14745})


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self._lock = threading.Lock()

    def _record(self, name: str, *args: object) -> None:
        assert_outside_tx(f"{type(self).__name__}.{name}")
        with self._lock:
            self.calls.append((name, args))


class StubE1(_Recorder):
    """Records dispatch, stop and cancel; dispatch returns attempt ids 1, 2, ...; set stop_error
    (for example StopNotConfirmed()) to make stop() fail."""

    def __init__(self) -> None:
        super().__init__()
        self._attempt_ids = itertools.count(1)
        self.stop_error: Exception | None = None

    def dispatch(self, job_id: int, allocation_id: int, limits: Limits, file_ref: str) -> int:
        self._record("dispatch", job_id, allocation_id, limits, file_ref)
        with self._lock:
            return next(self._attempt_ids)

    def stop(self, job_id: int) -> None:
        self._record("stop", job_id)
        if self.stop_error is not None:
            raise self.stop_error

    def cancel(self, job_id: int, by: str) -> None:
        self._record("cancel", job_id, by)


class StubE2(_Recorder):
    """Records every call; submit returns the scripted submit_results in order."""

    def __init__(self) -> None:
        super().__init__()
        self.submit_results: list[SubmitResult] = []
        self.requeue_result = True
        self.cancel_result = True
        self.queue: list[QueueEntry] = []

    def submit(self, user: User, req: SubmitRequest, source: bytes) -> SubmitResult:
        self._record("submit", user, req, source)
        with self._lock:
            if not self.submit_results:
                raise AssertionError("StubE2.submit: no result scripted")
            return self.submit_results.pop(0)

    def capacity_changed(self) -> None:
        self._record("capacity_changed")

    def requeue(self, job_id: int) -> bool:
        self._record("requeue", job_id)
        return self.requeue_result

    def cancel_queued(self, job_id: int, by: str) -> bool:
        self._record("cancel_queued", job_id, by)
        return self.cancel_result

    def pause_admission(self, reason: str) -> None:
        self._record("pause_admission", reason)

    def resume_admission(self) -> None:
        self._record("resume_admission")

    def set_policy(self, policy: Literal["fcfs", "need"]) -> None:
        self._record("set_policy", policy)

    def queue_view(self) -> list[QueueEntry]:
        self._record("queue_view")
        with self._lock:
            return list(self.queue)


class StubE3(_Recorder):
    """snapshot gives snapshot_value (the S0 seed); allocate gives the scripted allocate_results in
    order; release gives Released once per committed job, then AlreadyReleased (invariant 4)."""

    def __init__(self) -> None:
        super().__init__()
        self.snapshot_value = s0_free()
        self.allocate_results: list[Committed | Refused] = []
        self._open: dict[int, int] = {}  # job_id -> allocation_id

    def snapshot(self) -> Snapshot:
        self._record("snapshot")
        return self.snapshot_value

    def allocate(self, job_id: int, vector: Vector) -> Committed | Refused:
        self._record("allocate", job_id, vector)
        with self._lock:
            if not self.allocate_results:
                raise AssertionError("StubE3.allocate: no result scripted")
            result = self.allocate_results.pop(0)
            if isinstance(result, Committed):
                self._open[job_id] = result.allocation_id
            return result

    def release(self, job_id: int) -> Released | AlreadyReleased:
        self._record("release", job_id)
        with self._lock:
            allocation_id = self._open.pop(job_id, None)
        return AlreadyReleased() if allocation_id is None else Released(allocation_id)


class RecordingHub:
    """Keeps every published event (message 12)."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def publish(self, event: dict[str, Any]) -> None:
        assert_outside_tx("RecordingHub.publish")
        with self._lock:
            self.events.append(event)


class StubE4(_Recorder):
    """Records heartbeats and worker calls; heartbeat gives alive; close_worker is True once per
    attempt; publish goes to the hub."""

    def __init__(self, hub: RecordingHub) -> None:
        super().__init__()
        self.hub = hub
        self.alive = True
        self._closed: set[int] = set()

    def heartbeat(self, attempt_id: int) -> bool:
        self._record("heartbeat", attempt_id)
        return self.alive

    def register_worker(self, attempt_id: int) -> None:
        self._record("register_worker", attempt_id)

    def close_worker(self, attempt_id: int) -> bool:
        self._record("close_worker", attempt_id)
        with self._lock:
            first = attempt_id not in self._closed
            self._closed.add(attempt_id)
        return first

    def publish(self, event: dict[str, Any]) -> None:
        self._record("publish", event)
        self.hub.publish(event)


@dataclass(frozen=True)
class Stubs:
    e1: StubE1
    e2: StubE2
    e3: StubE3
    e4: StubE4
    hub: RecordingHub


def install_stubs() -> Stubs:
    """Fresh stubs for every engine, installed as app.common.ports.engines()."""
    hub = RecordingHub()
    stubs = Stubs(e1=StubE1(), e2=StubE2(), e3=StubE3(), e4=StubE4(hub), hub=hub)
    install(Engines(e1=stubs.e1, e2=stubs.e2, e3=stubs.e3, e4=stubs.e4))
    return stubs
