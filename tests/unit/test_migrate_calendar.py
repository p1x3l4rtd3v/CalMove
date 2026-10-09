"""MigrateCalendarUseCase behavior against in-memory fakes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from calmove.domain.model import Attendee, EventTime

ORIGINAL_START = EventTime(
    date_time=datetime(2026, 1, 12, 9, 0, tzinfo=UTC), time_zone="Europe/Zurich"
)


def _master(make_event: Any, event_id: str = "master-1") -> Any:
    return make_event(event_id, recurrence=("RRULE:FREQ=WEEKLY;BYDAY=MO",))


def _exception(
    make_event: Any, event_id: str = "exc-1", master_id: str = "master-1", **kw: Any
) -> Any:
    return make_event(
        event_id,
        recurring_event_id=master_id,
        original_start=ORIGINAL_START,
        **kw,
    )


class TestCreation:
    def test_creates_singles_and_masters(self, migrate: Any, make_event: Any) -> None:
        single = make_event("single-1")
        master = _master(make_event)
        result, writer, reporter = migrate([single, master])

        assert result.created == 2
        assert result.skipped == 0
        assert result.failed == []
        assert [e.id for _, e, _ in writer.created] == ["single-1", "master-1"]
        assert [e.id for e in reporter.created] == ["single-1", "master-1"]

    def test_migrated_source_ids_queried_once(self, migrate: Any, make_event: Any) -> None:
        _, writer, _ = migrate([make_event("single-1"), make_event("single-2")])
        assert writer.migrated_calls == 1


class TestIdempotentSkip:
    def test_skips_migrated_top_level_and_their_exceptions(
        self, migrate: Any, make_event: Any
    ) -> None:
        master = _master(make_event)
        exc = _exception(make_event)
        result, writer, reporter = migrate([master, exc], migrated={"master-1"})

        assert result.created == 0
        assert result.skipped == 2
        assert result.failed == []
        assert writer.created == []
        assert writer.applied == []
        assert {e.id for e in reporter.skipped} == {"master-1", "exc-1"}

    def test_unmigrated_events_still_created_alongside_skips(
        self, migrate: Any, make_event: Any
    ) -> None:
        result, writer, _ = migrate([make_event("old-1"), make_event("new-1")], migrated={"old-1"})
        assert result.created == 1
        assert result.skipped == 1
        assert [e.id for _, e, _ in writer.created] == ["new-1"]


class TestDryRun:
    def test_dry_run_counts_top_level_without_any_writes(
        self, migrate: Any, make_event: Any
    ) -> None:
        single = make_event("single-1")
        master = _master(make_event)
        exc = _exception(make_event)
        result, writer, _ = migrate([single, master, exc], dry_run=True)

        assert result.created == 2  # top-level would-creates only
        assert writer.created == []
        assert writer.applied == []

    def test_dry_run_still_counts_skips(self, migrate: Any, make_event: Any) -> None:
        result, writer, _ = migrate(
            [make_event("old-1"), make_event("new-1")], migrated={"old-1"}, dry_run=True
        )
        assert result.created == 1
        assert result.skipped == 1
        assert writer.created == []


class TestExceptions:
    def test_exception_applied_after_master_with_original_start(
        self, migrate: Any, make_event: Any
    ) -> None:
        master = _master(make_event)
        exc = _exception(make_event, summary="Moved occurrence")
        result, writer, _ = migrate([master, exc])

        assert result.created == 2  # master + applied exception
        assert result.failed == []
        assert len(writer.applied) == 1
        calendar_id, new_master_id, applied, send_updates = writer.applied[0]
        assert calendar_id == "dst-cal"
        assert new_master_id == "new-master-1"
        assert applied.original_start == ORIGINAL_START
        assert applied.recurring_event_id == "master-1"
        assert send_updates == "none"

    def test_cancelled_exception_passed_through(self, migrate: Any, make_event: Any) -> None:
        master = _master(make_event)
        exc = _exception(make_event, status="cancelled")
        _, writer, _ = migrate([master, exc])

        assert len(writer.applied) == 1
        assert writer.applied[0][2].status == "cancelled"

    def test_orphan_exception_fails_with_master_not_migrated(
        self, migrate: Any, make_event: Any
    ) -> None:
        exc = _exception(make_event, master_id="absent-master")
        result, writer, reporter = migrate([exc])

        assert result.created == 0
        assert result.failed == [("exc-1", "master not migrated")]
        assert writer.applied == []
        assert [e.id for e, _ in reporter.failed] == ["exc-1"]

    def test_exception_of_failed_master_fails_with_master_not_migrated(
        self, migrate: Any, make_event: Any
    ) -> None:
        master = _master(make_event)
        exc = _exception(make_event)
        result, writer, _ = migrate([master, exc], fail_ids={"master-1"})

        assert result.created == 0
        assert writer.applied == []
        failed_ids = [event_id for event_id, _ in result.failed]
        assert failed_ids == ["master-1", "exc-1"]
        assert result.failed[1][1] == "master not migrated"

    def test_exception_apply_failure_does_not_abort_run(
        self, migrate: Any, make_event: Any
    ) -> None:
        master = _master(make_event)
        bad = _exception(make_event, event_id="exc-bad")
        good = _exception(make_event, event_id="exc-good")
        result, writer, reporter = migrate([master, bad, good], fail_ids={"exc-bad"})

        assert result.created == 2  # master + good exception
        assert [event_id for event_id, _ in result.failed] == ["exc-bad"]
        assert "boom" in result.failed[0][1]
        applied_ids = {e.id for _, _, e, _ in writer.applied}
        assert applied_ids == {"exc-good"}
        assert [e.id for e, _ in reporter.failed] == ["exc-bad"]


class TestFailureIsolation:
    def test_create_failure_recorded_and_run_continues(self, migrate: Any, make_event: Any) -> None:
        bad = make_event("bad-1")
        good = make_event("good-1")
        result, writer, reporter = migrate([bad, good], fail_ids={"bad-1"})

        assert result.created == 1
        assert len(result.failed) == 1
        failed_id, message = result.failed[0]
        assert failed_id == "bad-1"
        assert "boom" in message
        assert [e.id for _, e, _ in writer.created] == ["good-1"]
        assert len(reporter.failed) == 1
        event, error = reporter.failed[0]
        assert event.id == "bad-1"
        assert isinstance(error, Exception)


class TestRoomMapping:
    def _event_with_rooms(self, make_event: Any) -> Any:
        return make_event(
            "roomy-1",
            attendees=(
                Attendee(email="alice@old.example", display_name="Alice"),
                Attendee(
                    email="room-old@resource.calendar.google.com",
                    display_name="Room Old",
                    is_resource=True,
                    response_status="accepted",
                ),
                Attendee(
                    email="room-other@resource.calendar.google.com",
                    is_resource=True,
                ),
            ),
        )

    def test_room_map_rewrites_resources_and_collects_unmapped(
        self, migrate: Any, make_event: Any
    ) -> None:
        event = self._event_with_rooms(make_event)
        room_map = {
            "room-old@resource.calendar.google.com": "room-new@resource.calendar.google.com"
        }
        result, writer, _ = migrate([event], room_map=room_map)

        assert result.unmapped_rooms == {"room-other@resource.calendar.google.com"}
        created = writer.created[0][1]
        by_email = {a.email: a for a in created.attendees}
        assert "room-new@resource.calendar.google.com" in by_email
        assert "room-old@resource.calendar.google.com" not in by_email
        mapped = by_email["room-new@resource.calendar.google.com"]
        assert mapped.is_resource is True
        assert mapped.response_status == "accepted"
        # non-resource attendee untouched
        assert by_email["alice@old.example"].display_name == "Alice"
        assert by_email["alice@old.example"].is_resource is False
        # unmapped room kept verbatim
        assert by_email["room-other@resource.calendar.google.com"].is_resource is True

    def test_empty_room_map_is_noop(self, migrate: Any, make_event: Any) -> None:
        event = self._event_with_rooms(make_event)
        result, writer, _ = migrate([event])

        assert result.unmapped_rooms == set()
        created = writer.created[0][1]
        assert {a.email for a in created.attendees} == {
            "alice@old.example",
            "room-old@resource.calendar.google.com",
            "room-other@resource.calendar.google.com",
        }


class TestSendUpdates:
    def test_send_updates_forwarded_to_create_and_apply(
        self, migrate: Any, make_event: Any
    ) -> None:
        master = _master(make_event)
        exc = _exception(make_event)
        _, writer, _ = migrate([master, exc], send_updates="all")

        assert writer.created[0][2] == "all"
        assert writer.applied[0][3] == "all"
