import base64
import hashlib
import io
import json
import re
import sqlite3
import time
import unittest
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit
from wsgiref.util import setup_testing_defaults

import msal
import requests

from calendar_busy_sync import connector_oauth
from calendar_busy_sync.connector_oauth import calendar_client
from calendar_busy_sync.connector_timing import SharingHours
from calendar_busy_sync.models import SyncError
from calendar_busy_sync.providers import GoogleCalendarClient, OutlookCalendarClient

# isort: split

from calendar_busy_sync.connector_enrollment import Enrollment

_GOOGLE_EVENTS = "https://www.googleapis.com/auth/calendar.events.owned"
_TENANT = "6a4e26da-a6e4-49b0-af7a-2be5418538b4"


def connector_config(role):
    peer_role = "google" if role == "microsoft" else "microsoft"
    return SimpleNamespace(
        role=role,
        origin=f"https://{role}.example.test",
        peer_origin=f"https://{peer_role}.example.test",
        tenant=_TENANT,
        client_id="synthetic-client-id",
        client_secret="synthetic-client-secret",
    )


class _FormInputs(HTMLParser):
    def __init__(self, body, action):
        super().__init__()
        self.action = action
        self.inside_form = False
        self.inputs = {}
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            self.inside_form = attributes.get("action") == self.action and attributes.get("method") == "post"
        if tag == "input" and self.inside_form:
            self.inputs[attributes["name"]] = attributes

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside_form = False

    def submission(self):
        return {
            name: attrs["value"]
            for name, attrs in self.inputs.items()
            if attrs.get("type") != "checkbox" or "checked" in attrs
        }


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        db = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        self.store = Mock(db=db)
        self.store.account.return_value = (None, False)
        self.store.sharing_hours.return_value = SharingHours()
        self.origin = "https://google.example.test"
        self.app = Enrollment(connector_config("google"), self.store)

    def request(self, method, path, *, cookie=None, form=None, query=None, cross_origin=False):
        environ = {}
        setup_testing_defaults(environ)
        environ.update(REQUEST_METHOD=method, PATH_INFO=path, QUERY_STRING=urlencode(query or {}))
        if cookie:
            environ["HTTP_COOKIE"] = cookie
        if form is not None:
            body = urlencode(form).encode("ascii")
            environ.update(
                HTTP_ORIGIN="https://unrelated.example.test" if cross_origin else self.origin,
                CONTENT_TYPE="application/x-www-form-urlencoded",
                CONTENT_LENGTH=str(len(body)),
            )
            environ["wsgi.input"] = io.BytesIO(body)
        start = Mock()
        body = b"".join(self.app.handle(environ, start)).decode()
        status, headers = start.call_args.args
        return status, dict(headers), body

    def open_home(self, cookie=None):
        status, headers, body = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, "200 OK")
        if cookie is None:
            cookie = headers["Set-Cookie"].split(";", 1)[0]
        csrf = re.search(r'name="csrf" value="([^"]+)"', body)[1]
        return cookie, csrf

    def start_sign_in(self, cookie, csrf):
        # Real Google authorization creation requires no network; Microsoft tests simulate Entra discovery.
        if self.app.config.role == "microsoft":
            url = "https://login.microsoftonline.com/authorize"
            with patch.object(connector_oauth, "start_oauth") as start:
                start.side_effect = lambda config, state: (url + "?" + urlencode({"state": state}), {})
                response = self.request(
                    "POST", "/connect", cookie=cookie, form={"csrf": csrf, "enroll": "yes"}
                )
        else:
            response = self.request("POST", "/connect", cookie=cookie, form={"csrf": csrf, "enroll": "yes"})
        status, headers, body = response
        self.assertEqual(status, "303 See Other")
        self.assertEqual(body, "")
        return parse_qs(urlsplit(headers["Location"]).query)["state"][0]

    def signed_in(self, subject):
        cookie, csrf = self.open_home()
        state = self.start_sign_in(cookie, csrf)
        with patch.object(connector_oauth, "complete_oauth", return_value=(subject, "synthetic-grant")):
            status, headers, body = self.request(
                "GET", "/callback", cookie=cookie, query={"state": state, "code": "synthetic-code"}
            )
        self.assertEqual(status, "303 See Other")
        self.assertEqual(body, "")
        self.assertEqual(headers["Location"], "/")
        return self.open_home(headers["Set-Cookie"].split(";", 1)[0])

    def test_home_discloses_google_scope_and_does_not_enroll(self):
        status, headers, body = self.request("GET", "/", query={"enroll": "yes"})

        self.assertEqual(status, "200 OK")
        self.assertIn("Google grants permission to read and edit events, including their details", body)
        self.assertIn("on calendars you own", body)
        self.assertIn("does not change its sharing settings", body)
        self.assertIn("uses only your primary calendar", body)
        self.assertRegex(body, r"start and end times")
        self.assertIn("Signing in does not pair", body)
        self.assertIn('name="enroll" value="yes" required', body)
        self.assertNotIn('action="/pair"', body)
        self.assertNotIn('action="/timing"', body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "same-origin")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertRegex(headers["Set-Cookie"], r"^__Host-cbs_session=[a-zA-Z0-9_-]+;")
        self.assertIn("; Path=/;", headers["Set-Cookie"])
        self.assertTrue(headers["Set-Cookie"].endswith("; Secure; HttpOnly; SameSite=Lax"))
        self.assertNotIn("Domain=", headers["Set-Cookie"])
        self.store.enroll.assert_not_called()

    def test_enrolled_calendar_displays_zero_padding_and_no_sharing_hours_limit_by_default(self):
        cookie, csrf = self.signed_in("google-user")

        status, _, body = self.request("GET", "/", cookie=cookie)

        self.assertEqual(status, "200 OK")
        controls = _FormInputs(body, "/timing")
        self.assertEqual(
            controls.submission(),
            {
                "csrf": csrf,
                "lead": "0",
                "trail": "0",
                "zone": "UTC",
                "mon": "yes",
                "tue": "yes",
                "wed": "yes",
                "thu": "yes",
                "fri": "yes",
                "start": "09:00",
                "end": "17:00",
            },
        )
        self.assertEqual(controls.inputs["enabled"]["type"], "checkbox")

    def test_paired_calendar_displays_its_saved_outbound_settings(self):
        self.origin = "https://microsoft.example.test"
        self.app = Enrollment(connector_config("microsoft"), self.store)
        self.store.account.return_value = (None, True)
        saved_hours = {
            "microsoft-user": SharingHours(
                lead=10,
                trail=15,
                enabled=True,
                zone="America/Vancouver",
                days="tue sat",
                start="08:30",
                end="18:15",
            ),
            "other-user": SharingHours(),
        }
        self.store.sharing_hours.side_effect = saved_hours.__getitem__
        cookie, csrf = self.signed_in("microsoft-user")
        self.signed_in("other-user")

        status, _, body = self.request("GET", "/", cookie=cookie)

        self.assertEqual(status, "200 OK")
        self.assertEqual(
            _FormInputs(body, "/timing").submission(),
            {
                "csrf": csrf,
                "lead": "10",
                "trail": "15",
                "enabled": "yes",
                "zone": "America/Vancouver",
                "tue": "yes",
                "sat": "yes",
                "start": "08:30",
                "end": "18:15",
            },
        )

    def test_timing_redirects_after_saving_for_the_signed_in_calendar(self):
        cookie, csrf = self.signed_in("user-a")
        self.signed_in("user-b")

        status, headers, body = self.request(
            "POST",
            "/timing",
            cookie=cookie,
            form={
                "csrf": csrf,
                "subject": "user-b",
                "lead": "15",
                "trail": "5",
                "enabled": "yes",
                "zone": "America/Vancouver",
                "wed": "yes",
                "sun": "yes",
                "start": "08:15",
                "end": "18:45",
            },
        )

        self.assertEqual(status, "303 See Other")
        self.assertEqual(body, "")
        self.assertEqual(headers["Location"], "/")
        self.store.save_sharing_hours.assert_called_once_with(
            "user-a",
            SharingHours(
                lead=15,
                trail=5,
                enabled=True,
                zone="America/Vancouver",
                days="wed sun",
                start="08:15",
                end="18:45",
            ),
        )

    def test_timing_can_turn_off_the_window_without_changing_its_hours(self):
        self.store.sharing_hours.return_value = SharingHours(
            enabled=True,
            zone="America/Vancouver",
            days="sat",
            start="10:00",
            end="12:00",
        )
        cookie, _ = self.signed_in("user-a")
        _, _, home = self.request("GET", "/", cookie=cookie)
        form = _FormInputs(home, "/timing").submission()
        del form["enabled"]

        status, _, body = self.request("POST", "/timing", cookie=cookie, form=form)

        self.assertEqual(status, "303 See Other")
        self.assertEqual(body, "")
        self.store.save_sharing_hours.assert_called_once_with(
            "user-a",
            SharingHours(
                lead=0,
                trail=0,
                enabled=False,
                zone="America/Vancouver",
                days="sat",
                start="10:00",
                end="12:00",
            ),
        )

    def test_timing_rejects_invalid_settings_without_showing_user_input(self):
        cookie, _ = self.signed_in("user-a")
        _, _, home = self.request("GET", "/", cookie=cookie)
        form = _FormInputs(home, "/timing").submission()
        form["zone"] = "<script>untrusted-zone</script>"

        status, _, body = self.request("POST", "/timing", cookie=cookie, form=form)

        self.assertEqual(status, "400 Bad Request")
        self.assertNotIn("untrusted-zone", body)
        self.assertIn('href="/"', body)
        self.store.save_sharing_hours.assert_not_called()

    def test_timing_requires_a_signed_in_calendar(self):
        cookie, csrf = self.open_home()

        status, _, _ = self.request("POST", "/timing", cookie=cookie, form={"csrf": csrf})

        self.assertEqual(status, "403 Forbidden")
        self.store.save_sharing_hours.assert_not_called()

    def test_timing_rejects_cross_origin_requests(self):
        cookie, csrf = self.signed_in("user-a")

        status, _, _ = self.request("POST", "/timing", cookie=cookie, form={"csrf": csrf}, cross_origin=True)

        self.assertEqual(status, "403 Forbidden")
        self.store.save_sharing_hours.assert_not_called()

    def test_timing_rejects_csrf_tokens_from_other_sessions(self):
        cookie, _ = self.signed_in("user-a")
        _, other_csrf = self.signed_in("user-b")

        status, _, _ = self.request("POST", "/timing", cookie=cookie, form={"csrf": other_csrf})

        self.assertEqual(status, "403 Forbidden")
        self.store.save_sharing_hours.assert_not_called()

    def test_post_rejects_wrong_origin_token_and_absent_cookie(self):
        cookie, csrf = self.open_home()
        other_cookie, _ = self.open_home()
        with patch.object(connector_oauth, "start_oauth") as start:
            for test_cookie, cross_origin in [(cookie, True), (other_cookie, False), (None, False)]:
                status, _, body = self.request(
                    "POST",
                    "/connect",
                    cookie=test_cookie,
                    form={"csrf": csrf, "enroll": "yes"},
                    cross_origin=cross_origin,
                )
                self.assertEqual(status, "403 Forbidden")
                self.assertIn("This form is no longer valid.", body)
        start.assert_not_called()

    def test_connect_rejects_missing_affirmative_enrollment(self):
        cookie, csrf = self.open_home()
        with patch.object(connector_oauth, "start_oauth") as start:
            status, _, body = self.request("POST", "/connect", cookie=cookie, form={"csrf": csrf})

        self.assertEqual(status, "400 Bad Request")
        self.assertIn("Affirmatively authorize calendar access before signing in.", body)
        start.assert_not_called()

    def test_state_is_browser_bound_single_use_and_rotates_the_cookie(self):
        cookie, csrf = self.open_home()
        other_cookie, _ = self.open_home()
        state = self.start_sign_in(cookie, csrf)
        query = {"state": state, "code": "synthetic-code"}
        with patch.object(
            connector_oauth, "complete_oauth", return_value=("user-a", "synthetic-grant")
        ) as complete:
            status, _, body = self.request("GET", "/callback", cookie=other_cookie, query=query)
            self.assertEqual(status, "400 Bad Request")
            self.assertIn("This sign-in request is no longer valid.", body)
            status, _, body = self.request(
                "GET", "/callback", cookie=cookie, query=dict(query, state="wrong")
            )
            self.assertEqual(status, "400 Bad Request")
            self.assertIn("This sign-in request is no longer valid.", body)
            complete.assert_not_called()

            status, headers, body = self.request("GET", "/callback", cookie=cookie, query=query)
            self.assertEqual(status, "303 See Other")
            self.assertEqual(body, "")
            self.assertNotEqual(headers["Set-Cookie"].split(";", 1)[0], cookie)
            self.assertEqual(headers["Location"], "/")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            complete.assert_called_once()

            status, _, body = self.request("GET", "/callback", cookie=cookie, query=query)
            self.assertEqual(status, "400 Bad Request")
            self.assertIn("This sign-in request is no longer valid.", body)
        self.store.enroll.assert_called_once_with("user-a", "synthetic-grant")
        self.store.offer.assert_not_called()
        self.store.join.assert_not_called()
        self.store.confirm.assert_not_called()

    def test_pending_authorization_expires_before_session(self):
        with patch.object(time, "time", return_value=1000):
            cookie, csrf = self.open_home()
            state = self.start_sign_in(cookie, csrf)
        with (
            patch.object(time, "time", return_value=1601),
            patch.object(connector_oauth, "complete_oauth") as complete,
        ):
            status, _, body = self.request(
                "GET", "/callback", cookie=cookie, query={"state": state, "code": "synthetic-code"}
            )

        self.assertEqual(status, "400 Bad Request")
        self.assertIn("This sign-in request is no longer valid.", body)
        complete.assert_not_called()

    def test_denial_consumes_valid_state_without_echoing_provider_error(self):
        cookie, csrf = self.open_home()
        state = self.start_sign_in(cookie, csrf)
        with patch.object(connector_oauth, "complete_oauth") as complete:
            status, _, body = self.request(
                "GET",
                "/callback",
                cookie=cookie,
                query={
                    "state": state,
                    "error": "access_denied",
                    "error_description": "private-provider-detail",
                },
            )
            self.assertEqual(status, "400 Bad Request")
            self.assertIn("Sign-in was not completed.", body)
            self.assertNotIn("private-provider-detail", body)
            status, _, body = self.request(
                "GET", "/callback", cookie=cookie, query={"state": state, "code": "synthetic-code"}
            )
            self.assertEqual(status, "400 Bad Request")
            self.assertIn("This sign-in request is no longer valid.", body)
        complete.assert_not_called()

    def test_reauthorization_cannot_switch_to_a_different_enrolled_user(self):
        cookie_a, csrf_a = self.signed_in("user-a")
        self.signed_in("user-b")
        self.store.enroll.reset_mock()
        state = self.start_sign_in(cookie_a, csrf_a)
        with patch.object(connector_oauth, "complete_oauth", return_value=("user-b", "other-secret")):
            status, headers, body = self.request(
                "GET", "/callback", cookie=cookie_a, query={"state": state, "code": "synthetic-code"}
            )

        self.assertEqual(status, "409 Conflict")
        self.assertIn("Sign in with the same account", body)
        self.assertNotIn("Set-Cookie", headers)
        self.assertNotIn("other-secret", body)
        self.store.enroll.assert_not_called()

    def test_pairing_requires_authentication_and_separate_transfer_agreement(self):
        cookie, csrf = self.open_home()
        status, _, body = self.request(
            "POST",
            "/pair",
            cookie=cookie,
            form={"csrf": csrf, "action": "join", "code": "microsoft-invite", "accept_transfer": "yes"},
        )
        self.assertEqual(status, "403 Forbidden")
        self.assertIn("Sign in before managing a calendar.", body)

        cookie, csrf = self.signed_in("google-user")
        status, _, body = self.request(
            "POST", "/pair", cookie=cookie, form={"csrf": csrf, "action": "join", "code": "microsoft-invite"}
        )
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("Authorize the transfer of busy times before pairing.", body)
        self.store.join.assert_not_called()

    def test_google_join_uses_session_subject_and_returns_only_confirmation_code(self):
        cookie, csrf = self.signed_in("google-user")
        self.store.join.return_value = "google-confirmation"
        status, _, body = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, "200 OK")
        consent = _FormInputs(body, "/pair").inputs["accept_transfer"]
        self.assertEqual(consent["type"], "checkbox")
        self.assertIn("required", consent)
        self.assertNotIn("checked", consent)

        status, headers, body = self.request(
            "POST",
            "/pair",
            cookie=cookie,
            form={
                "csrf": csrf,
                "action": "join",
                "code": "microsoft-invite",
                "subject": "unrelated-user",
                "accept_transfer": "yes",
            },
        )
        self.assertEqual(status, "200 OK")
        self.assertIn("<code>google-confirmation</code>", body)
        self.assertNotIn("microsoft-invite", body)
        self.assertNotIn("google-user", body)
        self.assertNotIn("synthetic-grant", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.store.join.assert_called_once_with("google-user", "microsoft-invite")

    def test_microsoft_enrollment_discloses_delegated_access_and_uses_two_pair_actions(self):
        self.origin = "https://microsoft.example.test"
        self.app = Enrollment(connector_config("microsoft"), self.store)
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, "200 OK")
        self.assertIn("Microsoft grants delegated read and write access", body)
        cookie, csrf = self.signed_in("microsoft-user")
        self.store.offer.return_value = "microsoft-invitation"

        status, _, body = self.request(
            "POST", "/pair", cookie=cookie, form={"csrf": csrf, "action": "offer", "accept_transfer": "yes"}
        )
        self.assertEqual(status, "200 OK")
        self.assertIn("<code>microsoft-invitation</code>", body)
        self.store.offer.assert_called_once_with("microsoft-user")
        self.store.confirm.assert_not_called()

        status, headers, body = self.request(
            "POST",
            "/pair",
            cookie=cookie,
            form={
                "csrf": csrf,
                "action": "confirm",
                "code": "google-confirmation",
                "accept_transfer": "yes",
            },
        )
        self.assertEqual(status, "303 See Other")
        self.assertEqual(body, "")
        self.assertEqual(headers["Location"], "/")
        self.store.confirm.assert_called_once_with("microsoft-user", "google-confirmation")

    def test_disconnect_uses_only_the_confirmed_subject_and_revokes_its_browser_sessions(self):
        cookie_a, csrf_a = self.signed_in("user-a")
        another_a, another_csrf_a = self.signed_in("user-a")
        cookie_b, csrf_b = self.signed_in("user-b")
        status, _, body = self.request("POST", "/disconnect", cookie=cookie_a, form={"csrf": csrf_a})
        self.assertEqual(status, "400 Bad Request")
        self.assertIn("Confirm that you want to disconnect this calendar.", body)
        self.store.disconnect.assert_not_called()

        status, headers, body = self.request(
            "POST",
            "/disconnect",
            cookie=cookie_a,
            form={"csrf": csrf_a, "subject": "user-b", "confirm_disconnect": "yes"},
        )
        self.assertEqual(status, "303 See Other")
        self.assertEqual(body, "")
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.store.disconnect.assert_called_once_with("user-a")

        status, _, body = self.request(
            "POST",
            "/disconnect",
            cookie=another_a,
            form={"csrf": another_csrf_a, "confirm_disconnect": "yes"},
        )
        self.assertEqual(status, "403 Forbidden")
        self.assertIn("This form is no longer valid.", body)
        self.assertEqual(self.open_home(cookie_b), (cookie_b, csrf_b))


