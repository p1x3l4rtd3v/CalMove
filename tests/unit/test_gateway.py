"""GoogleCalendarGateway behavior against a scripted fake service (pure, offline)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httplib2
import pytest
from googleapiclient.errors import HttpError

from calmove.domain.date_range import DateRange
from calmove.domain.errors import MigrationError
from calmove.domain.model import Attendee, CalendarEvent, EventTime
from calmove.infrastructure.google.gateway import GoogleCalendarGateway, _matches

VALID_FP = "a" * 64


def http_error(status: int, message: str) -> HttpError:
    resp = httplib2.Response({"status": str(status)})
    content = f'{{"error": {{"message": "{message}"}}}}'.encode()
    return HttpError(resp, content)


class _Request:
    def __init__(self, response: Any) -> None:
        self._response = response

    def execute(self) -> Any:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _Resource:
    """Dict-driven fake for one API resource: per-method response queues + call log."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self._script = script
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _invoke(self, method: str, kwargs: dict[str, Any]) -> _Request:
        self.calls.append((method, kwargs))
        queue = self._script.get(method)
        if not queue:
            raise AssertionError(f"unscripted call: {method}({kwargs})")
        return _Request(queue.pop(0))

    def list(self, **kwargs: Any) -> _Request:
        return self._invoke("list", kwargs)

    def get(self, **kwargs: Any) -> _Request:
        return self._invoke("get", kwargs)

    def insert(self, **kwargs: Any) -> _Request:
        return self._invoke("insert", kwargs)

    # The real discovery client rejects unknown query parameters with a TypeError;
    # mirror that strictness so body-vs-parameter mistakes fail here too.
    _PATCH_PARAMS = frozenset(
        {"calendarId", "eventId", "body", "sendUpdates", "conferenceDataVersion"}
    )

    def patch(self, **kwargs: Any) -> _Request:
        unexpected = set(kwargs) - self._PATCH_PARAMS
        if unexpected:
            raise TypeError(f"Got an unexpected keyword argument {sorted(unexpected)[0]}")
        return self._invoke("patch", kwargs)

    def delete(self, **kwargs: Any) -> _Request:
        return self._invoke("delete", kwargs)

    def instances(self, **kwargs: Any) -> _Request:
        return self._invoke("instances", kwargs)


class FakeService:
    def __init__(
        self,
        events_script: dict[str, list[Any]] | None = None,
        calendar_list_script: dict[str, list[Any]] | None = None,
    ) -> None:
        self.events_resource = _Resource(events_script or {})
        self.calendar_list_resource = _Resource(calendar_list_script or {})

    def events(self) -> _Resource:
        return self.events_resource

    def calendarList(self) -> _Resource:  # mirrors the Google SDK method name
        return self.calendar_list_resource


def marked(event_id: str, source_id: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": event_id,
        "start": {"dateTime": "2026-01-05T09:00:00+00:00"},
        "end": {"dateTime": "2026-01-05T10:00:00+00:00"},
        "extendedProperties": {
            "private": {"calmove-source-id": source_id, **extra.pop("private", {})}
        },
        "creator": {"self": True},
    }
    payload.update(extra)
    return payload


def build_event(event_id: str = "evt-1", **overrides: Any) -> CalendarEvent:
    defaults: dict[str, Any] = {
        "id": event_id,
        "start": EventTime(date_time=datetime(2026, 1, 5, 9, 0, tzinfo=UTC)),
        "end": EventTime(date_time=datetime(2026, 1, 5, 10, 0, tzinfo=UTC)),
        "summary": "Weekly sync",
    }
    defaults.update(overrides)
    return CalendarEvent(**defaults)


class TestFetchEvents:
    def test_paginates_and_filters_cancelled_top_level_but_keeps_cancelled_exceptions(
        self,
    ) -> None:
        page_one = {
            "items": [
                {"id": "a", "start": {}, "end": {}},
                {"id": "b", "start": {}, "end": {}, "status": "cancelled"},
            ],
            "nextPageToken": "tok",
        }
        page_two = {
            "items": [
                {
                    "id": "c",
                    "start": {},
                    "end": {},
                    "status": "cancelled",
                    "recurringEventId": "master",
                },
            ],
        }
        service = FakeService({"list": [page_one, page_two]})
        gateway = GoogleCalendarGateway(service)

        events = gateway.fetch_events("cal", DateRange.everything())

        assert [e.id for e in events] == ["a", "c"]
        assert events[1].status == "cancelled"
        tokens = [kwargs["pageToken"] for _, kwargs in service.events_resource.calls]
        assert tokens == [None, "tok"]

    def test_http_error_becomes_migration_error(self) -> None:
        service = FakeService({"list": [http_error(403, "rate limit")]})
        gateway = GoogleCalendarGateway(service)

        with pytest.raises(MigrationError, match=r"Google Calendar API error \(403\): rate limit"):
            gateway.fetch_events("cal", DateRange.everything())


