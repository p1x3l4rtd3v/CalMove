"""Thin gateway over the Google Calendar API, satisfying the reader/writer ports."""

from datetime import UTC, date, datetime
from typing import Any

from calmove.domain.date_range import DateRange
from calmove.domain.errors import MigrationError
from calmove.domain.model import Calendar, CalendarEvent, EventTime
from calmove.infrastructure.google.mapping import (
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

    def migrated_source_ids(self, calendar_id: str) -> set[str]:
        page_token: str | None = None
        collected: set[str] = set()
        while True:
            response = (
                self._service.events()
                .list(
                    calendarId=calendar_id,
                    singleEvents=False,
                    maxResults=_PAGE_SIZE,
                    pageToken=page_token,
                )
                .execute()
            )
            for payload in response.get("items", []):
                private = (payload.get("extendedProperties") or {}).get("private") or {}
                source_id = private.get(SOURCE_ID_KEY)
                if source_id:
                    collected.add(source_id)
            page_token = response.get("nextPageToken")
            if not page_token:
                return collected

    def create_event(self, calendar_id: str, event: CalendarEvent, *, send_updates: str) -> str:
        body = event_to_api(event, source_id=event.id, request_meet=True)
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
