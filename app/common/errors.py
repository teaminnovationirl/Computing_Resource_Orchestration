"""Errors shared by the engines and the API (plan 5.5)."""
from __future__ import annotations


class ApiError(Exception):
    """An error the API returns as {error, field} with this HTTP status (context §12)."""

    def __init__(self, status: int, error: str, field: str | None = None) -> None:
        super().__init__(error)
        self.status = status
        self.error = error
        self.field = field


class IllegalTransition(Exception):
    """The state trigger refused a transition: psycopg reported SQLSTATE JT001."""


class StopNotConfirmed(Exception):
    """E1 could not confirm that a job's processes stopped (message 9)."""


class NotFound(Exception):
    """The job, attempt or user does not exist."""
