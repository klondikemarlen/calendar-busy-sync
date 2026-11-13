import json
import os
import sys
import tempfile
import unittest
import webbrowser
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from google_auth_oauthlib.flow import InstalledAppFlow

from calendar_busy_sync.auth import _google_client, authorize_google
from calendar_busy_sync.models import SyncError
from calendar_busy_sync.settings import Paths, Settings, private_write


class AuthTests(unittest.TestCase):
    def test_google_browser_child_inherits_only_nonredundant_gtk_modules(self):
        cases = [
            ("gail:atk-bridge", "gail"),
            ("atk-bridge:gail:custom:atk-bridge", "gail:custom"),
            ("atk-bridge", ""),
            ("custom-atk-bridge:gail", "custom-atk-bridge:gail"),
            ("", ""),
            (None, None),
        ]
        for original, expected in cases:
            with self.subTest(modules=original), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                paths = Paths(root / "config", root / "state")
                paths.ensure()
                private_write(paths.google_client_file, "{}")
                observed = root / "browser-environment.json"
                browser = webbrowser.GenericBrowser(
                    [
                        sys.executable,
                        "-c",
                        "import json, os, pathlib, sys; "
                        "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
                        "'modules': os.environ.get('GTK_MODULES'), "
                        "'accessibility': os.environ.get('GTK_A11Y'), 'url': sys.argv[2]}))",
                        str(observed),
                        "%s",
                    ]
                )
                scopes = ["https://www.googleapis.com/auth/calendar.readonly"]
                credentials = SimpleNamespace(
                    granted_scopes=scopes, to_json=lambda: json.dumps({"token": "synthetic"})
                )
                flow = Mock()
                flow.credentials = credentials
                flow.authorization_url.return_value = ("https://example.invalid/consent", "synthetic-state")
                # Exercise the real SDK browser-opening chain, but replace its server/token exchange.
                flow.run_local_server.side_effect = partial(InstalledAppFlow.run_local_server, flow)

                def make_server(host, port, application, **kwargs):
                    def handle_request():
                        application.last_request_uri = (
                            "http://127.0.0.1/?code=synthetic&state=synthetic-state"
                        )

                    return SimpleNamespace(
                        server_port=12345, handle_request=handle_request, server_close=Mock()
                    )

                with patch.dict(os.environ, {"GTK_A11Y": "test-preserved"}):
                    if original is None:
                        os.environ.pop("GTK_MODULES", None)
                    else:
                        os.environ["GTK_MODULES"] = original
                    with (
                        patch.object(
                            InstalledAppFlow, "from_client_secrets_file", return_value=flow
                        ) as factory,
                        patch("google_auth_oauthlib.flow.wsgiref.simple_server.make_server", make_server),
                        patch("google_auth_oauthlib.flow.webbrowser.get", return_value=browser),
                    ):
                        authorize_google(Settings(), paths)
                    self.assertEqual(os.environ.get("GTK_MODULES"), original)
                    self.assertEqual(
                        json.loads(observed.read_text()),
                        {
                            "modules": expected,
                            "accessibility": "test-preserved",
                            "url": "https://example.invalid/consent",
                        },
                    )
                    factory.assert_called_once_with(
                        str(paths.google_client_file), scopes, autogenerate_code_verifier=True
                    )
                    flow.run_local_server.assert_called_once_with(
                        host="127.0.0.1",
                        bind_addr="127.0.0.1",
                        port=0,
                        open_browser=True,
                        access_type="offline",
                        prompt="consent",
                        timeout_seconds=300,
                    )
                    self.assertEqual(json.loads(paths.google_token_file.read_text())["scopes"], scopes)

    def test_google_browser_failure_restores_gtk_modules_without_saving_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = Paths(root / "config", root / "state")
            paths.ensure()
            private_write(paths.google_client_file, "{}")
            flow = Mock()

            def fail_launch(**kwargs):
                self.assertEqual(os.environ["GTK_MODULES"], "gail")
                raise RuntimeError("synthetic launch failure")

            flow.run_local_server.side_effect = fail_launch
            with (
                patch.dict(os.environ, {"GTK_MODULES": "gail:atk-bridge"}),
                patch.object(InstalledAppFlow, "from_client_secrets_file", return_value=flow),
            ):
                with self.assertRaisesRegex(SyncError, "Google authorization failed"):
                    authorize_google(Settings(), paths)
                self.assertEqual(os.environ["GTK_MODULES"], "gail:atk-bridge")
            self.assertFalse(paths.google_token_file.exists())

    def test_one_way_mode_rejects_a_cached_google_write_grant(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = Paths(root / "config", root / "state")
            paths.ensure()
            private_write(
                paths.google_token_file,
                json.dumps(
                    {
                        "scopes": [
                            "https://www.googleapis.com/auth/calendar.readonly",
                            "https://www.googleapis.com/auth/calendar.events",
                        ]
                    }
                ),
            )
            settings = Settings(microsoft_client_id="00000000-0000-0000-0000-000000000001")
            with self.assertRaisesRegex(SyncError, "revoke existing consent"):
                _google_client(settings, paths)

    def test_refresh_rejects_a_reduced_actual_grant_before_any_calendar_client_is_used(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = Paths(root / "config", root / "state")
            paths.ensure()
            expected = [
                "https://www.googleapis.com/auth/calendar.readonly",
                "https://www.googleapis.com/auth/calendar.events",
            ]
            cached = {
                "token": "synthetic-expired",
                "refresh_token": "synthetic-refresh",
                "client_id": "synthetic-client",
                "client_secret": "synthetic-desktop-secret",
                "scopes": expected,
                "expiry": "2000-01-01T00:00:00Z",
            }
            private_write(paths.google_token_file, json.dumps(cached))
            settings = Settings(direction="both", microsoft_client_id="00000000-0000-0000-0000-000000000001")

            def token_response(*args, **kwargs):
                return type(
                    "TokenResponse",
                    (),
                    {
                        "status": 200,
                        "data": json.dumps(
                            {"access_token": "synthetic-access", "expires_in": 3600, "scope": expected[0]}
                        ).encode(),
                    },
                )()

            with patch("google.auth.transport.requests.Request", return_value=token_response):
                with self.assertRaisesRegex(SyncError, "revoke existing consent"):
                    _google_client(settings, paths)
            self.assertEqual(json.loads(paths.google_token_file.read_text()), cached)


if __name__ == "__main__":
    unittest.main()
