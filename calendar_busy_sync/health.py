"""Private sync outcomes and best-effort same-user desktop notices."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import state

REMINDER_INTERVAL = timedelta(hours=24)
_RECOVERY = (
    "Run calendar-busy-sync doctor in a terminal. If authorization is rejected, run "
    "calendar-busy-sync auth google or calendar-busy-sync auth outlook for the affected account, "
    "then calendar-busy-sync sync. Inspect calendar-busy-sync status."
)


def snapshot(paths) -> dict:
    raw = state._metadata(paths).get("sync_health")
    if raw:
        return json.loads(raw)
    return {"outcome": "unknown", "last_attempt": None, "failure_since": None, "recovery": None}


def notify(title: str, body: str) -> bool:
    """Use the current UID's bus, never another user's session or an autolaunched bus."""
    bus = Path(f"/run/user/{os.getuid()}/bus")
    try:
        info = bus.stat()
        if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
            return False
        result = subprocess.run(
            ["/usr/bin/notify-send", "--app-name=Calendar Busy Sync", title, body],
            env=dict(os.environ, DBUS_SESSION_BUS_ADDRESS=f"unix:path={bus}"),
            capture_output=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def record(paths, *, failed: bool, now: datetime | None = None) -> None:
    """Called under the sync lock; notification/persistence failure never masks sync."""
    now = now or datetime.now(UTC)
    try:
        previous = snapshot(paths)
        recovering = previous["outcome"] == "failed" and not failed
        health = {
            "outcome": "failed" if failed else "ok",
            "last_attempt": now.isoformat(),
            "failure_since": (previous.get("failure_since") or now.isoformat()) if failed else None,
            "recovery": _RECOVERY if failed else None,
            "last_notification": previous.get("last_notification") if failed else None,
            "notification": "not-needed",
        }
        if failed:
            last = health["last_notification"]
            if last is None or now - datetime.fromisoformat(last) >= REMINDER_INTERVAL:
                delivered = notify("Calendar Busy Sync needs attention", _RECOVERY)
                health["notification"] = "sent" if delivered else "unavailable"
                if delivered:
                    health["last_notification"] = now.isoformat()
            else:
                health["notification"] = "rate-limited"
        elif recovering:
            delivered = notify("Calendar Busy Sync recovered", "Synchronization completed successfully.")
            health["notification"] = "sent" if delivered else "unavailable"
        with state.State(paths) as store:
            store.connection.execute(
                "INSERT INTO metadata VALUES ('sync_health', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(health),),
            )
            store.connection.commit()
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        # The original sync outcome and terminal diagnostic remain authoritative.
        return
