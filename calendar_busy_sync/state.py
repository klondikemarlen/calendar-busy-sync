"""Private crash-recovery references and successful-configuration checkpoints."""

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime

from .models import SyncError
from .settings import Paths, Settings


def fingerprint(settings: Settings) -> str:
    configuration = asdict(settings)
    if settings.outlook_backend == "graph":
        # Preserve checkpoints written before Outlook web mode was available.
        del configuration["outlook_backend"]
        del configuration["outlook_web_source_calendar"]
        del configuration["outlook_web_mirror_calendar"]
        del configuration["outlook_web_timezone"]
    return hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()


def check_outlook_pair(paths: Paths, settings: Settings, *, bind: bool = False) -> None:
    """Never reconcile existing references against a different backend or web calendar pair."""
    uses_web = settings.outlook_backend == "web"
    pair = [settings.outlook_backend]
    if uses_web:
        pair.extend((settings.outlook_web_source_calendar, settings.outlook_web_mirror_calendar))
    expected = json.dumps(pair)
    previous = json.dumps(["graph"])
    has_web_identity = paths.outlook_web_identity_file.exists()
    has_copies = False
    if paths.database_file.exists():
        with closing(sqlite3.connect(f"{paths.database_file.as_uri()}?mode=ro", uri=True)) as connection:
            saved = connection.execute("SELECT value FROM metadata WHERE key = 'outlook_pair'").fetchone()
            if saved:
                previous = saved[0]
            if previous != expected or (uses_web and not has_web_identity):
                has_copies = connection.execute("SELECT 1 FROM copies LIMIT 1").fetchone() is not None
    if previous != expected and (has_copies or has_web_identity):
        raise SyncError(
            "Existing Outlook identity or Busy copy references belong to a different backend or calendar pair. "
            "Restore the original configuration or use separate configuration and state."
        )
    if uses_web and not has_web_identity and has_copies:
        raise SyncError(
            "Outlook web mirror identity is missing; restore it from your backup before continuing."
        )
    if bind and expected != previous:
        with State(paths) as store:
            store.connection.execute(
                "INSERT INTO metadata VALUES ('outlook_pair', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (expected,),
            )
            store.connection.commit()


@dataclass(frozen=True)
class Mapping:
    source: str
    source_id: str
    target: str
    target_id: str
    marker: str
    content_version: int = 0


class State:
    def __init__(self, paths: Paths):
        paths.ensure()
        self.connection = sqlite3.connect(paths.database_file)
        paths.database_file.chmod(0o600)
        self.connection.executescript(
            "CREATE TABLE IF NOT EXISTS copies ("
            "source TEXT NOT NULL, source_id TEXT NOT NULL, target TEXT NOT NULL, "
            "target_id TEXT NOT NULL, marker TEXT NOT NULL, "
            "content_version INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY (source, source_id, target));"
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        )
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(copies)")}
        if "content_version" not in columns:
            self.connection.execute(
                "ALTER TABLE copies ADD COLUMN content_version INTEGER NOT NULL DEFAULT 0"
            )
            self.connection.commit()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    def mappings(self, source: str, target: str) -> list[Mapping]:
        return [
            Mapping(*row)
            for row in self.connection.execute(
                "SELECT source, source_id, target, target_id, marker, content_version FROM copies "
                "WHERE source = ? AND target = ?",
                (source, target),
            )
        ]

    def remember(self, mapping: Mapping):
        self.connection.execute(
            "INSERT INTO copies (source, source_id, target, target_id, marker, content_version) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(source, source_id, target) "
            "DO UPDATE SET target_id=excluded.target_id, marker=excluded.marker, "
            "content_version=excluded.content_version",
            (
                mapping.source,
                mapping.source_id,
                mapping.target,
                mapping.target_id,
                mapping.marker,
                mapping.content_version,
            ),
        )
        self.connection.commit()

    def forget(self, mapping: Mapping):
        self.connection.execute(
            "DELETE FROM copies WHERE source = ? AND source_id = ? AND target = ?",
            (mapping.source, mapping.source_id, mapping.target),
        )
        self.connection.commit()

    def success(self, settings: Settings, at: datetime):
        self.connection.executemany(
            "INSERT INTO metadata VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [("last_success", at.isoformat()), ("configuration", fingerprint(settings))],
        )
        self.connection.commit()


def _metadata(paths: Paths) -> dict[str, str]:
    if not paths.database_file.exists():
        return {}
    with sqlite3.connect(f"{paths.database_file.as_uri()}?mode=ro", uri=True) as connection:
        return dict(connection.execute("SELECT key, value FROM metadata"))


def has_success(paths: Paths, settings: Settings) -> bool:
    metadata = _metadata(paths)
    return bool(metadata.get("last_success")) and metadata.get("configuration") == fingerprint(settings)


def last_success(paths: Paths) -> str | None:
    return _metadata(paths).get("last_success")


def invalidate_success(paths: Paths) -> None:
    if paths.database_file.exists():
        with State(paths) as state:
            state.connection.execute("DELETE FROM metadata WHERE key = 'configuration'")
            state.connection.commit()
