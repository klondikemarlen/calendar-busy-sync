from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, date, datetime, time
from typing import Any
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import requests

from .models import GRAPH_PROPERTY_ID, CopyIdUnavailable, Event, SyncError

_GOOGLE = "https://www.googleapis.com"
_GRAPH = "https://graph.microsoft.com"
_TIMEOUT = (5, 30)
_KEY = "calendar-busy-sync"


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise SyncError("Calendar returned an invalid time.")
    return dt.astimezone(UTC)


def _parse_google_time(raw: dict[str, Any], default_timezone: str | None = None) -> datetime:
    if "dateTime" in raw:
        value = datetime.fromisoformat(raw["dateTime"].replace("Z", "+00:00"))
        if value.tzinfo is None:
            zone = raw.get("timeZone") or default_timezone
            if not zone:
                raise SyncError("Calendar timezone is unavailable for an event.")
            value = value.replace(tzinfo=ZoneInfo(zone))
        return _utc(value)
    day = date.fromisoformat(raw["date"])
    zone = raw.get("timeZone") or default_timezone
    if not zone:
        raise SyncError("Calendar timezone is unavailable for an all-day event.")
    return _utc(datetime.combine(day, time.min, ZoneInfo(zone)))


def _event_marker_google(item: dict[str, Any]) -> str | None:
    props = item.get("extendedProperties", {}).get("private", {})
    marker = props.get(_KEY)
    return marker if isinstance(marker, str) and marker else None


def _google_event(item: dict[str, Any], default_timezone: str | None = None) -> Event | None:
    if item.get("status") == "cancelled":
        return None
    if not isinstance(item.get("id"), str) or not item["id"]:
        raise SyncError("Calendar returned an invalid event.")
    start = _parse_google_time(item["start"], default_timezone)
    end = _parse_google_time(item["end"], default_timezone)
    if end <= start:
        raise SyncError("Calendar returned an invalid event.")
    return Event(
        item["id"], start, end, item.get("transparency") != "transparent", _event_marker_google(item)
    )


def _marker_graph(item: dict[str, Any]) -> str | None:
    for prop in item.get("singleValueExtendedProperties", []):
        if prop.get("id") == GRAPH_PROPERTY_ID:
            value = prop.get("value")
            if isinstance(value, str) and value:
                return value
    return None


def _graph_time(raw: dict[str, Any]) -> datetime:
    value = datetime.fromisoformat(raw["dateTime"].replace("Z", "+00:00"))
    if value.tzinfo is None:
        # Graph with Prefer: outlook.timezone="UTC" returns UTC wall time.
        if raw.get("timeZone") != "UTC":
            raise SyncError("Calendar returned an invalid time.")
        value = value.replace(tzinfo=UTC)
    return _utc(value)


def _graph_event(item: dict[str, Any]) -> Event | None:
    if item.get("isCancelled"):
        return None
    if not isinstance(item.get("id"), str) or not item["id"]:
        raise SyncError("Calendar returned an invalid event.")
    start, end = _graph_time(item["start"]), _graph_time(item["end"])
    if end <= start:
        raise SyncError("Calendar returned an invalid event.")
    return Event(
        item["id"], start, end, item.get("showAs") not in ("free", "workingElsewhere"), _marker_graph(item)
    )


class _HttpClient:
    def __init__(self, access_token: str, session: Any = None) -> None:
        if not access_token:
            raise SyncError("Calendar authorization is missing; run auth.")
        self._token = access_token
        self._session = session or requests.Session()

    def _request(self, method: str, url: str, *, origin: str, params=None, body=None, extra_headers=None):
        parsed = urlparse(url)
        expected = urlparse(origin)
        if (
            parsed.scheme != "https"
            or parsed.hostname != expected.hostname
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
        ):
            raise SyncError("Calendar returned an unsafe continuation link.")
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        try:
            response = self._session.request(
                method,
                url,
                params=params,
                json=body,
                headers=headers,
                timeout=_TIMEOUT,
                allow_redirects=False,
            )
        except requests.RequestException:
            if method in ("POST", "PATCH", "DELETE"):
                raise SyncError(
                    "Calendar write outcome is uncertain; inspect the calendar before retrying."
                ) from None
            raise SyncError(
                "Calendar read failed; synchronization stopped. Earlier operations may have completed."
            ) from None
        if 300 <= response.status_code < 400:
            raise SyncError("Calendar service returned an unsafe redirect.")
        return response

    @staticmethod
    def _json(response):
        try:
            return response.json()
        except ValueError:
            raise SyncError("Calendar service returned an invalid response.") from None

    @staticmethod
    def _check(response):
        if not response.ok:
            raise SyncError("Calendar request failed; check authorization and calendar access.")


