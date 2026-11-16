import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from calendar_busy_sync import cli, engine, settings, state
from calendar_busy_sync.models import Event
from calendar_busy_sync.timing import TimingPolicy


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.paths = settings.Paths(root / "config", root / "state")
        self.current = settings.Settings(
            direction="both",
            microsoft_client_id="00000000-0000-0000-0000-000000000001",
            google_calendar="private-calendar",
            event_padding_overrides=[
                {"source": "google", "event_id": "private-event", "lead_minutes": 5, "trail_minutes": 10}
            ],
        )
        settings.save_settings(self.paths, self.current)
        with state.State(self.paths) as store:
            store.success(self.current, datetime.now(UTC))
        self.scheduled = False
        self.enterContext(patch("calendar_busy_sync.cli._paths", return_value=self.paths))
        self.enterContext(
            patch("calendar_busy_sync.cli._config_is_scheduled", side_effect=lambda _: self.scheduled)
        )

    def run_config(self, *arguments):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = cli.main(["config", *arguments])
        return code, output.getvalue(), error.getvalue()

    def test_setting_padding_preserves_accounts_identity_and_per_event_overrides(self):
        self.run_config("set", "google-lead-minutes", "60")
        self.assertEqual(settings.load_settings(self.paths), replace(self.current, google_lead_minutes=60))

    def test_changed_configuration_requires_a_new_successful_sync(self):
        self.run_config("set", "google-lead-minutes", "60")
        self.assertFalse(state.has_success(self.paths, settings.load_settings(self.paths)))

    def test_padding_commands_expand_google_copies_not_outlook_originals_or_overrides(self):
        self.run_config("set", "google-lead-minutes", "60")
        self.run_config("set", "google-trail-minutes", "60")
        policy = TimingPolicy(settings.load_settings(self.paths))
        start = datetime(2026, 11, 16, 12, tzinfo=UTC)
        event = Event("appointment", start, start + timedelta(minutes=30))
        expected = replace(event, start=start - timedelta(hours=1), end=event.end + timedelta(hours=1))
        window_start, window_end = start - timedelta(days=1), start + timedelta(days=1)
        self.assertEqual(policy.desired_copy("google", event, window_start, window_end), expected)
        self.assertEqual(policy.desired_copy("outlook", event, window_start, window_end), event)
        overridden = replace(event, id="private-event")
        expected_override = replace(
            overridden, start=start - timedelta(minutes=5), end=event.end + timedelta(minutes=10)
        )
        self.assertEqual(
            policy.desired_copy("google", overridden, window_start, window_end), expected_override
        )

    def test_invalid_values_leave_configuration_and_success_checkpoint_untouched(self):
        original = self.paths.config_file.read_bytes()
        for key, value in (
            ("google-lead-minutes", "-1"),
            ("google-trail-minutes", "1441"),
            ("days-forward", "0"),
            ("days-back", "1.5"),
            ("work-hours-enabled", "maybe"),
            ("work-timezone", "not-a-timezone"),
            ("work-start", "23:00"),
            ("work-days", "mon mon"),
            ("direction", "outlook-only"),
        ):
            with self.subTest(key=key):
                code, _, error = self.run_config("set", key, value)
                self.assertEqual(code, 2)
                self.assertTrue(error)
                self.assertEqual(self.paths.config_file.read_bytes(), original)
                self.assertTrue(state.has_success(self.paths, self.current))

    def test_active_schedule_refuses_changes_with_recovery_command(self):
        self.scheduled = True
        code, _, error = self.run_config("set", "google-lead-minutes", "60")
        self.assertEqual(code, 2)
        self.assertIn("calendar-busy-sync schedule disable", error)
        self.assertEqual(settings.load_settings(self.paths), self.current)

    def test_unchanged_assignment_preserves_success_even_with_active_schedule(self):
        self.scheduled = True
        code, _, _ = self.run_config("set", "google-lead-minutes", "0")
        self.assertEqual(code, 0)
        self.assertTrue(state.has_success(self.paths, self.current))

    def test_concurrent_sync_prevents_configuration_mutation(self):
        with engine.sync_lock(self.paths):
            code, _, _ = self.run_config("set", "google-lead-minutes", "60")
        self.assertEqual(code, 2)
        self.assertEqual(settings.load_settings(self.paths), self.current)

    def test_boolean_and_weekday_values_round_trip_through_cli(self):
        self.run_config("set", "work-hours-enabled", "on")
        self.run_config("set", "work-days", "mon", "wed", "fri")
        self.assertEqual(
            settings.load_settings(self.paths),
            replace(self.current, work_hours_enabled=True, work_days=["mon", "wed", "fri"]),
        )
        self.assertEqual(self.run_config("get", "work-hours-enabled")[1], "on\n")
        self.assertEqual(self.run_config("get", "work-days")[1], "mon wed fri\n")
        self.run_config("set", "work-hours-enabled", "off")
        self.assertFalse(settings.load_settings(self.paths).work_hours_enabled)

    def test_listing_explains_padding_without_disclosing_private_identifiers(self):
        code, output, _ = self.run_config("list")
        self.assertEqual(code, 0)
        self.assertIn("NOT event padding", output)
        self.assertIn("Google appointments in Outlook Busy copies", output)
        self.assertIn("Per-event padding overrides: 1", output)
        for private in (
            self.current.installation_id,
            self.current.microsoft_client_id,
            "private-calendar",
            "private-event",
        ):
            self.assertNotIn(private, output)
        self.assertTrue(state.has_success(self.paths, self.current))

    def test_direction_change_explains_reauthorization_and_explicit_sync(self):
        code, output, _ = self.run_config("set", "direction", "google-to-outlook")
        self.assertEqual(code, 0)
        self.assertIn("calendar-busy-sync auth google", output)
        self.assertIn("calendar-busy-sync sync --dry-run", output)
        self.assertEqual(settings.load_settings(self.paths).direction, "google-to-outlook")
        self.assertFalse(self.paths.google_token_file.exists())

    def test_private_fields_are_not_editable(self):
        original = json.loads(self.paths.config_file.read_text())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["config", "set", "installation-id", "new-identity"])
        self.assertEqual(json.loads(self.paths.config_file.read_text()), original)


if __name__ == "__main__":
    unittest.main()
