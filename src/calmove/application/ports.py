"""Abstract ports the use case depends on; implemented by infrastructure/CLI."""

from typing import Protocol

from calmove.domain.date_range import DateRange
from calmove.domain.model import Calendar, CalendarEvent


class CalendarReader(Protocol):
    """Read access to the source workspace."""

    def list_calendars(self) -> list[Calendar]: ...

    def fetch_events(self, calendar_id: str, date_range: DateRange) -> list[CalendarEvent]: ...


class CalendarWriter(Protocol):
    """Write access to the destination workspace."""

    def list_calendars(self) -> list[Calendar]: ...

    def migrated_source_ids(self, calendar_id: str) -> set[str]: ...

    def create_event(self, calendar_id: str, event: CalendarEvent, *, send_updates: str) -> str: ...

    def apply_exception(
        self,
        calendar_id: str,
        new_master_id: str,
        exception: CalendarEvent,
        *,
        send_updates: str,
    ) -> None: ...


class MigrationReporter(Protocol):
    """Progress sink for the migration run."""

    def event_created(self, event: CalendarEvent) -> None: ...

    def event_skipped(self, event: CalendarEvent) -> None: ...

    def event_failed(self, event: CalendarEvent, error: Exception) -> None: ...
