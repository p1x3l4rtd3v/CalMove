"""The calendar migration use case: pure orchestration over the ports."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from calmove.application.ports import (
    CalendarReader,
    CalendarWriter,
    InvitationResponder,
    InviteForwarder,
    MigratedEvent,
    MigrationReporter,
)
from calmove.domain.date_range import DateRange
from calmove.domain.errors import MigrationError
from calmove.domain.fingerprint import event_fingerprint
from calmove.domain.model import Attendee, CalendarEvent

_MASTER_NOT_MIGRATED = "master not migrated"
_UNTRUSTED_MARKER = (
    "destination event carries a CalMove marker but was not created by this user"
    ", left untouched for safety"
)
_ANSWERED = frozenset({"accepted", "tentative", "declined"})
_PLACEHOLDER_NOTE = (
    "\n\n[CalMove copy: the organizer's invitation could not be transferred automatically."
    " Replies must go through the organizer's original invitation; ask the organizer to"
    " invite your new address.]"
)


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
    forward_address: str | None = None  # destination calendar's address (forward target)
    mirror_rsvp: bool = True  # False when the destination is a group calendar (no RSVP)
    resend_invitations: bool = False  # re-write in-sync owned events so Google re-invites


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
    placeholder_events: list[str] = field(default_factory=list)


class MigrateCalendarUseCase:
    """Synchronizes source events into the destination calendar, idempotently.

    The unit of sync is the top-level event (single or recurring series) the user
    organizes: a stored content fingerprint decides per event. Unchanged → skip,
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
        responder: InvitationResponder | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._reporter = reporter
        self._forwarder = forwarder
        self._responder = responder

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
            self._sync_owned(
                request, result, existing, event, exceptions_by_master.get(event.id, [])
            )
        for event in invited:
            self._forward(request, result, event)
            self._ensure_visible(request, result, existing, event)
        self._delete_vanished(request, result, existing, owned_ids, invited_ids)
        self._fail_orphans(result, exceptions_by_master, owned_ids | invited_ids)
        return result

    def _sync_owned(
        self,
        request: MigrationRequest,
        result: MigrationResult,
        existing: Mapping[str, MigratedEvent],
        event: CalendarEvent,
        exceptions: list[CalendarEvent],
    ) -> None:
        """Create, update, or skip one owned top-level event (plus its exceptions)."""
        prior = existing.get(event.id)
        if prior is not None and not prior.trusted:
            result.failed.append((event.id, _UNTRUSTED_MARKER))
            self._reporter.event_failed(event, MigrationError(_UNTRUSTED_MARKER))
            return
        if prior is not None and prior.duplicate_ids:
            self._delete_duplicates(request, result, prior)

        mapped_event = self._map_addresses(event, request, result)
        mapped_exceptions = [self._map_addresses(e, request, result) for e in exceptions]
        fingerprint = event_fingerprint(mapped_event, mapped_exceptions)

        # Unchanged events are skipped, unless re-send mode deliberately re-writes them
        # so Google delivers the invitations a previous silent run never sent.
        if (
            prior is not None
            and prior.fingerprint == fingerprint
            and not request.resend_invitations
        ):
            result.skipped += 1
            self._reporter.event_skipped(event)
            for exception in exceptions:
                result.skipped += 1
                self._reporter.event_skipped(exception)
            return

        if request.dry_run:
            if prior is None:
                result.created += 1 + len(exceptions)
            else:
                result.updated += 1 + len(exceptions)
            return

        creating = prior is None
        try:
            if prior is None:
                destination_id = self._writer.create_event(
                    request.destination_calendar_id,
                    mapped_event,
                    fingerprint=fingerprint,
                    send_updates=request.send_updates,
                )
            else:
                destination_id = prior.destination_id
                self._writer.update_event(
                    request.destination_calendar_id,
                    destination_id,
                    mapped_event,
                    fingerprint=fingerprint,
                    send_updates=request.send_updates,
                )
        except Exception as error:
            self._fail_series(result, event, exceptions, error)
            return

        if creating:
            self._reporter.event_created(event)
        else:
            self._reporter.event_updated(event)
        total = 1 + self._apply_exceptions(
            request, result, destination_id, exceptions, mapped_exceptions
        )
        if creating:
            result.created += total
        else:
            result.updated += total

    def _delete_duplicates(
        self, request: MigrationRequest, result: MigrationResult, prior: MigratedEvent
    ) -> None:
        """Remove surplus destination events carrying the same source-id marker."""
        if request.dry_run:
            result.deleted += len(prior.duplicate_ids)
            return
        for duplicate_id in prior.duplicate_ids:
            try:
                self._writer.delete_event(
                    request.destination_calendar_id,
                    duplicate_id,
                    send_updates=request.send_updates,
                )
            except Exception as error:
                result.failed.append((duplicate_id, str(error)))
            else:
                result.deleted += 1

    def _delete_vanished(
        self,
        request: MigrationRequest,
        result: MigrationResult,
        existing: Mapping[str, MigratedEvent],
        owned_ids: set[str],
        invited_ids: set[str],
    ) -> None:
        """Delete destination copies whose source event no longer exists.

        Placeholders of still-invited events are live and managed by
        ``_ensure_visible``; everything else carrying a marker outside the owned set
        is stale (vanished sources, pre-forwarding shadow copies) and is removed.
        """
        for source_id, prior in existing.items():
            if source_id in owned_ids:
                continue
            if prior.placeholder and source_id in invited_ids:
                continue
            if not prior.trusted:
                result.failed.append((source_id, _UNTRUSTED_MARKER))
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

    def _fail_orphans(
        self,
        result: MigrationResult,
        exceptions_by_master: Mapping[str, list[CalendarEvent]],
        known_master_ids: set[str],
    ) -> None:
        """Fail exceptions whose master was neither owned nor invited.

        Owned masters are handled in the sync loop; invited masters carry their own
        exceptions on the organizer's event, so forwarding the master covers them.
        """
        for master_id, orphans in exceptions_by_master.items():
            if master_id in known_master_ids:
                continue
            for exception in orphans:
                result.failed.append((exception.id, _MASTER_NOT_MIGRATED))
                self._reporter.event_failed(exception, MigrationError(_MASTER_NOT_MIGRATED))

    def _forward(
        self, request: MigrationRequest, result: MigrationResult, event: CalendarEvent
    ) -> None:
        """Forward an invited event to the new account instead of copying it."""
        forward_address = request.forward_address
        if forward_address is None:
            result.failed.append((event.id, "no destination account address for forwarding"))
            return
        guest = next(
            (a for a in event.attendees if a.email.lower() == forward_address.lower()), None
        )
        if guest is not None:
            result.skipped += 1
            self._reporter.event_skipped(event)
            # A prior run forwarded but may have failed to mirror the RSVP: retry while
            # the new address still shows "needsAction"; never override a manual answer.
            if (
                request.mirror_rsvp
                and not request.dry_run
                and guest.response_status == "needsAction"
            ):
                self._mirror_rsvp(request, result, event, forward_address)
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
        if request.mirror_rsvp:
            self._mirror_rsvp(request, result, event, forward_address)

    def _ensure_visible(
        self,
        request: MigrationRequest,
        result: MigrationResult,
        existing: Mapping[str, MigratedEvent],
        event: CalendarEvent,
    ) -> None:
        """Guarantee an invited event is visible in the destination calendar.

        A forward only reaches the organizer's live event for Google-to-Google
        invitations. External organizers (Microsoft 365/Exchange interop) never see
        the added guest, so the real invitation cannot materialize; a read-only
        placeholder copy keeps the new calendar complete until the organizer invites
        the new address. Placeholders are replaced by the real invitation once it
        arrives and follow the normal sync rules otherwise.
        """
        if request.dry_run:
            return
        prior = existing.get(event.id)
        if prior is not None and prior.placeholder and not prior.trusted:
            result.failed.append((event.id, _UNTRUSTED_MARKER))
            self._reporter.event_failed(event, MigrationError(_UNTRUSTED_MARKER))
            return
        try:
            visible = self._writer.has_event(request.destination_calendar_id, event.id)
        except Exception as error:
            result.failed.append((event.id, str(error)))
            self._reporter.event_failed(event, error)
            return
        prior_placeholder = prior if prior is not None and prior.placeholder else None
        if visible:
            if prior_placeholder is not None:
                # The real invitation materialized; the placeholder has served its purpose.
                try:
                    self._writer.delete_event(
                        request.destination_calendar_id,
                        prior_placeholder.destination_id,
                        send_updates="none",
                    )
                except Exception as error:
                    result.failed.append((event.id, str(error)))
                else:
                    result.deleted += 1
                    self._reporter.event_deleted(event.id)
            return
        stripped = replace(
            event,
            attendees=(),
            conference_link=None,
            description=event.description + _PLACEHOLDER_NOTE,
        )
        fingerprint = event_fingerprint(stripped)
        if prior_placeholder is not None and prior_placeholder.fingerprint == fingerprint:
            return  # placeholder already in sync; membership counting happened in _forward
        try:
            if prior_placeholder is None:
                self._writer.create_event(
                    request.destination_calendar_id,
                    stripped,
                    fingerprint=fingerprint,
                    send_updates="none",
                    placeholder=True,
                )
                result.created += 1
                self._reporter.event_created(event)
            else:
                self._writer.update_event(
                    request.destination_calendar_id,
                    prior_placeholder.destination_id,
                    stripped,
                    fingerprint=fingerprint,
                    send_updates="none",
                    placeholder=True,
                )
                result.updated += 1
                self._reporter.event_updated(event)
        except Exception as error:
            result.failed.append((event.id, str(error)))
            self._reporter.event_failed(event, error)
            return
        result.placeholder_events.append(event.id)

    def _mirror_rsvp(
        self,
        request: MigrationRequest,
        result: MigrationResult,
        event: CalendarEvent,
        forward_address: str,
    ) -> None:
        """Copy the old account's RSVP onto the new account's forwarded invite."""
        if request.source_address is None:
            return
        old = next(
            (a for a in event.attendees if a.email.lower() == request.source_address.lower()),
            None,
        )
        if old is None or old.response_status not in _ANSWERED:
            return
        if self._responder is None:
            result.failed.append((event.id, "RSVP mirror unavailable"))
            return
        try:
            self._responder.respond_to_invitation(event.id, forward_address, old.response_status)
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
            result.failed.append((exception.id, _MASTER_NOT_MIGRATED))
            self._reporter.event_failed(exception, MigrationError(_MASTER_NOT_MIGRATED))

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
