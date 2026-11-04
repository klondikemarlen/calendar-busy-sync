import io
import json
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from calendar_busy_sync import registration
from calendar_busy_sync.models import SyncError
from calendar_busy_sync.settings import Paths
from calendar_busy_sync.setup_tools import SetupTool


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.paths = Paths(root / "config", root / "state")
        self.commands = []
        self.results = []

    @contextmanager
    def fake_cli(self, name, confirm):
        yield SetupTool("cloud-cli", {})

    def run_command(self, command, **kwargs):
        self.commands.append((command[1:], kwargs))
        return self.results.pop(0)

    def test_declining_project_creation_never_mutates_google_cloud(self):
        self.results = [subprocess.CompletedProcess([], 0, "", "")]
        answers = iter(["", "new-calendar-project"])
        with (
            patch.object(registration, "temporary_cli", self.fake_cli),
            patch.object(registration.subprocess, "run", side_effect=self.run_command),
            redirect_stdout(io.StringIO()),
        ):
            registration.prepare_google(self.paths, lambda *_a, **_k: next(answers), lambda *_a: False)
        self.assertEqual([command for command, _options in self.commands], [["auth", "login"]])

    def test_console_handoff_selects_the_project_whose_calendar_api_was_enabled(self):
        self.results = [subprocess.CompletedProcess([], 0, "", "") for _ in range(2)]
        output = io.StringIO()
        with (
            patch.object(registration, "temporary_cli", self.fake_cli),
            patch.object(registration.subprocess, "run", side_effect=self.run_command),
            redirect_stdout(output),
        ):
            registration.prepare_google(self.paths, lambda *_a, **_k: "calendar-project", lambda *_a: True)
        self.assertEqual(
            [line for line in output.getvalue().splitlines() if line.startswith("https://")],
            [
                "https://console.cloud.google.com/auth/branding?project=calendar-project",
                "https://console.cloud.google.com/auth/audience?project=calendar-project",
                "https://console.cloud.google.com/auth/clients?project=calendar-project",
            ],
        )
        command, options = self.commands[-1]
        self.assertIn("--project=calendar-project", command)
        self.assertIn("calendar-json.googleapis.com", command)
        self.assertFalse(self.commands[0][1]["capture_output"])

    def test_failed_login_stops_before_google_mutation_without_exposing_diagnostics(self):
        self.results = [subprocess.CompletedProcess([], 1, "private-token", "private-payload")]
        with (
            patch.object(registration, "temporary_cli", self.fake_cli),
            patch.object(registration.subprocess, "run", side_effect=self.run_command),
            redirect_stdout(io.StringIO()),
            self.assertRaises(SyncError) as caught,
        ):
            registration.prepare_google(self.paths, lambda *_a, **_k: "calendar-project", lambda *_a: True)
        self.assertEqual([command for command, _options in self.commands], [["auth", "login"]])
        self.assertNotIn("private", str(caught.exception))

    def test_failed_api_enablement_preserves_a_created_project_and_warns_before_retry(self):
        self.results = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 1, "", "private-diagnostic"),
        ]
        answers = iter(["", "new-calendar-project"])
        with (
            patch.object(registration, "temporary_cli", self.fake_cli),
            patch.object(registration.subprocess, "run", side_effect=self.run_command),
            redirect_stdout(io.StringIO()),
            self.assertRaises(SyncError),
        ):
            registration.prepare_google(self.paths, lambda *_a, **_k: next(answers), lambda *_a: True)
        self.assertFalse(any(command[:2] == ["projects", "delete"] for command, _ in self.commands))

    def test_microsoft_registration_is_public_single_tenant_and_calendar_delegated_only(self):
        self.results = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "00000000-0000-0000-0000-000000000002\n", ""),
            subprocess.CompletedProcess([], 0, "00000000-0000-0000-0000-000000000001\n", ""),
        ]
        with (
            patch.object(registration, "temporary_cli", self.fake_cli),
            patch.object(registration.subprocess, "run", side_effect=self.run_command),
            redirect_stdout(io.StringIO()),
        ):
            result = registration.create_microsoft(
                self.paths, lambda *_a, **_k: "example.test", lambda *_a: True
            )
        self.assertEqual(
            result, ("00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002")
        )
        command, _ = self.commands[-1]
        access = json.loads(command[command.index("--required-resource-access") + 1])
        self.assertEqual(
            access,
            [
                {
                    "resourceAppId": "00000003-0000-0000-c000-000000000000",
                    "resourceAccess": [{"id": "1ec239c2-d7c9-4623-a91a-a9775856bb36", "type": "Scope"}],
                }
            ],
        )
        self.assertEqual(command[command.index("--is-fallback-public-client") + 1], "true")
        self.assertEqual(command[command.index("--sign-in-audience") + 1], "AzureADMyOrg")
        self.assertNotIn("--password", command)

    def test_declining_microsoft_registration_never_creates_an_app(self):
        self.results = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "00000000-0000-0000-0000-000000000002", ""),
        ]
        with (
            patch.object(registration, "temporary_cli", self.fake_cli),
            patch.object(registration.subprocess, "run", side_effect=self.run_command),
            redirect_stdout(io.StringIO()),
        ):
            result = registration.create_microsoft(
                self.paths, lambda *_a, **_k: "example.test", lambda *_a: False
            )
        self.assertIsNone(result)
        self.assertFalse(any(command[:3] == ["ad", "app", "create"] for command, _ in self.commands))

    def test_uncertain_microsoft_creation_warns_inspect_instead_of_retry_or_delete(self):
        self.results = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "00000000-0000-0000-0000-000000000002", ""),
            subprocess.CompletedProcess([], 1, "private-token", "private-payload"),
        ]
        with (
            patch.object(registration, "temporary_cli", self.fake_cli),
            patch.object(registration.subprocess, "run", side_effect=self.run_command),
            redirect_stdout(io.StringIO()),
            self.assertRaises(SyncError) as caught,
        ):
            registration.create_microsoft(self.paths, lambda *_a, **_k: "example.test", lambda *_a: True)
        self.assertNotIn("private", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
