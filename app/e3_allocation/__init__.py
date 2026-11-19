"""E3 public interface (plan 6.1): snapshot; allocate and release join it in E3-2 and E3-3. Other
engines reach E3 only through app.common.ports.
"""
from app.e3_allocation.service import snapshot

__all__ = ["snapshot"]
