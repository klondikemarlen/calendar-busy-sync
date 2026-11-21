"""Deployment identity, private storage, and transport for one side of a calendar pair."""

import fcntl
import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .models import SyncError

MAX_PAYLOAD = 512 * 1024


@dataclass(frozen=True)
class Config:
    role: str
    origin: str
    peer_origin: str
    client_id: str
    client_secret: str = field(repr=False)
    peer_secret: str = field(repr=False)
    state_dir: Path
    tenant: str = ""

    @classmethod
    def load(cls):
        try:
            path = Path(os.environ["CBS_CONNECTOR_CONFIG"])
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError
            raw = json.loads(path.read_text())
            config = cls(**{**raw, "state_dir": Path(raw["state_dir"])})
            for origin in (config.origin, config.peer_origin):
                parsed = urlsplit(origin)
                if not parsed.hostname or parsed.username or origin != f"https://{parsed.netloc}":
                    raise ValueError
                if not re.fullmatch(r"[a-zA-Z0-9.-]+(:[0-9]+)?", parsed.netloc) or parsed.port == 0:
                    raise ValueError
            if config.origin == config.peer_origin or not config.client_id or not config.client_secret:
                raise ValueError
            if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", config.peer_secret):
                raise ValueError
            if config.role == "microsoft":
                uuid.UUID(config.tenant)
                uuid.UUID(config.client_id)
            elif config.role != "google" or config.tenant:
                raise ValueError
            if not config.state_dir.is_absolute():
                raise ValueError
            config.state_dir.mkdir(mode=0o700, exist_ok=True)
            info = config.state_dir.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError
            return config
        except (KeyError, AttributeError, OSError, TypeError, ValueError):
            raise SyncError(
                "Check the private connector configuration, identity, and state directory."
            ) from None


class Occupied(SyncError):
    pass


@contextmanager
def database(config):
    with (config.state_dir / "connector.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Occupied("Connector is busy; retry shortly.") from None
        with closing(sqlite3.connect(config.state_dir / "connector.sqlite3", isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA secure_delete=ON")
            db.execute(
                "CREATE TABLE IF NOT EXISTS deployment (id INTEGER PRIMARY KEY, identity TEXT NOT NULL)"
            )
            identity = json.dumps(
                [config.role, config.origin, config.peer_origin, config.tenant, config.client_id]
            )
            db.execute("INSERT OR IGNORE INTO deployment VALUES (1, ?)", (identity,))
            if db.execute("SELECT identity FROM deployment WHERE id=1").fetchone()[0] != identity:
                raise SyncError("State belongs to another connector deployment. Restore its configuration.")
            yield db


def call_peer(config, path, payload):
    if not re.fullmatch(r"/peer/[a-z]+", path):
        raise SyncError("Invalid peer operation.")
    try:
        with requests.post(
            config.peer_origin + path,
            json=payload,
            headers={"Authorization": "Bearer " + config.peer_secret},
            timeout=(5, 90),
            allow_redirects=False,
            stream=True,
        ) as response:
            content_type = response.headers.get("Content-Type", "").split(";")[0]
            if response.status_code != 200 or content_type != "application/json":
                raise SyncError("Peer did not accept the operation. Check both connectors before retrying.")
            body = bytearray()
            for chunk in response.iter_content(16384):
                body.extend(chunk)
                if len(body) > MAX_PAYLOAD:
                    raise SyncError("Peer response exceeds the allowed size.")
            result = json.loads(body)
            if not isinstance(result, dict):
                raise ValueError
            return result
    except (requests.RequestException, ValueError):
        raise SyncError(
            "No confirmed response from the configured peer. Retry with both connectors available."
        ) from None
