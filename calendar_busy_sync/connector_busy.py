"""Exchange only the union of original Busy intervals; reconcile pair-owned copies."""

import re
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from hashlib import sha256

from .models import CalendarClient, CopyIdUnavailable, Event, SyncError

MAX_INTERVALS = 4096
_PAIR_ID = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_MARKER = re.compile(r"cbs2:([0-9a-f]{64}):([0-9a-f]{64}):[0-9a-f]")
_MAX_COPY_GENERATIONS = 16


def export_snapshot(client: CalendarClient, start: datetime, end: datetime) -> list[list[str]]:
    """Read a complete view, then return clipped, sorted, non-touching UTC intervals."""
    _validate_window(start, end)
    originals = []
    for event in _list_events(client, start, end):
        # The adapters use this field solely for the app's private metadata, including legacy copies.
        if not event.busy or event.marker:
            continue
        clipped_start = max(start, event.start.astimezone(UTC))
        clipped_end = min(end, event.end.astimezone(UTC))
        if clipped_start < clipped_end:
            # A whole-second wire representation must not understate real Busy time.
            clipped_start = clipped_start.replace(microsecond=0)
            if clipped_end.microsecond:
                clipped_end = clipped_end.replace(microsecond=0) + timedelta(seconds=1)
            originals.append((clipped_start, clipped_end))

    union: list[tuple[datetime, datetime]] = []
    for interval_start, interval_end in sorted(originals):
        if union and interval_start <= union[-1][1]:
            previous_start, previous_end = union[-1]
            union[-1] = previous_start, max(previous_end, interval_end)
        else:
            union.append((interval_start, interval_end))
            if len(union) > MAX_INTERVALS:
                raise SyncError("The calendar contains too many Busy intervals for a complete snapshot.")
    return [
        [_utc_string(interval_start), _utc_string(interval_end)] for interval_start, interval_end in union
    ]


def apply_snapshot(
    client: CalendarClient, pair_id: str, start: datetime, end: datetime, intervals: list[list[str]]
) -> None:
    """Apply a complete snapshot after its envelope has been authenticated by the caller."""
    _validate_pair_id(pair_id)
    _validate_window(start, end)
    desired_copies = _parse_intervals(intervals, start, end)
    copies = _list_owned(client, pair_id, start, end)

    # Establish all desired coverage first. A failed create/update must not cause stale deletions.
    stale_copies = []
    for desired in desired_copies:
        candidates = copies.pop(desired.id, [])
        if not candidates:
            _create_copy(client, pair_id, desired)
            continue
        chosen = min(candidates, key=lambda candidate: (not _matches(candidate[0], desired), candidate[0].id))
        existing, marker = chosen
        _ensure_busy_copy(client, existing, desired, marker)
        stale_copies.extend(candidate for candidate in candidates if candidate != chosen)

    for candidates in copies.values():
        stale_copies.extend(candidates)
    for event, marker in stale_copies:
        client.delete_busy(event.id, marker)


def cleanup_owned(client: CalendarClient, pair_id: str, start: datetime, end: datetime) -> None:
    """Remove exact pair-owned copies visible in the requested UTC day range."""
    _validate_pair_id(pair_id)
    _validate_window(start, end)
    for candidates in _list_owned(client, pair_id, start, end).values():
        for event, marker in candidates:
            client.delete_busy(event.id, marker)


def _validate_pair_id(pair_id: str) -> None:
    if type(pair_id) is not str or not _PAIR_ID.fullmatch(pair_id):
        raise SyncError("Invalid connector pair identifier.")


def _validate_window(start: datetime, end: datetime) -> None:
    for boundary in (start, end):
        if (
            not isinstance(boundary, datetime)
            or boundary.utcoffset() != timedelta(0)
            or boundary.time() != time.min
        ):
            raise SyncError("The connector snapshot window must use UTC midnight boundaries.")
    if start >= end:
        raise SyncError("Invalid connector snapshot window.")


def _utc_string(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if type(value) is not str or not _TIMESTAMP.fullmatch(value):
        raise SyncError("The connector interval must use whole-second UTC timestamps.")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise SyncError("Invalid connector interval timestamp.") from None


def _parse_intervals(intervals: list[list[str]], start: datetime, end: datetime) -> list[Event]:
    if type(intervals) is not list or len(intervals) > MAX_INTERVALS:
        raise SyncError("Invalid connector snapshot interval list.")

    desired = []
    previous_end = None
    for interval in intervals:
        if type(interval) is not list or len(interval) != 2:
            raise SyncError("Invalid connector snapshot interval shape.")
        start_text, end_text = interval
        interval_start = _parse_timestamp(start_text)
        interval_end = _parse_timestamp(end_text)
        if not start <= interval_start < interval_end <= end:
            raise SyncError("A connector interval is empty or outside the snapshot window.")
        if previous_end is not None and interval_start <= previous_end:
            raise SyncError("Connector intervals must be sorted and merged.")

        # Independent of the rolling horizon and any provider's event identifiers.
        key = sha256(f"{start_text}/{end_text}".encode("ascii")).hexdigest()
        desired.append(Event(key, interval_start, interval_end))
        previous_end = interval_end
    return desired


def _list_events(client: CalendarClient, start: datetime, end: datetime) -> list[Event]:
    # The client raises if any page is incomplete; duplicate IDs also invalidate the view.
    events = client.list_events(start, end)
    if not isinstance(events, list):
        raise SyncError("The calendar returned an incomplete event snapshot.")
    seen_ids = set()
    for event in events:
        if (
            not isinstance(event, Event)
            or event.id in seen_ids
            or not isinstance(event.busy, bool)
            or (event.marker is not None and not isinstance(event.marker, str))
        ):
            raise SyncError("The calendar returned invalid or duplicate snapshot events.")
        seen_ids.add(event.id)
    return events


def _list_owned(
    client: CalendarClient, pair_id: str, start: datetime, end: datetime
) -> dict[str, list[tuple[Event, str]]]:
    copies = defaultdict(list)
    for event in _list_events(client, start, end):
        marker = event.marker
        if not marker:
            continue
        owned = _MARKER.fullmatch(marker)
        if (
            owned
            and owned[1] == pair_id
            and event.start.astimezone(UTC) < end
            and event.end.astimezone(UTC) > start
        ):
            copies[owned[2]].append((event, marker))
    return copies


def _matches(existing: Event, desired: Event) -> bool:
    return (
        existing.busy
        and existing.start.astimezone(UTC) == desired.start
        and existing.end.astimezone(UTC) == desired.end
    )


def _ensure_busy_copy(client: CalendarClient, existing: Event, desired: Event, marker: str) -> None:
    if existing.marker != marker:
        raise SyncError("The calendar did not confirm ownership of the connector copy.")
    if not _matches(existing, desired):
        updated = client.update_busy(existing.id, desired, marker)
        if updated.id != existing.id or updated.marker != marker or not _matches(updated, desired):
            raise SyncError("The calendar did not confirm the updated connector copy.")


def _create_copy(client: CalendarClient, pair_id: str, desired: Event) -> None:
    # Retry the same marker after uncertain writes. Advance only on a confirmed reserved tombstone.
    for generation in range(_MAX_COPY_GENERATIONS):
        marker = f"cbs2:{pair_id}:{desired.id}:{generation:x}"
        try:
            created = client.create_busy(desired, marker)
        except CopyIdUnavailable:
            continue
        _ensure_busy_copy(client, created, desired, marker)
        return
    raise SyncError("The calendar has reserved too many deleted identifiers for this Busy interval.")
