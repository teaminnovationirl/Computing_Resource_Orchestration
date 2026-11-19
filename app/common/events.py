"""The live-update hub interface (message 12). The hub itself comes with slice E4-4, and so do the
event builders: plan 5 gives no event fields (docs/decisions.md D-54)."""
from __future__ import annotations

from typing import Any, Protocol


class Hub(Protocol):
    def publish(self, event: dict[str, Any]) -> None:
        """Push one event to the pages; only outside a transaction (invariant 3)."""
        ...
