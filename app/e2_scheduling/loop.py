"""E2's admission loop (plan 8.3, D-75): one thread runs run_cycle() every admission_cycle_s, or at once
after a wake (capacity_changed() or a submit). A single thread means cycles never overlap; run_cycle()
also holds E2's lock from start to end (D-104).
"""
from __future__ import annotations

import threading
from typing import Protocol

from app.common.clock import Clock
from app.common.logging import log
from app.config import Config


class Admitter(Protocol):
    """What the loop drives: E2Service."""

    @property
    def wake(self) -> threading.Event: ...

    def run_cycle(self) -> None: ...


class AdmissionLoop(threading.Thread):
    """The one admission thread of the process; main.py starts it once E4's reconciliation and E2's rebuild
    are done (plan 12.1).

    Each turn waits for the period or the wake Event, clears the Event and runs one cycle, so a wake during a
    cycle leaves the Event set and exactly one more cycle follows at once; no wake is lost. A cycle that
    raises is logged with its traceback when the failure starts or changes, (type, message); repeats are only
    counted, the loop goes on, and the first cycle that succeeds afterwards logs the recovery.
    """

    def __init__(self, service: Admitter, clock: Clock, config: Config) -> None:
        super().__init__(name="e2-admission", daemon=True)
        self._service = service
        self._clock = clock
        self._period = config.admission_cycle_s
        self._stopping = threading.Event()  # not _stop: threading.Thread has a method of that name
        self._failure: tuple[str, str] | None = None  # (type, message) of the failure last logged
        self._failed_cycles = 0
        self._failed_since = 0.0

    def stop(self, timeout: float) -> bool:
        """End the loop: wake it so a wait returns at once, then wait up to timeout seconds for a cycle in
        progress to finish. True once the thread has ended."""
        self._stopping.set()
        self._service.wake.set()
        self.join(timeout)
        if self.is_alive():
            log.warning("admission_loop_stop_timeout", engine="E2", timeout_s=timeout)
            return False
        return True

    def run(self) -> None:
        log.info("admission_loop_started", engine="E2", period_s=self._period)
        try:
            self._turns()
        except BaseException:  # logged and re-raised: an error outside run_cycle() (the clock's wait, or the failure logging) or SystemExit
            log.exception("admission_loop_died", engine="E2")
            raise
        log.info("admission_loop_stopped", engine="E2")

    def _turns(self) -> None:
        while not self._stopping.is_set():
            self._clock.wait(self._service.wake, self._period)
            self._service.wake.clear()  # before the cycle, so a wake during it starts the next one
            if self._stopping.is_set():
                return
            try:
                self._service.run_cycle()
            except Exception as exc:  # noqa: BLE001 (plan 8.3: no failed cycle may end admission; logged once per change)
                self._failed(exc)
            else:
                self._recovered()

    def _failed(self, exc: Exception) -> None:
        """Called inside the except block, so log.exception() adds the error and its traceback. A database
        error is keyed by its SQLSTATE, whose message varies (process ids, for one); others by message."""
        sqlstate: str | None = getattr(exc, "sqlstate", None)
        failure = (type(exc).__qualname__, sqlstate or str(exc))
        if self._failure is None:
            self._failed_since = self._clock.monotonic()
        self._failed_cycles += 1
        if failure != self._failure:
            self._failure = failure
            log.exception("admission_cycle_failed", engine="E2", failed_cycles=self._failed_cycles)

    def _recovered(self) -> None:
        if self._failure is None:
            return
        log.info("admission_cycle_recovered", engine="E2", failed_cycles=self._failed_cycles,
                 failed_s=round(self._clock.monotonic() - self._failed_since, 3))
        self._failure = None
        self._failed_cycles = 0
