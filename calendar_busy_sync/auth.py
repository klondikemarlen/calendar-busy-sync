from __future__ import annotations

import json

from .models import SyncError
from .providers import GoogleCalendarClient, OutlookCalendarClient
from .settings import private_write


def _google_scopes(settings):
    if settings.direction == "both":
        return [
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.events",
        ]
    return ["https://www.googleapis.com/auth/calendar.readonly"]


def _save_google_grant(paths, credentials, expected):
    granted = credentials.granted_scopes
    if granted is None:
        granted = credentials.scopes
    granted = set(granted or [])
    if granted != set(expected):
        raise SyncError(
            "Google granted a different permission set; revoke existing consent and run auth google again."
        )
    token_data = json.loads(credentials.to_json())
    token_data["scopes"] = sorted(granted)
    private_write(paths.google_token_file, json.dumps(token_data))


def authorize_google(settings, paths) -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow

        if not paths.google_client_file.is_file():
            raise SyncError(
                "Google desktop OAuth credentials are missing; copy the downloaded desktop client JSON into the configured credentials file."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(paths.google_client_file), _google_scopes(settings), autogenerate_code_verifier=True
        )
        creds = flow.run_local_server(
            host="127.0.0.1",
            bind_addr="127.0.0.1",
            port=0,
            open_browser=True,
            access_type="offline",
            prompt="consent",
            timeout_seconds=300,
        )
        _save_google_grant(paths, creds, _google_scopes(settings))
    except SyncError:
        raise
    except Exception:
        raise SyncError(
            "Google authorization failed; verify the desktop OAuth client and try auth google again."
        ) from None


def authorize_outlook(settings, paths) -> None:
    try:
        import msal

        cache = msal.SerializableTokenCache()
        app = msal.PublicClientApplication(
            settings.microsoft_client_id,
            authority=f"https://login.microsoftonline.com/{settings.microsoft_tenant}",
            token_cache=cache,
            timeout=30,
        )
        flow = app.initiate_device_flow(scopes=["Calendars.ReadWrite"])
        if "user_code" not in flow:
            raise SyncError(
                "Microsoft authorization could not start; verify the public-client application and tenant settings."
            )
        print(flow.get("message", "Complete Microsoft sign-in in your browser."))
        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise SyncError(
                "Microsoft authorization failed; ask your Microsoft 365 administrator to allow public-client device sign-in and Calendars.ReadWrite consent."
            )
        private_write(paths.microsoft_token_file, cache.serialize())
    except SyncError:
        raise
    except Exception:
        raise SyncError(
            "Microsoft authorization failed; verify the public-client ID, tenant, and administrator consent."
        ) from None


def _google_client(settings, paths):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        raw = json.loads(paths.google_token_file.read_text(encoding="utf-8"))
        expected = set(_google_scopes(settings))
        granted = set(raw.get("scopes", []))
        if granted != expected:
            raise SyncError(
                "Google authorization permissions do not match the current sync mode; revoke existing consent and run auth google again."
            )
        creds = Credentials.from_authorized_user_info(raw, scopes=list(expected))
        if not creds.valid:
            if not creds.refresh_token:
                raise SyncError("Google authorization has expired; run auth google again.")
            # google-auth's Request has a finite default timeout on each refresh call.
            creds.refresh(Request())
            _save_google_grant(paths, creds, expected)
        return GoogleCalendarClient(creds.token, settings.google_calendar)
    except SyncError:
        raise
    except Exception:
        raise SyncError("Google authorization is unavailable; run auth google again.") from None


def _outlook_client(settings, paths):
    try:
        import msal

        cache = msal.SerializableTokenCache()
        cache.deserialize(paths.microsoft_token_file.read_text(encoding="utf-8"))
        app = msal.PublicClientApplication(
            settings.microsoft_client_id,
            authority=f"https://login.microsoftonline.com/{settings.microsoft_tenant}",
            token_cache=cache,
            timeout=30,
        )
        accounts = app.get_accounts()
        if len(accounts) != 1:
            raise SyncError(
                "Microsoft authorization does not identify exactly one account; run auth outlook again."
            )
        result = app.acquire_token_silent(["Calendars.ReadWrite"], account=accounts[0])
        if not result or "access_token" not in result:
            raise SyncError(
                "Microsoft authorization is unavailable or needs interaction; run auth outlook again."
            )
        if cache.has_state_changed:
            private_write(paths.microsoft_token_file, cache.serialize())
        return OutlookCalendarClient(result["access_token"], settings.outlook_calendar)
    except SyncError:
        raise
    except Exception:
        raise SyncError("Microsoft authorization is unavailable; run auth outlook again.") from None


def build_clients(settings, paths) -> dict:
    return {"google": _google_client(settings, paths), "outlook": _outlook_client(settings, paths)}
