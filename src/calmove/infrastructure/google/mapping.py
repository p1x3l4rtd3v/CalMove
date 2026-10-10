"""Pure conversion between Google Calendar API payloads and domain objects."""

import uuid
from datetime import date, datetime
from typing import Any

from calmove.domain.model import Attendee, Calendar, CalendarEvent, EventTime, Reminder

SOURCE_ID_KEY = "calmove-source-id"
FINGERPRINT_KEY = "calmove-fingerprint"
_MIGRATION_NOTE = "\n\n[Migrated by CalMove — original meeting link: {link}]"


def calendar_from_api(payload: dict[str, Any]) -> Calendar:
    return Calendar(
        id=payload["id"],
        summary=payload.get("summary", ""),
        access_role=payload.get("accessRole", "reader"),
        primary=payload.get("primary", False),
    )


def _time_from_api(payload: dict[str, Any]) -> EventTime:
    raw_date = payload.get("date")
    raw_date_time = payload.get("dateTime")
    return EventTime(
        date_time=datetime.fromisoformat(raw_date_time) if raw_date_time else None,
        date=date.fromisoformat(raw_date) if raw_date else None,
        time_zone=payload.get("timeZone"),
    )


def _attendee_from_api(payload: dict[str, Any]) -> Attendee:
    return Attendee(
        email=payload.get("email", ""),
        display_name=payload.get("displayName"),
        optional=payload.get("optional", False),
        is_resource=payload.get("resource", False),
        response_status=payload.get("responseStatus", "needsAction"),
    )


def _conference_link_from_api(payload: dict[str, Any]) -> str | None:
    hangout_link = payload.get("hangoutLink")
    if hangout_link:
        return str(hangout_link)
    conference_data = payload.get("conferenceData") or {}
    for entry_point in conference_data.get("entryPoints", []):
        uri = entry_point.get("uri")
        if uri:
            return str(uri)
    return None


def event_from_api(payload: dict[str, Any]) -> CalendarEvent:
    reminders_payload = payload.get("reminders") or {}
    original_start = payload.get("originalStartTime")
    return CalendarEvent(
        id=payload["id"],
        start=_time_from_api(payload.get("start") or {}),
        end=_time_from_api(payload.get("end") or {}),
        summary=payload.get("summary", ""),
        description=payload.get("description", ""),
        location=payload.get("location", ""),
        attendees=tuple(_attendee_from_api(a) for a in payload.get("attendees", [])),
        recurrence=tuple(payload.get("recurrence", [])),
        recurring_event_id=payload.get("recurringEventId"),
        original_start=_time_from_api(original_start) if original_start else None,
        status=payload.get("status", "confirmed"),
        is_organizer=bool((payload.get("organizer") or {}).get("self", False)),
        visibility=payload.get("visibility", "default"),
        transparency=payload.get("transparency", "opaque"),
        reminders=tuple(
            Reminder(method=o["method"], minutes=o["minutes"])
            for o in reminders_payload.get("overrides", [])
        ),
        reminders_use_default=reminders_payload.get("useDefault", True),
        conference_link=_conference_link_from_api(payload),
    )


def _time_to_api(time: EventTime) -> dict[str, Any]:
    if time.date is not None:
        return {"date": time.date.isoformat()}
    payload: dict[str, Any] = {}
    if time.date_time is not None:
        payload["dateTime"] = time.date_time.isoformat()
    if time.time_zone is not None:
        payload["timeZone"] = time.time_zone
    return payload


def _attendee_to_api(attendee: Attendee) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "email": attendee.email,
        "responseStatus": attendee.response_status,
    }
    if attendee.display_name is not None:
        payload["displayName"] = attendee.display_name
    if attendee.optional:
        payload["optional"] = True
    if attendee.is_resource:
        payload["resource"] = True
    return payload


def event_to_api(
    event: CalendarEvent,
    *,
    source_id: str | None = None,
    fingerprint: str | None = None,
    request_meet: bool = False,
) -> dict[str, Any]:
    description = event.description
    payload: dict[str, Any] = {
        "summary": event.summary,
        "location": event.location,
        "start": _time_to_api(event.start),
        "end": _time_to_api(event.end),
        "status": event.status,
        "visibility": event.visibility,
        "transparency": event.transparency,
        "reminders": {
            "useDefault": event.reminders_use_default,
            "overrides": [{"method": r.method, "minutes": r.minutes} for r in event.reminders],
        },
    }
    if event.attendees:
        payload["attendees"] = [_attendee_to_api(a) for a in event.attendees]
    if event.recurrence:
        payload["recurrence"] = list(event.recurrence)
    if event.recurring_event_id is not None:
        payload["recurringEventId"] = event.recurring_event_id
    if event.original_start is not None:
        payload["originalStartTime"] = _time_to_api(event.original_start)
    if event.is_organizer:
        # Ignored by Google on insert; kept so from_api(to_api(e)) round-trips.
        payload["organizer"] = {"self": True}
    private: dict[str, str] = {}
    if source_id is not None:
        private[SOURCE_ID_KEY] = source_id
    if fingerprint is not None:
        private[FINGERPRINT_KEY] = fingerprint
    if private:
        payload["extendedProperties"] = {"private": private}
    if request_meet and event.conference_link is not None:
        payload["conferenceData"] = {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
        description = description + _MIGRATION_NOTE.format(link=event.conference_link)
    payload["description"] = description
    return payload
