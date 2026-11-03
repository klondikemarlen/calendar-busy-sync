import copy
import json
import unittest
from datetime import UTC, datetime

import requests

from calendar_busy_sync.models import GRAPH_PROPERTY_ID, CopyIdUnavailable, Event, SyncError
from calendar_busy_sync.providers import GoogleCalendarClient, OutlookCalendarClient

START = datetime(2026, 1, 1, 10, tzinfo=UTC)
END = datetime(2026, 1, 1, 11, tzinfo=UTC)


def response(payload, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    return result


class ScriptedSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, copy.deepcopy(kwargs)))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


class ProviderTests(unittest.TestCase):
    def test_google_all_day_uses_calendar_timezone_across_dst(self):
        session = ScriptedSession(
            [
                response(
                    {
                        "timeZone": "America/New_York",
                        "items": [
                            {"id": "all-day", "start": {"date": "2024-03-10"}, "end": {"date": "2024-03-11"}}
                        ],
                    }
                )
            ]
        )
        events = GoogleCalendarClient("synthetic-token", session=session).list_events(
            datetime(2024, 3, 9, tzinfo=UTC), datetime(2024, 3, 12, tzinfo=UTC)
        )
        self.assertEqual(
            events,
            [Event("all-day", datetime(2024, 3, 10, 5, tzinfo=UTC), datetime(2024, 3, 11, 4, tzinfo=UTC))],
        )

    def test_google_pagination_failure_discards_partial_snapshot_and_preserves_query(self):
        session = ScriptedSession(
            [
                response({"items": [], "nextPageToken": "next", "timeZone": "UTC"}),
                response({}, 503),
            ]
        )
        client = GoogleCalendarClient("synthetic-token", session=session)
        with self.assertRaises(SyncError):
            client.list_events(START, END)
        params = session.calls[1][2]["params"]
        self.assertEqual(
            {key: params[key] for key in ("timeMin", "timeMax", "singleEvents", "showDeleted", "pageToken")},
            {
                "timeMin": START.isoformat(),
                "timeMax": END.isoformat(),
                "singleEvents": "true",
                "showDeleted": "true",
                "pageToken": "next",
            },
        )

    def test_outlook_continuation_cannot_send_bearer_to_another_origin(self):
        session = ScriptedSession(
            [response({"value": [], "@odata.nextLink": "https://example.invalid/steal"})]
        )
        with self.assertRaises(SyncError):
            OutlookCalendarClient("synthetic-token", session=session).list_events(START, END)
        self.assertEqual(len(session.calls), 1)

    def test_unowned_event_cannot_be_updated_or_deleted(self):
        existing = {
            "id": "unrelated",
            "start": {"dateTime": START.isoformat()},
            "end": {"dateTime": END.isoformat()},
        }
        session = ScriptedSession([response(existing), response(existing)])
        client = GoogleCalendarClient("synthetic-token", session=session)
        with self.assertRaises(SyncError):
            client.update_busy("unrelated", Event("source", START, END), "mine")
        with self.assertRaises(SyncError):
            client.delete_busy("unrelated", "mine")
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET"])

    def test_google_copy_posts_only_anonymous_private_busy_content(self):
        returned = {
            "id": "copy",
            "start": {"dateTime": START.isoformat()},
            "end": {"dateTime": END.isoformat()},
            "extendedProperties": {"private": {"calendar-busy-sync": "owner"}},
        }
        session = ScriptedSession([response(returned, 201)])
        GoogleCalendarClient("synthetic-token", session=session).create_busy(
            Event("source", START, END), "owner"
        )
        payload = session.calls[0][2]["json"]
        identifier = payload.pop("id")
        self.assertRegex(identifier, "^[0-9a-v]{40}$")
        self.assertEqual(
            payload,
            {
                "summary": "Busy",
                "visibility": "private",
                "reminders": {"useDefault": False},
                "start": {"dateTime": START.isoformat()},
                "end": {"dateTime": END.isoformat()},
                "transparency": "opaque",
                "extendedProperties": {"private": {"calendar-busy-sync": "owner"}},
            },
        )

    def test_outlook_copy_posts_only_anonymous_private_busy_content(self):
        returned = {
            "id": "copy",
            "start": {"dateTime": START.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": END.isoformat(), "timeZone": "UTC"},
            "singleValueExtendedProperties": [{"id": GRAPH_PROPERTY_ID, "value": "owner"}],
        }
        write_response = {
            key: value for key, value in returned.items() if key != "singleValueExtendedProperties"
        }
        session = ScriptedSession([response(write_response, 201), response(returned)])
        OutlookCalendarClient("synthetic-token", session=session).create_busy(
            Event("source", START, END), "owner"
        )
        payload = session.calls[0][2]["json"]
        transaction = payload.pop("transactionId")
        self.assertRegex(transaction, "^[0-9a-f-]{36}$")
        self.assertEqual(
            payload,
            {
                "subject": "Busy",
                "sensitivity": "private",
                "isReminderOn": False,
                "showAs": "busy",
                "start": {"dateTime": START.isoformat(), "timeZone": "UTC"},
                "end": {"dateTime": END.isoformat(), "timeZone": "UTC"},
                "singleValueExtendedProperties": [{"id": GRAPH_PROPERTY_ID, "value": "owner"}],
            },
        )

    def test_cancelled_google_identifier_requires_a_new_generation_without_restoring_it(self):
        session = ScriptedSession(
            [response({}, 409), response({"id": "cancelled-copy", "status": "cancelled"})]
        )
        client = GoogleCalendarClient("synthetic-token", session=session)
        with self.assertRaises(CopyIdUnavailable):
            client.create_busy(Event("source", START, END), "owner")
        self.assertEqual([call[0] for call in session.calls], ["POST", "GET"])

    def test_uncertain_google_insert_recovers_existing_owned_copy(self):
        existing = {
            "id": "copy",
            "start": {"dateTime": START.isoformat()},
            "end": {"dateTime": END.isoformat()},
            "extendedProperties": {"private": {"calendar-busy-sync": "owner"}},
        }
        session = ScriptedSession([response({}, 409), response(existing)])
        result = GoogleCalendarClient("synthetic-token", session=session).create_busy(
            Event("source", START, END), "owner"
        )
        self.assertEqual(result, Event("copy", START, END, marker="owner"))
        self.assertEqual([call[0] for call in session.calls], ["POST", "GET"])

    def test_uncertain_network_write_does_not_claim_no_changes(self):
        session = ScriptedSession([requests.Timeout("private provider information")])
        with self.assertRaisesRegex(SyncError, "write outcome is uncertain"):
            OutlookCalendarClient("synthetic-token", session=session).create_busy(
                Event("source", START, END), "owner"
            )


if __name__ == "__main__":
    unittest.main()
