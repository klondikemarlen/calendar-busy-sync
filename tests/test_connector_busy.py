import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

from calendar_busy_sync.connector_busy import MAX_INTERVALS, apply_snapshot, cleanup_owned, export_snapshot
from calendar_busy_sync.models import CopyIdUnavailable, Event, SyncError

START = datetime(2026, 11, 20, tzinfo=UTC)
END = START + timedelta(days=30)
PAIR = "ab" * 32
OTHER_PAIR = "cd" * 32
MORNING = ["2026-11-20T09:00:00Z", "2026-11-20T11:00:00Z"]
AFTERNOON = ["2026-11-20T14:00:00Z", "2026-11-20T15:00:00Z"]


class MemoryCalendar:
    def __init__(self, *events):
        self.events = {event.id: event for event in events}
        self.reads = []
        self.writes = []
        self.fail_read = False
        self.fail_create = False
        self.repeat_event = False
        self.lose_create_response = False
        self.hidden_event_ids = set()
        self.create_attempts = []
        self.tombstones = set()

    def list_events(self, start, end):
        self.reads.append((start, end))
        if self.fail_read:
            raise SyncError("The next provider page was unavailable.")
        events = [
            event
            for event in self.events.values()
            if start < event.end and event.start < end and event.id not in self.hidden_event_ids
        ]
        if self.repeat_event and events:
            events.append(events[0])
        return events

    def get_event(self, event_id):
        return self.events.get(event_id)

    def create_busy(self, event, marker):
        self.create_attempts.append(marker)
        if self.fail_create:
            raise SyncError("The provider could not create a copy.")
        if marker in self.tombstones:
            raise CopyIdUnavailable("The provider still reserves this identifier.")
        for existing in self.events.values():
            if existing.marker == marker:
                return existing

        copy = replace(event, id=f"copy-{len(self.writes)}", marker=marker)
        self.events[copy.id] = copy
        self.writes.append(("create", copy.id))
        if self.lose_create_response:
            self.lose_create_response = False
            self.hidden_event_ids.add(copy.id)
            raise SyncError("The copy may have been created.")
        return copy

    def update_busy(self, event_id, event, marker):
        if self.events[event_id].marker != marker:
            raise SyncError("Refusing to update an unowned event.")
        copy = replace(event, id=event_id, marker=marker)
        self.events[copy.id] = copy
        self.writes.append(("update", copy.id))
        return copy

    def delete_busy(self, event_id, marker):
        if self.events[event_id].marker != marker:
            raise SyncError("Refusing to delete an unowned event.")
        self.tombstones.add(marker)
        del self.events[event_id]
        self.writes.append(("delete", event_id))


def event(event_id, start_hour, end_hour, **kwargs):
    start = START + timedelta(hours=start_hour)
    end = START + timedelta(hours=end_hour)
    return Event(event_id, start, end, **kwargs)


