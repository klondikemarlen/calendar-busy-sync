"""Local consent, provider grants, and durable peer unlink notices."""

import hashlib
import json
import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict

from .connector_timing import SharingHours
from .models import SyncError

_CODE_LIFETIME_SECONDS = 10 * 60
_DISCONNECT_BATCH_SIZE = 8
_CODE = re.compile(r"[A-Za-z0-9_-]{32}")
_PAIR_ID = re.compile(r"[0-9a-f]{64}")

_CLAIM = "/peer/claim"
_ACTIVATE = "/peer/activate"
_DISCONNECT = "/peer/disconnect"

_INVALID_CODE = "That pairing code is invalid or has expired."
_INACTIVE_PAIR = "This calendar pair is not active."


class Store:
    def __init__(self, config, db: sqlite3.Connection, peer_call: Callable[[str, dict], dict]):
        self.db = db
        self._role = config.role
        self._peer_call = peer_call
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS connector_accounts (
                subject TEXT NOT NULL PRIMARY KEY,
                grant_text TEXT NOT NULL,
                pair_id TEXT UNIQUE,
                phase TEXT NOT NULL DEFAULT 'unpaired'
                    CHECK (phase IN ('unpaired', 'joining', 'pending', 'confirming', 'active')),
                confirmation_digest TEXT,
                expires_at REAL,
                CHECK ((pair_id IS NULL) = (phase = 'unpaired')),
                CHECK ((expires_at IS NOT NULL) = (phase IN ('joining', 'pending', 'confirming')))
            );
            CREATE TABLE IF NOT EXISTS connector_sharing_hours (
                subject TEXT PRIMARY KEY REFERENCES connector_accounts(subject) ON DELETE CASCADE,
                policy TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS connector_invitations (
                subject TEXT NOT NULL PRIMARY KEY,
                digest TEXT NOT NULL UNIQUE,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS connector_disconnects (
                pair_id TEXT NOT NULL PRIMARY KEY,
                pending INTEGER NOT NULL DEFAULT 1,
                attempts INTEGER NOT NULL DEFAULT 0
            );
            """
        )

    def enroll(self, subject: str, grant_text: str) -> None:
        _require_grant(grant_text)
        if not isinstance(subject, str) or not subject:
            raise SyncError("The provider did not identify a calendar account.")

        with self._transaction():
            self.db.execute(
                """
                INSERT INTO connector_accounts (subject, grant_text) VALUES (?, ?)
                ON CONFLICT (subject) DO UPDATE SET grant_text = excluded.grant_text
                """,
                (subject, grant_text),
            )
            self.db.execute(
                "INSERT INTO connector_sharing_hours (subject, policy) VALUES (?, ?) "
                "ON CONFLICT (subject) DO NOTHING",
                (subject, json.dumps(asdict(SharingHours()))),
            )

    def account(self, subject: str) -> tuple[str | None, bool] | None:
        self._expire_codes(time.time())
        row = self.db.execute(
            "SELECT pair_id, phase FROM connector_accounts WHERE subject = ?", (subject,)
        ).fetchone()
        if row is None:
            return None

        pair_id, phase = row
        return pair_id, phase == "active"

    def sharing_hours(self, subject: str) -> SharingHours:
        row = self.db.execute(
            "SELECT policy FROM connector_sharing_hours WHERE subject = ?", (subject,)
        ).fetchone()
        return _read_hours(row)

    def pair_sharing_hours(self, pair_id: str) -> SharingHours:
        row = self.db.execute(
            "SELECT policy FROM connector_accounts JOIN connector_sharing_hours USING (subject) "
            "WHERE pair_id = ? AND phase = 'active'",
            (pair_id,),
        ).fetchone()
        return _read_hours(row)

    def save_sharing_hours(self, subject: str, hours: SharingHours) -> None:
        result = self.db.execute(
            "UPDATE connector_sharing_hours SET policy = ? WHERE subject = ?",
            (json.dumps(asdict(hours)), subject),
        )
        if result.rowcount != 1:
            raise SyncError("Sign in again before changing sharing hours.")

    def offer(self, subject: str) -> str:
        self._require_role("microsoft")
        now = time.time()
        self._expire_codes(now)
        code = secrets.token_urlsafe(24)
        with self._transaction():
            self._require_unpaired_account(subject)
            self.db.execute(
                """
                INSERT INTO connector_invitations (subject, digest, expires_at) VALUES (?, ?, ?)
                ON CONFLICT (subject) DO UPDATE
                SET digest = excluded.digest, expires_at = excluded.expires_at
                """,
                (subject, _digest(code), now + _CODE_LIFETIME_SECONDS),
            )
        return code

    def join(self, subject: str, code: str) -> str:
        self._require_role("google")
        if not _matches(_CODE, code):
            raise SyncError(_INVALID_CODE)

        now = time.time()
        self._expire_codes(now)
        pair_id = secrets.token_hex(32)
        confirmation = secrets.token_urlsafe(24)
        self._reserve_join(subject, pair_id, now)
        try:
            self._request(
                _CLAIM,
                {"code": code, "pair_id": pair_id, "confirmation_digest": _digest(confirmation)},
            )
            self._expire_codes(time.time())
            result = self.db.execute(
                """
                UPDATE connector_accounts SET phase = 'pending'
                WHERE subject = ? AND pair_id = ? AND phase = 'joining'
                """,
                (subject, pair_id),
            )
            if result.rowcount != 1:
                raise SyncError("The calendar pairing is no longer pending.")
        except SyncError:
            with self._transaction():
                self._queue_disconnect(pair_id)
                self._clear_pair(pair_id)
            raise

        return confirmation

    def confirm(self, subject: str, code: str) -> None:
        self._require_role("microsoft")
        if not _matches(_CODE, code):
            raise SyncError(_INVALID_CODE)

        self._expire_codes(time.time())
        with self._transaction():
            row = self.db.execute(
                """
                SELECT pair_id, confirmation_digest FROM connector_accounts
                WHERE subject = ? AND phase IN ('pending', 'confirming')
                """,
                (subject,),
            ).fetchone()
            if row is None or row[1] is None or not secrets.compare_digest(row[1], _digest(code)):
                raise SyncError(_INVALID_CODE)

            pair_id = row[0]
            self.db.execute(
                "UPDATE connector_accounts SET phase = 'confirming' WHERE pair_id = ?", (pair_id,)
            )

        # A lost acknowledgement keeps the same confirmation retryable, not active, on this side.
        self._request(_ACTIVATE, {"pair_id": pair_id})
        self._expire_codes(time.time())
        self._activate_pair(pair_id, "confirming")

    def disconnect(self, subject: str) -> None:
        with self._transaction():
            self.db.execute("DELETE FROM connector_invitations WHERE subject = ?", (subject,))
            row = self.db.execute(
                "SELECT pair_id FROM connector_accounts WHERE subject = ?", (subject,)
            ).fetchone()
            self.db.execute("DELETE FROM connector_accounts WHERE subject = ?", (subject,))
            if row is None or row[0] is None:
                return

            pair_id = row[0]
            self._queue_disconnect(pair_id)

        self._send_disconnect(pair_id)

    def peer(self, path: str, payload: dict) -> dict:
        if path == _CLAIM:
            self._require_role("microsoft")
            self._receive_claim(payload)
        elif path == _ACTIVATE:
            self._require_role("google")
            pair_id = _peer_pair_id(payload)
            self._expire_codes(time.time())
            self._activate_pair(pair_id, "pending", retry_phase="active")
        elif path == _DISCONNECT:
            self._receive_disconnect(_peer_pair_id(payload))
        else:
            raise SyncError("Unknown peer pairing operation.")

        return {}

    def active_pairs(self) -> list[str]:
        rows = self.db.execute("SELECT pair_id FROM connector_accounts WHERE phase = 'active'")
        return [pair_id for (pair_id,) in rows]

    def grant(self, pair_id: str) -> str:
        row = self.db.execute(
            "SELECT grant_text FROM connector_accounts WHERE pair_id = ? AND phase = 'active'",
            (pair_id,),
        ).fetchone()
        if row is None:
            raise SyncError(_INACTIVE_PAIR)

        return row[0]

    def save_grant(self, pair_id: str, grant_text: str) -> None:
        _require_grant(grant_text)
        result = self.db.execute(
            "UPDATE connector_accounts SET grant_text = ? WHERE pair_id = ? AND phase = 'active'",
            (grant_text, pair_id),
        )
        if result.rowcount != 1:
            raise SyncError(_INACTIVE_PAIR)

    def retry_disconnects(self) -> None:
        self._expire_codes(time.time())
        rows = self.db.execute(
            """
            SELECT pair_id FROM connector_disconnects WHERE pending = 1
            ORDER BY attempts, pair_id LIMIT ?
            """,
            (_DISCONNECT_BATCH_SIZE,),
        ).fetchall()
        for (pair_id,) in rows:
            self._send_disconnect(pair_id)

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _require_role(self, role: str) -> None:
        if self._role != role:
            raise SyncError("This operation is not available on this connector.")

    def _require_unpaired_account(self, subject: str) -> None:
        row = self.db.execute("SELECT phase FROM connector_accounts WHERE subject = ?", (subject,)).fetchone()
        if row is None:
            raise SyncError("Sign in to this connector before pairing calendars.")
        if row[0] != "unpaired":
            raise SyncError("This calendar already has a pending or active pair.")

    def _reserve_join(self, subject: str, pair_id: str, now: float) -> None:
        with self._transaction():
            self._require_unpaired_account(subject)
            self.db.execute(
                """
                UPDATE connector_accounts SET pair_id = ?, phase = 'joining', expires_at = ?
                WHERE subject = ?
                """,
                (pair_id, now + _CODE_LIFETIME_SECONDS, subject),
            )

    def _receive_claim(self, payload: dict) -> None:
        if not isinstance(payload, dict) or payload.keys() != {"code", "pair_id", "confirmation_digest"}:
            raise SyncError("Invalid peer pairing request.")

        code = payload["code"]
        pair_id = payload["pair_id"]
        confirmation_digest = payload["confirmation_digest"]
        if not _matches(_CODE, code) or not _matches(_PAIR_ID, pair_id):
            raise SyncError("Invalid peer pairing request.")
        if not _matches(_PAIR_ID, confirmation_digest):
            raise SyncError("Invalid peer pairing request.")

        now = time.time()
        self._expire_codes(now)
        with self._transaction():
            row = self.db.execute(
                """
                SELECT invitations.subject FROM connector_invitations AS invitations
                JOIN connector_accounts AS accounts USING (subject)
                WHERE invitations.digest = ? AND accounts.phase = 'unpaired'
                  AND NOT EXISTS (SELECT 1 FROM connector_disconnects WHERE pair_id = ?)
                  AND NOT EXISTS (SELECT 1 FROM connector_accounts WHERE pair_id = ?)
                """,
                (_digest(code), pair_id, pair_id),
            ).fetchone()
            if row is None:
                raise SyncError(_INVALID_CODE)

            subject = row[0]
            self.db.execute("DELETE FROM connector_invitations WHERE subject = ?", (subject,))
            self.db.execute(
                """
                UPDATE connector_accounts
                SET pair_id = ?, phase = 'pending', confirmation_digest = ?, expires_at = ?
                WHERE subject = ?
                """,
                (pair_id, confirmation_digest, now + _CODE_LIFETIME_SECONDS, subject),
            )

    def _activate_pair(self, pair_id: str, expected_phase: str, *, retry_phase: str | None = None) -> None:
        result = self.db.execute(
            """
            UPDATE connector_accounts
            SET phase = 'active', confirmation_digest = NULL, expires_at = NULL
            WHERE pair_id = ? AND phase IN (?, ?)
            """,
            (pair_id, expected_phase, retry_phase),
        )
        if result.rowcount != 1:
            raise SyncError("The calendar pairing is no longer pending.")

    def _receive_disconnect(self, pair_id: str) -> None:
        with self._transaction():
            # An unlink may arrive before a delayed claim, so unknown IDs must also be tombstoned.
            self.db.execute(
                """
                INSERT INTO connector_disconnects (pair_id, pending) VALUES (?, 0)
                ON CONFLICT (pair_id) DO UPDATE SET pending = 0
                """,
                (pair_id,),
            )
            self._clear_pair(pair_id)

    def _clear_pair(self, pair_id: str) -> None:
        self.db.execute(
            """
            UPDATE connector_accounts
            SET pair_id = NULL, phase = 'unpaired', confirmation_digest = NULL, expires_at = NULL
            WHERE pair_id = ?
            """,
            (pair_id,),
        )

    def _expire_codes(self, now: float) -> None:
        with self._transaction():
            self.db.execute("DELETE FROM connector_invitations WHERE expires_at <= ?", (now,))
            self.db.execute(
                """
                INSERT OR IGNORE INTO connector_disconnects (pair_id)
                SELECT pair_id FROM connector_accounts WHERE expires_at <= ?
                """,
                (now,),
            )
            self.db.execute(
                """
                UPDATE connector_accounts
                SET pair_id = NULL, phase = 'unpaired', confirmation_digest = NULL, expires_at = NULL
                WHERE expires_at <= ?
                """,
                (now,),
            )

    def _queue_disconnect(self, pair_id: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO connector_disconnects (pair_id) VALUES (?)", (pair_id,))

    def _send_disconnect(self, pair_id: str) -> None:
        result = self.db.execute(
            "UPDATE connector_disconnects SET attempts = attempts + 1 WHERE pair_id = ? AND pending = 1",
            (pair_id,),
        )
        if result.rowcount == 0:
            return

        try:
            self._request(_DISCONNECT, {"pair_id": pair_id})
        except SyncError:
            return

        self.db.execute("UPDATE connector_disconnects SET pending = 0 WHERE pair_id = ?", (pair_id,))

    def _request(self, path: str, payload: dict) -> None:
        try:
            response = self._peer_call(path, payload)
        except Exception:
            raise SyncError("The peer connector did not complete pairing. Try again.") from None

        if not isinstance(response, dict) or response:
            raise SyncError("The peer connector returned an invalid pairing response.")


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _peer_pair_id(payload: dict) -> str:
    if not isinstance(payload, dict) or payload.keys() != {"pair_id"}:
        raise SyncError("Invalid peer pairing request.")

    pair_id = payload["pair_id"]
    if not _matches(_PAIR_ID, pair_id):
        raise SyncError("Invalid peer pairing request.")

    return pair_id


def _require_grant(grant_text: str) -> None:
    if not isinstance(grant_text, str) or not grant_text:
        raise SyncError("The provider did not grant calendar access.")


def _read_hours(row) -> SharingHours:
    if row is not None:
        try:
            fields = json.loads(row[0])
            if isinstance(fields, dict) and fields.keys() == SharingHours.__dataclass_fields__.keys():
                return SharingHours(**fields)
        except (TypeError, ValueError):
            pass
    raise SyncError("Sharing hours are unavailable; no busy times were exported.")