class TestMigratedEvents:
    def test_paginates_and_keys_by_source_id(self) -> None:
        page_one = {
            "items": [marked("dst-1", "src-1", private={"calmove-fingerprint": VALID_FP})],
            "nextPageToken": "tok",
        }
        page_two = {"items": [marked("dst-2", "src-2")]}
        service = FakeService({"list": [page_one, page_two]})
        gateway = GoogleCalendarGateway(service)

        migrated = gateway.migrated_events("cal", DateRange.everything())

        assert migrated["src-1"].destination_id == "dst-1"
        assert migrated["src-1"].fingerprint == VALID_FP
        assert migrated["src-1"].trusted is True
        assert migrated["src-2"].fingerprint is None
        assert migrated["src-2"].trusted is True  # None fingerprint is well-formed

    def test_skips_exception_instances_and_unmarked_events(self) -> None:
        page = {
            "items": [
                {"id": "plain", "start": {}, "end": {}},
                marked("dst-exc", "src-exc", recurringEventId="master"),
                marked("dst-1", "src-1"),
            ],
        }
        gateway = GoogleCalendarGateway(FakeService({"list": [page]}))

        migrated = gateway.migrated_events("cal", DateRange.everything())

        assert set(migrated) == {"src-1"}

    def test_untrusted_without_creator_self(self) -> None:
        payload = marked("dst-1", "src-1", private={"calmove-fingerprint": VALID_FP})
        del payload["creator"]
        gateway = GoogleCalendarGateway(FakeService({"list": [{"items": [payload]}]}))

        migrated = gateway.migrated_events("cal", DateRange.everything())

        assert migrated["src-1"].trusted is False

    def test_untrusted_with_malformed_fingerprint(self) -> None:
        for bad in ("short", "A" * 64, "g" * 64, VALID_FP + "aa"):
            payload = marked("dst-1", "src-1", private={"calmove-fingerprint": bad})
            gateway = GoogleCalendarGateway(FakeService({"list": [{"items": [payload]}]}))

            migrated = gateway.migrated_events("cal", DateRange.everything())

            assert migrated["src-1"].trusted is False, bad
            assert migrated["src-1"].fingerprint == bad

    def test_duplicate_markers_first_wins_and_collects_duplicate_ids(self) -> None:
        page_one = {
            "items": [
                marked("dst-1", "src-1", private={"calmove-fingerprint": VALID_FP}),
                marked("dst-2", "src-1"),
            ],
            "nextPageToken": "tok",
        }
        page_two = {"items": [marked("dst-3", "src-1")]}
        gateway = GoogleCalendarGateway(FakeService({"list": [page_one, page_two]}))

        migrated = gateway.migrated_events("cal", DateRange.everything())

        entry = migrated["src-1"]
        assert entry.destination_id == "dst-1"
        assert entry.fingerprint == VALID_FP
        assert entry.trusted is True
        assert entry.duplicate_ids == ("dst-2", "dst-3")

    def test_http_error_becomes_migration_error(self) -> None:
        gateway = GoogleCalendarGateway(FakeService({"list": [http_error(500, "boom")]}))

        with pytest.raises(MigrationError, match=r"Google Calendar API error \(500\): boom"):
            gateway.migrated_events("cal", DateRange.everything())


class TestCreateEvent:
    def test_requests_meet_and_annotates_original_link(self) -> None:
        service = FakeService({"insert": [{"id": "new-1"}]})
        gateway = GoogleCalendarGateway(service)
        event = build_event(conference_link="https://meet.google.com/old-link")

        created = gateway.create_event("cal", event, fingerprint=VALID_FP, send_updates="none")

        assert created == "new-1"
        ((_, kwargs),) = service.events_resource.calls
        body = kwargs["body"]
        assert "createRequest" in body["conferenceData"]
        assert body["description"].endswith(
            "\n\n[Migrated by CalMove - original meeting link: https://meet.google.com/old-link]"
        )
        private = body["extendedProperties"]["private"]
        assert private == {"calmove-source-id": "evt-1", "calmove-fingerprint": VALID_FP}

    def test_http_error_becomes_migration_error(self) -> None:
        gateway = GoogleCalendarGateway(FakeService({"insert": [http_error(403, "forbidden")]}))

        with pytest.raises(MigrationError, match=r"\(403\): forbidden"):
            gateway.create_event("cal", build_event(), fingerprint=VALID_FP, send_updates="none")


