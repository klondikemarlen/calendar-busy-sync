"""Opt-in two-domain calendar connectors. No provider grant leaves its owner."""

import argparse
import hmac
import json
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from functools import partial
from urllib.parse import urlsplit

from .connector_busy import apply_snapshot, export_snapshot
from .connector_enrollment import Enrollment
from .connector_oauth import calendar_client
from .connector_runtime import MAX_PAYLOAD, Config, Occupied, call_peer, database
from .connector_store import Store
from .models import SyncError


def _window():
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=30)


def _calendar(config, store, pair_id):
    grant = store.grant(pair_id)
    client, refreshed = calendar_client(config, grant)
    if grant != refreshed:
        store.save_grant(pair_id, refreshed)
    return client


def _ownership_namespace(config, pair_id):
    # Do not write the wire pairing identifier or a common identifier to both providers.
    return hmac.digest(bytes.fromhex(pair_id), config.role.encode("ascii"), "sha256").hex()


def _peer_operation(config, store, path, payload):
    if path not in ("/peer/pull", "/peer/apply"):
        return store.peer(path, payload)
    fields = {"pair_id", "day"}
    if path == "/peer/apply":
        fields.add("intervals")
    if config.role != "google" or set(payload) != fields:
        raise SyncError("Invalid calendar exchange request.")
    start, end = _window()
    pair_id = payload["pair_id"]
    if not isinstance(pair_id, str) or not re.fullmatch(r"[0-9a-f]{64}", pair_id):
        raise SyncError("Invalid calendar pair.")
    if payload["day"] != start.date().isoformat():
        raise SyncError("Snapshot dates differ; retry a fresh synchronization.")
    client = _calendar(config, store, pair_id)
    if path == "/peer/pull":
        return {
            **payload,
            "intervals": export_snapshot(client, start, end, store.pair_sharing_hours(pair_id)),
        }
    apply_snapshot(client, _ownership_namespace(config, pair_id), start, end, payload["intervals"])
    return {}


def _sync_pair(config, store, peer, pair_id):
    start, end = _window()
    request = {"pair_id": pair_id, "day": start.date().isoformat()}
    microsoft = _calendar(config, store, pair_id)
    outbound = export_snapshot(microsoft, start, end, store.pair_sharing_hours(pair_id))
    inbound = peer("/peer/pull", request)
    if set(inbound) != {*request, "intervals"} or any(inbound[k] != v for k, v in request.items()):
        raise SyncError("Peer returned a different pair or time window; synchronization stopped.")
    apply_snapshot(microsoft, _ownership_namespace(config, pair_id), start, end, inbound["intervals"])
    if peer("/peer/apply", {**request, "intervals": outbound}) != {}:
        raise SyncError("Peer did not confirm the Busy snapshot.")


def synchronize(config):
    peer = partial(call_peer, config)
    with database(config) as db:
        store = Store(config, db, peer)
        store.retry_disconnects()
        pairs = store.active_pairs() if config.role == "microsoft" else []
    failures = 0
    for pair_id in pairs:
        try:
            with database(config) as db:
                _sync_pair(config, Store(config, db, peer), peer, pair_id)
        except (SyncError, sqlite3.Error, OSError):
            failures += 1
    if failures:
        raise SyncError(f"{failures} pair(s) could not synchronize. Check consent and peer availability.")
    return len(pairs)


def _response(start_response, status, body=b"", content_type="text/plain; charset=utf-8"):
    start_response(
        status,
        [
            ("Content-Type", content_type),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
        ],
    )
    return [body]


def application():
    """WSGI factory; terminate HTTPS at an operator-controlled reverse proxy."""
    config = Config.load()
    peer = partial(call_peer, config)

    def handle(environ, start_response):
        if environ.get("HTTP_HOST", "").lower() != urlsplit(config.origin).netloc.lower():
            return _response(start_response, "400 Bad Request")
        path = environ.get("PATH_INFO", "")
        try:
            if path.startswith("/peer/"):
                auth = environ.get("HTTP_AUTHORIZATION", "").encode("utf-8")
                if not hmac.compare_digest(auth, ("Bearer " + config.peer_secret).encode()):
                    return _response(start_response, "403 Forbidden")
                if environ["REQUEST_METHOD"] != "POST" or environ.get("CONTENT_TYPE") != "application/json":
                    return _response(start_response, "400 Bad Request")
                size = int(environ.get("CONTENT_LENGTH") or "0")
                if not 0 < size <= MAX_PAYLOAD:
                    return _response(start_response, "413 Content Too Large")
                raw = environ["wsgi.input"].read(size)
                if len(raw) != size:
                    return _response(start_response, "400 Bad Request")
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    return _response(start_response, "400 Bad Request")
                with database(config) as db:
                    result = _peer_operation(config, Store(config, db, peer), path, payload)
                return _response(start_response, "200 OK", json.dumps(result).encode(), "application/json")
            with database(config) as db:
                return Enrollment(config, Store(config, db, peer)).handle(environ, start_response)
        except Occupied:
            return _response(start_response, "503 Service Unavailable", b"Connector is busy; retry shortly.")
        except (ValueError, TypeError, RecursionError):
            return _response(start_response, "400 Bad Request", b"Invalid request.")
        except SyncError as exc:
            return _response(start_response, "409 Conflict", str(exc).encode())
        except (OSError, sqlite3.Error):
            return _response(
                start_response, "503 Service Unavailable", b"Connector storage or service unavailable."
            )

    return handle


def main():
    parser = argparse.ArgumentParser(description="Operate one domain's private Busy connector.")
    parser.add_argument(
        "action", choices=["sync"], help="process pending disconnects and synchronize active pairs"
    )
    parser.parse_args()
    try:
        count = synchronize(Config.load())
    except (SyncError, sqlite3.Error, OSError) as exc:
        print(str(exc) if isinstance(exc, SyncError) else "Connector storage unavailable.", file=sys.stderr)
        return 1
    print(f"Completed. Calendar pairs synchronized: {count}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
