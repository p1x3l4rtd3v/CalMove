"""Shared fixtures: in-memory fakes for the application ports and event factories."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from calmove.application.ports import MigratedEvent
from calmove.domain.date_range import DateRange
from calmove.domain.fingerprint import event_fingerprint
from calmove.domain.model import CalendarEvent, EventTime

DEFAULT_START = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
DEFAULT_END = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)


def build_event(event_id: str = "evt-1", **overrides: Any) -> CalendarEvent:
    defaults: dict[str, Any] = {
        "id": event_id,
        "start": EventTime(date_time=DEFAULT_START),
        "end": EventTime(date_time=DEFAULT_END),
        "summary": f"Event {event_id}",
        "is_organizer": True,
    }
    defaults.update(overrides)
    return CalendarEvent(**defaults)


def synced(
    event: CalendarEvent,
    exceptions: Sequence[CalendarEvent] = (),
    *,
    fingerprint: str | None = "",
    destination_id: str | None = None,
    trusted: bool = True,
    duplicate_ids: tuple[str, ...] = (),
) -> dict[str, MigratedEvent]:
    """A ``migrated_events`` entry for ``event``.

    Default (``fingerprint=""`` sentinel): the stored fingerprint matches the event's
    current content → the use case must skip. Pass ``fingerprint="stale"`` to simulate
    a changed source event, or ``fingerprint=None`` for pre-fingerprint migrations.
    """
    stored = event_fingerprint(event, exceptions) if fingerprint == "" else fingerprint
    return {
        event.id: MigratedEvent(
            destination_id=destination_id or f"dest-{event.id}",
            fingerprint=stored,
            trusted=trusted,
            duplicate_ids=duplicate_ids,
        )
    }


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
    """CalendarWriter fake: records writes, injectable failures per event/destination id."""

    def __init__(
        self,
        migrated: Mapping[str, MigratedEvent] | None = None,
        fail_ids: set[str] | frozenset[str] | tuple[str, ...] = (),
    ) -> None:
        self.migrated = dict(migrated or {})
        self.fail_ids = set(fail_ids)
        self.created: list[tuple[str, CalendarEvent, str]] = []
        self.created_fingerprints: dict[str, str] = {}
        self.updated: list[tuple[str, str, CalendarEvent, str, str]] = []
        self.deleted: list[tuple[str, str, str]] = []
        self.applied: list[tuple[str, str, CalendarEvent, str]] = []
        self.migrated_calls = 0

    def list_calendars(self) -> list[Any]:
        return []

    def migrated_events(self, calendar_id: str, date_range: DateRange) -> dict[str, MigratedEvent]:
        self.migrated_calls += 1
        return dict(self.migrated)

    def create_event(
        self, calendar_id: str, event: CalendarEvent, *, fingerprint: str, send_updates: str
    ) -> str:
        if event.id in self.fail_ids:
            raise RuntimeError(f"boom:{event.id}")
        self.created.append((calendar_id, event, send_updates))
        self.created_fingerprints[event.id] = fingerprint
        return f"new-{event.id}"

    def update_event(
        self,
        calendar_id: str,
        destination_id: str,
        event: CalendarEvent,
        *,
        fingerprint: str,
        send_updates: str,
    ) -> None:
        if event.id in self.fail_ids:
            raise RuntimeError(f"boom:{event.id}")
        self.updated.append((calendar_id, destination_id, event, fingerprint, send_updates))

    def delete_event(self, calendar_id: str, destination_id: str, *, send_updates: str) -> None:
        if destination_id in self.fail_ids:
            raise RuntimeError(f"boom:{destination_id}")
        self.deleted.append((calendar_id, destination_id, send_updates))

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
        self.forwarded: list[CalendarEvent] = []
        self.updated: list[CalendarEvent] = []
        self.deleted: list[str] = []
        self.skipped: list[CalendarEvent] = []
        self.failed: list[tuple[CalendarEvent, Exception]] = []

    def event_created(self, event: CalendarEvent) -> None:
        self.created.append(event)

    def event_forwarded(self, event: CalendarEvent) -> None:
        self.forwarded.append(event)

    def event_updated(self, event: CalendarEvent) -> None:
        self.updated.append(event)

    def event_deleted(self, source_id: str) -> None:
        self.deleted.append(source_id)

    def event_skipped(self, event: CalendarEvent) -> None:
        self.skipped.append(event)

    def event_failed(self, event: CalendarEvent, error: Exception) -> None:
        self.failed.append((event, error))


class FakeResponder:
    """InvitationResponder fake recording responses; injectable failures per event id."""

    def __init__(self, fail_ids: set[str] | tuple[str, ...] = ()) -> None:
        self.fail_ids = set(fail_ids)
        self.responses: list[tuple[str, str, str]] = []

    def respond_to_invitation(self, event_id: str, email: str, response_status: str) -> None:
        if event_id in self.fail_ids:
            raise RuntimeError(f"boom:rsvp:{event_id}")
        self.responses.append((event_id, email, response_status))


class FakeForwarder:
    """InviteForwarder fake recording forwards; injectable failures per event id."""

    def __init__(self, fail_ids: set[str] | tuple[str, ...] = ()) -> None:
        self.fail_ids = set(fail_ids)
        self.forwarded: list[tuple[str, CalendarEvent, str, str]] = []

    def forward_invitation(
        self, calendar_id: str, event: CalendarEvent, email: str, *, send_updates: str
    ) -> None:
        if event.id in self.fail_ids:
            raise RuntimeError(f"boom:forward:{event.id}")
        self.forwarded.append((calendar_id, event, email, send_updates))


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
        migrated: Mapping[str, MigratedEvent] | None = None,
        fail_ids: set[str] | tuple[str, ...] = (),
        forwarder: FakeForwarder | None = None,
        responder: FakeResponder | None = None,
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
        use_case = MigrateCalendarUseCase(
            reader=reader, writer=writer, reporter=rec, forwarder=forwarder, responder=responder
        )
        result = use_case.execute(request)
        return result, writer, rec

    return _migrate
