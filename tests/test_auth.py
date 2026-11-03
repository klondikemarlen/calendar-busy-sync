import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from calendar_busy_sync.auth import _google_client
from calendar_busy_sync.models import SyncError
from calendar_busy_sync.settings import Paths, Settings, private_write


class AuthTests(unittest.TestCase):
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
