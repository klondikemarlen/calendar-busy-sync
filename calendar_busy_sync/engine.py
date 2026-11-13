"""Snapshot first, reconcile only owned copies, checkpoint each remote mutation."""

import fcntl
import hashlib
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import health
from .models import CalendarClient, CopyIdUnavailable, Event, SyncError
from .settings import Paths, Settings, load_settings
from .state import Mapping, State
from .timing import TimingPolicy

_CONTENT_VERSION = 1


@contextmanager
def sync_lock(paths: Paths):
    paths.ensure()
    descriptor = os.open(paths.lock_file, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SyncError("Another sync or setup is running. Wait for it to finish.") from error
        yield
    finally:
        os.close(descriptor)


def _window(settings: Settings, now: datetime):
    return now - timedelta(days=settings.days_back), now + timedelta(days=settings.days_forward)


def _marker(settings: Settings, source: str, source_id: str) -> str:
    calendar = settings.google_calendar if source == "google" else settings.outlook_calendar
    key = hashlib.sha256(f"{calendar}\0{source_id}".encode()).hexdigest()
    return f"cbs1:{settings.installation_id}:{source}:{key}"


@dataclass(frozen=True)
class Operation:
    kind: str
    mapping: Mapping
    event: Event | None = None


def _owned(event: Event | None, marker: str) -> Event | None:
    if event is not None and event.marker != marker:
        raise SyncError("A tracked copy lost its ownership marker; no unrelated event was changed.")
    return event


def _generation_copies(events: list[Event], marker: str) -> list[Event]:
    source_key = ":".join(marker.split(":")[:4])
    return [
        event
        for event in events
        if event.marker == source_key or (event.marker and event.marker.startswith(source_key + ":"))
    ]


def _plan_direction(settings, state, clients, snapshots, source, target, start, end, timing):
    originals = {}
    for event in snapshots[source]:
        desired = timing.desired_copy(source, event, start, end)
        if desired is not None:
            originals[event.id] = desired
    mappings = {row.source_id: row for row in state.mappings(source, target)}
    operations = []

    for source_id, original in originals.items():
        base_marker = _marker(settings, source, source_id)
        mapping = mappings.get(source_id)
        candidates = _generation_copies(snapshots[target], base_marker)
        if mapping and not (mapping.marker == base_marker or mapping.marker.startswith(base_marker + ":")):
            raise SyncError(
                "The selected calendars changed. Restore the original configuration before reconciling existing copies."
            )
        if mapping:
            marker = mapping.marker
        elif candidates:
            marker = candidates[0].marker
        else:
            marker = f"{base_marker}:{uuid.uuid4().hex}"
        if mapping and mapping.target_id and all(event.id != mapping.target_id for event in candidates):
            tracked = _owned(clients[target].get_event(mapping.target_id), marker)
            if tracked:
                candidates.append(tracked)
            elif not candidates:
                marker = f"{base_marker}:{uuid.uuid4().hex}"
        if not candidates:
            operations.append(Operation("created", Mapping(source, source_id, target, "", marker), original))
            continue
        candidates.sort(key=lambda event: (event.id != (mapping.target_id if mapping else ""), event.id))
        chosen = candidates[0]
        content_version = (
            mapping.content_version
            if mapping and mapping.target_id == chosen.id and mapping.marker == chosen.marker
            else 0
        )
        mapping = Mapping(source, source_id, target, chosen.id, chosen.marker, content_version)
        kind = "unchanged"
        if (chosen.start, chosen.end, chosen.busy) != (
            original.start,
            original.end,
            True,
        ) or content_version < _CONTENT_VERSION:
            kind = "updated"
        operations.append(Operation(kind, mapping, original))
        for duplicate in candidates[1:]:
            operations.append(
                Operation("duplicate", Mapping(source, source_id, target, duplicate.id, duplicate.marker))
            )

    for source_id, mapping in mappings.items():
        if source_id in originals:
            continue
        original = clients[source].get_event(source_id)
        if original is not None and timing.desired_copy(source, original, start, end) is not None:
            raise SyncError(
                "The source snapshot omitted an active event. No changes were applied; retry a complete sync."
            )
        candidates = _generation_copies(snapshots[target], mapping.marker)
        if mapping.target_id and all(event.id != mapping.target_id for event in candidates):
            tracked = _owned(clients[target].get_event(mapping.target_id), mapping.marker)
            if tracked:
                candidates.append(tracked)
        for candidate in candidates:
            operations.append(
                Operation("deleted", Mapping(source, source_id, target, candidate.id, candidate.marker))
            )
        operations.append(Operation("forget", mapping))
    return operations


def synchronize(
    settings: Settings, paths: Paths, clients: dict[str, CalendarClient], dry_run=False, now=None
):
    settings.validate()
    now = now or datetime.now(UTC)
    start, end = _window(settings, now)
    timing = TimingPolicy(settings)
    query_start, query_end = timing.query_window(start, end)
    # Both paginated views must complete before planning or writing anything.
    snapshots = {name: client.list_events(query_start, query_end) for name, client in clients.items()}
    if set(snapshots) != {"google", "outlook"}:
        raise SyncError("Both calendar connections are required before syncing.")
    for events in snapshots.values():
        if len({event.id for event in events}) != len(events):
            raise SyncError("A calendar returned duplicate event identifiers; sync stopped.")
    counts = dict.fromkeys(("created", "updated", "deleted", "unchanged"), 0)
    directions = [("google", "outlook")]
    if settings.direction == "both":
        directions.append(("outlook", "google"))

    with State(paths) as state:
        operations = []
        for source, target in directions:
            operations.extend(
                _plan_direction(settings, state, clients, snapshots, source, target, start, end, timing)
            )
        for operation in operations:
            kind, mapping, original = operation.kind, operation.mapping, operation.event
            counter = "deleted" if kind == "duplicate" else kind
            if counter in counts:
                counts[counter] += 1
            if dry_run:
                continue
            client = clients[mapping.target]
            if kind == "created":
                # Persist intent before POST so a process crash can recover its owned copy.
                state.remember(mapping)
                try:
                    created = client.create_busy(original, mapping.marker)
                except CopyIdUnavailable:
                    # A confirmed cancelled resource may retain its old ID indefinitely.
                    marker = f"{_marker(settings, mapping.source, mapping.source_id)}:{uuid.uuid4().hex}"
                    mapping = Mapping(mapping.source, mapping.source_id, mapping.target, "", marker)
                    state.remember(mapping)
                    created = client.create_busy(original, mapping.marker)
                if (created.start, created.end, created.busy) != (original.start, original.end, True):
                    created = client.update_busy(created.id, original, mapping.marker)
                _owned(created, mapping.marker)
                state.remember(
                    Mapping(
                        mapping.source,
                        mapping.source_id,
                        mapping.target,
                        created.id,
                        mapping.marker,
                        _CONTENT_VERSION,
                    )
                )
            elif kind == "updated":
                updated = client.update_busy(mapping.target_id, original, mapping.marker)
                _owned(updated, mapping.marker)
                state.remember(
                    Mapping(
                        mapping.source,
                        mapping.source_id,
                        mapping.target,
                        updated.id,
                        mapping.marker,
                        _CONTENT_VERSION,
                    )
                )
            elif kind in ("deleted", "duplicate"):
                client.delete_busy(mapping.target_id, mapping.marker)
            elif kind == "forget":
                state.forget(mapping)
            else:
                # A prior POST may have succeeded before its response/checkpoint was lost.
                state.remember(mapping)
        if not dry_run:
            state.success(settings, now)
    return counts


def run_sync(settings: Settings | None, paths: Paths, dry_run=False):
    from .auth import build_clients

    with sync_lock(paths):
        try:
            current = load_settings(paths)
            if settings is not None and current != settings:
                raise SyncError("Configuration changed before sync; run the command again.")
            result = synchronize(current, paths, build_clients(current, paths), dry_run=dry_run)
        except (SyncError, OSError, sqlite3.Error):
            if not dry_run:
                health.record(paths, failed=True)
            raise
        if not dry_run:
            health.record(paths, failed=False)
        return result


def doctor(settings: Settings, paths: Paths):
    from .auth import build_clients

    with sync_lock(paths):
        if load_settings(paths) != settings:
            raise SyncError("Configuration changed before the connectivity check; run it again.")
        clients = build_clients(settings, paths)
        now = datetime.now(UTC)
        # Read-only probe: failures surface before any initial synchronization.
        return {
            name: len(client.list_events(now, now + timedelta(days=1))) for name, client in clients.items()
        }