class ProviderGrantTests(unittest.TestCase):
    def test_google_web_flow_uses_oidc_offline_pkce_and_only_owned_events(self):
        config = connector_config("google")
        location, pending = connector_oauth.start_oauth(config, "synthetic-state")
        query = parse_qs(urlsplit(location).query)
        verifier_digest = hashlib.sha256(pending["verifier"].encode()).digest()
        challenge = base64.urlsafe_b64encode(verifier_digest).decode().rstrip("=")
        self.assertTrue(location.startswith("https://accounts.google.com/"))
        self.assertEqual(query["client_id"], [config.client_id])
        self.assertEqual(query["redirect_uri"], [config.origin + "/callback"])
        self.assertEqual(query["state"], ["synthetic-state"])
        self.assertEqual(query["nonce"], [pending["nonce"]])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["code_challenge"], [challenge])
        self.assertEqual(set(query["scope"][0].split()), {"openid", _GOOGLE_EVENTS})
        self.assertNotIn(config.client_secret, location)
        self.assertNotIn(pending["verifier"], location)

    def test_google_validates_id_token_and_nonce_and_keeps_only_scheduling_grant(self):
        config = connector_config("google")
        _, pending = connector_oauth.start_oauth(config, "synthetic-state")
        callback = {"state": "synthetic-state", "code": "synthetic-code"}

        def issue_token(flow, *, code, timeout):
            self.assertEqual(code, "synthetic-code")
            self.assertGreater(timeout, 0)
            self.assertEqual(flow.code_verifier, pending["verifier"])
            flow.oauth2session.token = {
                "refresh_token": "synthetic-refresh",
                "access_token": "synthetic-access",
                "id_token": "synthetic-identity",
                "expires_at": time.time() + 3600,
                "scope": ["openid", _GOOGLE_EVENTS],
            }
            return flow.oauth2session.token

        with (
            patch.object(connector_oauth.Flow, "fetch_token", autospec=True, side_effect=issue_token),
            patch.object(connector_oauth.id_token, "verify_oauth2_token") as verify,
        ):
            verify.return_value = {"nonce": "wrong", "sub": "verified-user"}
            with self.assertRaises(ValueError):
                connector_oauth.complete_oauth(config, pending, callback)
            verify.return_value = {
                "nonce": pending["nonce"],
                "sub": "verified-user",
                "email": "private@example.test",
            }
            subject, grant = connector_oauth.complete_oauth(config, pending, callback)
            self.assertEqual(verify.call_args.args[0], "synthetic-identity")
            self.assertEqual(verify.call_args.kwargs, {"audience": config.client_id})

        self.assertEqual(subject, "verified-user")
        self.assertEqual(set(json.loads(grant)), {"token", "refresh_token", "expiry"})
        client, saved = calendar_client(config, grant)
        self.assertIsInstance(client, GoogleCalendarClient)
        self.assertEqual(client.calendar_id, "primary")
        self.assertEqual(saved, grant)

    def test_microsoft_web_flow_and_worker_use_msal_with_synthetic_token_endpoint(self):
        config = connector_config("microsoft")
        directory = f"https://login.microsoftonline.com/{_TENANT}"
        discovery = directory + "/v2.0/.well-known/openid-configuration"
        authorize = directory + "/oauth2/v2.0/authorize"
        token_endpoint = directory + "/oauth2/v2.0/token"
        issuer = directory + "/v2.0"
        callback = {"state": "synthetic-state", "code": "synthetic-code"}
        token_requests = []

        def encode_segment(value):
            raw = json.dumps(value).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        def entra(_session, method, url, **kwargs):
            if url == discovery:
                self.assertEqual(method, "GET")
                payload = {
                    "authorization_endpoint": authorize,
                    "token_endpoint": token_endpoint,
                    "issuer": issuer,
                }
            else:
                self.assertEqual((method, url), ("POST", token_endpoint))
                data = kwargs["data"]
                token_requests.append(data["grant_type"])
                payload = {
                    "token_type": "Bearer",
                    "scope": "Calendars.ReadWrite openid",
                    "expires_in": 3600,
                    "client_info": encode_segment({"uid": "provider-account", "utid": _TENANT}),
                }
                if data["grant_type"] == "authorization_code":
                    self.assertEqual(data["code"], callback["code"])
                    self.assertEqual(data["redirect_uri"], config.origin + "/callback")
                    digest = hashlib.sha256(data["code_verifier"].encode()).digest()
                    self.assertEqual(
                        query["code_challenge"], [base64.urlsafe_b64encode(digest).decode().rstrip("=")]
                    )
                    now = int(time.time())
                    claims = {
                        "iss": issuer,
                        "tid": _TENANT,
                        "aud": config.client_id,
                        "nonce": query["nonce"][0],
                        "sub": "verified-work-user",
                        "iat": now,
                        "exp": now + 3600,
                    }
                    identity = f"{encode_segment({'alg': 'RS256'})}.{encode_segment(claims)}.synthetic"
                    payload.update(
                        access_token="initial-access",
                        id_token=identity,
                        refresh_token="initial-refresh",
                        expires_in=120,
                    )
                else:
                    self.assertEqual(data["grant_type"], "refresh_token")
                    self.assertEqual(data["refresh_token"], "initial-refresh")
                    payload.update(access_token="refreshed-access", refresh_token="refreshed-refresh")
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(payload).encode()
            response.headers["Content-Type"] = "application/json"
            return response

        with patch.object(requests.Session, "request", autospec=True, side_effect=entra):
            location, pending = connector_oauth.start_oauth(config, callback["state"])
            query = parse_qs(urlsplit(location).query)
            self.assertTrue(location.startswith(authorize + "?"))
            self.assertEqual(query["state"], [callback["state"]])
            self.assertEqual(query["response_mode"], ["query"])
            self.assertEqual(query["code_challenge_method"], ["S256"])
            self.assertEqual(
                set(query["scope"][0].split()), {"Calendars.ReadWrite", "openid", "offline_access"}
            )
            self.assertNotIn(config.client_secret, location)
            subject, grant = connector_oauth.complete_oauth(config, pending, callback)
            client, updated_grant = calendar_client(config, grant)

        self.assertEqual(subject, "verified-work-user")
        self.assertFalse(json.loads(grant).get("IdToken"))
        self.assertEqual(client.calendar_id, "default")
        self.assertEqual(token_requests, ["authorization_code", "refresh_token"])
        self.assertIn("refreshed-refresh", updated_grant)

    def test_microsoft_rejects_wrong_tenant_audience_and_nonce_before_enrollment(self):
        config = connector_config("microsoft")
        pending = {"auth_uri": "https://login.microsoftonline.com/authorize?nonce=openid-nonce"}
        callback = {"state": "synthetic-state", "code": "synthetic-code"}
        cache = msal.SerializableTokenCache()
        cache.deserialize('{"RefreshToken":{"account":{"secret":"synthetic-refresh"}}}')
        app = Mock()
        app.get_accounts.return_value = [{"home_account_id": "synthetic-account"}]
        now = int(time.time())
        claims = {
            "iss": f"https://login.microsoftonline.com/{_TENANT}/v2.0",
            "tid": _TENANT,
            "aud": config.client_id,
            "nonce": "openid-nonce",
            "sub": "verified-work-user",
            "iat": now - 10,
            "exp": now + 3600,
        }
        with (
            patch.object(connector_oauth, "_microsoft_app", return_value=app),
            patch.object(connector_oauth.msal, "SerializableTokenCache", return_value=cache),
        ):
            app.acquire_token_by_auth_code_flow.return_value = {
                "access_token": "synthetic-access",
                "id_token": "synthetic-identity",
                "id_token_claims": claims,
                "scope": "Calendars.ReadWrite openid",
            }
            for name, bad_value in [
                ("tid", "wrong-tenant"),
                ("aud", "wrong-client"),
                ("nonce", "wrong-nonce"),
            ]:
                with self.subTest(claim=name):
                    app.acquire_token_by_auth_code_flow.return_value["id_token_claims"] = dict(
                        claims, **{name: bad_value}
                    )
                    with self.assertRaises(ValueError):
                        connector_oauth.complete_oauth(config, pending, callback)
            app.acquire_token_by_auth_code_flow.return_value["id_token_claims"] = claims
            subject, grant = connector_oauth.complete_oauth(config, pending, callback)
            app.acquire_token_silent.return_value = {"access_token": "synthetic-access"}
            client, saved = calendar_client(config, grant)

        self.assertEqual(subject, "verified-work-user")
        self.assertNotIn("synthetic-identity", saved)
        self.assertIsInstance(client, OutlookCalendarClient)
        self.assertEqual(client.calendar_id, "default")
        app.acquire_token_silent.assert_called_once_with(
            ["Calendars.ReadWrite"], account=app.get_accounts()[0]
        )

    def test_bad_google_grant_does_not_expose_credentials_to_the_worker_error(self):
        with self.assertRaisesRegex(SyncError, r"^Calendar authorization is unavailable\.") as failure:
            calendar_client(connector_config("google"), "not-json synthetic-secret")
        self.assertNotIn("synthetic-secret", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
