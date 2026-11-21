"""Browser consent for one private connector deployment."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from functools import partial
from html import escape
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qsl

from . import connector_oauth
from .connector_timing import MAX_PADDING_MINUTES, SharingHours
from .models import SyncError

_COOKIE = "__Host-cbs_session"
_SESSION_SECONDS = 60 * 60
_PENDING_SECONDS = 10 * 60
_MAX_FORM_BYTES = 8192
_WEEKDAYS = (
    ("mon", "Monday"),
    ("tue", "Tuesday"),
    ("wed", "Wednesday"),
    ("thu", "Thursday"),
    ("fri", "Friday"),
    ("sat", "Saturday"),
    ("sun", "Sunday"),
)


@dataclass(frozen=True)
class _BrowserSession:
    key: str
    csrf: str
    subject: str | None


class Enrollment:
    """Handle browser requests against the store's request-local SQLite connection."""

    def __init__(self, config, store):
        self.config = config
        self.store = store
        self.db = store.db
        self.provider_origin = (
            connector_oauth.MICROSOFT_ORIGIN if config.role == "microsoft" else connector_oauth.GOOGLE_ORIGIN
        )
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS connector_browser_sessions (
                key TEXT PRIMARY KEY,
                csrf TEXT NOT NULL,
                subject TEXT,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS connector_oauth_pending (
                session_key TEXT PRIMARY KEY REFERENCES connector_browser_sessions(key) ON DELETE CASCADE,
                state TEXT NOT NULL,
                flow TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
        """)

    def handle(self, environ, start_response):
        method, path = environ["REQUEST_METHOD"], environ["PATH_INFO"]
        respond = partial(self._respond, start_response)
        match method, path:
            case "GET", "/":
                # Firefox sends Origin: null on form POSTs from a no-referrer document.
                return self._home(environ, partial(respond, referrer_policy="same-origin"))
            case "GET", "/callback":
                return self._callback(environ, respond)
            case "POST", "/connect" | "/pair" | "/timing" | "/disconnect":
                return self._post(environ, respond, path)
            case _:
                return respond("404 Not Found", "Page not found.")

    def _home(self, environ, respond):
        session = self._session(environ)
        headers = []
        if session is None:
            now = int(time.time())
            self.db.execute("DELETE FROM connector_oauth_pending WHERE expires_at <= ?", (now,))
            self.db.execute("DELETE FROM connector_browser_sessions WHERE expires_at <= ?", (now,))
            session, cookie = self._new_session(None)
            headers.append(("Set-Cookie", cookie))

        subject = session.subject
        if subject is None:
            return respond("200 OK", self._connect_form(session), headers=headers)

        account = self.store.account(subject)
        if account is None:
            status = (
                "<p>This calendar is no longer enrolled. Disconnect this session before starting again.</p>"
            )
        else:
            _, active = account
            if active:
                status = "<p>Your calendars are paired. Busy times can be exchanged.</p>"
            else:
                status = self._pair_forms(session)
            status += self._timing_form(session, self.store.sharing_hours(subject))
            status += self._connect_form(session)
        disconnect = self._form(
            session,
            "/disconnect",
            "<p>Disconnecting stops this connector's exchange and removes its saved calendar access. "
            "Existing Busy appointments may remain. Disconnect on the other connector to remove its access, "
            "and use your provider's app permissions if you also want to revoke its approval.</p>"
            '<label><input type="checkbox" name="confirm_disconnect" value="yes" required> '
            "I want to disconnect this calendar.</label>",
            "Disconnect and remove saved access",
        )
        return respond("200 OK", status + disconnect, headers=headers)

    def _connect_form(self, session):
        if self.config.role == "microsoft":
            provider = "Microsoft"
            permission = (
                "Microsoft grants delegated read and write access to your calendars. "
                "This permission can read event details, not just whether you are busy."
            )
        else:
            provider = "Google"
            permission = (
                "Google grants permission to read and edit events, including their details, "
                "on calendars you own. This connector does not change its sharing settings."
            )
        label = f"Sign in with {provider}" if session.subject is None else f"Reauthorize with {provider}"
        return self._form(
            session,
            "/connect",
            f"<p>{permission} This connector uses only your primary calendar, reads events to find busy times, "
            "and writes private Busy appointments. It stores access for scheduled sync while you are signed out. "
            "After pairing, each primary calendar sends its busy times to the other primary calendar. "
            "Unless you set sharing hours, all busy times within the next 30 days can be shared. "
            "Only start and end times are shared, not event details. "
            "Signing in does not pair your calendar or start exchanging your times.</p>"
            '<label><input type="checkbox" name="enroll" value="yes" required> '
            f"I authorize this connector to keep access to my {provider} calendar.</label>",
            label,
        )

    def _pair_forms(self, session):
        peer_origin = escape(self.config.peer_origin, quote=True)
        transfer = (
            "<p>After the Google connector joins and the Microsoft connector confirms, each primary calendar "
            "sends its busy times to the other primary calendar as private Busy appointments. "
            "By default, all busy times within the next 30 days can be shared. Each calendar can limit "
            "what it sends using its outbound sharing settings. "
            "Only start and end times are exchanged, not event titles, descriptions, or attendees. "
            f'Other connector: <a href="{peer_origin}/">{peer_origin}</a>.</p>'
            '<label><input type="checkbox" name="accept_transfer" value="yes" required> '
            "I authorize this transfer of busy times.</label>"
        )
        if self.config.role == "google":
            return self._form(
                session,
                "/pair",
                transfer + self._code_input("Microsoft invitation code"),
                "Join this pair",
                "join",
            )

        offer = self._form(
            session,
            "/pair",
            "<p>Step 1: Generate an invitation to share privately.</p>" + transfer,
            "Generate invitation",
            "offer",
        )
        confirm = self._form(
            session,
            "/pair",
            "<p>Step 2: Enter the confirmation code shown by the Google connector to start exchanging times.</p>"
            + transfer
            + self._code_input("Google confirmation code"),
            "Confirm pairing and start exchange",
            "confirm",
        )
        return offer + confirm

    def _timing_form(self, session, hours):
        if self.config.role == "google":
            peer_provider = "Microsoft"
        else:
            peer_provider = "Google"

        selected_days = hours.days.split()
        day_inputs = []
        for day, label in _WEEKDAYS:
            checked = ""
            if day in selected_days:
                checked = " checked"
            day_inputs.append(
                f'<label><input type="checkbox" name="{day}" value="yes"{checked}> {label}</label>'
            )

        enabled = ""
        if hours.enabled:
            enabled = " checked"
        zone = escape(hours.zone, quote=True)
        start = escape(hours.start, quote=True)
        end = escape(hours.end, quote=True)
        return self._form(
            session,
            "/timing",
            "<h2>Outbound sharing hours and padding</h2>"
            f"<p>These settings affect only Busy appointments sent from this calendar to the {peer_provider} calendar. "
            "To change what comes into this calendar, use the other connector.</p>"
            "<p>By default there is no padding (0 minutes) and no sharing-hours restriction. "
            "Busy time, including padding, is clipped to the next 30 days and, when enabled, "
            "to the selected sharing hours.</p>"
            "<fieldset><legend>Busy padding</legend>"
            "<label>Minutes before "
            f'<input type="number" min="0" max="{MAX_PADDING_MINUTES}" step="1" '
            f'name="lead" value="{hours.lead}" required></label>'
            "<label>Minutes after "
            f'<input type="number" min="0" max="{MAX_PADDING_MINUTES}" step="1" '
            f'name="trail" value="{hours.trail}" required></label>'
            "</fieldset>"
            "<fieldset><legend>Sharing hours</legend>"
            "<p>Choose at least one day. Start and end must be on the same day, with end later than start.</p>"
            f'<label><input type="checkbox" name="enabled" value="yes"{enabled}> Limit sharing to these hours</label>'
            f'<label>Time zone <input name="zone" value="{zone}" required></label> '
            "(IANA name, for example America/Vancouver or UTC)"
            f"<fieldset><legend>Days to share</legend>{''.join(day_inputs)}</fieldset>"
            f'<label>Start <input type="time" name="start" value="{start}" required></label>'
            f'<label>End <input type="time" name="end" value="{end}" required></label>'
            "</fieldset>",
            "Save outbound sharing settings",
        )

    def _post(self, environ, respond, path):
        session = self._session(environ)
        invalid_form = "This form is no longer valid. Return to the connector and try again."
        if session is None or environ.get("HTTP_ORIGIN") != self.config.origin:
            return respond("403 Forbidden", invalid_form)

        try:
            if environ.get("CONTENT_TYPE", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
                raise ValueError
            length = int(environ.get("CONTENT_LENGTH", ""))
            if not 0 < length <= _MAX_FORM_BYTES:
                raise ValueError
            raw = environ["wsgi.input"].read(length)
            if len(raw) != length:
                raise ValueError
            form = _parse_fields(raw.decode("ascii"))
        except ValueError:
            return respond("400 Bad Request", "Invalid form.")

        if not _same_token(form.get("csrf"), session.csrf):
            return respond("403 Forbidden", invalid_form)

        if path == "/connect":
            return self._connect(session, form, respond)
        if session.subject is None:
            return respond("403 Forbidden", "Sign in before managing a calendar.")
        if path == "/pair":
            return self._pair(session.subject, form, respond)
        if path == "/timing":
            return self._save_timing(session.subject, form, respond)

        if form.get("confirm_disconnect") != "yes":
            return respond("400 Bad Request", "Confirm that you want to disconnect this calendar.")
        try:
            self.store.disconnect(session.subject)
        except SyncError:
            return respond(
                "409 Conflict", "Could not disconnect the calendar. Return to the connector and retry."
            )
        self.db.execute("DELETE FROM connector_browser_sessions WHERE subject = ?", (session.subject,))
        return respond("303 See Other", headers=[("Location", "/"), ("Set-Cookie", _cookie("", 0))])

    def _save_timing(self, subject, form, respond):
        if self.store.account(subject) is None:
            return respond("403 Forbidden", "Enroll this calendar before changing sharing settings.")

        try:
            hours = _parse_timing(form)
        except SyncError as error:
            return respond("400 Bad Request", f"<p>{escape(str(error))}</p><p>No settings were saved.</p>")

        try:
            self.store.save_sharing_hours(subject, hours)
        except SyncError:
            return respond(
                "409 Conflict", "Could not save sharing settings. Return to the connector and retry."
            )
        return respond("303 See Other", headers=[("Location", "/")])

    def _connect(self, session, form, respond):
        if form.get("enroll") != "yes":
            return respond("400 Bad Request", "Affirmatively authorize calendar access before signing in.")

        state = secrets.token_urlsafe(32)
        try:
            location, flow = connector_oauth.start_oauth(self.config, state)
        except Exception:
            return respond(
                "502 Bad Gateway", "Could not start provider sign-in. Return to the connector and retry."
            )

        self.db.execute(
            """INSERT INTO connector_oauth_pending (session_key, state, flow, expires_at)
               VALUES (?, ?, ?, ?) ON CONFLICT(session_key) DO UPDATE SET
               state = excluded.state, flow = excluded.flow, expires_at = excluded.expires_at""",
            (session.key, _digest(state), json.dumps(flow), int(time.time()) + _PENDING_SECONDS),
        )
        return respond("303 See Other", headers=[("Location", location)])

    def _callback(self, environ, respond):
        session = self._session(environ)
        invalid = "This sign-in request is no longer valid. Return to the connector and start again."
        try:
            callback = _parse_fields(environ.get("QUERY_STRING", ""))
        except ValueError:
            return respond("400 Bad Request", invalid)
        if session is None or not callback.get("state"):
            return respond("400 Bad Request", invalid)

        # Only a matching callback can consume this transaction. Consumption precedes provider I/O.
        pending = self.db.execute(
            """DELETE FROM connector_oauth_pending
               WHERE session_key = ? AND state = ? AND expires_at > ? RETURNING flow""",
            (session.key, _digest(callback["state"]), int(time.time())),
        ).fetchone()
        if pending is None:
            return respond("400 Bad Request", invalid)
        if "error" in callback or not callback.get("code"):
            return respond(
                "400 Bad Request", "Sign-in was not completed. Return to the connector to try again."
            )

        try:
            subject, grant = connector_oauth.complete_oauth(self.config, json.loads(pending[0]), callback)
        except Exception:
            return respond(
                "400 Bad Request", "Could not verify calendar access. Return to the connector and retry."
            )

        if session.subject is not None and session.subject != subject:
            return respond(
                "409 Conflict", "Sign in with the same account, or disconnect it before switching."
            )

        # Rotate the browser credential when authentication succeeds. Reject a callback if its
        # session expired or was revoked while the provider exchange was in flight.
        rotated = self.db.execute(
            "DELETE FROM connector_browser_sessions WHERE key = ? AND expires_at > ? RETURNING key",
            (session.key, int(time.time())),
        ).fetchone()
        if rotated is None:
            return respond("400 Bad Request", invalid)
        try:
            self.store.enroll(subject, grant)
        except SyncError:
            return respond(
                "409 Conflict", "Could not enroll this calendar. Return to the connector and try again."
            )

        _, cookie = self._new_session(subject)
        return respond("303 See Other", headers=[("Location", "/"), ("Set-Cookie", cookie)])

    def _pair(self, subject, form, respond):
        if form.get("accept_transfer") != "yes":
            return respond("400 Bad Request", "Authorize the transfer of busy times before pairing.")
        if self.store.account(subject) is None:
            return respond("403 Forbidden", "Enroll this calendar before pairing.")

        code = form.get("code", "").strip()
        try:
            match self.config.role, form.get("action"):
                case "microsoft", "offer":
                    offer = escape(self.store.offer(subject))
                    return respond(
                        "200 OK",
                        f"<p>Enter this invitation on the Google connector:</p><p><code>{offer}</code></p>",
                    )
                case "google", "join" if code:
                    confirmation = escape(self.store.join(subject, code))
                    return respond(
                        "200 OK",
                        f"<p>Enter this confirmation on the Microsoft connector:</p><p><code>{confirmation}</code></p>",
                    )
                case "microsoft", "confirm" if code:
                    self.store.confirm(subject, code)
                    return respond("303 See Other", headers=[("Location", "/")])
                case _:
                    return respond("400 Bad Request", "Invalid pairing action or missing code.")
        except SyncError:
            return respond(
                "409 Conflict", "Could not pair the calendars. Check the code or start pairing again."
            )

    def _session(self, environ):
        cookies = SimpleCookie()
        try:
            cookies.load(environ.get("HTTP_COOKIE", ""))
        except CookieError:
            return None
        cookie = cookies.get(_COOKIE)
        if cookie is None or not re.fullmatch(r"[A-Za-z0-9_-]{43}", cookie.value):
            return None

        key = _digest(cookie.value)
        row = self.db.execute(
            "SELECT csrf, subject FROM connector_browser_sessions WHERE key = ? AND expires_at > ?",
            (key, int(time.time())),
        ).fetchone()
        return _BrowserSession(key, row[0], row[1]) if row is not None else None

    def _new_session(self, subject):
        token = secrets.token_urlsafe(32)
        session = _BrowserSession(_digest(token), secrets.token_urlsafe(32), subject)
        self.db.execute(
            "INSERT INTO connector_browser_sessions (key, csrf, subject, expires_at) VALUES (?, ?, ?, ?)",
            (session.key, session.csrf, subject, int(time.time()) + _SESSION_SECONDS),
        )
        return session, _cookie(token, _SESSION_SECONDS)

    def _respond(self, start_response, status, content="", *, headers=(), referrer_policy="no-referrer"):
        body = b""
        if content:
            body = (
                '<!doctype html><html lang="en"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                "<title>Private Calendar Connector</title><body><main><h1>Private Calendar Connector</h1>"
                f'{content}<p><a href="/">Connector home</a></p></main></body></html>'
            ).encode()
        start_response(
            status,
            [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                ("Referrer-Policy", referrer_policy),
                ("X-Content-Type-Options", "nosniff"),
                (
                    "Content-Security-Policy",
                    "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
                    f"form-action 'self' {self.provider_origin}",
                ),
                *headers,
            ],
        )
        return [body]

    @staticmethod
    def _form(session, path, content, button, action=None):
        operation = f'<input type="hidden" name="action" value="{action}">' if action is not None else ""
        return (
            f'<form method="post" action="{path}"><input type="hidden" name="csrf" value="{session.csrf}">'
            f'{operation}{content}<button type="submit">{button}</button></form>'
        )

    @staticmethod
    def _code_input(label):
        return f'<label>{label} <input name="code" required autocomplete="off" maxlength="512"></label>'


def _parse_timing(form):
    lead = form.get("lead", "")
    trail = form.get("trail", "")
    if not re.fullmatch(r"[0-9]+", lead) or not re.fullmatch(r"[0-9]+", trail):
        raise SyncError("Enter both padding amounts as whole minutes, using 0 for no padding.")
    try:
        lead_minutes, trail_minutes = int(lead), int(trail)
    except ValueError:
        raise SyncError("Enter a smaller number of minutes for padding.") from None

    checkbox_names = ["enabled", *(day for day, _ in _WEEKDAYS)]
    if any(name in form and form[name] != "yes" for name in checkbox_names):
        raise SyncError("Choose sharing hours and days using the checkboxes on the connector.")

    return SharingHours(
        lead=lead_minutes,
        trail=trail_minutes,
        enabled="enabled" in form,
        zone=form.get("zone", "").strip(),
        days=" ".join(day for day, _ in _WEEKDAYS if day in form),
        start=form.get("start", ""),
        end=form.get("end", ""),
    )


def _parse_fields(raw):
    if len(raw) > _MAX_FORM_BYTES:
        raise ValueError
    fields = parse_qsl(raw, keep_blank_values=True, max_num_fields=20, errors="strict")
    result = dict(fields)
    if len(result) != len(fields):
        raise ValueError
    return result


def _cookie(token, max_age):
    return f"{_COOKIE}={token}; Path=/; Max-Age={max_age}; Secure; HttpOnly; SameSite=Lax"


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _same_token(received, expected):
    return isinstance(received, str) and secrets.compare_digest(received.encode(), expected.encode())
