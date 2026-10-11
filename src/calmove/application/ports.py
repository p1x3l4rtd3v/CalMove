"""Abstract ports the use case depends on; implemented by infrastructure/CLI."""

from dataclasses import dataclass
from typing import Protocol

from calmove.domain.date_range import DateRange
from calmove.domain.model import Calendar, CalendarEvent


@dataclass(frozen=True, slots=True, kw_only=True)
class MigratedEvent:
    """A destination event previously created by CalMove (identified by its marker)."""

    destination_id: str
    fingerprint: str | None  # None: migrated by a pre-fingerprint version → update once
    trusted: bool = True  # created by this user AND marker payload is well-formed
    duplicate_ids: tuple[str, ...] = ()  # other destination events carrying the same marker


class CalendarReader(Protocol):
    """Read access to the source workspace."""

    def list_calendars(self) -> list[Calendar]: ...

    def fetch_events(self, calendar_id: str, date_range: DateRange) -> list[CalendarEvent]: ...


class CalendarWriter(Protocol):
    """Write access to the destination workspace."""

    def list_calendars(self) -> list[Calendar]: ...

    def migrated_events(
        self, calendar_id: str, date_range: DateRange
    ) -> dict[str, MigratedEvent]: ...

    def create_event(
        self, calendar_id: str, event: CalendarEvent, *, fingerprint: str, send_updates: str
    ) -> str: ...

    def update_event(
        self,
        calendar_id: str,
        destination_id: str,
        event: CalendarEvent,
        *,
        fingerprint: str,
        send_updates: str,
    ) -> None: ...

    def delete_event(self, calendar_id: str, destination_id: str, *, send_updates: str) -> None: ...

    def apply_exception(
        self,
        calendar_id: str,
        new_master_id: str,
        exception: CalendarEvent,
        *,
        send_updates: str,
    ) -> None: ...


class InvitationResponder(Protocol):
    """Answers invitations on behalf of the *destination* account."""

    def respond_to_invitation(self, event_id: str, email: str, response_status: str) -> None: ...


class InviteForwarder(Protocol):
    """Forwards invitations on the *source* workspace (the one write it may perform)."""

    def forward_invitation(
        self, calendar_id: str, event: CalendarEvent, email: str, *, send_updates: str
    ) -> None: ...


class MigrationReporter(Protocol):
    """Progress sink for the migration run."""

    def event_created(self, event: CalendarEvent) -> None: ...

    def event_forwarded(self, event: CalendarEvent) -> None: ...

    def event_updated(self, event: CalendarEvent) -> None: ...

    def event_deleted(self, source_id: str) -> None: ...

    def event_skipped(self, event: CalendarEvent) -> None: ...

    def event_failed(self, event: CalendarEvent, error: Exception) -> None: ...
