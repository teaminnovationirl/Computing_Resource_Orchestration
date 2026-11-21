"""Server configuration (plan section 3.3).

One frozen dataclass read from environment variables; .env.example lists
all of them. Each field is named after its variable in lower case.
Baseline numbers are fixed defaults. Changing a proposal default needs a
decision line in docs/decisions.md.
"""

import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Self, cast

# Allowed values of the variables that name a mode.
CHOICES: dict[str, tuple[str, ...]] = {
    "sandbox_backend": ("docker", "fake"),
    "gpu_mode": ("none", "simulated", "nvidia"),
    "capacity_profile": ("s0", "host"),
}


@dataclass(frozen=True)
class Config:
    # Proposals, except capacity_profile "s0", which comes from S0.
    database_url: str = "postgresql://orch_app:orch@localhost:5432/orch"
    # Tests and scripts only.
    admin_database_url: str = "postgresql://postgres:postgres@localhost:5432/postgres"
    data_dir: Path = Path("./data")
    sandbox_backend: str = "docker"  # "fake" in tests
    sandbox_image: str = "judge-c:1"
    gpu_mode: str = "none"
    capacity_profile: str = "s0"

    # Baseline.
    admission_cycle_s: float = 0.5
    starvation_guard_s: float = 60.0  # FR-01
    heartbeat_s: float = 2.0
    lost_after_s: float = 6.0  # FR-12
    sweep_s: float = 1.0
    max_retries: int = 2  # FR-14
    stop_grace_s: float = 2.0  # SIGTERM, then SIGKILL after 2 s
    pids_limit: int = 64
    default_policy: str = "fcfs"

    # Proposals.
    token_ttl_s: float = 28800.0  # 8 h
    source_max_bytes: int = 65536
    # python_ml uploads (D-85): one .py or one .zip; the zip's caps count its entries and the sizes
    # they declare.
    python_ml_upload_max_bytes: int = 1073741824  # 1 GiB
    python_ml_unzipped_max_bytes: int = 4294967296  # 4 GiB
    python_ml_max_entries: int = 10000
    output_cap_bytes: int = 65536  # per test case
    compile_timeout_s: float = 10.0
    stop_confirm_timeout_s: float = 10.0
    db_pool_min: int = 4
    db_pool_max: int = 40
    # Host MemAvailable as a percentage of MemTotal; mirrors the 10% reserve.
    health_min_available_pct: int = 10

    def __post_init__(self) -> None:
        for f in fields(self):
            name, value = f.name.upper(), getattr(self, f.name)
            allowed = CHOICES.get(f.name)
            if allowed is not None and value not in allowed:
                raise ValueError(f"{name} must be one of {', '.join(allowed)}, got {value!r}")
            if f.type in (int, float):
                zero_ok = f.name == "max_retries"
                if not math.isfinite(value) or value < 0 or (value == 0 and not zero_ok):
                    rule = "0 or more" if zero_ok else "more than 0"
                    raise ValueError(f"{name} must be {rule}, got {value!r}")

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Self:
        """Build a Config from env. A variable that is not set keeps its default."""
        values: dict[str, Any] = {}
        for f in fields(cls):
            name = f.name.upper()
            raw = env.get(name)
            if raw is None:
                continue
            text = raw.strip()
            if not text:
                raise ValueError(f"{name} is set but empty")
            parse = cast(Callable[[str], object], f.type)  # the annotation itself: str, Path, int or float
            try:
                values[f.name] = parse(text)
            except ValueError as exc:
                kind = "a whole number" if f.type is int else "a number"
                raise ValueError(f"{name} must be {kind}, got {raw!r}") from exc
        return cls(**values)
