"""Immutable domain model for calendars and events."""

from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class Attendee:
    """A participant of an event; rooms are resource attendees."""

    email: str
    display_name: str | None = None
    optional: bool = False
    is_resource: bool = False
    response_status: str = "needsAction"


@dataclass(frozen=True, slots=True, kw_only=True)
class EventTime:
    """A point in time of an event: either a timed moment or an all-day date."""

    date_time: datetime | None = None
    date: date_type | None = None
    time_zone: str | None = None

    @property
    def all_day(self) -> bool:
        return self.date is not None


@dataclass(frozen=True, slots=True, kw_only=True)
class Reminder:
    """An event reminder override."""

    method: str
    minutes: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Calendar:
    """A calendar visible to the authenticated user."""

    id: str
    summary: str
    access_role: str
    primary: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarEvent:
    """A calendar event: single, recurring master, or recurrence exception."""

    id: str
    start: EventTime
    end: EventTime
    summary: str = ""
    description: str = ""
    location: str = ""
    attendees: tuple[Attendee, ...] = ()
    recurrence: tuple[str, ...] = ()
    recurring_event_id: str | None = None
    original_start: EventTime | None = None
    status: str = "confirmed"
    visibility: str = "default"
    transparency: str = "opaque"
    reminders: tuple[Reminder, ...] = ()
    reminders_use_default: bool = True
    conference_link: str | None = None

    def is_recurring_master(self) -> bool:
        return bool(self.recurrence)

    def is_exception(self) -> bool:
        return self.recurring_event_id is not None


__all__ = [
    "Attendee",
    "Calendar",
    "CalendarEvent",
    "EventTime",
    "Reminder",
]
