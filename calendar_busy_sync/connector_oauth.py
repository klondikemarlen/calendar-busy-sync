"""Confidential web OAuth and primary-calendar grants for each connector role."""

from __future__ import annotations

import json
import logging
import secrets
import time
from functools import partial
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import msal
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from .models import CalendarClient, SyncError
from .providers import GoogleCalendarClient, OutlookCalendarClient

GOOGLE_ORIGIN = "https://accounts.google.com"
MICROSOFT_ORIGIN = "https://login.microsoftonline.com"
_GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_GOOGLE_SCOPES = ("openid", "https://www.googleapis.com/auth/calendar.events.owned")
_MICROSOFT_SCOPES = ("Calendars.ReadWrite",)
_PROVIDER_TIMEOUT = 20


def start_oauth(config, state):
    """Return the provider URL and pending transaction to keep server-side."""
    _silence_oauth_logs()
    if config.role == "google":
        flow = _google_flow(config, state)
        nonce = secrets.token_urlsafe(32)
        location, _ = flow.authorization_url(
            state=state, nonce=nonce, access_type="offline", prompt="consent select_account"
        )
        return location, {"verifier": flow.code_verifier, "nonce": nonce}

    app = _microsoft_app(config, msal.SerializableTokenCache())
    flow = app.initiate_auth_code_flow(
        list(_MICROSOFT_SCOPES),
        redirect_uri=config.origin + "/callback",
        state=state,
        prompt="select_account",
        response_mode="query",
    )
    return flow["auth_uri"], flow


def complete_oauth(config, pending, callback):
    """Redeem the already state-checked code; return a verified subject and scheduling grant."""
    _silence_oauth_logs()
    if config.role == "google":
        flow = _google_flow(config, callback["state"], verifier=pending["verifier"])
        token = flow.fetch_token(code=callback["code"], timeout=_PROVIDER_TIMEOUT)
        claims = id_token.verify_oauth2_token(
            token["id_token"], partial(Request(), timeout=_PROVIDER_TIMEOUT), audience=config.client_id
        )
        if (
            not _nonce_matches(claims, pending["nonce"])
            or claims.get("azp", config.client_id) != config.client_id
        ):
            raise ValueError
        _check_google_scopes(token.get("scope", _GOOGLE_SCOPES))
        return _subject(claims), _google_grant(flow.credentials)

    cache = msal.SerializableTokenCache()
    app = _microsoft_app(config, cache)
    result = app.acquire_token_by_auth_code_flow(pending, callback)
    claims = result["id_token_claims"]
    nonce = parse_qs(urlsplit(pending["auth_uri"]).query)["nonce"][0]
    # OIDC permits the direct TLS token exchange (with the pinned tenant) to authenticate the issuer.
    # MSAL versions differ in claim validation, so enforce the identity checks explicitly here.
    tenant = str(UUID(config.tenant))
    now = int(time.time())
    if (
        not result.get("access_token")
        or not result.get("id_token")
        or claims.get("iss") != f"{MICROSOFT_ORIGIN}/{tenant}/v2.0"
        or claims.get("tid") != tenant
        or claims.get("aud") != config.client_id.lower()
        or not _nonce_matches(claims, nonce)
        or claims["exp"] <= now - 120
        or claims.get("nbf", now) > now + 120
        or claims["iat"] > now + 120
    ):
        raise ValueError
    _check_microsoft_scopes(result["scope"])
    if len(app.get_accounts()) != 1 or not cache.find(cache.CredentialType.REFRESH_TOKEN):
        raise ValueError
    return _subject(claims), _microsoft_grant(cache)


def calendar_client(config, grant_text: str) -> tuple[CalendarClient, str]:
    """Open a primary-calendar adapter; return any refreshed provider grant for the same account."""
    _silence_oauth_logs()
    try:
        if config.role == "google":
            grant = json.loads(grant_text)
            credentials = Credentials.from_authorized_user_info(
                dict(grant, client_id=config.client_id, client_secret=config.client_secret),
                scopes=_GOOGLE_SCOPES,
            )
            if not credentials.valid:
                credentials.refresh(partial(Request(), timeout=_PROVIDER_TIMEOUT))
                if credentials.granted_scopes is not None:
                    _check_google_scopes(credentials.granted_scopes)
                grant_text = _google_grant(credentials)
            return GoogleCalendarClient(credentials.token), grant_text

        cache = msal.SerializableTokenCache()
        cache.deserialize(grant_text)
        app = _microsoft_app(config, cache)
        accounts = app.get_accounts()
        if len(accounts) != 1:
            raise ValueError
        result = app.acquire_token_silent(list(_MICROSOFT_SCOPES), account=accounts[0])
        if not result or not result.get("access_token"):
            raise ValueError
        if "scope" in result:
            _check_microsoft_scopes(result["scope"])
        if cache.has_state_changed:
            grant_text = _microsoft_grant(cache)
        return OutlookCalendarClient(result["access_token"]), grant_text
    except Exception:
        raise SyncError("Calendar authorization is unavailable. Reauthorize in the connector.") from None


def _microsoft_app(config, cache):
    return msal.ConfidentialClientApplication(
        config.client_id.lower(),
        client_credential=config.client_secret,
        authority=f"{MICROSOFT_ORIGIN}/{UUID(config.tenant)}",
        instance_discovery=False,
        exclude_scopes=["profile"],
        token_cache=cache,
        timeout=_PROVIDER_TIMEOUT,
    )


def _google_flow(config, state, *, verifier=None):
    return Flow.from_client_config(
        {
            "web": {
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "auth_uri": GOOGLE_ORIGIN + "/o/oauth2/v2/auth",
                "token_uri": _GOOGLE_TOKEN_ENDPOINT,
            }
        },
        list(_GOOGLE_SCOPES),
        redirect_uri=config.origin + "/callback",
        state=state,
        code_verifier=verifier,
        autogenerate_code_verifier=verifier is None,
    )


def _google_grant(credentials):
    if not credentials.token or not credentials.refresh_token or not credentials.expiry:
        raise ValueError
    return json.dumps(
        {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "expiry": credentials.expiry.isoformat(),
        }
    )


def _microsoft_grant(cache):
    # The calendar worker needs the account and OAuth grants, not the original identity token.
    # find() materializes a list and works with MSAL 1.24, which predates search().
    for entry in cache.find(cache.CredentialType.ID_TOKEN):
        cache.remove_idt(entry)
    return cache.serialize()


def _check_google_scopes(scopes):
    if set(scopes.split() if isinstance(scopes, str) else scopes) != set(_GOOGLE_SCOPES):
        raise ValueError


def _check_microsoft_scopes(scopes):
    granted = {scope.lower().removeprefix("https://graph.microsoft.com/") for scope in scopes.split()}
    if granted - {"openid", "profile", "email", "offline_access"} != {"calendars.readwrite"}:
        raise ValueError


def _subject(claims):
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise ValueError
    return subject


def _nonce_matches(claims, expected):
    nonce = claims.get("nonce")
    return isinstance(nonce, str) and secrets.compare_digest(nonce.encode(), expected.encode())


def _silence_oauth_logs():
    # These SDKs can put response bodies, authorization codes, and tokens in diagnostics.
    for namespace in ("oauthlib", "requests_oauthlib", "msal", "google.auth", "google.oauth2", "urllib3"):
        logging.getLogger(namespace).setLevel(logging.CRITICAL + 1)