class GoogleCalendarClient(_HttpClient):
    def __init__(self, access_token: str, calendar_id: str = "primary", session=None):
        super().__init__(access_token, session)
        self.calendar_id = calendar_id
        self._base = f"{_GOOGLE}/calendar/v3/calendars/{quote(calendar_id, safe='')}/events"

    def list_events(self, start: datetime, end: datetime) -> list[Event]:
        start, end = _utc(start), _utc(end)
        if end <= start:
            raise SyncError("Invalid calendar range.")
        params = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "singleEvents": "true",
            "showDeleted": "true",
            "maxResults": 2500,
            "fields": "timeZone,nextPageToken,items(id,status,start,end,transparency,extendedProperties/private)",
        }
        url, out, seen_tokens = self._base, [], set()
        while url:
            response = self._request("GET", url, origin=_GOOGLE, params=params, body=None)
            self._check(response)
            data = self._json(response)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise SyncError("Calendar returned an incomplete response.")
            try:
                out.extend(
                    event
                    for item in data["items"]
                    if (event := _google_event(item, data.get("timeZone"))) is not None
                )
            except (KeyError, TypeError, ValueError, SyncError):
                raise SyncError("Calendar returned an invalid event.") from None
            token = data.get("nextPageToken")
            if token is not None and (not isinstance(token, str) or not token):
                raise SyncError("Calendar returned an invalid continuation token.")
            if token:
                if token in seen_tokens:
                    raise SyncError("Calendar returned a repeated continuation token.")
                seen_tokens.add(token)
                params["pageToken"] = token
            else:
                url = ""
        return out

    def get_event(self, event_id: str) -> Event | None:
        response = self._request("GET", f"{self._base}/{quote(event_id, safe='')}", origin=_GOOGLE)
        if response.status_code in (404, 410):
            return None
        self._check(response)
        item = self._json(response)
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise SyncError("Calendar returned an invalid event.")
        if "date" in item.get("start", {}) or "date" in item.get("end", {}):
            calendar_response = self._request(
                "GET",
                f"{_GOOGLE}/calendar/v3/calendars/{quote(self.calendar_id, safe='')}?fields=timeZone",
                origin=_GOOGLE,
            )
            self._check(calendar_response)
            calendar = self._json(calendar_response)
            zone = calendar.get("timeZone") if isinstance(calendar, dict) else None
        else:
            zone = None
        try:
            return _google_event(item, zone)
        except (KeyError, TypeError, ValueError, SyncError):
            raise SyncError("Calendar returned an invalid event.") from None

    @staticmethod
    def _body(event: Event, marker: str, event_id: str | None = None):
        body = {
            "summary": "Busy",
            "visibility": "private",
            "reminders": {"useDefault": False},
            "start": {"dateTime": _utc(event.start).isoformat()},
            "end": {"dateTime": _utc(event.end).isoformat()},
            "transparency": "opaque",
            "extendedProperties": {"private": {_KEY: marker}},
        }
        if event_id:
            body["id"] = event_id
        return body

    def create_busy(self, event: Event, marker: str) -> Event:
        event_id = (
            base64.b32hexencode(hashlib.sha256(marker.encode()).digest()).decode().lower().rstrip("=")[:40]
        )
        response = self._request("POST", self._base, origin=_GOOGLE, body=self._body(event, marker, event_id))
        if response.status_code == 409:
            existing = self.get_event(event_id)
            if existing is None:
                raise CopyIdUnavailable("The deleted copy's provider identifier is still reserved.")
            if existing.marker != marker:
                raise SyncError("The requested copy identifier belongs to an unrelated event.")
            return existing
        self._check(response)
        result = _google_event(self._json(response))
        if result is None or result.marker != marker:
            raise SyncError("Calendar did not confirm ownership of the created event.")
        return result

    def update_busy(self, event_id: str, event: Event, marker: str) -> Event:
        current = self.get_event(event_id)
        if current is None or current.marker != marker:
            raise SyncError("Refusing to update an event not owned by this tool.")
        response = self._request(
            "PATCH",
            f"{self._base}/{quote(event_id, safe='')}",
            origin=_GOOGLE,
            body=self._body(event, marker),
        )
        self._check(response)
        result = _google_event(self._json(response))
        if result is None or result.marker != marker:
            raise SyncError("Calendar did not confirm ownership of the updated event.")
        return result

    def delete_busy(self, event_id: str, marker: str) -> None:
        current = self.get_event(event_id)
        if current is None:
            return
        if current.marker != marker:
            raise SyncError("Refusing to delete an event not owned by this tool.")
        response = self._request("DELETE", f"{self._base}/{quote(event_id, safe='')}", origin=_GOOGLE)
        if response.status_code not in (404, 410):
            self._check(response)


