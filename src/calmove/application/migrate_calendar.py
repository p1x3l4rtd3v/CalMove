"""The calendar migration use case — pure orchestration over the ports."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from calmove.application.ports import CalendarReader, CalendarWriter, MigrationReporter
from calmove.domain.date_range import DateRange
from calmove.domain.errors import MigrationError
from calmove.domain.model import Attendee, CalendarEvent

_MASTER_NOT_MIGRATED = "master not migrated"


@dataclass(frozen=True, slots=True, kw_only=True)
class MigrationRequest:
    """All inputs of a migration run."""

    source_calendar_id: str
    destination_calendar_id: str
    date_range: DateRange
    dry_run: bool = False
    send_updates: str = "none"
    room_map: Mapping[str, str] = field(default_factory=dict)


@dataclass
class MigrationResult:
    """Mutable tally of a migration run."""

    created: int = 0
    skipped: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    unmapped_rooms: set[str] = field(default_factory=set)


class MigrateCalendarUseCase:
    """Re-creates source events in the destination calendar, idempotently."""

    def __init__(
        self,
        reader: CalendarReader,
        writer: CalendarWriter,
        reporter: MigrationReporter,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._reporter = reporter

    def execute(self, request: MigrationRequest) -> MigrationResult:
        result = MigrationResult()
        events = self._reader.fetch_events(request.source_calendar_id, request.date_range)

        exceptions_by_master: dict[str, list[CalendarEvent]] = {}
        top_level: list[CalendarEvent] = []
        for event in events:
            if event.recurring_event_id is not None:
                exceptions_by_master.setdefault(event.recurring_event_id, []).append(event)
            else:
                top_level.append(event)

        migrated = self._writer.migrated_source_ids(request.destination_calendar_id)
        top_level_ids = {event.id for event in top_level}

        for event in top_level:
            exceptions = exceptions_by_master.get(event.id, [])
            if event.id in migrated:
                result.skipped += 1
                self._reporter.event_skipped(event)
                for exception in exceptions:
                    result.skipped += 1
                    self._reporter.event_skipped(exception)
                continue

            mapped_event = self._map_rooms(event, request, result)
            mapped_exceptions = [self._map_rooms(exc, request, result) for exc in exceptions]

            if request.dry_run:
                result.created += 1
                continue

            try:
                new_id = self._writer.create_event(
                    request.destination_calendar_id, mapped_event, send_updates=request.send_updates
                )
            except Exception as error:
                result.failed.append((event.id, str(error)))
                self._reporter.event_failed(event, error)
                for exception in exceptions:
                    master_error = MigrationError(_MASTER_NOT_MIGRATED)
                    result.failed.append((exception.id, _MASTER_NOT_MIGRATED))
                    self._reporter.event_failed(exception, master_error)
                continue

            result.created += 1
            self._reporter.event_created(event)

            for exception, mapped_exception in zip(exceptions, mapped_exceptions, strict=True):
                try:
                    self._writer.apply_exception(
                        request.destination_calendar_id,
                        new_id,
                        mapped_exception,
                        send_updates=request.send_updates,
                    )
                except Exception as error:
                    result.failed.append((exception.id, str(error)))
                    self._reporter.event_failed(exception, error)
                else:
                    result.created += 1
                    self._reporter.event_created(exception)

        for master_id, orphans in exceptions_by_master.items():
            if master_id in top_level_ids:
                continue
            for exception in orphans:
                orphan_error = MigrationError(_MASTER_NOT_MIGRATED)
                result.failed.append((exception.id, _MASTER_NOT_MIGRATED))
                self._reporter.event_failed(exception, orphan_error)

        return result

    @staticmethod
    def _map_rooms(
        event: CalendarEvent, request: MigrationRequest, result: MigrationResult
    ) -> CalendarEvent:
        if not request.room_map or not any(a.is_resource for a in event.attendees):
            return event
        attendees: list[Attendee] = []
        for attendee in event.attendees:
            if attendee.is_resource:
                new_email = request.room_map.get(attendee.email)
                if new_email is not None:
                    attendee = replace(attendee, email=new_email)
                else:
                    result.unmapped_rooms.add(attendee.email)
            attendees.append(attendee)
        return replace(event, attendees=tuple(attendees))
