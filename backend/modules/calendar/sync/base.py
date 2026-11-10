"""The adapter contract every provider implements (see tests/contract.py)."""

from __future__ import annotations

from typing import Protocol

from modules.calendar.ics import RemoteEvent


class SyncConflict(Exception):
    """The remote copy changed since our etag (HTTP 412)."""


class CalendarAdapter(Protocol):
    def changes(self, sync_token: str | None) -> tuple[list[RemoteEvent], list[str], str | None]:
        """(changed or new events, ids deleted remotely, next sync token). ``None`` = full listing."""

    def put(self, event: RemoteEvent, remote_id: str | None, etag: str | None) -> tuple[str, str | None]:
        """Create (``remote_id`` None) or update; returns (remote id, new etag)."""

    def delete(self, remote_id: str, etag: str | None) -> None:
        """Delete remotely (missing is fine)."""
