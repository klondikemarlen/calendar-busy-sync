"""Project outbound Busy time onto the connector's sharing window."""

import re
from bisect import bisect_right
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Event, SyncError

MAX_PADDING_MINUTES = 1440

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
_DAY = timedelta(days=1)
_SECOND = timedelta(seconds=1)
_Interval = tuple[datetime, datetime]


@dataclass(frozen=True)
class SharingHours:
    lead: int = 0
    trail: int = 0
    enabled: bool = False
    zone: str = "UTC"
    days: str = "mon tue wed thu fri"
    start: str = "09:00"
    end: str = "17:00"

    def __post_init__(self):
        if any(
            type(minutes) is not int or not 0 <= minutes <= MAX_PADDING_MINUTES
            for minutes in (self.lead, self.trail)
        ):
            raise SyncError(
                f"Sharing padding must be a whole number from 0 to {MAX_PADDING_MINUTES} minutes."
            )
        if type(self.enabled) is not bool:
            raise SyncError("Choose whether to limit sharing to specific hours.")
        try:
            ZoneInfo(self.zone)
        except (ZoneInfoNotFoundError, OSError, ValueError, TypeError):
            raise SyncError("Use an IANA sharing timezone such as UTC or America/Vancouver.") from None
        if type(self.days) is not str:
            raise SyncError("Choose one or more unique sharing days from mon through sun.")
        days = self.days.split()
        if not days or any(day not in _DAYS for day in days) or len(set(days)) != len(days):
            raise SyncError("Choose one or more unique sharing days from mon through sun.")
        if any(type(value) is not str or not _CLOCK.fullmatch(value) for value in (self.start, self.end)):
            raise SyncError("Sharing start and end must use 24-hour HH:MM.")
        if self.start >= self.end:
            raise SyncError("Sharing end must be later than its start on the same day.")

    def query_window(self, start: datetime, end: datetime) -> _Interval:
        return start - timedelta(minutes=self.trail), end + timedelta(minutes=self.lead)

    def project(self, originals: Iterable[Event], start: datetime, end: datetime) -> Iterator[_Interval]:
        windows = self._windows(start, end)
        window_ends = [window_end for _, window_end in windows]
        lead = timedelta(minutes=self.lead)
        trail = timedelta(minutes=self.trail)
        earliest_start = start + lead
        latest_end = end - trail
        for event in originals:
            # Bound the original timestamps before padding so a far-away endpoint cannot overflow.
            padded_start = max(earliest_start, event.start.astimezone(UTC)) - lead
            padded_end = min(latest_end, event.end.astimezone(UTC)) + trail
            if padded_start >= padded_end:
                continue
            for index in range(bisect_right(window_ends, padded_start), len(windows)):
                window_start, window_end = windows[index]
                if window_start >= padded_end:
                    break
                yield max(window_start, padded_start), min(window_end, padded_end)

    def _windows(self, start: datetime, end: datetime) -> list[_Interval]:
        if not self.enabled:
            return [(start, end)]

        zone = ZoneInfo(self.zone)
        weekdays = frozenset(self.days.split())
        opens = time.fromisoformat(self.start)
        closes = time.fromisoformat(self.end)
        windows = []
        # UTC offsets are less than one day, including around a local date change.
        day = start.date() - _DAY
        last_day = end.date()
        while day <= last_day:
            if _DAYS[day.weekday()] in weekdays:
                local_start = datetime.combine(day, opens)
                local_end = datetime.combine(day, closes)
                for window_start, window_end in _wall_intervals(local_start, local_end, zone):
                    if window_start < end and start < window_end:
                        windows.append((max(window_start, start), min(window_end, end)))
            day += _DAY
        return sorted(windows)


def _wall_intervals(start: datetime, end: datetime, zone: ZoneInfo) -> Iterator[_Interval]:
    # Local membership changes only at a real boundary or an offset jump across a boundary.
    cuts = sorted(set(_boundary_cuts(start, zone) + _boundary_cuts(end, zone)))
    for left, right in pairwise(cuts):
        if start <= left.astimezone(zone).replace(tzinfo=None) < end:
            yield left, right


def _boundary_cuts(wall_time: datetime, zone: ZoneInfo) -> tuple[datetime, ...]:
    first = wall_time.replace(tzinfo=zone, fold=0).astimezone(UTC)
    second = wall_time.replace(tzinfo=zone, fold=1).astimezone(UTC)
    if first == second:
        return (first,)

    # For a fold or gap, the two UTC interpretations bracket the offset transition.
    left, right = sorted((first, second))
    return left, _offset_transition(left, right, zone), right


def _offset_transition(left: datetime, right: datetime, zone: ZoneInfo) -> datetime:
    old_offset = left.astimezone(zone).utcoffset()
    while right - left > _SECOND:
        half_seconds = ((right - left) // _SECOND) // 2
        middle = left + _SECOND * half_seconds
        if middle.astimezone(zone).utcoffset() == old_offset:
            left = middle
        else:
            right = middle
    return right
