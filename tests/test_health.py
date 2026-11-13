import io
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from calendar_busy_sync import cli, engine, health, models, settings, state


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.paths = settings.Paths(root / "config", root / "state")
        self.configured = settings.Settings(microsoft_client_id="00000000-0000-0000-0000-000000000001")
        settings.save_settings(self.paths, self.configured)
        self.now = datetime(2026, 11, 13, tzinfo=UTC)

    def test_failure_reminders_are_persistent_and_recovery_is_once(self):
        with patch("calendar_busy_sync.health.notify", return_value=True) as notify:
            health.record(self.paths, failed=True, now=self.now)
            first = health.snapshot(self.paths)
            notify.assert_called_once_with(
                "Calendar Busy Sync needs attention",
                "Check the connection:\ncalendar-busy-sync doctor\n\n"
                "Recovery instructions:\ncalendar-busy-sync status",
            )
            self.assertEqual(first["outcome"], "failed")
            self.assertEqual(first["failure_since"], self.now.isoformat())
            self.assertIn("calendar-busy-sync auth google", first["recovery"])
            self.assertIn("calendar-busy-sync auth outlook", first["recovery"])
            health.record(self.paths, failed=True, now=self.now + timedelta(hours=23))
            self.assertEqual(notify.call_count, 1)
            self.assertEqual(health.snapshot(self.paths)["notification"], "rate-limited")
            health.record(self.paths, failed=True, now=self.now + timedelta(hours=24))
            self.assertEqual(notify.call_count, 2)
            self.assertEqual(health.snapshot(self.paths)["failure_since"], first["failure_since"])
            health.record(self.paths, failed=False, now=self.now + timedelta(hours=25))
            self.assertEqual(notify.call_args.args[0], "Calendar Busy Sync recovered")
            self.assertIsNone(health.snapshot(self.paths)["failure_since"])
            health.record(self.paths, failed=False, now=self.now + timedelta(hours=26))
            self.assertEqual(notify.call_count, 3)
            health.record(self.paths, failed=True, now=self.now + timedelta(hours=27))
            self.assertEqual(notify.call_count, 4)
        self.assertEqual(self.paths.database_file.stat().st_mode & 0o777, 0o600)

    def test_unavailable_desktop_retries_on_next_sync_without_claiming_delivery(self):
        with patch("calendar_busy_sync.health.notify", side_effect=[False, True]) as notify:
            health.record(self.paths, failed=True, now=self.now)
            self.assertEqual(health.snapshot(self.paths)["notification"], "unavailable")
            self.assertIsNone(health.snapshot(self.paths)["last_notification"])
            health.record(self.paths, failed=True, now=self.now + timedelta(minutes=15))
            self.assertEqual(notify.call_count, 2)
            self.assertEqual(health.snapshot(self.paths)["notification"], "sent")

    def test_cron_notification_uses_same_uid_bus_not_inherited_address(self):
        info = SimpleNamespace(st_uid=os.getuid(), st_mode=stat.S_IFSOCK | 0o600)
        with (
            patch("calendar_busy_sync.health.Path.stat", return_value=info),
            patch(
                "calendar_busy_sync.health.subprocess.run", return_value=SimpleNamespace(returncode=0)
            ) as run,
            patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/untrusted"}),
        ):
            self.assertTrue(health.notify("Synthetic failure", "Generic recovery"))
        self.assertEqual(run.call_args.args[0][0], "/usr/bin/notify-send")
        self.assertEqual(
            run.call_args.kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"],
            f"unix:path=/run/user/{os.getuid()}/bus",
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_missing_session_or_notification_command_is_safe(self):
        with patch("calendar_busy_sync.health.Path.stat", side_effect=FileNotFoundError):
            self.assertFalse(health.notify("title", "body"))
        info = SimpleNamespace(st_uid=os.getuid(), st_mode=stat.S_IFSOCK | 0o600)
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("notify-send", 5)):
            with (
                patch("calendar_busy_sync.health.Path.stat", return_value=info),
                patch("calendar_busy_sync.health.subprocess.run", side_effect=error),
            ):
                self.assertFalse(health.notify("title", "body"))
        info.st_uid += 1
        with patch("calendar_busy_sync.health.Path.stat", return_value=info):
            self.assertFalse(health.notify("title", "body"))

    def test_cli_failure_status_and_recovery_preserve_exit_code_and_privacy(self):
        clients = {name: SimpleNamespace(list_events=lambda *_: []) for name in ("google", "outlook")}
        with (
            patch("calendar_busy_sync.cli._paths", return_value=self.paths),
            patch("calendar_busy_sync.health.notify", return_value=True) as notify,
            redirect_stderr(io.StringIO()),
            patch(
                "calendar_busy_sync.auth.build_clients",
                side_effect=models.SyncError("Google authorization expired; run auth google."),
            ),
        ):
            self.assertEqual(cli.main(["sync"]), 2)
        self.assertEqual(health.snapshot(self.paths)["outcome"], "failed")
        self.assertNotIn("expired", json.dumps(health.snapshot(self.paths)))
        self.assertNotIn("expired", str(notify.call_args))
        output = io.StringIO()
        with (
            patch("calendar_busy_sync.cli._paths", return_value=self.paths),
            patch("calendar_busy_sync.cli.schedule_status", return_value=SimpleNamespace(enabled=True)),
            redirect_stdout(output),
        ):
            self.assertEqual(cli.main(["status"]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["sync_health"]["outcome"], "failed")
        self.assertIsNone(report["last_success"])
        with (
            patch("calendar_busy_sync.auth.build_clients", return_value=clients),
            patch("calendar_busy_sync.health.notify", return_value=True) as notify,
        ):
            self.assertEqual(engine.run_sync(None, self.paths)["created"], 0)
        self.assertEqual(health.snapshot(self.paths)["outcome"], "ok")
        self.assertIsNotNone(state.last_success(self.paths))
        self.assertEqual(notify.call_args.args[0], "Calendar Busy Sync recovered")

    def test_dry_run_and_lock_contention_do_not_change_health(self):
        with patch("calendar_busy_sync.health.notify", return_value=True):
            health.record(self.paths, failed=True, now=self.now)
        before = health.snapshot(self.paths)
        clients = {name: SimpleNamespace(list_events=lambda *_: []) for name in ("google", "outlook")}
        with patch("calendar_busy_sync.auth.build_clients", return_value=clients):
            engine.run_sync(None, self.paths, dry_run=True)
        with patch("calendar_busy_sync.auth.build_clients", side_effect=models.SyncError("failed")):
            with self.assertRaises(models.SyncError):
                engine.run_sync(None, self.paths, dry_run=True)
        with engine.sync_lock(self.paths), self.assertRaises(models.SyncError):
            engine.run_sync(None, self.paths)
        self.assertEqual(health.snapshot(self.paths), before)
        self.assertIsNone(state.last_success(self.paths))

    def test_state_failure_does_not_replace_original_sync_error(self):
        with (
            patch("calendar_busy_sync.auth.build_clients", side_effect=models.SyncError("original")),
            patch("calendar_busy_sync.health.snapshot", side_effect=sqlite3.OperationalError("unavailable")),
        ):
            with self.assertRaisesRegex(models.SyncError, "original"):
                engine.run_sync(None, self.paths)

    def test_legacy_state_retains_checkpoint_and_reports_unknown_health(self):
        with state.State(self.paths) as store:
            store.success(self.configured, self.now)
        self.assertEqual(health.snapshot(self.paths)["outcome"], "unknown")
        self.assertTrue(state.has_success(self.paths, self.configured))
