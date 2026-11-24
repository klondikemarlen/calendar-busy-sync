"""Firefox-backed Outlook source and dedicated mirror calendars."""

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import CalendarClient, Event, EventLookupUnknown, SyncError
from .outlook_web_driver import open_firefox
from .outlook_web_events import (
    BUSY_TITLE,
    IDENTITY_TITLE,
    busy_note,
    editor_interval,
    identity_note,
    note_marker,
    parse_card,
    write_calendar_file,
)
from .outlook_web_ui import OutlookUI, item_id_from_url, valid_editor_url
from .settings import Paths, Settings, private_write

_LOOKUP_UNKNOWN = "Outlook could not verify this exact calendar item; its Busy copy was preserved."
_BINDING_ERROR = (
    "Outlook could not verify the original dedicated mirror. Restore its identity event and local binding."
)
_WRITE_ERROR = "Outlook did not confirm the exact managed event; no uncertain write will be retried."


@dataclass(frozen=True)
class _Identity:
    configuration: str
    token: str
    start_utc: str
    item_id: str = ""
    editor_url: str = ""
    mailbox: str = ""

    def appointment(self):
        start = datetime.fromisoformat(self.start_utc)
        return Event(self.token, start, start + timedelta(minutes=5), busy=False)

    @classmethod
    def load(cls, path, configuration):
        try:
            record = cls(**json.loads(path.read_text(encoding="utf-8")))
            start = record.appointment().start
            if (
                start.utcoffset() != timedelta(0)
                or record.configuration != configuration
                or not re.fullmatch(r"[0-9a-f]{32}", record.token)
            ):
                raise ValueError
            if (record.item_id, record.editor_url, record.mailbox) != ("", "", "") and (
                not isinstance(record.item_id, str)
                or not record.item_id
                or not valid_editor_url(record.editor_url)
                or item_id_from_url(record.editor_url) != record.item_id
                or not re.fullmatch(r"[0-9a-f]{64}", record.mailbox)
            ):
                raise ValueError
            return record
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, AttributeError, SyncError):
            raise SyncError(_BINDING_ERROR) from None

    def save(self, path):
        try:
            private_write(path, json.dumps(asdict(self)) + "\n")
        except OSError:
            raise SyncError("Could not save the private Outlook mirror binding locally.") from None


