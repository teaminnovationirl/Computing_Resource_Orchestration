"""Value types shared by the engines (plan 5.3). Integer units only: millicores and MiB (invariant 7).

SubmitRequest and SubmitResult follow the POST /api/jobs body and its 201 reply (context §12,
docs/decisions.md D-52); QueueEntry is a proposal (D-53).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class Vector:
    """A resource vector in integer units."""
    cpu_millicores: int
    ram_mib: int
    vram_mib: int = 0


@dataclass(frozen=True)
class Snapshot:
    """Free capacity per resource (message 3)."""
    cpu_free: int
    ram_free: int
    gpu_free: dict[int, int]  # gpu_index -> free VRAM MiB

    def fits(self, v: Vector) -> bool:
        return (v.cpu_millicores <= self.cpu_free and v.ram_mib <= self.ram_free and
                (v.vram_mib == 0 or any(f >= v.vram_mib for f in self.gpu_free.values())))


@dataclass(frozen=True)
class Limits:
    """What E1 enforces (message 5)."""
    cpu_millicores: int
    ram_mib: int
    gpu_index: int | None
    time_limit_s: int


@dataclass(frozen=True)
class Committed:
    allocation_id: int
    gpu_index: int | None


@dataclass(frozen=True)
class Refused:
    reason: str


@dataclass(frozen=True)
class Released:
    allocation_id: int


@dataclass(frozen=True)
class AlreadyReleased:
    pass


@dataclass(frozen=True)
class User:
    user_id: str
    role: Literal["student", "faculty", "administrator"]


@dataclass(frozen=True)
class SubmitRequest:
    """An authenticated job request (message 2) without its source, which travels as bytes.
    source_name is the uploaded file's name; E2 uses only its extension and never stores it or puts it
    in a path, and "" means the name was not given (D-76)."""
    workload_type: str
    cpu_millicores: int
    ram_mib: int
    vram_mib: int
    time_limit_s: int
    problem_id: int | None = None
    lab_session_id: int | None = None
    source_name: str = field(default="", kw_only=True, repr=False)  # repr=False: never in a log (D-76)


@dataclass(frozen=True)
class SubmitResult:
    """The 201 reply of POST /api/jobs."""
    job_id: int
    state: str
    need_class: int


@dataclass(frozen=True)
class QueueEntry:
    """One queued job, in queue order (need_class, submitted_at, job_id)."""
    job_id: int
    user_id: str
    need_class: int
    submitted_at: datetime
    vector: Vector
