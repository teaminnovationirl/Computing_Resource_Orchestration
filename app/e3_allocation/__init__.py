"""E3 public interface (plan 6.1): snapshot, allocate and release. Other engines reach E3 only through
app.common.ports.
"""
from app.e3_allocation.service import allocate, release, snapshot

__all__ = ["allocate", "release", "snapshot"]
