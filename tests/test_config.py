"""Slice A-1: app/config.py against plan section 3.3 and .env.example."""

import dataclasses
from pathlib import Path

import pytest

from app.config import Config

REPO_ROOT = Path(__file__).resolve().parents[1]

# Plan section 3.3, and the python_ml caps of D-85: every variable with its default.
TABLE_3_3 = {
    "DATABASE_URL": "postgresql://orch_app:orch@localhost:5432/orch",
    "ADMIN_DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/postgres",
    "DATA_DIR": Path("./data"),
    "SANDBOX_BACKEND": "docker",
    "SANDBOX_IMAGE": "judge-c:1",
    "GPU_MODE": "none",
    "CAPACITY_PROFILE": "s0",
    "ADMISSION_CYCLE_S": 0.5,
    "STARVATION_GUARD_S": 60,
    "HEARTBEAT_S": 2,
    "LOST_AFTER_S": 6,
    "SWEEP_S": 1,
    "MAX_RETRIES": 2,
    "STOP_GRACE_S": 2,
    "PIDS_LIMIT": 64,
    "DEFAULT_POLICY": "fcfs",
    "TOKEN_TTL_S": 28800,
    "SOURCE_MAX_BYTES": 65536,
    "PYTHON_ML_UPLOAD_MAX_BYTES": 1073741824,
    "PYTHON_ML_UNZIPPED_MAX_BYTES": 4294967296,
    "PYTHON_ML_MAX_ENTRIES": 10000,
    "OUTPUT_CAP_BYTES": 65536,
    "COMPILE_TIMEOUT_S": 10,
    "STOP_CONFIRM_TIMEOUT_S": 10,
    "DB_POOL_MIN": 4,
    "DB_POOL_MAX": 40,
    "HEALTH_MIN_AVAILABLE_PCT": 10,
}


def test_defaults_match_table_3_3():
    config = Config.from_env({})
    assert {f.name.upper(): getattr(config, f.name) for f in dataclasses.fields(config)} == TABLE_3_3


def test_env_example_lists_every_variable_with_its_default():
    pairs = []
    for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, _, value = line.partition("=")
            pairs.append((name, value))
    assert sorted(name for name, _ in pairs) == sorted(TABLE_3_3)
    assert Config.from_env(dict(pairs)) == Config()


def test_set_variables_override_defaults():
    config = Config.from_env(
        {
            "DATA_DIR": "/srv/orch/data",
            "SANDBOX_BACKEND": "fake",
            "GPU_MODE": "simulated",
            "ADMISSION_CYCLE_S": "0.25",
            "MAX_RETRIES": "0",
            "DB_POOL_MAX": " 8 ",
        }
    )
    assert config.data_dir == Path("/srv/orch/data")
    assert config.sandbox_backend == "fake"
    assert config.gpu_mode == "simulated"
    assert config.admission_cycle_s == 0.25
    assert config.max_retries == 0
    assert config.db_pool_max == 8
    assert config.heartbeat_s == 2.0  # not set, so it keeps the default


def test_from_env_reads_the_process_environment_by_default(monkeypatch):
    for name in TABLE_3_3:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HEARTBEAT_S", "3")
    assert Config.from_env().heartbeat_s == 3.0


def test_config_is_frozen():
    config = Config()
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(config, "heartbeat_s", 5.0)  # noqa: B010 (what an assignment does, without the type error)


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("MAX_RETRIES", "two"),
        ("PIDS_LIMIT", "64.5"),
        ("HEARTBEAT_S", "fast"),
        ("HEARTBEAT_S", "0"),
        ("LOST_AFTER_S", "-6"),
        ("SWEEP_S", "nan"),
        ("MAX_RETRIES", "-1"),
        ("SANDBOX_BACKEND", "podman"),
        ("GPU_MODE", "cuda"),
        ("CAPACITY_PROFILE", "S0"),
        ("DATABASE_URL", "  "),
    ],
)
def test_bad_value_is_rejected_naming_the_variable(name, raw):
    with pytest.raises(ValueError, match=name):
        Config.from_env({name: raw})