class _OutlookCalendar:
    idempotent_create = False
    best_effort_discovery = True

    def __init__(self, ui, name, settings, identity):
        self.ui = ui
        self.name = name
        self.zone = ZoneInfo(settings.outlook_web_timezone)
        self.mirror_name = settings.outlook_web_mirror_calendar
        self.identity = identity
        self.item_weeks: dict[str, date] = {}

    def list_events(self, start: datetime, end: datetime) -> list[Event]:
        self.ui.open_calendar()
        self.ui.select_calendar(self.name)
        first_day = start.astimezone(self.zone).date()
        last_day = end.astimezone(self.zone).date()
        week = self.ui.go_to_week(first_day)
        events = {}
        while week <= last_day:
            week_end = week + timedelta(days=7)
            for item_id, label in self.ui.sample_week(self.name):
                self.item_weeks[item_id] = week
                event = self._read_card(item_id, label)
                if event is None:
                    continue
                if not (
                    event.start.astimezone(self.zone).date() < week_end
                    and event.end.astimezone(self.zone).date() >= week
                ):
                    raise SyncError(
                        "Outlook displayed items outside the requested week; no partial read will be used."
                    )
                if event.start < end and event.end > start:
                    if item_id in events and events[item_id] != event:
                        raise SyncError("Outlook displayed conflicting details for the same calendar item.")
                    events[item_id] = event
            if week_end > last_day:
                break
            week = self.ui.go_to_week(week_end)
        return list(events.values())

    def _read_card(self, item_id: str, label: str) -> Event | None:
        return parse_card(item_id, label, self.zone)

    def _locate(self, item_id: str) -> str:
        if not self.ui.on_outlook():
            raise SyncError("Outlook web signed out during sync. Run Outlook web sign-in again.")
        week = self.item_weeks.get(item_id)
        if week is None:
            raise EventLookupUnknown(_LOOKUP_UNKNOWN)
        self.ui.open_calendar()
        self.ui.select_calendar(self.name)
        self.ui.go_to_week(week)
        labels = [label for current_id, label in self.ui.sample_week(self.name) if current_id == item_id]
        if len(set(labels)) != 1:
            raise EventLookupUnknown(_LOOKUP_UNKNOWN)
        return labels[0]

    def get_event(self, event_id: str) -> Event | None:
        if not self.ui.open_item(event_id):
            raise EventLookupUnknown(_LOOKUP_UNKNOWN)
        binding = self.ui.calendar_binding(self.name)
        if not binding or _digest(self.mirror_name + binding[len(self.name) :]) != self.identity.mailbox:
            raise EventLookupUnknown(_LOOKUP_UNKNOWN)
        if not self.ui.show_editor_timing(message=None):
            raise EventLookupUnknown(_LOOKUP_UNKNOWN)
        fields = self.ui.editor_times(message=None)
        availability = self.ui.editor_availability()
        if not fields or availability is None:
            raise EventLookupUnknown(_LOOKUP_UNKNOWN)
        try:
            start, end = editor_interval(fields, self.zone)
        except SyncError:
            raise EventLookupUnknown(_LOOKUP_UNKNOWN) from None
        return Event(event_id, start, end, availability not in ("Free", "Working elsewhere"))

    def create_busy(self, event: Event, marker: str) -> Event:
        raise SyncError("The Outlook source calendar is read-only.")

    def update_busy(self, event_id: str, event: Event, marker: str) -> Event:
        raise SyncError("The Outlook source calendar is read-only.")

    def delete_busy(self, event_id: str, marker: str) -> None:
        raise SyncError("The Outlook source calendar is read-only.")


