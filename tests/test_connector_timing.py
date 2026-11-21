import unittest

from calendar_busy_sync.models import SyncError

# isort: split

from calendar_busy_sync.connector_timing import SharingHours


class SharingHoursTests(unittest.TestCase):
    def test_rejects_invalid_padding(self):
        invalid = ({"lead": -1}, {"trail": 1441}, {"lead": True}, {"trail": 0.5}, {"lead": "30"})
        for padding in invalid:
            with (
                self.subTest(padding=padding),
                self.assertRaisesRegex(SyncError, "whole number from 0 to 1440"),
            ):
                SharingHours(**padding)

    def test_rejects_invalid_window_toggle(self):
        for enabled in (0, 1, "true", None):
            with self.subTest(enabled=enabled), self.assertRaisesRegex(SyncError, "whether to limit sharing"):
                SharingHours(enabled=enabled)

    def test_rejects_invalid_timezones_even_when_the_window_is_disabled(self):
        for zone in ("", "Invalid/Zone", "/etc/passwd", None):
            with self.subTest(zone=zone), self.assertRaisesRegex(SyncError, "IANA sharing timezone"):
                SharingHours(zone=zone)

    def test_rejects_invalid_weekday_sets(self):
        for days in ("", " ", "mon MON", "mon mon", "mon,wed", "mon holiday", ["mon"]):
            with self.subTest(days=days), self.assertRaisesRegex(SyncError, "unique sharing days"):
                SharingHours(days=days)

    def test_rejects_clock_formats_other_than_24_hour_hhmm(self):
        for start in ("9:00", "09:00:00", "09:00Z", "24:00", "09:60", "09:00\n", None):
            with self.subTest(start=start), self.assertRaisesRegex(SyncError, "24-hour HH:MM"):
                SharingHours(start=start)

    def test_rejects_empty_or_overnight_windows(self):
        for end in ("17:00", "09:00"):
            with (
                self.subTest(end=end),
                self.assertRaisesRegex(SyncError, "later than its start on the same day"),
            ):
                SharingHours(start="17:00", end=end)
