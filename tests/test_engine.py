import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from calendar_busy_sync.engine import sync_lock, synchronize
from calendar_busy_sync.models import Event, SyncError
from calendar_busy_sync.settings import Paths, Settings, load_settings, save_settings
from calendar_busy_sync.state import State, has_success

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


class MemoryCalendar:
    def __init__(self, events=()):
        self.events = {event.id: event for event in events}
        self.writes = []
        self.fail_read = False
        self.lose_create_response = False

    def list_events(self, start, end):
        if self.fail_read:
            raise SyncError("Calendar snapshot failed.")
        return [event for event in self.events.values() if event.start < end and event.end > start]

    def get_event(self, event_id):
        return self.events.get(event_id)

    def create_busy(self, event, marker):
        copy = replace(event, id=f"copy-{len(self.writes)}", marker=marker, busy=True)
        self.events[copy.id] = copy
        self.writes.append(("create", copy.id))
        if self.lose_create_response:
            self.lose_create_response = False
            raise SyncError("The create result is uncertain.")
        return copy

    def update_busy(self, event_id, event, marker):
        if self.events[event_id].marker != marker:
            raise SyncError("Unowned event.")
        copy = replace(event, id=event_id, marker=marker, busy=True)
        self.events[event_id] = copy
        self.writes.append(("update", event_id))
        return copy

    def delete_busy(self, event_id, marker):
        if self.events[event_id].marker != marker:
            raise SyncError("Unowned event.")
        del self.events[event_id]
        self.writes.append(("delete", event_id))