class _OutlookMirror(_OutlookCalendar):
    replace_for_reschedule = True

    def __init__(self, ui, settings, paths, identity):
        super().__init__(ui, settings.outlook_web_mirror_calendar, settings, identity)
        self.installation = settings.installation_id
        self.profile = paths.outlook_web_profile_dir

    def _read_card(self, item_id, label):
        if not label.startswith(BUSY_TITLE + ", "):
            return None
        event, _ = self._inspect(item_id, label)
        self.ui.close_editor()
        return event if event.marker else None

    def _locate(self, item_id):
        try:
            return super()._locate(item_id)
        except EventLookupUnknown:
            # A direct editor can locate a moved item, but cannot prove it is still in the scoped grid.
            exact = super().get_event(item_id)
            self.item_weeks[item_id] = exact.start.astimezone(self.zone).date()
            return super()._locate(item_id)

    def get_event(self, event_id):
        label = self._locate(event_id)
        event, _ = self._inspect(event_id, label)
        self.ui.close_editor()
        return event

    def create_busy(self, event, marker):
        _require_minute_precision(event)
        note = busy_note(marker, self.installation)
        self._assert_identity()
        self._import(event, BUSY_TITLE, note)
        created = self._find_import(event.start.astimezone(self.zone).date(), BUSY_TITLE, note)
        if len(created) != 1:
            raise SyncError(_WRITE_ERROR)
        item_id, label, _, mailbox = created[0]
        self._check_mailbox(mailbox)
        result = parse_card(item_id, label, self.zone)
        self._require_interval(result, event)
        self.item_weeks[item_id] = self.ui.week_start()
        return replace(result, marker=marker)

    def update_busy(self, event_id, event, marker):
        # A recovered save with a lost checkpoint needs verification, not a second write.
        current, _ = self._open_owned(event_id, marker)
        self._require_interval(current, event)
        self.ui.close_editor()
        return current

    def delete_busy(self, event_id, marker):
        note = busy_note(marker, self.installation)
        current, editor_url = self._open_owned(event_id, marker)
        mailbox = self.ui.calendar_binding(self.name)
        self._check_mailbox(mailbox)
        self.ui.delete_editor_event(editor_url, mailbox, BUSY_TITLE, note)
        self.ui.open_calendar()
        self.ui.select_calendar(self.name)
        self.ui.go_to_week(current.start.astimezone(self.zone).date())
        if any(item_id == event_id for item_id, _ in self.ui.sample_week(self.name)):
            raise SyncError(
                "Outlook still displays the deleted item after reload; cleanup was not confirmed."
            )
        self.item_weeks.pop(event_id, None)

    def _open_owned(self, event_id, marker):
        self._assert_identity()
        current, url = self._inspect(event_id, self._locate(event_id))
        if current.marker != marker:
            raise SyncError(
                "The Outlook mirror event does not have the exact managed title and note; unchanged."
            )
        return current, url

    def _inspect(self, item_id, label):
        url = self.ui.open_grid_editor(self.name, item_id)
        if url is None or item_id_from_url(url) != item_id:
            raise EventLookupUnknown(_LOOKUP_UNKNOWN)
        self._check_mailbox(self.ui.calendar_binding(self.name))
        content = self.ui.editor_content()
        if not content:
            raise EventLookupUnknown(_LOOKUP_UNKNOWN)
        marker = note_marker(*content, self.installation)
        event = parse_card(item_id, label, self.zone)
        return replace(event, marker=marker), url

    def _check_mailbox(self, label):
        if not label or _digest(label) != self.identity.mailbox:
            raise SyncError(_BINDING_ERROR)

    def _assert_identity(self):
        if not self.identity.item_id:
            raise SyncError(_BINDING_ERROR)
        self.ui.open_calendar()
        self.ui.assert_owned_calendar(self.name)
        self.ui.select_calendar(self.name)
        anchor = self.identity.appointment()
        self.ui.go_to_week(anchor.start.astimezone(self.zone).date())
        matches = [
            label for item_id, label in self.ui.sample_week(self.name) if item_id == self.identity.item_id
        ]
        if len(set(matches)) != 1 or not matches[0].startswith(IDENTITY_TITLE + ", "):
            raise SyncError(_BINDING_ERROR)
        self._assert_anchor_calendar_time(self.identity.item_id, matches[0])
        if self.ui.open_grid_editor(self.name, self.identity.item_id) != self.identity.editor_url:
            raise SyncError(_BINDING_ERROR)
        if not self.ui.reopen_editor(self.identity.editor_url):
            raise SyncError(_BINDING_ERROR)
        self._check_mailbox(self.ui.calendar_binding(self.name))
        if self.ui.editor_content() != [IDENTITY_TITLE, identity_note(self.identity.token)]:
            raise SyncError(_BINDING_ERROR)
        self.ui.close_editor()
        self.ui.open_calendar()
        self.ui.select_calendar(self.name)

    def _assert_anchor_calendar_time(self, item_id, label):
        observed = parse_card(item_id, label, self.zone)
        if observed.busy or not label.endswith(", Private"):
            raise SyncError(
                "The Outlook mirror identity event must remain Free and Private; no sync was run."
            )
        expected = self.identity.appointment()
        if (observed.start, observed.end) != (expected.start, expected.end):
            raise SyncError(
                "The Outlook mirror identity time does not match. Check the configured Outlook display time zone "
                "and restore the identity event's original time before retrying."
            )

    def _import(self, event: Event, title: str, note: str, *, before_submit=None):
        temporary, file = write_calendar_file(self.profile, event, title, note)
        try:
            self.ui.import_calendar_file(file, self.name, before_submit=before_submit)
        finally:
            temporary.cleanup()

    def _find_import(self, day, title, note):
        self.ui.open_calendar()
        self.ui.select_calendar(self.name)
        self.ui.go_to_week(day)
        candidates = {}
        for item_id, label in self.ui.sample_week(self.name):
            if not label.startswith(title + ", ") or item_id in candidates:
                continue
            editor_url = self.ui.open_grid_editor(self.name, item_id)
            if editor_url is None or item_id_from_url(editor_url) != item_id:
                raise SyncError(_WRITE_ERROR)
            if self.ui.editor_content() == [title, note]:
                candidates[item_id] = (item_id, label, editor_url, self.ui.calendar_binding(self.name))
            self.ui.close_editor()
        return list(candidates.values())

    @staticmethod
    def _require_interval(observed, requested):
        if (observed.start, observed.end, observed.busy) != (requested.start, requested.end, requested.busy):
            raise SyncError(_WRITE_ERROR)

    def bind(self, new_identity, path):
        anchor = self.identity.appointment()
        day = anchor.start.astimezone(self.zone).date()
        note = identity_note(self.identity.token)
        self.ui.open_calendar()
        self.ui.assert_owned_calendar(self.name)
        self.ui.select_calendar(self.name)
        if new_identity:
            self._import(anchor, IDENTITY_TITLE, note, before_submit=lambda: self.identity.save(path))
        candidates = self._find_import(day, IDENTITY_TITLE, note)
        if len(candidates) != 1:
            raise SyncError(_BINDING_ERROR)
        item_id, label, editor_url, mailbox = candidates[0]
        if not mailbox:
            raise SyncError(_BINDING_ERROR)
        self._assert_anchor_calendar_time(item_id, label)
        self.identity = replace(
            self.identity, item_id=item_id, editor_url=editor_url, mailbox=_digest(mailbox)
        )
        self.identity.save(path)
        self._assert_identity()