class TestUpdateEvent:
    def test_body_defaults_and_migration_note(self) -> None:
        service = FakeService({"patch": [{}]})
        gateway = GoogleCalendarGateway(service)
        event = build_event(conference_link="https://meet.google.com/old-link")

        gateway.update_event("cal", "dst-1", event, fingerprint=VALID_FP, send_updates="all")

        ((method, kwargs),) = service.events_resource.calls
        assert method == "patch"
        assert kwargs["eventId"] == "dst-1"
        assert kwargs["sendUpdates"] == "all"
        body = kwargs["body"]
        assert body["attendees"] == []
        assert body["recurrence"] == []
        # The note is re-applied on update; the fresh Meet conference is never requested.
        assert "conferenceData" not in body
        assert body["description"].endswith(
            "\n\n[Migrated by CalMove - original meeting link: https://meet.google.com/old-link]"
        )


class TestForwardInvitation:
    def test_refetches_preserves_attendee_fields_and_appends(self) -> None:
        existing = [
            {
                "email": "boss@old.example",
                "displayName": "Boss",
                "responseStatus": "accepted",
                "organizer": True,
            },
            {"email": "room@old.example", "resource": True, "responseStatus": "accepted"},
            {"email": "me@old.example", "optional": True, "responseStatus": "tentative"},
        ]
        service = FakeService({"get": [{"id": "evt-1", "attendees": existing}], "patch": [{}]})
        gateway = GoogleCalendarGateway(service)
        # Stale domain attendees would clobber responseStatus if used; they must be ignored.
        event = build_event(attendees=(Attendee(email="boss@old.example"),))

        gateway.forward_invitation("cal", event, "me@new.example", send_updates="all")

        get_call, patch_call = service.events_resource.calls
        assert get_call == ("get", {"calendarId": "cal", "eventId": "evt-1"})
        method, kwargs = patch_call
        assert method == "patch"
        assert kwargs["body"] == {"attendees": [*existing, {"email": "me@new.example"}]}
        assert kwargs["sendUpdates"] == "all"

    def test_skips_when_email_already_present_case_insensitively(self) -> None:
        service = FakeService(
            {"get": [{"id": "evt-1", "attendees": [{"email": "Me@New.example"}]}]}
        )
        gateway = GoogleCalendarGateway(service)

        gateway.forward_invitation("cal", build_event(), "me@new.example", send_updates="all")

        assert [m for m, _ in service.events_resource.calls] == ["get"]

    def test_http_error_becomes_migration_error(self) -> None:
        gateway = GoogleCalendarGateway(FakeService({"get": [http_error(404, "gone")]}))

        with pytest.raises(MigrationError, match=r"\(404\): gone"):
            gateway.forward_invitation("cal", build_event(), "x@new.example", send_updates="all")


class TestMatches:
    def test_naive_target_vs_aware_candidate_compares_wall_clock(self) -> None:
        target = EventTime(date_time=datetime(2026, 1, 5, 9, 0))
        assert _matches(target, {"dateTime": "2026-01-05T09:00:00+01:00"})

    def test_aware_target_vs_naive_candidate_compares_wall_clock(self) -> None:
        target = EventTime(date_time=datetime(2026, 1, 5, 9, 0, tzinfo=UTC))
        assert _matches(target, {"dateTime": "2026-01-05T09:00:00"})

    def test_aware_both_compares_absolute_instant(self) -> None:
        target = EventTime(date_time=datetime(2026, 1, 5, 9, 0, tzinfo=UTC))
        assert _matches(target, {"dateTime": "2026-01-05T10:00:00+01:00"})
        assert not _matches(target, {"dateTime": "2026-01-05T09:00:00+01:00"})

    def test_all_day_matches_on_date(self) -> None:
        target = EventTime(date=datetime(2026, 1, 5).date())
        assert _matches(target, {"date": "2026-01-05"})
        assert not _matches(target, {"date": "2026-01-06"})


class TestRespondToInvitation:
    def test_attendees_omitted_travels_in_body_not_as_parameter(self) -> None:
        """Regression: attendeesOmitted as a query parameter raised TypeError in production."""
        service = FakeService(events_script={"patch": [{}]})
        GoogleCalendarGateway(service).respond_to_invitation("evt-1", "me@new.example", "accepted")
        ((method, kwargs),) = service.events_resource.calls
        assert method == "patch"
        assert kwargs["calendarId"] == "primary"
        assert kwargs["body"] == {
            "attendees": [{"email": "me@new.example", "responseStatus": "accepted"}],
            "attendeesOmitted": True,
        }
        assert "attendeesOmitted" not in kwargs
