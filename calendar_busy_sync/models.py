"""Provider-independent unavailable intervals; never carry private event details."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

GRAPH_PROPERTY_ID = "String {e575b7e1-a0e2-4dd8-8b82-c0a3a37f104b} Name calendar-busy-sync"


class SyncError(Exception):
    """An operator-safe error that contains no provider response or event data."""


class CopyIdUnavailable(SyncError):
    """A deleted provider resource still reserves this copy generation's ID."""


@dataclass(frozen=True)
class Event:
    id: str
    start: datetime
    end: datetime
    busy: bool = True
    marker: str | None = None

    def __post_init__(self):
        if not self.id or not isinstance(self.id, str):
            raise SyncError("A calendar returned an event without an identifier; sync stopped.")
        if self.start.tzinfo is None or self.end.tzinfo is None or self.end <= self.start:
            raise SyncError("A calendar returned an invalid time interval; sync stopped.")


class CalendarClient(Protocol):
    def list_events(self, start: datetime, end: datetime) -> list[Event]: ...

    def get_event(self, event_id: str) -> Event | None: ...

    def create_busy(self, event: Event, marker: str) -> Event: ...

    def update_busy(self, event_id: str, event: Event, marker: str) -> Event: ...

    def delete_busy(self, event_id: str, marker: str) -> None: ...
