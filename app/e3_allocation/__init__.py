"""E3 public interface (plan 6.1): snapshot and allocate; release joins it in E3-3. Other engines
reach E3 only through app.common.ports.
"""
from app.e3_allocation.service import allocate, snapshot

__all__ = ["allocate", "snapshot"]
