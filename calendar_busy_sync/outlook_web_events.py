"""Parse visible Outlook event labels and produce private one-event calendar files."""

import re
import uuid
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from .models import Event, SyncError

BUSY_TITLE = "Busy (from sync)"
IDENTITY_TITLE = "Calendar Busy Sync identity"
_NOTE_PREFIX = (
    "Managed by Calendar Busy Sync to mark this time unavailable. Edit the original appointment, not this copy. "
    "This copy may be replaced or removed by sync. Sync reference: "
)
_IDENTITY_PREFIX = "Calendar Busy Sync uses this free event to identify its mirror calendar. Reference: "
_TIMING_ERROR = "Outlook returned an unsupported or ambiguous event time; no partial range will be used."
_MARKER = re.compile(r"cbs[12]:([0-9a-f]{32}):(google|outlook):[0-9a-f]{64}(?::[0-9a-f]{32})?")
_TIME = r"[0-9]{1,2}:[0-9]{2}\s*[AP]M"
_DATE = r"[A-Za-z]+, [A-Za-z]+ [0-9]{1,2}, [0-9]{4}"
_TIMED = re.compile(rf", ({_TIME}) to ({_TIME}), ({_DATE})$")
_SPANNING = re.compile(rf", ({_TIME}), ({_DATE}) to ({_TIME}), ({_DATE})$")
_ALL_DAY = re.compile(rf", All day, ({_DATE})(?: to ({_DATE}))?$", re.IGNORECASE)
_STATE = re.compile(r", (Busy|Free|Tentative|Out of office|Working elsewhere)(?:, |$)", re.IGNORECASE)


def busy_note(marker: str, installation: str) -> str:
    match = _MARKER.fullmatch(marker)
    if not match or match.group(1) != installation:
        raise SyncError("Outlook web refused an invalid ownership marker.")
    return _NOTE_PREFIX + marker


def note_marker(title: str, note: str, installation: str) -> str | None:
    if title != BUSY_TITLE or not note.startswith(_NOTE_PREFIX):
        return None
    marker = note.removeprefix(_NOTE_PREFIX)
    match = _MARKER.fullmatch(marker)
    return marker if match and match.group(1) == installation else None


def identity_note(token: str) -> str:
    return _IDENTITY_PREFIX + token


def parse_card(item_id: str, label: str, zone) -> Event:
    for state in reversed(list(_STATE.finditer(label))):
        schedule = label[: state.start()]
        try:
            start, end = _card_interval(schedule, zone)
        except ValueError:
            raise SyncError(_TIMING_ERROR) from None
        if start is not None:
            busy = state.group(1).lower() not in ("free", "working elsewhere")
            return Event(item_id, start, end, busy)
    raise SyncError(_TIMING_ERROR)


def _card_interval(schedule, zone):
    match = _TIMED.search(schedule)
    if match:
        start_clock, end_clock, day = match.groups()
        first_day = _parse_display_date(day)
        start_time, end_time = _parse_clock(start_clock), _parse_clock(end_clock)
        last_day = first_day + timedelta(days=end_time < start_time)
        return _local_time(first_day, start_time, zone), _local_time(last_day, end_time, zone)
    match = _SPANNING.search(schedule)
    if match:
        start_clock, first_day, end_clock, last_day = match.groups()
        return (
            _local_time(_parse_display_date(first_day), _parse_clock(start_clock), zone),
            _local_time(_parse_display_date(last_day), _parse_clock(end_clock), zone),
        )
    match = _ALL_DAY.search(schedule)
    if match:
        first_day, last_day = match.groups()
        first = _parse_display_date(first_day)
        last = _parse_display_date(last_day) if last_day else first
        return _local_time(first, time.min, zone), _local_time(last + timedelta(days=1), time.min, zone)
    return None, None


def editor_interval(values, zone):
    start_day, start_clock, end_day, end_clock = values
    try:
        first_day = date.fromisoformat(start_day)
        last_day = date.fromisoformat(end_day) if end_day else first_day
        start = _local_time(first_day, _parse_clock(start_clock), zone)
        end = _local_time(last_day, _parse_clock(end_clock), zone)
    except (TypeError, ValueError):
        raise SyncError(_TIMING_ERROR) from None
    if end <= start:
        raise SyncError(_TIMING_ERROR)
    return start, end


def _parse_clock(value):
    return datetime.strptime(" ".join(value.split()), "%I:%M %p").time()


def _parse_display_date(value):
    return datetime.strptime(value.split(", ", 1)[1], "%B %d, %Y").date()


def _local_time(day, clock, zone):
    wall_time = datetime.combine(day, clock)
    first = wall_time.replace(tzinfo=zone, fold=0).astimezone(UTC)
    second = wall_time.replace(tzinfo=zone, fold=1).astimezone(UTC)
    if first != second or first.astimezone(zone).replace(tzinfo=None) != wall_time:
        raise SyncError(_TIMING_ERROR)
    return first


def write_calendar_file(profile: Path, event: Event, title: str, note: str):
    # Snap Firefox can only read an upload within an accessible directory. Keep the temporary file
    # inside the user-selected isolated profile and return ownership of its cleanup to the caller.
    try:
        temporary = TemporaryDirectory(prefix="calendar-upload-", dir=profile)
    except OSError:
        raise SyncError(
            "Could not stage an Outlook calendar upload in the isolated Firefox profile."
        ) from None
    file = Path(temporary.name) / f"calendar-busy-sync-{uuid.uuid4().hex}.ics"
    stamp = datetime.now(UTC)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Calendar Busy Sync//Outlook web//EN",
        "BEGIN:VEVENT",
        f"UID:{uuid.uuid4().hex}@calendar-busy-sync.invalid",
        f"DTSTAMP:{stamp:%Y%m%dT%H%M%SZ}",
        f"DTSTART:{event.start.astimezone(UTC):%Y%m%dT%H%M%SZ}",
        f"DTEND:{event.end.astimezone(UTC):%Y%m%dT%H%M%SZ}",
        f"SUMMARY:{_calendar_text(title)}",
        f"DESCRIPTION:{_calendar_text(note)}",
        "CLASS:PRIVATE",
        f"TRANSP:{'OPAQUE' if event.busy else 'TRANSPARENT'}",
        f"X-MICROSOFT-CDO-BUSYSTATUS:{'BUSY' if event.busy else 'FREE'}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    try:
        # The title/note are controlled ASCII. Fold long content lines per RFC 5545.
        file.write_bytes(("\r\n".join(_fold_line(line) for line in lines) + "\r\n").encode("ascii"))
    except OSError:
        temporary.cleanup()
        raise SyncError(
            "Could not stage an Outlook calendar upload in the isolated Firefox profile."
        ) from None
    return temporary, file


def _calendar_text(value):
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def _fold_line(line):
    return "\r\n ".join([line[:75], *(line[index : index + 74] for index in range(75, len(line), 74))])