class ConnectorBusyTests(unittest.TestCase):
    def test_export_clips_merges_and_normalizes_originals_without_echo(self):
        offset = timezone(timedelta(hours=-7))
        local_event = Event(
            "local-zone-original",
            datetime(2026, 11, 20, 1, tzinfo=offset),
            datetime(2026, 11, 20, 3, tzinfo=offset),
        )
        calendar = MemoryCalendar(
            event("starts-before-horizon", -2, 1),
            event("adjacent", 1, 2),
            event("overlapping", 9, 11),
            event("transparent", 2, 8, busy=False),
            event("legacy-copy", 11, 12, marker="cbs1:legacy:outlook:hash"),
            event("ends-after-horizon", 719, 722),
            local_event,
        )
        apply_snapshot(calendar, OTHER_PAIR, START, END, [AFTERNOON])

        self.assertEqual(
            export_snapshot(calendar, START, END),
            [
                ["2026-11-20T00:00:00Z", "2026-11-20T02:00:00Z"],
                ["2026-11-20T08:00:00Z", "2026-11-20T11:00:00Z"],
                ["2026-12-19T23:00:00Z", "2026-12-20T00:00:00Z"],
            ],
        )

    def test_export_rounds_subsecond_originals_outward_at_wire_precision(self):
        start = START.replace(microsecond=1000)
        end = START.replace(microsecond=2000)
        calendar = MemoryCalendar(Event("subsecond", start, end))

        self.assertEqual(
            export_snapshot(calendar, START, END),
            [["2026-11-20T00:00:00Z", "2026-11-20T00:00:01Z"]],
        )

    def test_move_cancel_and_repeat_preserve_overlapping_originals_and_other_pair(self):
        source = MemoryCalendar(event("original-source", 9, 11))
        overlapping = event("overlapping-destination-original", 10, 12)
        legacy = event("legacy-destination-copy", 6, 7, marker="cbs1:legacy:google:hash")
        malformed = event("marker-lookalike", 3, 4, marker=f"cbs2:{PAIR}:malformed")
        destination = MemoryCalendar(overlapping, legacy, malformed)
        apply_snapshot(destination, OTHER_PAIR, START, END, [MORNING])
        unrelated = dict(destination.events)

        apply_snapshot(destination, PAIR, START, END, export_snapshot(source, START, END))
        self.assertEqual(destination.writes[-1], ("create", "copy-1"))
        self.assertEqual(
            export_snapshot(destination, START, END),
            [["2026-11-20T10:00:00Z", "2026-11-20T12:00:00Z"]],
        )
        apply_snapshot(destination, PAIR, START, END, export_snapshot(source, START, END))
        self.assertEqual(destination.writes, [("create", "copy-0"), ("create", "copy-1")])

        source.events["original-source"] = event("original-source", 14, 15)
        self.assertEqual(export_snapshot(source, START, END), [AFTERNOON])
        apply_snapshot(destination, PAIR, START, END, [AFTERNOON])
        self.assertEqual(destination.writes[-2:], [("create", "copy-2"), ("delete", "copy-1")])
        self.assertEqual(destination.events["copy-2"].start, START + timedelta(hours=14))

        del source.events["original-source"]
        apply_snapshot(destination, PAIR, START, END, export_snapshot(source, START, END))
        self.assertEqual(destination.writes[-1], ("delete", "copy-2"))
        self.assertEqual(destination.events, unrelated)

    def test_repairs_a_moved_or_nonbusy_projection_and_removes_distinct_duplicates(self):
        calendar = MemoryCalendar()
        apply_snapshot(calendar, PAIR, START, END, [MORNING])
        original = calendar.events["copy-0"]
        calendar.events["copy-0"] = replace(original, start=START, busy=False)

        apply_snapshot(calendar, PAIR, START, END, [MORNING])
        self.assertEqual(calendar.writes[-1], ("update", "copy-0"))
        self.assertEqual(calendar.events["copy-0"], original)

        calendar.events["copy-z"] = replace(original, id="copy-z")
        apply_snapshot(calendar, PAIR, START, END, [MORNING])
        self.assertEqual(calendar.writes[-1], ("delete", "copy-z"))
        self.assertEqual(calendar.events, {"copy-0": original})

    def test_rolling_window_adopts_stable_copies_and_replaces_boundary_clips(self):
        source = MemoryCalendar(event("crosses-start", 20, 26), event("inside-both-horizons", 36, 37))
        destination = MemoryCalendar()
        apply_snapshot(destination, PAIR, START, END, export_snapshot(source, START, END))
        stable_copy = destination.events["copy-1"]
        next_start = START + timedelta(days=1)
        next_end = END + timedelta(days=1)

        apply_snapshot(destination, PAIR, next_start, next_end, export_snapshot(source, next_start, next_end))
        self.assertEqual(destination.writes[-2:], [("create", "copy-2"), ("delete", "copy-0")])
        self.assertEqual(destination.events["copy-1"], stable_copy)
        self.assertEqual(destination.events["copy-2"].start, next_start)
        self.assertEqual(destination.events["copy-2"].end, START + timedelta(hours=26))

    def test_tombstoned_create_retries_and_recovers_an_uncertain_response(self):
        calendar = MemoryCalendar()
        apply_snapshot(calendar, PAIR, START, END, [MORNING])
        apply_snapshot(calendar, PAIR, START, END, [])
        calendar.lose_create_response = True

        with self.assertRaises(SyncError):
            apply_snapshot(calendar, PAIR, START, END, [MORNING])
        reserved_marker, attempted_tombstone, replacement_marker = calendar.create_attempts
        self.assertEqual(reserved_marker, attempted_tombstone)
        self.assertNotEqual(reserved_marker, replacement_marker)

        apply_snapshot(calendar, PAIR, START, END, [MORNING])
        self.assertEqual(calendar.create_attempts[-2:], [reserved_marker, replacement_marker])
        self.assertEqual(calendar.writes, [("create", "copy-0"), ("delete", "copy-0"), ("create", "copy-2")])
        self.assertEqual(list(calendar.events), ["copy-2"])

    def test_invalid_wire_data_is_rejected_before_provider_reads_or_writes(self):
        invalid = (
            None,
            {"intervals": [MORNING]},
            (MORNING,),
            [tuple(MORNING)],
            [MORNING * 2],
            [MORNING] * (MAX_INTERVALS + 1),
            [MORNING, ["2026-12-21T14:00:00Z", "2026-12-21T15:00:00Z"]],
            [["2026-11-19T23:00:00Z", MORNING[1]]],
            [[MORNING[0], "2026-12-20T00:00:01Z"]],
            [["2026-11-20T09:00:00+00:00", MORNING[1]]],
            [["2026-11-20T09:00:00.000Z", MORNING[1]]],
            [["2026-11-20t09:00:00z", MORNING[1]]],
            [["2026-11-20T09:00:00Z\n", MORNING[1]]],
            [["2026-11-31T09:00:00Z", MORNING[1]]],
            [["2026-11-20T09:00:60Z", MORNING[1]]],
            [[MORNING[1], MORNING[0]]],
            [[MORNING[0], MORNING[0]]],
            [AFTERNOON, MORNING],
            [MORNING, MORNING],
            [MORNING, [MORNING[1], AFTERNOON[1]]],
        )
        calendar = MemoryCalendar()
        for index, intervals in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(SyncError):
                apply_snapshot(calendar, PAIR, START, END, intervals)

        self.assertEqual(calendar.reads, [])
        self.assertEqual(calendar.writes, [])

    def test_rejects_invalid_pair_ids_or_windows_before_reading(self):
        calendar = MemoryCalendar()
        for pair in ("", PAIR.upper(), PAIR + ":other", PAIR[:-1], None):
            with self.subTest(pair=pair), self.assertRaises(SyncError):
                apply_snapshot(calendar, pair, START, END, [])

        for start in (START.replace(tzinfo=None), START + timedelta(seconds=1), END):
            with self.subTest(start=start):
                with self.assertRaises(SyncError):
                    export_snapshot(calendar, start, END)
                with self.assertRaises(SyncError):
                    apply_snapshot(calendar, PAIR, start, END, [])

        self.assertEqual(calendar.reads, [])
        self.assertEqual(calendar.writes, [])

    def test_export_fails_rather_than_truncating_an_oversized_union(self):
        events = []
        for index in range(MAX_INTERVALS + 1):
            start = START + timedelta(seconds=2 * index)
            end = start + timedelta(seconds=1)
            events.append(Event(str(index), start, end))
        calendar = MemoryCalendar(*events)

        with self.assertRaises(SyncError):
            export_snapshot(calendar, START, END)

    def test_read_failure_or_repeated_provider_id_cannot_delete_or_apply(self):
        for failure in ("fail_read", "repeat_event"):
            with self.subTest(failure=failure):
                calendar = MemoryCalendar()
                apply_snapshot(calendar, PAIR, START, END, [MORNING])
                setattr(calendar, failure, True)
                with self.assertRaises(SyncError):
                    export_snapshot(calendar, START, END)
                with self.assertRaises(SyncError):
                    apply_snapshot(calendar, PAIR, START, END, [AFTERNOON])
                with self.assertRaises(SyncError):
                    cleanup_owned(calendar, PAIR, START, END)

                self.assertEqual(calendar.writes, [("create", "copy-0")])

    def test_failed_create_preserves_previous_busy_coverage(self):
        calendar = MemoryCalendar()
        apply_snapshot(calendar, PAIR, START, END, [MORNING])
        calendar.fail_create = True

        with self.assertRaises(SyncError):
            apply_snapshot(calendar, PAIR, START, END, [AFTERNOON])

        self.assertEqual(calendar.writes, [("create", "copy-0")])

    def test_cleanup_is_idempotent_and_scoped_to_pair_and_requested_window(self):
        original = event("unrelated", 9, 11)
        calendar = MemoryCalendar(original)
        apply_snapshot(calendar, OTHER_PAIR, START, END, [MORNING])
        apply_snapshot(calendar, PAIR, START, END, [MORNING])
        next_start = START + timedelta(days=1)

        cleanup_owned(calendar, PAIR, next_start, END)
        self.assertEqual(list(calendar.events), ["unrelated", "copy-0", "copy-1"])
        cleanup_owned(calendar, PAIR, START, END)
        cleanup_owned(calendar, PAIR, START, END)
        self.assertEqual(calendar.writes, [("create", "copy-0"), ("create", "copy-1"), ("delete", "copy-1")])
        self.assertEqual(list(calendar.events), ["unrelated", "copy-0"])