def _require_minute_precision(event):
    if any(value.second or value.microsecond for value in (event.start, event.end)):
        raise SyncError(
            "Outlook web cannot verify calendar changes more precise than a minute; no change was made."
        )


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _configuration(settings):
    return _digest(
        "\0".join(
            (
                settings.installation_id,
                settings.outlook_web_source_calendar,
                settings.outlook_web_mirror_calendar,
            )
        )
    )


def _validate_pair(settings):
    if (
        not settings.outlook_web_mirror_calendar.strip()
        or not settings.outlook_web_source_calendar.strip()
        or settings.outlook_web_source_calendar == settings.outlook_web_mirror_calendar
    ):
        raise SyncError("Choose a dedicated Outlook mirror distinct from the Outlook source calendar.")


@contextmanager
def open_web_calendars(settings: Settings, paths: Paths) -> Iterator[tuple[CalendarClient, CalendarClient]]:
    """Open separately scoped source and mirror roles using the prepared Firefox profile."""
    _validate_pair(settings)
    identity = _Identity.load(paths.outlook_web_identity_file, _configuration(settings))
    if not identity or not identity.item_id:
        raise SyncError(
            "Outlook web does not have a confirmed mirror binding. Run Outlook web sign-in first."
        )
    with open_firefox(paths.outlook_web_profile_dir, headless=True) as browser:
        ui = OutlookUI(browser)
        source = _OutlookCalendar(ui, settings.outlook_web_source_calendar, settings, identity)
        mirror = _OutlookMirror(ui, settings, paths, identity)
        mirror._assert_identity()
        yield source, mirror


@contextmanager
def open_web_login(settings: Settings, paths: Paths) -> Iterator[Callable[[], None]]:
    """Open headed isolated Firefox for manual sign-in and explicit mirror binding."""
    _validate_pair(settings)
    path: Path = paths.outlook_web_identity_file
    configuration = _configuration(settings)
    identity = _Identity.load(path, configuration)
    with open_firefox(paths.outlook_web_profile_dir, headless=False) as browser:
        ui = OutlookUI(browser)
        browser.navigate("https://outlook.cloud.microsoft/calendar/")

        def confirm_sign_in():
            ui.await_sign_in()
            zone = ZoneInfo(settings.outlook_web_timezone)
            yesterday = datetime.now(zone).date() - timedelta(days=1)
            start = datetime.combine(yesterday, time(12), zone).astimezone(UTC)
            record = identity or _Identity(configuration, uuid.uuid4().hex, start.isoformat())
            mirror = _OutlookMirror(ui, settings, paths, record)
            ui.show_calendars()
            ui.select_calendar(settings.outlook_web_source_calendar)
            if record.item_id:
                mirror._assert_identity()
            else:
                mirror.bind(identity is None, path)

        yield confirm_sign_in
