import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from test_engine import MemoryCalendar

from calendar_busy_sync.cli import main
from calendar_busy_sync.models import Event, SyncError
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


if __name__ == "__main__":
    unittest.main()