def event(identifier, offset=1):
    start = NOW + timedelta(hours=offset)
    return Event(identifier, start, start + timedelta(hours=1))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.paths = Paths(root / "config", root / "state")
        self.settings = Settings(microsoft_client_id="00000000-0000-0000-0000-000000000001")
        self.google = MemoryCalendar([event("personal")])
        self.outlook = MemoryCalendar([event("work", 3)])
        self.clients = {"google": self.google, "outlook": self.outlook}

    def sync(self, **kwargs):
        return synchronize(self.settings, self.paths, self.clients, now=NOW, **kwargs)

    def test_original_lifecycle_reconciles_one_owned_copy_without_touching_work(self):
        original_work = self.outlook.events["work"]
        self.assertEqual(self.sync(), {"created": 1, "updated": 0, "deleted": 0, "unchanged": 0})
        self.assertEqual(self.sync(), {"created": 0, "updated": 0, "deleted": 0, "unchanged": 1})
        self.google.events["personal"] = event("personal", 2)
        self.assertEqual(self.sync(), {"created": 0, "updated": 1, "deleted": 0, "unchanged": 0})
        copy = next(value for value in self.outlook.events.values() if value.marker)
        self.assertEqual((copy.start, copy.end), (event("personal", 2).start, event("personal", 2).end))
        del self.google.events["personal"]
        self.assertEqual(self.sync(), {"created": 0, "updated": 0, "deleted": 1, "unchanged": 0})
        self.assertEqual(self.outlook.events, {"work": original_work})

    def test_bidirectional_mode_does_not_copy_copies_back(self):
        self.settings.direction = "both"
        self.sync()
        second = self.sync()
        self.assertEqual(second, {"created": 0, "updated": 0, "deleted": 0, "unchanged": 2})
        self.assertEqual(
            [entry for entry in self.google.events if not self.google.events[entry].marker], ["personal"]
        )
        self.assertEqual(
            [entry for entry in self.outlook.events if not self.outlook.events[entry].marker], ["work"]
        )

    def test_a_failed_snapshot_prevents_all_calendar_mutations(self):
        self.settings.direction = "both"
        self.outlook.fail_read = True
        with self.assertRaises(SyncError):
            self.sync()
        self.assertEqual(self.google.writes, [])
        self.assertEqual(self.outlook.writes, [])
        self.assertFalse(has_success(self.paths, self.settings))

    def test_lost_create_response_recovers_without_another_post(self):
        self.outlook.lose_create_response = True
        with self.assertRaises(SyncError):
            self.sync()
        self.assertFalse(has_success(self.paths, self.settings))
        self.assertEqual(self.sync(), {"created": 0, "updated": 1, "deleted": 0, "unchanged": 0})
        self.assertEqual(self.outlook.writes, [("create", "copy-0"), ("update", "copy-0")])
        self.assertEqual(self.sync(), {"created": 0, "updated": 0, "deleted": 0, "unchanged": 1})

    def test_unowned_replacement_is_never_deleted(self):
        self.sync()
        copy = next(value for value in self.outlook.events.values() if value.marker)
        self.outlook.events[copy.id] = replace(copy, marker=None)
        del self.google.events["personal"]
        with self.assertRaises(SyncError):
            self.sync()
        self.assertIn(copy.id, self.outlook.events)
        self.assertEqual(self.outlook.writes, [("create", copy.id)])

    def test_free_source_removes_its_busy_copy(self):
        self.sync()
        self.google.events["personal"] = replace(self.google.events["personal"], busy=False)
        self.assertEqual(self.sync()["deleted"], 1)
        self.assertEqual(set(self.outlook.events), {"work"})

    def test_free_then_busy_uses_a_new_google_copy_generation(self):
        class ReservedCalendar(MemoryCalendar):
            def __init__(self, events):
                super().__init__(events)
                self.used_markers = set()

            def create_busy(self, event, marker):
                if marker in self.used_markers:
                    raise SyncError("A deleted identifier remains reserved.")
                self.used_markers.add(marker)
                return super().create_busy(event, marker)

        self.google = ReservedCalendar(self.google.events.values())
        self.clients["google"] = self.google
        self.settings.direction = "both"
        self.sync()
        first_marker = next(value.marker for value in self.google.events.values() if value.marker)
        self.outlook.events["work"] = replace(self.outlook.events["work"], busy=False)
        self.sync()
        self.outlook.events["work"] = replace(self.outlook.events["work"], busy=True)
        self.assertEqual(self.sync()["created"], 1)
        second_marker = next(value.marker for value in self.google.events.values() if value.marker)
        self.assertNotEqual(first_marker, second_marker)

    def test_source_deletion_removes_restored_older_copy_generations(self):
        self.settings.direction = "both"
        self.sync()
        older_copy = next(value for value in self.google.events.values() if value.marker)
        del self.google.events[older_copy.id]
        self.sync()
        self.google.events[older_copy.id] = older_copy
        del self.outlook.events["work"]
        self.assertEqual(self.sync()["deleted"], 2)
        self.assertEqual(self.google.events, {"personal": event("personal")})

    def test_move_outside_horizon_removes_stale_visible_copy(self):
        self.sync()
        self.google.events["personal"] = event("personal", 24 * 800)
        self.assertEqual(self.sync()["deleted"], 1)
        self.assertEqual(set(self.outlook.events), {"work"})

    def test_horizon_limits_copies_and_respects_configured_days(self):
        self.google.events = {
            "recent": event("recent", -24 * 6),
            "near": event("near", 24 * 59),
            "distant": event("distant", 24 * 90),
            "old": event("old", -24 * 8),
        }
        self.sync()
        with State(self.paths) as state:
            self.assertEqual(
                {row.source_id for row in state.mappings("google", "outlook")}, {"recent", "near"}
            )
        self.settings.days_back = 0
        self.settings.days_forward = 100
        self.sync()
        with State(self.paths) as state:
            self.assertEqual(
                {row.source_id for row in state.mappings("google", "outlook")}, {"near", "distant"}
            )

    def test_dry_run_does_not_write_calendars_or_authorize_a_schedule(self):
        self.assertEqual(self.sync(dry_run=True)["created"], 1)
        self.assertEqual(self.outlook.writes, [])
        self.assertFalse(has_success(self.paths, self.settings))

    def test_changed_configuration_requires_a_new_successful_sync(self):
        self.sync()
        self.assertTrue(has_success(self.paths, self.settings))
        self.settings.direction = "both"
        self.assertFalse(has_success(self.paths, self.settings))

    def test_work_filter_and_travel_changes_reconcile_only_owned_copies(self):
        originals = (dict(self.google.events), dict(self.outlook.events))
        self.settings.work_hours_enabled = True
        self.settings.work_timezone = "Etc/GMT+7"
        self.settings.event_padding_overrides = [
            {"source": "google", "event_id": "personal", "lead_minutes": 60, "trail_minutes": 30}
        ]
        self.sync()
        copy = next(value for value in self.outlook.events.values() if value.marker)
        source = self.google.events["personal"]
        self.assertEqual(
            (copy.start, copy.end), (source.start - timedelta(hours=1), source.end + timedelta(minutes=30))
        )
        self.settings.event_padding_overrides[0]["lead_minutes"] = 90
        self.assertEqual(self.sync()["updated"], 1)
        self.settings.work_start, self.settings.work_end = "15:00", "17:00"
        self.settings.work_lead_minutes = self.settings.work_trail_minutes = 0
        self.assertEqual(self.sync()["deleted"], 1)
        self.assertEqual((self.google.events, self.outlook.events), originals)

    def test_padding_retrieves_source_beyond_nominal_horizon(self):
        end = NOW + timedelta(days=self.settings.days_forward)
        source = Event("edge", end + timedelta(minutes=15), end + timedelta(minutes=45))
        self.google.events = {source.id: source}
        self.settings.google_lead_minutes = 30
        self.assertEqual(self.sync()["created"], 1)
        copy = next(value for value in self.outlook.events.values() if value.marker)
        self.assertEqual((copy.start, copy.end), (end - timedelta(minutes=15), source.end))
        self.assertEqual(self.sync()["unchanged"], 1)
        self.google.list_events = lambda start, end: []
        with self.assertRaises(SyncError):
            self.sync()
        self.assertEqual(self.outlook.events[copy.id], copy)
        self.assertEqual(self.outlook.writes, [("create", copy.id)])

    def test_legacy_references_migrate_and_refresh_content_once(self):
        self.sync()
        copy = next(value for value in self.outlook.events.values() if value.marker)
        with sqlite3.connect(self.paths.database_file) as connection:
            connection.executescript(
                "CREATE TABLE legacy_copies (source TEXT NOT NULL, source_id TEXT NOT NULL, "
                "target TEXT NOT NULL, target_id TEXT NOT NULL, marker TEXT NOT NULL, "
                "PRIMARY KEY (source, source_id, target));"
                "INSERT INTO legacy_copies SELECT source, source_id, target, target_id, marker FROM copies;"
                "DROP TABLE copies; ALTER TABLE legacy_copies RENAME TO copies;"
            )
        self.assertEqual(self.sync(), {"created": 0, "updated": 1, "deleted": 0, "unchanged": 0})
        self.assertEqual(self.sync(), {"created": 0, "updated": 0, "deleted": 0, "unchanged": 1})
        self.assertEqual(self.outlook.events[copy.id], copy)
        with State(self.paths) as state:
            row = state.mappings("google", "outlook")[0]
        self.assertEqual((row.target_id, row.marker, row.content_version), (copy.id, copy.marker, 1))

    def test_existing_configuration_without_identity_is_rejected(self):
        save_settings(self.paths, self.settings)
        configuration = json.loads(self.paths.config_file.read_text())
        del configuration["installation_id"]
        self.paths.config_file.write_text(json.dumps(configuration))
        with self.assertRaisesRegex(SyncError, "installation identity"):
            load_settings(self.paths)

    def test_concurrent_manual_invocation_is_rejected(self):
        with sync_lock(self.paths), self.assertRaises(SyncError):
            with sync_lock(self.paths):
                self.fail("A concurrent invocation acquired the lock.")

    def test_configuration_and_state_are_owner_only(self):
        save_settings(self.paths, self.settings)
        self.sync()
        self.assertEqual(self.paths.config_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.paths.database_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.paths.state_dir.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
