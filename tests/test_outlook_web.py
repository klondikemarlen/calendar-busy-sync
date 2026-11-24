import hashlib
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from calendar_busy_sync.models import Event, EventLookupUnknown, SyncError
from calendar_busy_sync.outlook_web import _Identity, _OutlookCalendar, _OutlookMirror
from calendar_busy_sync.outlook_web_events import (
    BUSY_TITLE,
    IDENTITY_TITLE,
    busy_note,
    note_marker,
    parse_card,
    write_calendar_file,
)
from calendar_busy_sync.outlook_web_ui import OutlookUI
from calendar_busy_sync.settings import Paths, Settings

_INSTALLATION = "a" * 32
_MARKER = f"cbs1:{_INSTALLATION}:google:{'b' * 64}:{'c' * 32}"
_START = datetime(2026, 11, 24, 13, tzinfo=UTC)
_END = datetime(2026, 11, 24, 13, 5, tzinfo=UTC)
_SOURCE = "Calendar"
_MIRROR = "Dedicated Mirror"
_ACCOUNT = "fixture@example.invalid"
_ITEM_ID = "synthetic+calendar/item="
_EDITOR = "https://outlook.cloud.microsoft/calendar/item/synthetic%2Bcalendar%2Fitem%3D"


class OutlookEventTests(unittest.TestCase):
    def test_parses_outlook_display_time_and_availability(self):
        label = "Generic event, 5:00 AM to 5:05 AM, Tuesday, November 24, 2026, Free, Private"
        result = parse_card(_ITEM_ID, label, ZoneInfo("America/Los_Angeles"))
        self.assertEqual(result, Event(_ITEM_ID, _START, _END, busy=False))

    def test_never_guesses_ambiguous_dst_interval(self):
        label = "Generic event, 1:30 AM to 2:30 AM, Sunday, November 1, 2026, Busy"
        with self.assertRaisesRegex(SyncError, "ambiguous event time"):
            parse_card(_ITEM_ID, label, ZoneInfo("America/Los_Angeles"))

    def test_requires_exact_title_note_and_installation_for_ownership(self):
        note = busy_note(_MARKER, _INSTALLATION)
        self.assertEqual(note_marker(BUSY_TITLE, note, _INSTALLATION), _MARKER)
        self.assertIsNone(note_marker("Unrelated appointment", note, _INSTALLATION))
        self.assertIsNone(note_marker(BUSY_TITLE, note + " ", _INSTALLATION))
        self.assertIsNone(note_marker(BUSY_TITLE, note, "f" * 32))

    def test_upload_file_contains_only_the_managed_copy(self):
        original = Event("original-event-identifier", _START, _END)
        note = busy_note(_MARKER, _INSTALLATION)
        with tempfile.TemporaryDirectory() as directory:
            owned, upload = write_calendar_file(Path(directory), original, BUSY_TITLE, note)
            try:
                actual = upload.read_bytes()
                unfolded = actual.replace(b"\r\n ", b"")
                self.assertIn(b"SUMMARY:Busy (from sync)\r\n", unfolded)
                self.assertIn(b"DTSTART:20261124T130000Z\r\n", unfolded)
                self.assertIn(b"CLASS:PRIVATE\r\nTRANSP:OPAQUE\r\n", unfolded)
                self.assertNotIn(original.id.encode(), unfolded)
                self.assertTrue(all(len(line) <= 75 for line in actual.split(b"\r\n")))
            finally:
                owned.cleanup()


class OutlookCalendarTests(unittest.TestCase):
    def setUp(self):
        self.ui = Mock(spec=OutlookUI)
        self.settings = Settings(
            outlook_backend="web",
            outlook_web_source_calendar=_SOURCE,
            outlook_web_mirror_calendar=_MIRROR,
            outlook_web_timezone="America/Los_Angeles",
        )
        binding = hashlib.sha256(f"{_MIRROR} ({_ACCOUNT})".encode()).hexdigest()
        self.identity = _Identity("fixture", "d" * 32, _START.isoformat(), _ITEM_ID, _EDITOR, binding)
        self.source = _OutlookCalendar(self.ui, _SOURCE, self.settings, self.identity)

    def test_positive_direct_source_lookup_checks_calendar_account_and_show_as(self):
        self.ui.open_item.return_value = True
        self.ui.calendar_binding.return_value = f"{_SOURCE} ({_ACCOUNT})"
        self.ui.show_editor_timing.return_value = True
        self.ui.editor_times.return_value = ["2026-11-24", "5:00 AM", None, "5:05 AM"]
        self.ui.editor_availability.return_value = "Free"

        result = self.source.get_event(_ITEM_ID)

        self.ui.open_item.assert_called_once_with(_ITEM_ID)
        self.ui.calendar_binding.assert_called_once_with(_SOURCE)
        self.assertEqual(result, Event(_ITEM_ID, _START, _END, busy=False))

    def test_missing_or_mismatched_editor_is_unknown_not_absent(self):
        self.ui.open_item.return_value = False
        with self.assertRaises(EventLookupUnknown):
            self.source.get_event(_ITEM_ID)
        self.ui.open_item.return_value = True
        self.ui.calendar_binding.return_value = f"{_SOURCE} (other@example.invalid)"
        with self.assertRaises(EventLookupUnknown):
            self.source.get_event(_ITEM_ID)
        self.ui.show_editor_timing.assert_not_called()

    def test_source_mutations_do_not_touch_browser(self):
        original = Event(_ITEM_ID, _START, _END)
        for operation, arguments in (
            (self.source.create_busy, (original, _MARKER)),
            (self.source.update_busy, (_ITEM_ID, original, _MARKER)),
            (self.source.delete_busy, (_ITEM_ID, _MARKER)),
        ):
            with self.assertRaisesRegex(SyncError, "source calendar is read-only"):
                operation(*arguments)
        self.assertEqual(self.ui.mock_calls, [])

    def test_timezone_mismatch_blocks_browser_writes(self):
        self.ui.sample_week.return_value = [
            (_ITEM_ID, f"{IDENTITY_TITLE}, 6:00 AM to 6:05 AM, Tuesday, November 24, 2026, Free, Private")
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = Paths(root / "config", root / "state")
            mirror = _OutlookMirror(self.ui, self.settings, paths, self.identity)
            with self.assertRaisesRegex(SyncError, "display time zone"):
                mirror.delete_busy(_ITEM_ID, _MARKER.replace(_INSTALLATION, self.settings.installation_id))
        self.ui.open_grid_editor.assert_not_called()
        self.ui.delete_editor_event.assert_not_called()


if __name__ == "__main__":
    unittest.main()
