"""The only way one engine reaches another (plan 5.4; message numbers from context §8).

Every call through a port happens outside a transaction: an engine registers it with
cur.after_commit() (invariant 3). transition() is not a port: it runs inside the caller's
transaction, so engines import it from app.e4_monitoring.state.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path  # at run time: typing.get_type_hints() resolves the port's annotations (test_ports)
from typing import Any, Literal, Protocol

from app.common.types import (AlreadyReleased, Committed, Limits, QueueEntry, Refused, Released, Snapshot,
                              SubmitRequest, SubmitResult, User, Vector)


class E3Port(Protocol):
    def snapshot(self) -> Snapshot: ...                                            # message 3
    def allocate(self, job_id: int, vector: Vector) -> Committed | Refused: ...   # message 4
    def release(self, job_id: int) -> Released | AlreadyReleased: ...             # messages 7, 10


class E2Port(Protocol):
    def submit(self, user: User, req: SubmitRequest, source: Path) -> SubmitResult:  # message 2
        """source is the spooled upload (D-90): a plain file directly in DATA_DIR/spool that the caller has
        closed (on Windows os.replace fails on an open file). Moved into the job's payload folder on success;
        left where it is after a 400, a ValueError or any other failure, for the caller to delete."""
        ...
    def capacity_changed(self) -> None: ...                                # message 8
    def requeue(self, job_id: int) -> bool: ...                            # message 11; False = retries used up
    def cancel_queued(self, job_id: int, by: str) -> bool: ...             # message 11; proposal name
    def pause_admission(self, reason: str) -> None: ...                    # message 11
    def resume_admission(self) -> None: ...                                # message 11
    def set_policy(self, policy: Literal["fcfs", "need"]) -> None: ...     # proposal name
    def queue_view(self) -> list[QueueEntry]: ...                          # proposal name


class E1Port(Protocol):
    def dispatch(self, job_id: int, allocation_id: int, limits: Limits, file_ref: str) -> int: ...  # message 5
    def stop(self, job_id: int) -> None: ...      # message 9: stop and confirm; raises StopNotConfirmed
    def cancel(self, job_id: int, by: str) -> None: ...                    # message 9; proposal name


class E4Port(Protocol):
    def heartbeat(self, attempt_id: int) -> bool: ...        # message 6; False = ignored (worker not alive)
    def register_worker(self, attempt_id: int) -> None: ...  # proposal name
    def close_worker(self, attempt_id: int) -> bool: ...     # proposal name; True = caller owns the ending
    def publish(self, event: dict[str, Any]) -> None: ...    # message 12; outside transactions only


@dataclass
class Engines:
    e1: E1Port
    e2: E2Port
    e3: E3Port
    e4: E4Port


_installed: Engines | None = None


def install(e: Engines) -> None:
    """main.py installs the real engines; tests install the stubs of tests/stubs.py."""
    global _installed
    _installed = e


def engines() -> Engines:
    if _installed is None:
        raise RuntimeError("no engines installed: main.py calls install(), tests call install_stubs()")
    return _installed
