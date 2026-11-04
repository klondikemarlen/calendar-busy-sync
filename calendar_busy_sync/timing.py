"""Event timing policy for buffered copies and work-hour filtering."""

from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .models import Event

if TYPE_CHECKING:
    from .settings import Settings


class TimingPolicy:
    def __init__(self, settings: "Settings"):
        self._enabled = settings.work_hours_enabled
        self._zone = ZoneInfo(settings.work_timezone)
        self._work_days = frozenset(settings.work_days)
        self._work_start = time.fromisoformat(settings.work_start)
        self._work_end = time.fromisoformat(settings.work_end)
        self._work_lead = settings.work_lead_minutes
        self._work_trail = settings.work_trail_minutes
        self._defaults = {
            "google": (settings.google_lead_minutes, settings.google_trail_minutes),
            "outlook": (settings.outlook_lead_minutes, settings.outlook_trail_minutes),
        }
        self._overrides = {
            (row["source"], row["event_id"]): (row["lead_minutes"], row["trail_minutes"])
            for row in settings.event_padding_overrides
        }
        self._max_lead = max((value[0] for value in self._defaults.values()), default=0)
        self._max_trail = max((value[1] for value in self._defaults.values()), default=0)
        for lead, trail in self._overrides.values():
            self._max_lead = max(self._max_lead, lead)
            self._max_trail = max(self._max_trail, trail)

    def query_window(self, start: datetime, end: datetime) -> tuple[datetime, datetime]:
        """Expand nominal horizon to retrieve events whose padding can overlap it."""
        return self._instant(start) - timedelta(minutes=self._max_trail), self._instant(end) + timedelta(
            minutes=self._max_lead
        )

    def desired_copy(self, source: str, event: Event, start: datetime, end: datetime) -> Event | None:
        if not event.busy or event.marker:
            return None
        lead, trail = self._overrides.get((source, event.id), self._defaults[source])
        buffered_start = self._instant(event.start) - timedelta(minutes=lead)
        buffered_end = self._instant(event.end) + timedelta(minutes=trail)
        if not self._overlaps(buffered_start, buffered_end, start, end):
            return None
        if self._enabled and not self._overlaps_work_window(buffered_start, buffered_end):
            return None
        if lead == 0 and trail == 0:
            return event
        return Event(event.id, buffered_start, buffered_end, event.busy, event.marker)

    @staticmethod
    def _instant(value: datetime) -> datetime:
        return value.astimezone(UTC)

    @classmethod
    def _overlaps(
        cls, left_start: datetime, left_end: datetime, right_start: datetime, right_end: datetime
    ) -> bool:
        return cls._instant(left_start) < cls._instant(right_end) and cls._instant(
            right_start
        ) < cls._instant(left_end)

    def _overlaps_work_window(self, start: datetime, end: datetime) -> bool:
        local_start = self._instant(start).astimezone(self._zone).date()
        local_end = self._instant(end).astimezone(self._zone).date()
        day = local_start - timedelta(days=1)
        last_day = local_end + timedelta(days=1)
        while day <= last_day:
            if ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[day.weekday()] in self._work_days:
                window_start = datetime.combine(day, self._work_start, self._zone) - timedelta(
                    minutes=self._work_lead
                )
                window_end = datetime.combine(day, self._work_end, self._zone) + timedelta(
                    minutes=self._work_trail
                )
                if self._overlaps(start, end, window_start, window_end):
                    return True
            day += timedelta(days=1)
        return False
