import io
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from test_engine import MemoryCalendar

from calendar_busy_sync import connector
from calendar_busy_sync.connector_runtime import Config, database
from calendar_busy_sync.connector_store import Store
from calendar_busy_sync.models import Event, SyncError


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.hosts = {}
        self.calendars = {}
        self.messages = []
        stack.enter_context(patch.object(connector, "call_peer", self.peer))
        stack.enter_context(patch.object(connector, "calendar_client", self.calendar))
        for role, other in (("microsoft", "google"), ("google", "microsoft")):
            local = root / role
            local.mkdir(mode=0o700)
            config = Config(
                role, f"https://{role}.invalid", f"https://{other}.invalid", role, "app", "key", local
            )
            with patch.object(Config, "load", return_value=config):
                self.hosts[role] = (config, connector.application())

    def calendar(self, config, grant):
        return self.calendars[config.role, grant], grant

    def call(self, role, path, payload, bearer):
        config, app = self.hosts[role]
        raw = json.dumps(payload).encode()
        environ = {
            "HTTP_HOST": urlsplit(config.origin).netloc,
            "PATH_INFO": path,
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": io.BytesIO(raw),
            "HTTP_AUTHORIZATION": "Bearer " + bearer,
        }
        status = []
        body = b"".join(app(environ, lambda value, headers: status.append(value)))
        return status[0], body

    def peer(self, config, path, payload):
        remote = "microsoft" if config.role == "google" else "google"
        status, body = self.call(remote, path, payload, config.peer_secret)
        if status != "200 OK":
            raise SyncError("Peer rejected the request.")
        result = json.loads(body)
        self.messages.append((path, payload, result))
        return result

    def local(self, role, operation, *args):
        config, _ = self.hosts[role]
        with database(config) as db:
            store = Store(config, db, lambda path, payload: self.peer(config, path, payload))
            return getattr(store, operation)(*args)

    def test_two_domains_only_exchange_after_bilateral_pairing_and_stop_after_unlink(self):
        work, _ = self.hosts["microsoft"]
        day, _ = connector._window()
        personal = Event("PERSONAL_SOURCE_ID", day + timedelta(hours=8), day + timedelta(hours=10))
        official = Event("WORK_SOURCE_ID", day + timedelta(hours=9), day + timedelta(hours=11))
        google = MemoryCalendar([personal])
        outlook = MemoryCalendar([official])
        self.calendars["google", "PERSONAL_GRANT"] = google
        self.calendars["microsoft", "WORK_GRANT"] = outlook
        self.local("microsoft", "enroll", "WORK_SUBJECT", "WORK_GRANT")
        self.local("google", "enroll", "PERSONAL_SUBJECT", "PERSONAL_GRANT")
        # A different signed-in work user never authorizes pairing. There is no adapter for that user.
        self.local("microsoft", "enroll", "OTHER_SUBJECT", "OTHER_GRANT")
        invite = self.local("microsoft", "offer", "WORK_SUBJECT")
        confirmation = self.local("google", "join", "PERSONAL_SUBJECT", invite)
        self.assertEqual(connector.synchronize(work), 0)
        with self.assertRaises(SyncError):
            self.local("microsoft", "confirm", "OTHER_SUBJECT", confirmation)
        self.local("microsoft", "confirm", "WORK_SUBJECT", confirmation)
        self.assertEqual(connector.synchronize(work), 1)
        self.assertEqual(connector.synchronize(work), 1)
        for calendar, original in ((google, official), (outlook, personal)):
            self.assertEqual(len(calendar.writes), 1)
            (copy,) = [e for e in calendar.events.values() if e.marker]
            self.assertEqual((copy.start, copy.end), (original.start, original.end))
        for path, payload, result in self.messages:
            wire = json.dumps([payload, result])
            for private in ("WORK_", "PERSONAL_", "OTHER_"):
                self.assertNotIn(private, wire)
            if path == "/peer/pull":
                self.assertEqual(set(result), {"pair_id", "day", "intervals"})
                self.assertEqual(
                    result["intervals"],
                    [[s.isoformat().replace("+00:00", "Z") for s in (personal.start, personal.end)]],
                )
        del google.events[personal.id]
        google.fail_read = True
        with self.assertRaises(SyncError):
            connector.synchronize(work)
        self.assertEqual(len(outlook.writes), 1)
        google.fail_read = False
        self.assertEqual(connector.synchronize(work), 1)
        self.assertEqual(outlook.events, {official.id: official})
        self.local("google", "disconnect", "PERSONAL_SUBJECT")
        self.assertEqual(connector.synchronize(work), 0)
        self.assertEqual(self.local("microsoft", "account", "OTHER_SUBJECT"), (None, False))

    def test_missing_peer_permission_and_unenrolled_pair_cannot_read_any_calendar(self):
        day, _ = connector._window()
        payload = {"pair_id": "0" * 64, "day": day.date().isoformat()}
        status, _ = self.call("google", "/peer/pull", payload, "not-the-peer")
        self.assertEqual(status, "403 Forbidden")
        status, body = self.call("google", "/peer/pull", payload, "key")
        self.assertEqual(status, "409 Conflict")
        self.assertNotIn(b"intervals", body)
