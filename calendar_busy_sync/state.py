"""Private crash-recovery references and successful-configuration checkpoints."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime

from .settings import Paths, Settings


def fingerprint(settings: Settings) -> str:
    return hashlib.sha256(json.dumps(asdict(settings), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Mapping:
    source: str
    source_id: str
    target: str
    target_id: str
    marker: str


class State:
    def __init__(self, paths: Paths):
        paths.ensure()
        self.connection = sqlite3.connect(paths.database_file)
        paths.database_file.chmod(0o600)
        self.connection.executescript(
            "CREATE TABLE IF NOT EXISTS copies ("
            "source TEXT NOT NULL, source_id TEXT NOT NULL, target TEXT NOT NULL, "
            "target_id TEXT NOT NULL, marker TEXT NOT NULL, "
            "PRIMARY KEY (source, source_id, target));"
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    def mappings(self, source: str, target: str) -> list[Mapping]:
        return [
            Mapping(*row)
            for row in self.connection.execute(
                "SELECT source, source_id, target, target_id, marker FROM copies "
                "WHERE source = ? AND target = ?",
                (source, target),
            )
        ]

    def remember(self, mapping: Mapping):
        self.connection.execute(
            "INSERT INTO copies VALUES (?, ?, ?, ?, ?) ON CONFLICT(source, source_id, target) "
            "DO UPDATE SET target_id=excluded.target_id, marker=excluded.marker",
            (mapping.source, mapping.source_id, mapping.target, mapping.target_id, mapping.marker),
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
