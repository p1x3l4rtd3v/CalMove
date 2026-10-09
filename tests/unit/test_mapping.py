"""Google API dict <-> domain mapping round-trips (pure, offline)."""

from __future__ import annotations

import dataclasses
from datetime import date
from typing import Any, ClassVar

from calmove.domain.model import CalendarEvent, Reminder
from calmove.infrastructure.google.mapping import calendar_from_api, event_from_api, event_to_api


def _roundtrip(event: CalendarEvent) -> CalendarEvent:
    """Domain -> API -> domain; re-inject the id that event_to_api deliberately omits."""
    payload = event_to_api(event)
    assert "id" not in payload
    return dataclasses.replace(event_from_api({"id": event.id, **payload}), id=event.id)


TIMED_PAYLOAD: dict[str, Any] = {
    "id": "evt-timed",
    "summary": "Quarterly planning",
    "description": "Agenda in the doc",
    "location": "HQ 3.14",
    "start": {"dateTime": "2026-01-05T09:00:00+01:00", "timeZone": "Europe/Zurich"},
    "end": {"dateTime": "2026-01-05T10:00:00+01:00", "timeZone": "Europe/Zurich"},
    "status": "confirmed",
    "visibility": "private",
    "transparency": "transparent",
    "attendees": [
        {"email": "alice@old.example", "displayName": "Alice", "responseStatus": "accepted"},
        {"email": "bob@old.example", "optional": True},
        {
            "email": "room-1@resource.calendar.google.com",
            "displayName": "Room 1",
            "resource": True,
            "responseStatus": "accepted",
        },
    ],
    "reminders": {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": 10}, {"method": "email", "minutes": 60}],
    },
}


class TestCalendarFromApi:
    def test_maps_fields(self) -> None:
        calendar = calendar_from_api(
            {"id": "cal-1", "summary": "Team", "accessRole": "owner", "primary": True}
        )
        assert calendar.id == "cal-1"
        assert calendar.summary == "Team"
        assert calendar.access_role == "owner"
        assert calendar.primary is True

    def test_primary_defaults_false(self) -> None:
        calendar = calendar_from_api({"id": "cal-2", "summary": "Other", "accessRole": "reader"})
        assert calendar.primary is False


class TestTimedEvent:
    def test_from_api(self) -> None:
        event = event_from_api(TIMED_PAYLOAD)
        assert event.id == "evt-timed"
        assert event.summary == "Quarterly planning"
        assert event.location == "HQ 3.14"
        assert event.visibility == "private"
        assert event.transparency == "transparent"
        assert not event.start.all_day
        assert event.start.time_zone == "Europe/Zurich"
        assert len(event.attendees) == 3
        alice, bob, room = event.attendees
        assert (alice.email, alice.display_name, alice.response_status) == (
            "alice@old.example",
            "Alice",
            "accepted",
        )
        assert alice.is_resource is False
        assert bob.optional is True
        assert room.is_resource is True
        assert event.reminders_use_default is False
        assert event.reminders == (
            Reminder(method="popup", minutes=10),
            Reminder(method="email", minutes=60),
        )

    def test_round_trip(self) -> None:
        event = event_from_api(TIMED_PAYLOAD)
        assert _roundtrip(event) == event

    def test_to_api_emits_datetime_and_timezone(self) -> None:
        payload = event_to_api(event_from_api(TIMED_PAYLOAD))
        assert "dateTime" in payload["start"]
        assert payload["start"]["timeZone"] == "Europe/Zurich"
        assert "date" not in payload["start"]


class TestAllDayEvent:
    PAYLOAD: ClassVar[dict[str, Any]] = {
        "id": "evt-allday",
        "summary": "Holiday",
        "start": {"date": "2026-01-05"},
        "end": {"date": "2026-01-06"},
        "status": "confirmed",
        "transparency": "transparent",
    }

    def test_from_api(self) -> None:
        event = event_from_api(self.PAYLOAD)
        assert event.start.all_day
        assert event.start.date == date(2026, 1, 5)
        assert event.end.date == date(2026, 1, 6)

    def test_to_api_emits_date_only(self) -> None:
        payload = event_to_api(event_from_api(self.PAYLOAD))
        assert payload["start"] == {"date": "2026-01-05"}
        assert payload["end"] == {"date": "2026-01-06"}

    def test_round_trip(self) -> None:
        event = event_from_api(self.PAYLOAD)
        assert _roundtrip(event) == event


