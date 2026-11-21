"""E2 Workload Scheduling (plan 8). One E2Service per process; main.py installs it as the E2Port (D-77)."""
from app.e2_scheduling.service import E2Service

__all__ = ["E2Service"]
