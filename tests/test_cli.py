import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from calendar_busy_sync.cli import main
from calendar_busy_sync.settings import Paths, load_settings


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

    def test_setup_saves_private_configuration_without_authorizing_or_scheduling_by_default(self):
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
            answers = TerminalInput(f"\n{credentials}\n00000000-0000-0000-0000-000000000001\n\nn\nn\nn\nn\n")
            with (
                patch("calendar_busy_sync.cli._paths", return_value=paths),
                patch.dict(os.environ, {"PATH": f"{binary}:/usr/bin:/bin"}),
                patch("sys.stdin", answers),
                redirect_stdout(io.StringIO()),
            ):
                code = main(["setup"])
            self.assertEqual(code, 0)
            self.assertEqual(load_settings(paths).direction, "google-to-outlook")
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
                f"\n\nn\n{credentials}\n\nn\n00000000-0000-0000-0000-000000000001\n\nn\nn\nn\nn\n"
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


if __name__ == "__main__":
    unittest.main()