class TestRecurringMaster:
    RECURRENCE: ClassVar[list[str]] = [
        "RRULE:FREQ=WEEKLY;BYDAY=MO",
        "EXDATE;TZID=Europe/Zurich:20260112T090000",
    ]

    def _payload(self) -> dict[str, Any]:
        return {**TIMED_PAYLOAD, "id": "evt-master", "recurrence": list(self.RECURRENCE)}

    def test_recurrence_preserved_verbatim(self) -> None:
        event = event_from_api(self._payload())
        assert event.is_recurring_master()
        assert event.recurrence == tuple(self.RECURRENCE)
        payload = event_to_api(event)
        assert list(payload["recurrence"]) == self.RECURRENCE

    def test_round_trip(self) -> None:
        event = event_from_api(self._payload())
        assert _roundtrip(event) == event


class TestExceptionEvent:
    PAYLOAD: ClassVar[dict[str, Any]] = {
        **TIMED_PAYLOAD,
        "id": "evt-exc",
        "recurringEventId": "evt-master",
        "originalStartTime": {
            "dateTime": "2026-01-12T09:00:00+01:00",
            "timeZone": "Europe/Zurich",
        },
    }

    def test_from_api(self) -> None:
        event = event_from_api(self.PAYLOAD)
        assert event.is_exception()
        assert event.recurring_event_id == "evt-master"
        assert event.original_start is not None
        assert event.original_start.time_zone == "Europe/Zurich"

    def test_round_trip(self) -> None:
        event = event_from_api(self.PAYLOAD)
        back = _roundtrip(event)
        assert back == event
        assert back.recurring_event_id == "evt-master"
        assert back.original_start == event.original_start


class TestConferenceLink:
    def test_hangout_link_mapped(self) -> None:
        event = event_from_api({**TIMED_PAYLOAD, "hangoutLink": "https://meet.google.com/abc"})
        assert event.conference_link == "https://meet.google.com/abc"

    def test_conference_data_uri_mapped(self) -> None:
        payload = {
            **TIMED_PAYLOAD,
            "conferenceData": {
                "entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/xyz"}]
            },
        }
        assert event_from_api(payload).conference_link == "https://meet.google.com/xyz"


class TestEventToApi:
    def test_omits_id_and_organizer(self) -> None:
        source = {**TIMED_PAYLOAD, "organizer": {"email": "owner@old.example", "self": True}}
        payload = event_to_api(event_from_api(source))
        assert "id" not in payload
        assert "organizer" not in payload

    def test_stamps_source_id(self) -> None:
        payload = event_to_api(event_from_api(TIMED_PAYLOAD), source_id="evt-timed")
        assert payload["extendedProperties"]["private"]["calmove-source-id"] == "evt-timed"

    def test_no_source_id_stamp_by_default(self) -> None:
        payload = event_to_api(event_from_api(TIMED_PAYLOAD))
        private = payload.get("extendedProperties", {}).get("private", {})
        assert "calmove-source-id" not in private

    def test_request_meet_emits_create_request_and_appends_old_link(self) -> None:
        source = {**TIMED_PAYLOAD, "hangoutLink": "https://meet.google.com/old-link"}
        payload = event_to_api(event_from_api(source), request_meet=True)

        create_request = payload["conferenceData"]["createRequest"]
        assert create_request["conferenceSolutionKey"]["type"] == "hangoutsMeet"
        assert isinstance(create_request["requestId"], str)
        assert create_request["requestId"]
        assert payload["description"].startswith("Agenda in the doc")
        assert payload["description"].endswith(
            "\n\n[Migrated by CalMove — original meeting link: https://meet.google.com/old-link]"
        )

    def test_request_meet_without_conference_link_is_noop(self) -> None:
        payload = event_to_api(event_from_api(TIMED_PAYLOAD), request_meet=True)
        assert "conferenceData" not in payload
        assert payload.get("description", "") == "Agenda in the doc"

    def test_no_create_request_when_meet_not_requested(self) -> None:
        source = {**TIMED_PAYLOAD, "hangoutLink": "https://meet.google.com/old-link"}
        payload = event_to_api(event_from_api(source))
        assert "createRequest" not in payload.get("conferenceData", {})
