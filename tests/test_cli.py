import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from test_engine import MemoryCalendar

from calendar_busy_sync.cli import main
from calendar_busy_sync.models import Event, EventLookupUnknown, SyncError
from calendar_busy_sync.schedule import ScheduleStatus
from calendar_busy_sync.settings import Paths, Settings, load_settings, save_settings
from calendar_busy_sync.state import has_success


class TerminalInput(io.StringIO):
    def isatty(self):
        return True


class CliTests(unittest.TestCase):
    def test_unconfigured_sync_exits_with_a_setup_action_without_traceback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = Paths(root / "config", root / "state")
            error = io.StringIO()
            with patch("calendar_busy_sync.cli._paths", return_value=paths), redirect_stderr(error):
                code = main(["sync"])
            self.assertEqual(code, 2)
            self.assertIn("Run calendar-busy-sync setup first", error.getvalue())
            self.assertNotIn("Traceback", error.getvalue())

    def test_declining_setup_actions_saves_configuration_without_authorizing_or_scheduling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = Paths(root / "config", root / "state")
            credentials = root / "desktop.json"
            credentials.write_text(json.dumps({"installed": {"client_id": "synthetic-desktop-client"}}))
            binary = root / "bin"
            binary.mkdir()
            crontab = binary / "crontab"
            crontab.write_text("#!/bin/sh\nprintf 'no crontab for test\\n' >&2\nexit 1\n")
            crontab.chmod(0o755)
            answers = TerminalInput(
                f"\n{credentials}\n00000000-0000-0000-0000-000000000001\n\n\n\nn\nn\nn\nn\n"
            )
            output = io.StringIO()
            with (
                patch("calendar_busy_sync.cli._paths", return_value=paths),
                patch.dict(os.environ, {"PATH": f"{binary}:/usr/bin:/bin"}),
                patch("sys.stdin", answers),
                redirect_stdout(output),
            ):
                code = main(["setup"])
            self.assertEqual(code, 0)
            self.assertEqual(load_settings(paths).direction, "both")
            self.assertIn("Setup incomplete", output.getvalue())
            self.assertIn("calendar-busy-sync auth all", output.getvalue())
            self.assertFalse(paths.google_token_file.exists())
            self.assertFalse(paths.microsoft_token_file.exists())
            self.assertFalse(paths.database_file.exists())
            self.assertEqual(paths.google_client_file.stat().st_mode & 0o777, 0o600)

    def test_declining_registration_assistance_keeps_manual_setup_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = Paths(root / "config", root / "state")
            credentials = root / "desktop.json"
            credentials.write_text(json.dumps({"installed": {"client_id": "synthetic-desktop-client"}}))
            binary = root / "bin"
            binary.mkdir()
            crontab = binary / "crontab"
            crontab.write_text("#!/bin/sh\nprintf 'no crontab for test\\n' >&2\nexit 1\n")
            crontab.chmod(0o755)
            answers = TerminalInput(
                f"\n\nn\n{credentials}\n\nn\n00000000-0000-0000-0000-000000000001\n\n\n\nn\nn\nn\nn\n"
            )
            with (
                patch("calendar_busy_sync.cli._paths", return_value=paths),
                patch.dict(os.environ, {"PATH": f"{binary}:/usr/bin:/bin"}),
                patch("sys.stdin", answers),
                redirect_stdout(io.StringIO()),
            ):
                code = main(["setup"])
            self.assertEqual(code, 0)
            configured = load_settings(paths)
            self.assertEqual(configured.microsoft_client_id, "00000000-0000-0000-0000-000000000001")
            self.assertEqual(configured.microsoft_tenant, "organizations")
            self.assertEqual(
                json.loads(paths.google_client_file.read_text()),
                {"installed": {"client_id": "synthetic-desktop-client"}},
            )
            self.assertFalse(paths.google_token_file.exists())
            self.assertFalse(paths.microsoft_token_file.exists())
            self.assertFalse(paths.database_file.exists())

    def test_enter_defaults_authorize_and_sync_both_directions_without_enabling_schedule(self):
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                paths = Paths(root / "config", root / "state")
                paths.ensure()
                paths.google_client_file.write_text(json.dumps({"installed": {"client_id": "synthetic"}}))
                previous = Settings(microsoft_client_id="00000000-0000-0000-0000-000000000001")
                if existing:
                    save_settings(paths, previous)
                    paths.google_token_file.write_text(json.dumps({"scopes": ["readonly"]}))
                start = datetime.now(UTC) + timedelta(hours=1)
                personal = Event("personal", start, start + timedelta(hours=1))
                work = Event("work", start + timedelta(hours=2), start + timedelta(hours=3))
                google = MemoryCalendar([personal])
                outlook = MemoryCalendar([work])

                def authorize_google(configured, storage):
                    self.assertEqual(configured.direction, "both")
                    storage.google_token_file.write_text(json.dumps({"scopes": ["read", "write"]}))

                def clients(configured, storage, google=google, outlook=outlook):
                    grant = json.loads(storage.google_token_file.read_text())
                    if grant["scopes"] != ["read", "write"]:
                        raise SyncError("Google permissions do not match.")
                    return {"google": google, "outlook": outlook}

                answers = "\n" * 11 if existing else f"\n\n{previous.microsoft_client_id}\n" + "\n" * 8
                output = io.StringIO()
                with (
                    patch("calendar_busy_sync.cli._paths", return_value=paths),
                    patch("calendar_busy_sync.cli._config_is_scheduled", return_value=False),
                    patch("calendar_busy_sync.cli.enable_schedule") as schedule,
                    patch("calendar_busy_sync.auth.authorize_google", side_effect=authorize_google),
                    patch("calendar_busy_sync.auth.authorize_outlook"),
                    patch("calendar_busy_sync.auth.build_clients", side_effect=clients),
                    patch("sys.stdin", TerminalInput(answers)),
                    redirect_stdout(output),
                ):
                    self.assertEqual(main(["setup"]), 0)
                configured = load_settings(paths)
                self.assertEqual(configured.direction, "both")
                self.assertTrue(has_success(paths, configured))
                if existing:
                    self.assertEqual(configured.installation_id, previous.installation_id)
                self.assertEqual(google.events["personal"], personal)
                self.assertEqual(outlook.events["work"], work)
                google_copy = next(event for event in google.events.values() if event.marker)
                outlook_copy = next(event for event in outlook.events.values() if event.marker)
                self.assertEqual((google_copy.start, google_copy.end), (work.start, work.end))
                self.assertEqual((outlook_copy.start, outlook_copy.end), (personal.start, personal.end))
                schedule.assert_not_called()
                self.assertIn("Google calendar reads: OK", output.getvalue())
                self.assertIn("Outlook calendar reads: OK", output.getvalue())
                self.assertIn("Automatic syncing is off", output.getvalue())

    def test_failed_authorization_stops_default_setup_before_calendar_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = Paths(root / "config", root / "state")
            paths.ensure()
            paths.google_client_file.write_text(json.dumps({"installed": {"client_id": "synthetic"}}))
            save_settings(paths, Settings(microsoft_client_id="00000000-0000-0000-0000-000000000001"))
            error = io.StringIO()
            with (
                patch("calendar_busy_sync.cli._paths", return_value=paths),
                patch("calendar_busy_sync.cli._config_is_scheduled", return_value=False),
                patch("calendar_busy_sync.auth.authorize_google", side_effect=SyncError("Consent declined.")),
                patch("calendar_busy_sync.auth.build_clients") as clients,
                patch("calendar_busy_sync.cli.enable_schedule") as schedule,
                patch("sys.stdin", TerminalInput("\n" * 11)),
                redirect_stdout(io.StringIO()),
                redirect_stderr(error),
            ):
                self.assertEqual(main(["setup"]), 2)
            self.assertIn("Consent declined", error.getvalue())
            self.assertFalse(has_success(paths, load_settings(paths)))
            clients.assert_not_called()
            schedule.assert_not_called()


class OutlookWebCliTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(temporary)
        self.paths = Paths(root / "config", root / "state")
        self.paths.ensure()
        self.paths.google_client_file.write_text(json.dumps({"installed": {"client_id": "synthetic"}}))
        start = datetime.now(UTC) + timedelta(hours=1)
        self.personal = Event("personal", start, start + timedelta(hours=1))
        self.work = Event("work", start + timedelta(hours=2), start + timedelta(hours=3))
        self.unrelated = Event("unrelated", start, start + timedelta(hours=1))
        self.google = MemoryCalendar([self.personal])
        self.source = MemoryCalendar([self.work])
        self.mirror = MemoryCalendar([self.unrelated])
        self.browser_lifecycle = []
        self.enterContext(patch("calendar_busy_sync.cli._paths", return_value=self.paths))
        self.enterContext(patch("calendar_busy_sync.cli._config_is_scheduled", return_value=False))
        self.enable_schedule = self.enterContext(patch("calendar_busy_sync.cli.enable_schedule"))
        self.enterContext(patch("calendar_busy_sync.auth.authorize_google"))
        self.enterContext(patch("calendar_busy_sync.auth._google_client", return_value=self.google))
        self.enterContext(patch("calendar_busy_sync.health.notify", return_value=False))
        self.graph_client = self.enterContext(patch("calendar_busy_sync.auth._outlook_client"))
        self.enterContext(
            patch("calendar_busy_sync.outlook_web.open_web_calendars", side_effect=self.open_calendars)
        )
        self.enterContext(patch("calendar_busy_sync.outlook_web.open_web_login", side_effect=self.open_login))

    @contextmanager
    def open_login(self, configured, paths):
        self.browser_lifecycle.append("login")
        self.assertEqual(configured.outlook_backend, "web")

        def sign_in():
            paths.outlook_web_identity_file.write_text('{"fixture": true}')
            self.browser_lifecycle.append("signed in")

        try:
            yield sign_in
        finally:
            self.browser_lifecycle.append("login closed")

    @contextmanager
    def open_calendars(self, configured, paths):
        self.assertEqual(configured.outlook_web_source_calendar, "Work")
        self.assertEqual(configured.outlook_web_mirror_calendar, "Busy Sync")
        self.assertEqual(configured.outlook_web_timezone, "UTC")
        self.assertEqual(paths.outlook_web_profile_dir, self.paths.state_dir / "outlook-web-profile")
        self.browser_lifecycle.append("headless")
        try:
            yield self.source, self.mirror
        finally:
            self.browser_lifecycle.append("headless closed")

    def run_cli(self, *args, answers=""):
        output, error = io.StringIO(), io.StringIO()
        with (
            patch("sys.stdin", TerminalInput(answers)),
            redirect_stdout(output),
            redirect_stderr(error),
        ):
            code = main(list(args))
        return code, output.getvalue(), error.getvalue()

    def setup_web(self, schedule_answer="n"):
        return self.run_cli(
            "setup",
            "--outlook-backend",
            "web",
            answers=f"\n\ny\nWork\nBusy Sync\nUTC\n\n\nn\n\n\n\n{schedule_answer}\n",
        )

    def test_setup_authorizes_web_then_reads_headlessly_and_writes_only_to_mirror(self):
        code, output, error = self.setup_web()
        self.assertEqual((code, error), (0, ""))
        configured = load_settings(self.paths)
        self.assertEqual(configured.microsoft_client_id, "")
        self.assertEqual(configured.direction, "both")
        self.assertFalse(self.paths.microsoft_token_file.exists())
        self.graph_client.assert_not_called()
        self.assertEqual(
            self.browser_lifecycle,
            [
                "login",
                "signed in",
                "login closed",
                "headless",
                "headless closed",
                "headless",
                "headless closed",
            ],
        )
        self.assertIn("including manual duplicates", output)
        self.assertIn("Busy (from sync)", output)
        self.assertIn("Outlook mirror calendar reads: OK", output)
        self.assertEqual(
            self.source.events,
            {
                "work": self.work,
            },
        )
        self.assertEqual(self.source.writes, [])
        self.assertEqual(self.mirror.events["unrelated"], self.unrelated)
        google_copy = next(event for event in self.google.events.values() if event.marker)
        outlook_copy = next(event for event in self.mirror.events.values() if event.marker)
        self.assertEqual((google_copy.start, google_copy.end), (self.work.start, self.work.end))
        self.assertEqual((outlook_copy.start, outlook_copy.end), (self.personal.start, self.personal.end))
        self.assertTrue(has_success(self.paths, configured))
        self.enable_schedule.assert_not_called()

    def test_declining_the_mirror_rules_leaves_setup_unchanged(self):
        code, output, error = self.run_cli("setup", "--outlook-backend", "web", answers="\n\nn\n")
        self.assertEqual(code, 2)
        self.assertIn("configuration was not changed", error)
        self.assertFalse(self.paths.config_file.exists())
        self.assertEqual(self.browser_lifecycle, [])

    def test_deferred_first_sync_is_not_setup_success_and_does_not_offer_scheduling(self):
        with patch("calendar_busy_sync.engine.synchronize", return_value={"deferred": 1}):
            code, output, error = self.setup_web(schedule_answer="y")
        self.assertEqual((code, error), (2, ""))
        self.assertIn("Setup incomplete", output)
        self.assertNotIn("Initial synchronization succeeded", output)
        self.assertNotIn("Enable a recurring schedule?", output)
        self.assertFalse(has_success(self.paths, load_settings(self.paths)))
        self.enable_schedule.assert_not_called()

    def test_deferred_sync_leaves_the_copy_and_invalidates_the_prior_checkpoint(self):
        self.assertEqual(self.setup_web()[0], 0)
        self.source.events.clear()
        with patch.object(self.source, "get_event", side_effect=EventLookupUnknown("Uncertain lookup.")):
            code, output, error = self.run_cli("sync")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["deferred"], 1)
        self.assertIn("Sync incomplete", error)
        self.assertEqual([kind for kind, _ in self.google.writes], ["create"])
        self.assertFalse(has_success(self.paths, load_settings(self.paths)))
        with patch("calendar_busy_sync.cli.schedule_status", return_value=ScheduleStatus(False)):
            code, output, error = self.run_cli("status")
        self.assertEqual((code, error), (0, ""))
        report = json.loads(output)
        self.assertEqual(report["outlook_backend"], "web")
        self.assertEqual(report["sync_health"]["outcome"], "failed")

    def test_authorization_does_not_recreate_a_missing_identity_while_copies_are_tracked(self):
        self.assertEqual(self.setup_web()[0], 0)
        self.paths.outlook_web_identity_file.unlink()
        self.browser_lifecycle.clear()
        code, output, error = self.run_cli("auth", "outlook")
        self.assertEqual(code, 2)
        self.assertIn("Outlook web mirror identity is missing", error)
        self.assertEqual(self.browser_lifecycle, [])


if __name__ == "__main__":
    unittest.main()
