"""Shared fixtures: in-memory fakes for the application ports and event factories."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from calmove.domain.date_range import DateRange
from calmove.domain.model import CalendarEvent, EventTime

DEFAULT_START = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
DEFAULT_END = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)


def build_event(event_id: str = "evt-1", **overrides: Any) -> CalendarEvent:
    defaults: dict[str, Any] = {
        "id": event_id,
        "start": EventTime(date_time=DEFAULT_START, time_zone="Europe/Zurich"),
        "end": EventTime(date_time=DEFAULT_END, time_zone="Europe/Zurich"),
        "summary": f"Event {event_id}",
    }
    defaults.update(overrides)
    return CalendarEvent(**defaults)


class FakeReader:
    """CalendarReader fake preloaded with events."""

    def __init__(self, events: list[CalendarEvent] | tuple[CalendarEvent, ...] = ()) -> None:
        self.events = list(events)
        self.calendars: list[Any] = []
        self.fetch_calls: list[tuple[str, DateRange]] = []

    def list_calendars(self) -> list[Any]:
        return list(self.calendars)

    def fetch_events(self, calendar_id: str, date_range: DateRange) -> list[CalendarEvent]:
        self.fetch_calls.append((calendar_id, date_range))
        return list(self.events)


class FakeWriter:
    """CalendarWriter fake: records creates/applies, injectable failures per event id."""

    def __init__(
        self,
        migrated: set[str] | frozenset[str] | tuple[str, ...] = (),
        fail_ids: set[str] | frozenset[str] | tuple[str, ...] = (),
    ) -> None:
        self.migrated = set(migrated)
        self.fail_ids = set(fail_ids)
        self.created: list[tuple[str, CalendarEvent, str]] = []
        self.applied: list[tuple[str, str, CalendarEvent, str]] = []
        self.migrated_calls = 0

    def list_calendars(self) -> list[Any]:
        return []

    def migrated_source_ids(self, calendar_id: str) -> set[str]:
        self.migrated_calls += 1
        return set(self.migrated)

    def create_event(self, calendar_id: str, event: CalendarEvent, *, send_updates: str) -> str:
        if event.id in self.fail_ids:
            raise RuntimeError(f"boom:{event.id}")
        self.created.append((calendar_id, event, send_updates))
        return f"new-{event.id}"

    def apply_exception(
        self,
        calendar_id: str,
        new_master_id: str,
        exception: CalendarEvent,
        *,
        send_updates: str,
    ) -> None:
        if exception.id in self.fail_ids:
            raise RuntimeError(f"boom:{exception.id}")
        self.applied.append((calendar_id, new_master_id, exception, send_updates))


class RecordingReporter:
    """MigrationReporter fake recording every callback."""

    def __init__(self) -> None:
        self.created: list[CalendarEvent] = []
        self.skipped: list[CalendarEvent] = []
        self.failed: list[tuple[CalendarEvent, Exception]] = []

    def event_created(self, event: CalendarEvent) -> None:
        self.created.append(event)

    def event_skipped(self, event: CalendarEvent) -> None:
        self.skipped.append(event)

    def event_failed(self, event: CalendarEvent, error: Exception) -> None:
        self.failed.append((event, error))


@pytest.fixture
def make_event() -> Any:
    return build_event


@pytest.fixture
def make_reader() -> type[FakeReader]:
    return FakeReader


@pytest.fixture
def make_writer() -> type[FakeWriter]:
    return FakeWriter


@pytest.fixture
def reporter() -> RecordingReporter:
    return RecordingReporter()


@pytest.fixture
def migrate() -> Any:
    """Wire reader/writer/reporter around the use case and run one migration."""
    from calmove.application.migrate_calendar import MigrateCalendarUseCase, MigrationRequest

    def _migrate(
        events: list[CalendarEvent],
        *,
        migrated: set[str] | tuple[str, ...] = (),
        fail_ids: set[str] | tuple[str, ...] = (),
        **request_overrides: Any,
    ) -> tuple[Any, FakeWriter, RecordingReporter]:
        reader = FakeReader(events=events)
        writer = FakeWriter(migrated=migrated, fail_ids=fail_ids)
        rec = RecordingReporter()
        request = MigrationRequest(
            source_calendar_id="src-cal",
            destination_calendar_id="dst-cal",
            date_range=DateRange.everything(),
            **request_overrides,
        )
        use_case = MigrateCalendarUseCase(reader=reader, writer=writer, reporter=rec)
        result = use_case.execute(request)
        return result, writer, rec

    return _migrate
