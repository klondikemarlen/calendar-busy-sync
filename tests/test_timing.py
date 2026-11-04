import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from calendar_busy_sync.models import Event
from calendar_busy_sync.timing import TimingPolicy


class TimingPolicyTests(unittest.TestCase):
    def settings(self, **values):
        base = dict(
            work_hours_enabled=False,
            work_timezone="UTC",
            work_days=["mon", "tue", "wed", "thu", "fri"],
            work_start="08:00",
            work_end="17:00",
            work_lead_minutes=0,
            work_trail_minutes=0,
            google_lead_minutes=0,
            google_trail_minutes=0,
            outlook_lead_minutes=0,
            outlook_trail_minutes=0,
            event_padding_overrides=[],
        )
        return SimpleNamespace(**(base | values))

    def test_half_open_horizon_edges_and_unchanged_identity(self):
        policy = TimingPolicy(self.settings())
        start, end = datetime(2025, 1, 6, 9, tzinfo=UTC), datetime(2025, 1, 6, 17, tzinfo=UTC)
        self.assertIsNone(
            policy.desired_copy("google", Event("before", start - timedelta(hours=1), start), start, end)
        )
        self.assertIsNone(
            policy.desired_copy("google", Event("after", end, end + timedelta(hours=1)), start, end)
        )
        event = Event("inside", start, start + timedelta(hours=1))
        self.assertIs(policy.desired_copy("google", event, start, end), event)
        self.assertIsNone(policy.desired_copy("google", Event("free", start, end, busy=False), start, end))
        self.assertIsNone(
            policy.desired_copy("google", Event("owned", start, end, marker="owned"), start, end)
        )

    def test_padding_direction_and_override_precedence(self):
        policy = TimingPolicy(
            self.settings(
                google_lead_minutes=30,
                google_trail_minutes=10,
                event_padding_overrides=[
                    {"source": "google", "event_id": "special", "lead_minutes": 60, "trail_minutes": 20}
                ],
            )
        )
        start, end = datetime(2025, 1, 6, 8, tzinfo=UTC), datetime(2025, 1, 6, 18, tzinfo=UTC)
        ordinary = Event("ordinary", start + timedelta(minutes=5), end - timedelta(minutes=5))
        special = Event("special", start + timedelta(minutes=15), end - timedelta(minutes=45))
        copied_ordinary = policy.desired_copy("google", ordinary, start, end)
        self.assertEqual(
            (copied_ordinary.start, copied_ordinary.end),
            (start - timedelta(minutes=25), end + timedelta(minutes=5)),
        )
        copied = policy.desired_copy("google", special, start, end)
        self.assertEqual(
            (copied.start, copied.end), (start - timedelta(minutes=45), end - timedelta(minutes=25))
        )
        self.assertEqual(policy.desired_copy("outlook", ordinary, start, end), ordinary)

    def test_fixed_utc_minus_seven_window_includes_midnight_margin(self):
        policy = TimingPolicy(
            self.settings(
                work_hours_enabled=True,
                work_timezone="Etc/GMT+7",
                work_days=["mon"],
                work_start="00:30",
                work_end="01:30",
                work_lead_minutes=60,
                work_trail_minutes=0,
            )
        )
        # Monday 00:00 through 00:10 local, admitted by the preceding-hour margin.
        start = datetime(2025, 1, 6, 7, tzinfo=UTC)
        event = Event("edge", start, start + timedelta(minutes=10))
        nominal_start, nominal_end = start - timedelta(hours=1), start + timedelta(hours=1)
        self.assertIsNotNone(policy.desired_copy("google", event, nominal_start, nominal_end))

    def test_dst_ordering_and_local_work_window(self):
        zone = ZoneInfo("America/Los_Angeles")
        policy = TimingPolicy(
            self.settings(
                work_hours_enabled=True,
                work_timezone="America/Los_Angeles",
                work_days=["sun"],
                work_start="01:00",
                work_end="03:00",
            )
        )
        # Fall-back 01:30 PST is later as an instant than 01:45 PDT.
        event = Event(
            "fold",
            datetime(2025, 11, 2, 1, 45, tzinfo=zone, fold=0),
            datetime(2025, 11, 2, 1, 30, tzinfo=zone, fold=1),
        )
        start = datetime(2025, 11, 2, 8, tzinfo=UTC)
        result = policy.desired_copy("google", event, start, start + timedelta(hours=3))
        self.assertIsNotNone(result)
        self.assertLess(result.start.astimezone(UTC), result.end.astimezone(UTC))
        # A buffered interval spanning local dates remains eligible if any allowed window overlaps.
        multi = Event("multi", datetime(2025, 11, 1, 8, tzinfo=UTC), datetime(2025, 11, 3, 8, tzinfo=UTC))
        self.assertIsNotNone(
            policy.desired_copy("google", multi, start - timedelta(days=2), start + timedelta(days=2))
        )
        buffered = TimingPolicy(self.settings(google_lead_minutes=30, google_trail_minutes=15))
        result = buffered.desired_copy("google", event, start, start + timedelta(hours=3))
        self.assertEqual(
            (result.start, result.end),
            (datetime(2025, 11, 2, 8, 15, tzinfo=UTC), datetime(2025, 11, 2, 9, 45, tzinfo=UTC)),
        )

    def test_all_day_and_multiday_overlap_any_allowed_day(self):
        policy = TimingPolicy(
            self.settings(work_hours_enabled=True, work_days=["wed"], work_start="09:00", work_end="10:00")
        )
        start = datetime(2025, 1, 8, 0, tzinfo=UTC)
        all_day = Event("all-day", start, start + timedelta(days=1))
        self.assertIsNotNone(policy.desired_copy("google", all_day, start, start + timedelta(days=2)))
        weekend = Event("weekend", start + timedelta(days=3), start + timedelta(days=4))
        self.assertIsNone(policy.desired_copy("google", weekend, start, start + timedelta(days=7)))

    def test_work_window_boundaries_and_travel_only_eligibility_keep_full_interval(self):
        policy = TimingPolicy(
            self.settings(
                work_hours_enabled=True,
                work_timezone="Etc/GMT+7",
                work_lead_minutes=120,
                work_trail_minutes=120,
                google_trail_minutes=30,
            )
        )
        day = datetime(2025, 1, 6, tzinfo=UTC)
        start, end = day, day + timedelta(days=1)
        # Effective window is 13:00Z–02:00Z next day (06:00–19:00 UTC-7).
        before = Event("boundary", day + timedelta(hours=12), day + timedelta(hours=12, minutes=30))
        self.assertIsNone(policy.desired_copy("google", before, start, end))
        crossing = Event("crossing", before.start, before.end + timedelta(minutes=1))
        copied = policy.desired_copy("google", crossing, start, end)
        self.assertEqual((copied.start, copied.end), (crossing.start, day + timedelta(hours=13, minutes=1)))
        after = Event("after", day + timedelta(days=1, hours=2), day + timedelta(days=1, hours=3))
        self.assertIsNone(policy.desired_copy("outlook", after, start, end + timedelta(days=1)))

    def test_query_horizon_uses_largest_defaults_and_overrides(self):
        policy = TimingPolicy(
            self.settings(
                google_lead_minutes=20,
                outlook_trail_minutes=35,
                event_padding_overrides=[
                    {"source": "outlook", "event_id": "x", "lead_minutes": 50, "trail_minutes": 40}
                ],
            )
        )
        start, end = datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 1, 2, tzinfo=UTC)
        self.assertEqual(
            policy.query_window(start, end), (start - timedelta(minutes=40), end + timedelta(minutes=50))
        )


if __name__ == "__main__":
    unittest.main()
