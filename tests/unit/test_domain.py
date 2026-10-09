"""Domain model behavior and immutability."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime

import pytest

from calmove.domain.model import Attendee, CalendarEvent, EventTime, Reminder


class TestEventTime:
    def test_all_day_true_when_date_set(self) -> None:
        assert EventTime(date=date(2026, 1, 5)).all_day is True

    def test_all_day_false_for_timed(self) -> None:
        et = EventTime(date_time=datetime(2026, 1, 5, 9, tzinfo=UTC), time_zone="Europe/Zurich")
        assert et.all_day is False

    def test_all_fields_optional_default_none(self) -> None:
        et = EventTime()
        assert et.date_time is None
        assert et.date is None
        assert et.time_zone is None


class TestCalendarEvent:
    def _timed(self) -> EventTime:
        return EventTime(date_time=datetime(2026, 1, 5, 9, tzinfo=UTC))

    def test_is_recurring_master_when_recurrence_present(self) -> None:
        event = CalendarEvent(
            id="m1",
            start=self._timed(),
            end=self._timed(),
            recurrence=("RRULE:FREQ=WEEKLY;BYDAY=MO",),
        )
        assert event.is_recurring_master() is True
        assert event.is_exception() is False

    def test_is_exception_when_recurring_event_id_set(self) -> None:
        event = CalendarEvent(
            id="e1",
            start=self._timed(),
            end=self._timed(),
            recurring_event_id="m1",
            original_start=self._timed(),
        )
        assert event.is_exception() is True
        assert event.is_recurring_master() is False

    def test_single_event_is_neither_master_nor_exception(self) -> None:
        event = CalendarEvent(id="s1", start=self._timed(), end=self._timed())
        assert event.is_recurring_master() is False
        assert event.is_exception() is False

    def test_defaults(self) -> None:
        event = CalendarEvent(id="s1", start=self._timed(), end=self._timed())
        assert event.summary == ""
        assert event.attendees == ()
        assert event.recurrence == ()
        assert event.reminders == ()
        assert event.reminders_use_default is True
        assert event.status == "confirmed"
        assert event.visibility == "default"
        assert event.transparency == "opaque"
        assert event.conference_link is None


class TestImmutability:
    def test_attendee_is_frozen(self) -> None:
        attendee = Attendee(email="a@example.com")
        with pytest.raises(FrozenInstanceError):
            attendee.email = "b@example.com"  # type: ignore[misc]

    def test_event_time_is_frozen(self) -> None:
        et = EventTime(date=date(2026, 1, 5))
        with pytest.raises(FrozenInstanceError):
            et.date = date(2026, 1, 6)  # type: ignore[misc]

    def test_reminder_is_frozen(self) -> None:
        reminder = Reminder(method="popup", minutes=10)
        with pytest.raises(FrozenInstanceError):
            reminder.minutes = 20  # type: ignore[misc]

    def test_calendar_event_is_frozen(self) -> None:
        event = CalendarEvent(
            id="s1",
            start=EventTime(date=date(2026, 1, 5)),
            end=EventTime(date=date(2026, 1, 6)),
        )
        with pytest.raises(FrozenInstanceError):
            event.summary = "changed"  # type: ignore[misc]


class TestAttendeeDefaults:
    def test_defaults(self) -> None:
        attendee = Attendee(email="a@example.com")
        assert attendee.display_name is None
        assert attendee.optional is False
        assert attendee.is_resource is False
        assert attendee.response_status == "needsAction"