class OutlookCalendarClient(_HttpClient):
    def __init__(self, access_token: str, calendar_id: str = "default", session=None):
        super().__init__(access_token, session)
        self.calendar_id = calendar_id
        self._base = (
            f"{_GRAPH}/v1.0/me/calendars/{quote(calendar_id, safe='')}/events"
            if calendar_id != "default"
            else f"{_GRAPH}/v1.0/me/events"
        )
        self._view = (
            f"{_GRAPH}/v1.0/me/calendars/{quote(calendar_id, safe='')}/calendarView"
            if calendar_id != "default"
            else f"{_GRAPH}/v1.0/me/calendarView"
        )

    def list_events(self, start: datetime, end: datetime) -> list[Event]:
        start, end = _utc(start), _utc(end)
        if end <= start:
            raise SyncError("Invalid calendar range.")
        params = {
            "startDateTime": start.isoformat(),
            "endDateTime": end.isoformat(),
            "$expand": f"singleValueExtendedProperties($filter=id eq '{GRAPH_PROPERTY_ID}')",
            "$select": "id,start,end,isCancelled,showAs,singleValueExtendedProperties",
        }
        url, out, seen_urls = self._view, [], set()
        while url:
            if url in seen_urls:
                raise SyncError("Calendar returned a repeated continuation link.")
            seen_urls.add(url)
            response = self._request(
                "GET",
                url,
                origin=_GRAPH,
                params=params,
                body=None,
                extra_headers={"Prefer": 'outlook.timezone="UTC"'},
            )
            self._check(response)
            data = self._json(response)
            if not isinstance(data, dict) or not isinstance(data.get("value"), list):
                raise SyncError("Calendar returned an incomplete response.")
            try:
                out.extend(event for item in data["value"] if (event := _graph_event(item)) is not None)
            except (KeyError, TypeError, ValueError, SyncError):
                raise SyncError("Calendar returned an invalid event.") from None
            next_url = data.get("@odata.nextLink")
            if next_url is not None and (not isinstance(next_url, str) or not next_url):
                raise SyncError("Calendar returned an invalid continuation link.")
            url = next_url or ""
            params = None
        return out

    def get_event(self, event_id: str) -> Event | None:
        url = f"{self._base}/{quote(event_id, safe='')}?$expand=singleValueExtendedProperties($filter=id eq '{GRAPH_PROPERTY_ID}')&$select=id,start,end,isCancelled,showAs,singleValueExtendedProperties"
        response = self._request(
            "GET", url, origin=_GRAPH, extra_headers={"Prefer": 'outlook.timezone="UTC"'}
        )
        if response.status_code in (404, 410):
            return None
        self._check(response)
        item = self._json(response)
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise SyncError("Calendar returned an invalid event.")
        try:
            return _graph_event(item)
        except (KeyError, TypeError, ValueError, SyncError):
            raise SyncError("Calendar returned an invalid event.") from None

    @staticmethod
    def _body(event: Event, marker: str, transaction_id: str | None = None):
        body = {
            "subject": "Busy",
            "sensitivity": "private",
            "isReminderOn": False,
            "start": {"dateTime": _utc(event.start).isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": _utc(event.end).isoformat(), "timeZone": "UTC"},
            "showAs": "busy",
            "singleValueExtendedProperties": [{"id": GRAPH_PROPERTY_ID, "value": marker}],
        }
        if transaction_id:
            body["transactionId"] = transaction_id
        return body

    def create_busy(self, event: Event, marker: str) -> Event:
        tx = str(uuid.uuid5(uuid.NAMESPACE_URL, marker))
        response = self._request(
            "POST",
            self._base,
            origin=_GRAPH,
            body=self._body(event, marker, tx),
            extra_headers={"Prefer": 'outlook.timezone="UTC"'},
        )
        self._check(response)
        returned = self._json(response)
        if not isinstance(returned, dict) or not isinstance(returned.get("id"), str):
            raise SyncError("Calendar did not return the created event identifier.")
        result = self.get_event(returned["id"])
        if result is None or result.marker != marker:
            raise SyncError("Calendar did not confirm ownership of the created event.")
        return result

    def update_busy(self, event_id: str, event: Event, marker: str) -> Event:
        current = self.get_event(event_id)
        if current is None or current.marker != marker:
            raise SyncError("Refusing to update an event not owned by this tool.")
        response = self._request(
            "PATCH",
            f"{self._base}/{quote(event_id, safe='')}",
            origin=_GRAPH,
            body=self._body(event, marker),
            extra_headers={"Prefer": 'outlook.timezone="UTC"'},
        )
        self._check(response)
        result = self.get_event(event_id)
        if result is None or result.marker != marker:
            raise SyncError("Calendar did not confirm ownership of the updated event.")
        return result

    def delete_busy(self, event_id: str, marker: str) -> None:
        current = self.get_event(event_id)
        if current is None:
            return
        if current.marker != marker:
            raise SyncError("Refusing to delete an event not owned by this tool.")
        response = self._request("DELETE", f"{self._base}/{quote(event_id, safe='')}", origin=_GRAPH)
        if response.status_code not in (404, 410):
            self._check(response)
