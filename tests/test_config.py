import hashlib
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
from calendar_busy_sync.models import Event, SyncError
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

    def test_setting_padding_preserves_accounts_and_identity(self):
        self.run_config("set", "google-lead-minutes", "60")
        self.assertEqual(settings.load_settings(self.paths), replace(self.current, google_lead_minutes=60))

    def test_legacy_graph_configuration_preserves_a_successful_checkpoint(self):
        legacy = json.loads(self.paths.config_file.read_text())
        for new_key in (
            "outlook_backend",
            "outlook_web_source_calendar",
            "outlook_web_mirror_calendar",
            "outlook_web_timezone",
        ):
            del legacy[new_key]
        self.paths.config_file.write_text(json.dumps(legacy))
        legacy_fingerprint = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
        with state.State(self.paths) as store:
            store.connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'configuration'", (legacy_fingerprint,)
            )
            store.connection.commit()
        configured = settings.load_settings(self.paths)
        self.assertEqual(configured.outlook_backend, "graph")
        self.assertTrue(state.has_success(self.paths, configured))

    def test_web_requires_distinct_exact_calendar_names_and_the_display_timezone(self):
        configured = settings.Settings(
            outlook_backend="web",
            outlook_web_source_calendar="Work",
            outlook_web_mirror_calendar="Busy Sync",
            outlook_web_timezone="America/Phoenix",
        )
        settings.save_settings(self.paths, configured)
        self.assertEqual(settings.load_settings(self.paths), configured)
        for overrides in (
            {"outlook_web_source_calendar": ""},
            {"outlook_web_mirror_calendar": "work"},
            {"outlook_web_mirror_calendar": "Busy\nSync"},
            {"outlook_web_timezone": ""},
            {"outlook_web_timezone": "not-a-timezone"},
            {"outlook_backend": "fallback"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(SyncError):
                replace(configured, **overrides).validate()

    def test_graph_mapping_cannot_be_reconciled_against_a_web_pair(self):
        with state.State(self.paths) as store:
            store.remember(state.Mapping("google", "original", "outlook", "copy", "fixture"))
        configured = replace(
            self.current,
            outlook_backend="web",
            outlook_web_source_calendar="Work",
            outlook_web_mirror_calendar="Busy Sync",
            outlook_web_timezone="UTC",
        )
        settings.save_settings(self.paths, configured)
        error = io.StringIO()
        with redirect_stderr(error), patch("calendar_busy_sync.auth._google_client") as google:
            self.assertEqual(cli.main(["doctor"]), 2)
        google.assert_not_called()

    def test_bound_web_pair_cannot_be_replaced_without_separate_state(self):
        configured = replace(
            self.current,
            outlook_backend="web",
            outlook_web_source_calendar="Work",
            outlook_web_mirror_calendar="Busy Sync",
            outlook_web_timezone="UTC",
        )
        state.check_outlook_pair(self.paths, configured, bind=True)
        self.paths.outlook_web_identity_file.write_text('{"fixture": true}')
        state.check_outlook_pair(self.paths, replace(configured, outlook_web_timezone="America/Phoenix"))
        renamed = replace(configured, outlook_web_mirror_calendar="A different mirror")
        with self.assertRaises(SyncError):
            state.check_outlook_pair(self.paths, renamed)

    def test_changed_configuration_requires_a_new_successful_sync(self):
        self.run_config("set", "google-lead-minutes", "60")
        self.assertFalse(state.has_success(self.paths, settings.load_settings(self.paths)))

    def test_padding_commands_expand_google_copies_not_outlook_originals(self):
        self.run_config("set", "google-lead-minutes", "60")
        self.run_config("set", "google-trail-minutes", "60")
        policy = TimingPolicy(settings.load_settings(self.paths))
        start = datetime(2026, 11, 16, 12, tzinfo=UTC)
        event = Event("appointment", start, start + timedelta(minutes=30))
        expected = replace(event, start=start - timedelta(hours=1), end=event.end + timedelta(hours=1))
        window_start, window_end = start - timedelta(days=1), start + timedelta(days=1)
        self.assertEqual(policy.desired_copy("google", event, window_start, window_end), expected)
        self.assertEqual(policy.desired_copy("outlook", event, window_start, window_end), event)

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
        for private in (
            self.current.installation_id,
            self.current.microsoft_client_id,
            "private-calendar",
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

    def test_empty_legacy_overrides_load_and_disappear_on_save(self):
        legacy = json.loads(self.paths.config_file.read_text())
        legacy["event_padding_overrides"] = []
        self.paths.config_file.write_text(json.dumps(legacy))
        self.assertEqual(settings.load_settings(self.paths), self.current)
        code, _, error = self.run_config("set", "google-lead-minutes", "60")
        self.assertEqual((code, error), (0, ""))
        self.assertNotIn("event_padding_overrides", json.loads(self.paths.config_file.read_text()))
        self.assertEqual(settings.load_settings(self.paths), replace(self.current, google_lead_minutes=60))

    def test_nonempty_or_invalid_legacy_overrides_stop_sync_without_mutation(self):
        legacy = json.loads(self.paths.config_file.read_text())
        for overrides in (
            [{"source": "google", "event_id": "private-event", "lead_minutes": 60, "trail_minutes": 60}],
            None,
            {},
        ):
            with self.subTest(overrides=overrides):
                legacy["event_padding_overrides"] = overrides
                original = json.dumps(legacy)
                self.paths.config_file.write_text(original)
                error = io.StringIO()
                with patch("calendar_busy_sync.auth.build_clients") as clients, redirect_stderr(error):
                    code = cli.main(["sync"])
                self.assertEqual(code, 2)
                self.assertIn("calendar-busy-sync schedule disable", error.getvalue())
                self.assertNotIn("private-event", error.getvalue())
                clients.assert_not_called()
                self.assertEqual(self.paths.config_file.read_text(), original)


if __name__ == "__main__":
    unittest.main()
