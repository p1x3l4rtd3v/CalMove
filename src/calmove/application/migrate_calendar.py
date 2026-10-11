"""The calendar migration use case — pure orchestration over the ports."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from calmove.application.ports import (
    CalendarReader,
    CalendarWriter,
    InviteForwarder,
    MigrationReporter,
)
from calmove.domain.date_range import DateRange
from calmove.domain.errors import MigrationError
from calmove.domain.fingerprint import event_fingerprint
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
    attendee_map: Mapping[str, str] = field(default_factory=dict)  # explicit old → new address
    domain_map: Mapping[str, str] = field(default_factory=dict)  # old domain → new domain
    source_address: str | None = None  # old account's primary e-mail (RSVP mirroring)
    forward_address: str | None = None  # new account's primary e-mail (forward target)


@dataclass
class MigrationResult:
    """Mutable tally of a migration run."""

    created: int = 0
    updated: int = 0
    deleted: int = 0
    forwarded: int = 0
    skipped: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    unmapped_rooms: set[str] = field(default_factory=set)


class MigrateCalendarUseCase:
    """Synchronizes source events into the destination calendar, idempotently.

    The unit of sync is the top-level event (single or recurring series) the user
    organizes: a stored content fingerprint decides per event — unchanged → skip,
    changed → update in place (exceptions re-applied), new → create, gone from the
    source → delete the destination copy carrying CalMove's marker. Events the user
    is merely invited to are forwarded to the new account instead of copied.
    """

    def __init__(
        self,
        reader: CalendarReader,
        writer: CalendarWriter,
        reporter: MigrationReporter,
        forwarder: InviteForwarder | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._reporter = reporter
        self._forwarder = forwarder

    def execute(self, request: MigrationRequest) -> MigrationResult:
        result = MigrationResult()
        events = self._reader.fetch_events(request.source_calendar_id, request.date_range)

        exceptions_by_master: dict[str, list[CalendarEvent]] = {}
        owned: list[CalendarEvent] = []
        invited: list[CalendarEvent] = []
        for event in events:
            if event.recurring_event_id is not None:
                exceptions_by_master.setdefault(event.recurring_event_id, []).append(event)
            elif event.is_organizer:
                owned.append(event)
            else:
                invited.append(event)

        existing = self._writer.migrated_events(request.destination_calendar_id, request.date_range)
        owned_ids = {event.id for event in owned}
        invited_ids = {event.id for event in invited}

        for event in owned:
            exceptions = exceptions_by_master.get(event.id, [])
            mapped_event = self._map_addresses(event, request, result)
            mapped_exceptions = [self._map_addresses(e, request, result) for e in exceptions]
            fingerprint = event_fingerprint(mapped_event, mapped_exceptions)
            prior = existing.get(event.id)

            if prior is not None and prior.fingerprint == fingerprint:
                result.skipped += 1
                self._reporter.event_skipped(event)
                for exception in exceptions:
                    result.skipped += 1
                    self._reporter.event_skipped(exception)
                continue

            if request.dry_run:
                if prior is None:
                    result.created += 1
                else:
                    result.updated += 1
                continue

            if prior is None:
                try:
                    new_id = self._writer.create_event(
                        request.destination_calendar_id,
                        mapped_event,
                        fingerprint=fingerprint,
                        send_updates=request.send_updates,
                    )
                except Exception as error:
                    self._fail_series(result, event, exceptions, error)
                    continue
                result.created += 1
                self._reporter.event_created(event)
                result.created += self._apply_exceptions(
                    request, result, new_id, exceptions, mapped_exceptions
                )
            else:
                try:
                    self._writer.update_event(
                        request.destination_calendar_id,
                        prior.destination_id,
                        mapped_event,
                        fingerprint=fingerprint,
                        send_updates=request.send_updates,
                    )
                except Exception as error:
                    self._fail_series(result, event, exceptions, error)
                    continue
                result.updated += 1
                self._reporter.event_updated(event)
                result.updated += self._apply_exceptions(
                    request, result, prior.destination_id, exceptions, mapped_exceptions
                )

        for event in invited:
            self._forward(request, result, event)
        for source_id, prior in existing.items():
            if source_id in owned_ids:
                continue
            if request.dry_run:
                result.deleted += 1
                continue
            try:
                self._writer.delete_event(
                    request.destination_calendar_id,
                    prior.destination_id,
                    send_updates=request.send_updates,
                )
            except Exception as error:
                result.failed.append((source_id, str(error)))
            else:
                result.deleted += 1
                self._reporter.event_deleted(source_id)

        for master_id, orphans in exceptions_by_master.items():
            if master_id in owned_ids or master_id in invited_ids:
                # Owned: handled above. Invited: the organizer's event carries its own
                # exceptions; forwarding the master covers them.
                continue
            for exception in orphans:
                orphan_error = MigrationError(_MASTER_NOT_MIGRATED)
                result.failed.append((exception.id, _MASTER_NOT_MIGRATED))
                self._reporter.event_failed(exception, orphan_error)

        return result

    def _forward(
        self, request: MigrationRequest, result: MigrationResult, event: CalendarEvent
    ) -> None:
        """Forward an invited event to the new account instead of copying it."""
        forward_address = request.forward_address
        if forward_address is None:
            result.failed.append((event.id, "no destination account address for forwarding"))
            return
        if any(a.email.lower() == forward_address.lower() for a in event.attendees):
            result.skipped += 1
            self._reporter.event_skipped(event)
            return
        if request.dry_run:
            result.forwarded += 1
            return
        if self._forwarder is None:
            result.failed.append((event.id, "forwarding unavailable"))
            return
        try:
            self._forwarder.forward_invitation(
                request.source_calendar_id,
                event,
                forward_address,
                send_updates=request.send_updates,
            )
        except Exception as error:
            result.failed.append((event.id, str(error)))
            self._reporter.event_failed(event, error)
            return
        result.forwarded += 1
        self._reporter.event_forwarded(event)
        self._mirror_rsvp(request, result, event, forward_address)

    def _mirror_rsvp(
        self,
        request: MigrationRequest,
        result: MigrationResult,
        event: CalendarEvent,
        forward_address: str,
    ) -> None:
        """Copy the old account's RSVP onto the new account's freshly forwarded invite."""
        if request.source_address is None:
            return
        old = next(
            (a for a in event.attendees if a.email.lower() == request.source_address.lower()),
            None,
        )
        if old is None or old.response_status not in {"accepted", "tentative", "declined"}:
            return
        try:
            self._writer.respond_to_invitation(event.id, forward_address, old.response_status)
        except Exception as error:
            result.failed.append((event.id, f"RSVP mirror failed: {error}"))

    def _apply_exceptions(
        self,
        request: MigrationRequest,
        result: MigrationResult,
        master_destination_id: str,
        exceptions: list[CalendarEvent],
        mapped_exceptions: list[CalendarEvent],
    ) -> int:
        """Patch each exception onto the destination master; return the success count."""
        succeeded = 0
        for exception, mapped_exception in zip(exceptions, mapped_exceptions, strict=True):
            try:
                self._writer.apply_exception(
                    request.destination_calendar_id,
                    master_destination_id,
                    mapped_exception,
                    send_updates=request.send_updates,
                )
            except Exception as error:
                result.failed.append((exception.id, str(error)))
                self._reporter.event_failed(exception, error)
            else:
                succeeded += 1
        return succeeded

    def _fail_series(
        self,
        result: MigrationResult,
        event: CalendarEvent,
        exceptions: list[CalendarEvent],
        error: Exception,
    ) -> None:
        result.failed.append((event.id, str(error)))
        self._reporter.event_failed(event, error)
        for exception in exceptions:
            master_error = MigrationError(_MASTER_NOT_MIGRATED)
            result.failed.append((exception.id, _MASTER_NOT_MIGRATED))
            self._reporter.event_failed(exception, master_error)

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

    @classmethod
    def _map_addresses(
        cls, event: CalendarEvent, request: MigrationRequest, result: MigrationResult
    ) -> CalendarEvent:
        """Rooms via the room map, humans via the attendee/domain maps (spec §6)."""
        return cls._map_attendees(cls._map_rooms(event, request, result), request)

    @staticmethod
    def _map_attendees(event: CalendarEvent, request: MigrationRequest) -> CalendarEvent:
        if (not request.attendee_map and not request.domain_map) or not event.attendees:
            return event
        explicit = {old.lower(): new for old, new in request.attendee_map.items()}
        domains = {old.lower(): new for old, new in request.domain_map.items()}
        attendees: list[Attendee] = []
        changed = False
        for attendee in event.attendees:
            if attendee.is_resource:  # rooms are governed by the room map only
                attendees.append(attendee)
                continue
            new_email = explicit.get(attendee.email.lower())
            if new_email is None and "@" in attendee.email:
                local, _, domain = attendee.email.rpartition("@")
                new_domain = domains.get(domain.lower())
                if new_domain is not None:
                    new_email = f"{local}@{new_domain}"
            if new_email is not None and new_email.lower() != attendee.email.lower():
                attendee = replace(attendee, email=new_email)
                changed = True
            attendees.append(attendee)
        if not changed:
            return event
        return replace(event, attendees=tuple(attendees))
