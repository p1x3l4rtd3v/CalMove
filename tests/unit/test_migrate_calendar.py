"""MigrateCalendarUseCase behavior against in-memory fakes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from conftest import FakeForwarder, synced

from calmove.domain.fingerprint import event_fingerprint
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
    def test_skips_unchanged_top_level_and_their_exceptions(
        self, migrate: Any, make_event: Any
    ) -> None:
        master = _master(make_event)
        exc = _exception(make_event)
        result, writer, reporter = migrate([master, exc], migrated=synced(master, [exc]))

        assert result.created == 0
        assert result.updated == 0
        assert result.skipped == 2
        assert result.failed == []
        assert writer.created == []
        assert writer.updated == []
        assert writer.applied == []
        assert {e.id for e in reporter.skipped} == {"master-1", "exc-1"}

    def test_unmigrated_events_still_created_alongside_skips(
        self, migrate: Any, make_event: Any
    ) -> None:
        old = make_event("old-1")
        result, writer, _ = migrate([old, make_event("new-1")], migrated=synced(old))
        assert result.created == 1
        assert result.skipped == 1
        assert [e.id for _, e, _ in writer.created] == ["new-1"]


class TestSync:
    def test_changed_event_is_updated_in_place(self, migrate: Any, make_event: Any) -> None:
        event = make_event("evt-1", summary="New title")
        stale = synced(event, fingerprint="stale")
        result, writer, reporter = migrate([event], migrated=stale)

        assert result.updated == 1
        assert result.created == 0
        assert result.skipped == 0
        assert writer.created == []
        calendar_id, destination_id, updated, fingerprint, _send = writer.updated[0]
        assert (calendar_id, destination_id) == ("dst-cal", "dest-evt-1")
        assert updated.summary == "New title"
        assert fingerprint  # new fingerprint stored for the next run
        assert [e.id for e in reporter.updated] == ["evt-1"]

    def test_changed_exception_updates_master_and_reapplies_exceptions(
        self, migrate: Any, make_event: Any
    ) -> None:
        master = _master(make_event)
        old_exc = _exception(make_event)
        new_exc = _exception(make_event, summary="Moved again")
        # Stored fingerprint covers the OLD exception; the source now has the new one.
        result, writer, _ = migrate([master, new_exc], migrated=synced(master, [old_exc]))

        assert result.updated == 2  # master + re-applied exception
        assert len(writer.updated) == 1
        assert writer.updated[0][1] == "dest-master-1"
        assert [e.id for _, _, e, _ in writer.applied] == ["exc-1"]
        # exceptions are applied onto the EXISTING destination master
        assert writer.applied[0][1] == "dest-master-1"

    def test_pre_fingerprint_migrations_are_updated_once(
        self, migrate: Any, make_event: Any
    ) -> None:
        event = make_event("evt-1")
        result, writer, _ = migrate([event], migrated=synced(event, fingerprint=None))
        assert result.updated == 1
        assert writer.updated[0][1] == "dest-evt-1"

    def test_source_deleted_event_is_deleted_in_destination(
        self, migrate: Any, make_event: Any
    ) -> None:
        survivor = make_event("evt-1")
        gone = make_event("gone-1")
        migrated = synced(survivor) | synced(gone)
        result, writer, reporter = migrate([survivor], migrated=migrated)

        assert result.skipped == 1
        assert result.deleted == 1
        assert writer.deleted == [("dst-cal", "dest-gone-1", "none")]
        assert reporter.deleted == ["gone-1"]

    def test_delete_failure_is_recorded_and_run_continues(
        self, migrate: Any, make_event: Any
    ) -> None:
        gone = make_event("gone-1")
        other_gone = make_event("gone-2")
        migrated = synced(gone) | synced(other_gone)
        result, writer, _ = migrate([], migrated=migrated, fail_ids={"dest-gone-1"})

        assert result.deleted == 1
        assert ("gone-1", "boom:dest-gone-1") in result.failed
        assert [d[1] for d in writer.deleted] == ["dest-gone-2"]

    def test_update_failure_fails_series_and_run_continues(
        self, migrate: Any, make_event: Any
    ) -> None:
        master = _master(make_event)
        exc = _exception(make_event)
        good = make_event("good-1")
        result, writer, _ = migrate(
            [master, exc, good],
            migrated=synced(master, [exc], fingerprint="stale"),
            fail_ids={"master-1"},
        )

        assert result.updated == 0
        assert result.created == 1  # good-1 unaffected
        assert writer.applied == []
        failed_ids = [event_id for event_id, _ in result.failed]
        assert failed_ids == ["master-1", "exc-1"]
        assert result.failed[1][1] == "master not migrated"

    def test_create_stores_fingerprint_for_next_run(self, migrate: Any, make_event: Any) -> None:
        event = make_event("evt-1")
        result, writer, _ = migrate([event])
        assert result.created == 1
        assert writer.created_fingerprints["evt-1"] == event_fingerprint(event)

    def test_room_map_change_triggers_update(self, migrate: Any, make_event: Any) -> None:
        event = make_event(
            "evt-1",
            attendees=(Attendee(email="old-room@resource.calendar.google.com", is_resource=True),),
        )
        # First run stored the fingerprint of the UNMAPPED event; now a room map exists.
        result, writer, _ = migrate(
            [event],
            migrated=synced(event),
            room_map={"old-room@resource.calendar.google.com": "new-room@resource.example"},
        )
        assert result.updated == 1
        assert writer.updated[0][2].attendees[0].email == "new-room@resource.example"


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

    def test_dry_run_counts_updates_and_deletes_without_writes(
        self, migrate: Any, make_event: Any
    ) -> None:
        changed = make_event("changed-1")
        gone = make_event("gone-1")
        migrated = synced(changed, fingerprint="stale") | synced(gone)
        result, writer, _ = migrate([changed, make_event("new-1")], migrated=migrated, dry_run=True)
        assert result.created == 1
        assert result.updated == 1
        assert result.deleted == 1
        assert writer.created == writer.updated == writer.deleted == []

    def test_dry_run_still_counts_skips(self, migrate: Any, make_event: Any) -> None:
        old = make_event("old-1")
        result, writer, _ = migrate([old, make_event("new-1")], migrated=synced(old), dry_run=True)
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


class TestForwarding:
    @staticmethod
    def _invited(make_event: Any, event_id: str = "inv-1", **kw: Any) -> Any:
        attendees = kw.pop(
            "attendees",
            (
                Attendee(email="organizer@old.example"),
                Attendee(email="me@old.example", response_status="accepted"),
            ),
        )
        return make_event(event_id, is_organizer=False, attendees=attendees, **kw)

    def test_invited_event_is_forwarded_not_copied(self, migrate: Any, make_event: Any) -> None:
        forwarder = FakeForwarder()
        result, writer, reporter = migrate(
            [self._invited(make_event)],
            forwarder=forwarder,
            source_address="me@old.example",
            forward_address="me@new.example",
        )

        assert result.forwarded == 1
        assert result.created == 0
        assert writer.created == []
        calendar_id, event, email, send = forwarder.forwarded[0]
        assert (calendar_id, event.id, email, send) == (
            "src-cal",
            "inv-1",
            "me@new.example",
            "none",
        )
        assert [e.id for e in reporter.forwarded] == ["inv-1"]

    def test_already_invited_address_is_skipped(self, migrate: Any, make_event: Any) -> None:
        forwarder = FakeForwarder()
        invited = self._invited(
            make_event,
            attendees=(
                Attendee(email="me@old.example", response_status="accepted"),
                Attendee(email="ME@NEW.example"),  # case-insensitive membership
            ),
        )
        result, writer, _ = migrate(
            [invited],
            forwarder=forwarder,
            source_address="me@old.example",
            forward_address="me@new.example",
        )

        assert result.forwarded == 0
        assert result.skipped == 1
        assert forwarder.forwarded == []
        assert writer.responses == []  # re-runs never override a manual RSVP

    def test_rsvp_is_mirrored_after_forward(self, migrate: Any, make_event: Any) -> None:
        forwarder = FakeForwarder()
        _, writer, _ = migrate(
            [self._invited(make_event)],
            forwarder=forwarder,
            source_address="me@old.example",
            forward_address="me@new.example",
        )
        assert writer.responses == [("inv-1", "me@new.example", "accepted")]

    def test_unanswered_invite_gets_no_rsvp(self, migrate: Any, make_event: Any) -> None:
        forwarder = FakeForwarder()
        invited = self._invited(
            make_event,
            attendees=(Attendee(email="me@old.example", response_status="needsAction"),),
        )
        _, writer, _ = migrate(
            [invited],
            forwarder=forwarder,
            source_address="me@old.example",
            forward_address="me@new.example",
        )
        assert writer.responses == []

    def test_forward_failure_is_recorded_and_run_continues(
        self, migrate: Any, make_event: Any
    ) -> None:
        forwarder = FakeForwarder(fail_ids={"inv-1"})
        result, writer, _ = migrate(
            [self._invited(make_event), make_event("own-1")],
            forwarder=forwarder,
            forward_address="me@new.example",
        )
        assert result.forwarded == 0
        assert result.created == 1  # owned event unaffected
        assert ("inv-1", "boom:forward:inv-1") in result.failed
        assert writer.responses == []

    def test_missing_forward_address_is_reported(self, migrate: Any, make_event: Any) -> None:
        result, _, _ = migrate([self._invited(make_event)], forwarder=FakeForwarder())
        assert result.failed == [("inv-1", "no destination account address for forwarding")]

    def test_dry_run_counts_forwards_without_calls(self, migrate: Any, make_event: Any) -> None:
        forwarder = FakeForwarder()
        result, writer, _ = migrate(
            [self._invited(make_event)],
            forwarder=forwarder,
            forward_address="me@new.example",
            dry_run=True,
        )
        assert result.forwarded == 1
        assert forwarder.forwarded == []
        assert writer.responses == []

    def test_old_shadow_copy_of_invited_event_is_cleaned_up(
        self, migrate: Any, make_event: Any
    ) -> None:
        invited = self._invited(make_event)
        # A pre-forwarding CalMove run copied this invited event into the destination.
        result, writer, _ = migrate(
            [invited],
            migrated=synced(invited),
            forwarder=FakeForwarder(),
            forward_address="me@new.example",
        )
        assert result.forwarded == 1
        assert result.deleted == 1
        assert writer.deleted == [("dst-cal", "dest-inv-1", "none")]

    def test_exceptions_of_invited_master_are_not_orphans(
        self, migrate: Any, make_event: Any
    ) -> None:
        invited_master = self._invited(
            make_event, event_id="master-1", recurrence=("RRULE:FREQ=WEEKLY",)
        )
        exc = _exception(make_event)
        result, writer, _ = migrate(
            [invited_master, exc],
            forwarder=FakeForwarder(),
            forward_address="me@new.example",
        )
        assert result.failed == []  # the organizer's event carries its own exceptions
        assert writer.applied == []
        assert result.forwarded == 1


class TestUnicodeContent:
    """Sync decisions must be exact for non-ASCII titles/descriptions (Vietnamese etc.)."""

    def test_unicode_event_created_byte_exact(self, migrate: Any, make_event: Any) -> None:
        event = make_event(
            "evt-vn",
            summary="Họp giao ban tuần 🚀",
            description="Thảo luận kế hoạch quý 4 — tầng 3",
            location="Toà nhà A, Hà Nội",
        )
        result, writer, _ = migrate([event])
        assert result.created == 1
        created = writer.created[0][1]
        assert created.summary == "Họp giao ban tuần 🚀"
        assert created.description == "Thảo luận kế hoạch quý 4 — tầng 3"
        assert created.location == "Toà nhà A, Hà Nội"

    def test_unchanged_unicode_event_is_skipped_on_rerun(
        self, migrate: Any, make_event: Any
    ) -> None:
        event = make_event("evt-vn", summary="Họp giao ban tuần", description="Kế hoạch quý 4")
        result, writer, _ = migrate([event], migrated=synced(event))
        assert result.skipped == 1
        assert writer.created == writer.updated == []

    def test_diacritic_only_change_triggers_update(self, migrate: Any, make_event: Any) -> None:
        # "Hop giao ban" → "Họp giao ban": differs only in combining marks.
        before = make_event("evt-vn", summary="Hop giao ban")
        after = make_event("evt-vn", summary="Họp giao ban")
        result, writer, _ = migrate([after], migrated=synced(before))
        assert result.updated == 1
        assert writer.updated[0][2].summary == "Họp giao ban"

    def test_fingerprint_is_deterministic_for_unicode(self, make_event: Any) -> None:
        a = make_event("evt-vn", summary="週次定例 🚀", description="議題は計画")
        b = make_event("evt-vn", summary="週次定例 🚀", description="議題は計画")
        assert event_fingerprint(a) == event_fingerprint(b)
        assert event_fingerprint(a) != event_fingerprint(
            make_event("evt-vn", summary="週次定例 🚀", description="議題は計画変更")
        )
