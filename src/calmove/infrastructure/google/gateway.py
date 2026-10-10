"""Thin gateway over the Google Calendar API, satisfying the reader/writer ports."""

from datetime import UTC, date, datetime
from typing import Any

from calmove.application.ports import MigratedEvent
from calmove.domain.date_range import DateRange
from calmove.domain.errors import MigrationError
from calmove.domain.model import Calendar, CalendarEvent, EventTime
from calmove.infrastructure.google.mapping import (
    FINGERPRINT_KEY,
    SOURCE_ID_KEY,
    calendar_from_api,
    event_from_api,
    event_to_api,
)

_PAGE_SIZE = 250


def _rfc3339(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.isoformat()


class GoogleCalendarGateway:
    """Implements both CalendarReader and CalendarWriter against one service."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def list_calendars(self) -> list[Calendar]:
        calendars: list[Calendar] = []
        page_token: str | None = None
        while True:
            response = (
                self._service.calendarList()
                .list(maxResults=_PAGE_SIZE, pageToken=page_token)
                .execute()
            )
            calendars.extend(calendar_from_api(item) for item in response.get("items", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return calendars

    def fetch_events(self, calendar_id: str, date_range: DateRange) -> list[CalendarEvent]:
        params: dict[str, Any] = {
            "calendarId": calendar_id,
            "singleEvents": False,
            "maxResults": _PAGE_SIZE,
        }
        if date_range.time_min is not None:
            params["timeMin"] = _rfc3339(date_range.time_min)
        if date_range.time_max is not None:
            params["timeMax"] = _rfc3339(date_range.time_max)
        events: list[CalendarEvent] = []
        page_token: str | None = None
        while True:
            response = self._service.events().list(pageToken=page_token, **params).execute()
            for payload in response.get("items", []):
                if payload.get("status") == "cancelled" and not payload.get("recurringEventId"):
                    continue
                events.append(event_from_api(payload))
            page_token = response.get("nextPageToken")
            if not page_token:
                return events

    def migrated_events(self, calendar_id: str, date_range: DateRange) -> dict[str, MigratedEvent]:
        """Destination events carrying CalMove's marker, keyed by their source event id.

        Fetched with the same window and ``singleEvents=False`` as the source fetch, so
        skip/update/delete decisions stay symmetric to what was read from the source.
        Exception instances (``recurringEventId``) are excluded: they are patched via
        their master, never tracked as independent events.
        """
        params: dict[str, Any] = {
            "calendarId": calendar_id,
            "singleEvents": False,
            "maxResults": _PAGE_SIZE,
        }
        if date_range.time_min is not None:
            params["timeMin"] = _rfc3339(date_range.time_min)
        if date_range.time_max is not None:
            params["timeMax"] = _rfc3339(date_range.time_max)
        collected: dict[str, MigratedEvent] = {}
        page_token: str | None = None
        while True:
            response = self._service.events().list(pageToken=page_token, **params).execute()
            for payload in response.get("items", []):
                if payload.get("recurringEventId"):
                    continue
                private = (payload.get("extendedProperties") or {}).get("private") or {}
                source_id = private.get(SOURCE_ID_KEY)
                if source_id:
                    collected[source_id] = MigratedEvent(
                        destination_id=str(payload["id"]),
                        fingerprint=private.get(FINGERPRINT_KEY),
                    )
            page_token = response.get("nextPageToken")
            if not page_token:
                return collected

    def create_event(
        self, calendar_id: str, event: CalendarEvent, *, fingerprint: str, send_updates: str
    ) -> str:
        body = event_to_api(event, source_id=event.id, fingerprint=fingerprint, request_meet=True)
        created = (
            self._service.events()
            .insert(
                calendarId=calendar_id,
                body=body,
                conferenceDataVersion=1,
                sendUpdates=send_updates,
            )
            .execute()
        )
        return str(created["id"])

    def update_event(
        self,
        calendar_id: str,
        destination_id: str,
        event: CalendarEvent,
        *,
        fingerprint: str,
        send_updates: str,
    ) -> None:
        """Patch the full migrated field set onto the existing destination event.

        ``patch`` (merge) keeps destination-only state — notably the fresh Meet
        conference created on first migration — while attendees/recurrence are always
        sent (empty lists clear fields removed at the source).
        """
        body = event_to_api(event, source_id=event.id, fingerprint=fingerprint)
        body.setdefault("attendees", [])
        body.setdefault("recurrence", [])
        self._service.events().patch(
            calendarId=calendar_id,
            eventId=destination_id,
            body=body,
            conferenceDataVersion=1,
            sendUpdates=send_updates,
        ).execute()

    def delete_event(self, calendar_id: str, destination_id: str, *, send_updates: str) -> None:
        self._service.events().delete(
            calendarId=calendar_id,
            eventId=destination_id,
            sendUpdates=send_updates,
        ).execute()

    def forward_invitation(
        self, calendar_id: str, event: CalendarEvent, email: str, *, send_updates: str
    ) -> None:
        """Append ``email`` to the guest list of the user's copy of an invited event.

        The API equivalent of "Add guests": patches only the attendee list on the
        user's own calendar; Google propagates the new guest to the organizer's
        event. Fails (HTTP 403) when the organizer disabled ``guestsCanInviteOthers``.
        """
        attendees = [{"email": a.email} for a in event.attendees]
        attendees.append({"email": email})
        self._service.events().patch(
            calendarId=calendar_id,
            eventId=event.id,
            body={"attendees": attendees},
            sendUpdates=send_updates,
        ).execute()

    def respond_to_invitation(self, event_id: str, email: str, response_status: str) -> None:
        """Set the new account's RSVP on its own copy of a freshly forwarded event.

        ``attendeesOmitted=True`` restricts the patch to the listed attendee — the
        documented way for an attendee to change only their own response.
        """
        self._service.events().patch(
            calendarId="primary",
            eventId=event_id,
            body={"attendees": [{"email": email, "responseStatus": response_status}]},
            attendeesOmitted=True,
            sendUpdates="none",
        ).execute()

    def apply_exception(
        self,
        calendar_id: str,
        new_master_id: str,
        exception: CalendarEvent,
        *,
        send_updates: str,
    ) -> None:
        instance_id = self._find_instance_id(calendar_id, new_master_id, exception)
        if exception.status == "cancelled":
            body: dict[str, Any] = {"status": "cancelled"}
        else:
            body = event_to_api(exception, source_id=exception.id)
        self._service.events().patch(
            calendarId=calendar_id,
            eventId=instance_id,
            body=body,
            conferenceDataVersion=1,
            sendUpdates=send_updates,
        ).execute()

    def _find_instance_id(
        self, calendar_id: str, new_master_id: str, exception: CalendarEvent
    ) -> str:
        target = exception.original_start
        if target is None:
            raise MigrationError(f"exception {exception.id} has no original start time")
        page_token: str | None = None
        while True:
            response = (
                self._service.events()
                .instances(
                    calendarId=calendar_id,
                    eventId=new_master_id,
                    maxResults=_PAGE_SIZE,
                    pageToken=page_token,
                )
                .execute()
            )
            for payload in response.get("items", []):
                start = payload.get("originalStartTime") or payload.get("start") or {}
                if _matches(target, start):
                    return str(payload["id"])
            page_token = response.get("nextPageToken")
            if not page_token:
                raise MigrationError(
                    f"no instance of master {new_master_id} matches the original start "
                    f"of exception {exception.id}"
                )


def _matches(target: EventTime, start_payload: dict[str, Any]) -> bool:
    raw_date = start_payload.get("date")
    if target.date is not None:
        return raw_date is not None and date.fromisoformat(raw_date) == target.date
    raw_date_time = start_payload.get("dateTime")
    if target.date_time is None or raw_date_time is None:
        return False
    candidate = datetime.fromisoformat(raw_date_time)
    wanted = target.date_time
    if candidate.tzinfo is None or wanted.tzinfo is None:
        return candidate.replace(tzinfo=None) == wanted.replace(tzinfo=None)
    return candidate.astimezone(UTC) == wanted.astimezone(UTC)
