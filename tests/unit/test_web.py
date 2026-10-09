"""Web wizard tests: session store TTL/isolation and the full browser flow, offline."""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import Flask
from flask.testing import FlaskClient

from calmove.domain.date_range import DateRange
from calmove.domain.model import Calendar, CalendarEvent, EventTime
from calmove.web.main import WebConfig, create_app
from calmove.web.sessions import SessionStore, WebSession

SOURCE_CAL = Calendar(id="src-cal", summary="Team", access_role="owner", primary=True)
DEST_WRITABLE = Calendar(id="dst-cal", summary="New Team", access_role="writer")
DEST_READONLY = Calendar(id="dst-ro", summary="Read only", access_role="reader")
COOKIE = "calmove_session"


def _event(event_id: str) -> CalendarEvent:
    return CalendarEvent(
        id=event_id,
        start=EventTime(date_time=datetime(2026, 1, 5, 9, 0, tzinfo=UTC)),
        end=EventTime(date_time=datetime(2026, 1, 5, 10, 0, tzinfo=UTC)),
        summary="standup",
    )


class SourceGateway:
    """CalendarReader fake with calendars."""

    def __init__(self, events: Sequence[CalendarEvent]) -> None:
        self.events = list(events)

    def list_calendars(self) -> list[Calendar]:
        return [SOURCE_CAL]

    def fetch_events(self, calendar_id: str, date_range: DateRange) -> list[CalendarEvent]:
        return list(self.events)


class DestinationGateway:
    """CalendarWriter fake with calendars and recorded writes."""

    def __init__(self, calendars: Sequence[Calendar]) -> None:
        self.calendars = list(calendars)
        self.created: list[tuple[str, CalendarEvent, str]] = []

    def list_calendars(self) -> list[Calendar]:
        return list(self.calendars)

    def migrated_source_ids(self, calendar_id: str) -> set[str]:
        return set()

    def create_event(self, calendar_id: str, event: CalendarEvent, *, send_updates: str) -> str:
        self.created.append((calendar_id, event, send_updates))
        return f"new-{event.id}"

    def apply_exception(
        self,
        calendar_id: str,
        new_master_id: str,
        exception: CalendarEvent,
        *,
        send_updates: str,
    ) -> None:
        raise AssertionError("no exceptions in these fixtures")


class FakeOAuth:
    """Deterministic stand-in for the Google web OAuth adapter."""

    def __init__(self) -> None:
        self.counter = 0
        self.exchanged: list[tuple[tuple[str, ...], str, str, str]] = []

    def authorization_url(self, scopes: Sequence[str]) -> tuple[str, str, str]:
        self.counter += 1
        state = f"state-{self.counter}"
        return (
            f"https://accounts.google.example/auth?state={state}",
            state,
            f"verifier-{self.counter}",
        )

    def exchange(
        self, scopes: Sequence[str], *, code: str, state: str, code_verifier: str
    ) -> object:
        self.exchanged.append((tuple(scopes), code, state, code_verifier))
        return f"creds-{state}"


class Harness:
    def __init__(self, events: Sequence[CalendarEvent], dest_calendars: Sequence[Calendar]):
        self.store = SessionStore()
        self.oauth = FakeOAuth()
        self.source = SourceGateway(events)
        self.destination = DestinationGateway(dest_calendars)
        pending: list[Any] = [self.source, self.destination]
        config = WebConfig(
            client_secret=Path("/nonexistent/client_secret.json"),
            base_url="http://testserver",
        )
        self.app: Flask = create_app(
            config,
            store=self.store,
            oauth=self.oauth,  # type: ignore[arg-type] # test double for GoogleWebOAuth
            gateway_factory=lambda creds: pending.pop(0),
        )
        self.app.testing = True

    def client(self) -> FlaskClient:
        return self.app.test_client()

    def session_for(self, client: FlaskClient) -> WebSession:
        cookie = client.get_cookie(COOKIE)
        assert cookie is not None
        session = self.store.get(cookie.value)
        assert session is not None
        return session


def _harness(
    events: Sequence[CalendarEvent] | None = None,
    dest_calendars: Sequence[Calendar] | None = None,
) -> Harness:
    return Harness(
        events if events is not None else [_event("evt-1"), _event("evt-2")],
        dest_calendars if dest_calendars is not None else [DEST_WRITABLE, DEST_READONLY],
    )


def _login(client: FlaskClient, harness: Harness, role: str, code: str) -> None:
    response = client.get(f"/login/{role}")
    assert response.status_code == 302
    assert response.location.startswith("https://accounts.google.example/")
    session = harness.session_for(client)
    response = client.get(f"/oauth/callback?code={code}&state={session.oauth_state}")
    assert response.status_code == 302
    assert response.location.endswith(f"/calendars/{role}")


def _advance_to_plan(client: FlaskClient, harness: Harness) -> str:
    """Drive the wizard to the plan page; return the session's CSRF token."""
    assert client.post("/start").status_code == 302
    csrf = harness.session_for(client).csrf_token
    _login(client, harness, "source", "code-src")
    response = client.post("/calendars/source", data={"calendar_id": "src-cal", "csrf": csrf})
    assert response.status_code == 302
    _login(client, harness, "destination", "code-dst")
    response = client.post("/calendars/destination", data={"calendar_id": "dst-cal", "csrf": csrf})
    assert response.status_code == 302
    response = client.post("/range", data={"preset": "everything", "csrf": csrf})
    assert response.status_code == 302
    assert response.location.endswith("/plan")
    return csrf


def _wait_for_completion(session: WebSession, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with session.lock:
            if session.status in {"done", "failed"}:
                return
        time.sleep(0.01)
    raise AssertionError("migration thread did not finish")


class TestSessionStore:
    def test_create_and_get_roundtrip(self) -> None:
        store = SessionStore()
        session = store.create()
        assert store.get(session.id) is session
        assert store.get("unknown") is None
        assert store.get(None) is None

    def test_sessions_have_unique_unguessable_ids(self) -> None:
        store = SessionStore()
        a, b = store.create(), store.create()
        assert a.id != b.id
        assert len(a.id) >= 32
        assert a.csrf_token != b.csrf_token

    def test_expiry_evicts_session(self) -> None:
        now = [0.0]
        store = SessionStore(ttl_seconds=10, clock=lambda: now[0])
        session = store.create()
        now[0] = 11.0
        assert store.get(session.id) is None

    def test_access_slides_the_deadline(self) -> None:
        now = [0.0]
        store = SessionStore(ttl_seconds=10, clock=lambda: now[0])
        session = store.create()
        now[0] = 9.0
        assert store.get(session.id) is session
        now[0] = 18.0  # 9s after the touch, 18s after creation
        assert store.get(session.id) is session

    def test_delete_erases_state(self) -> None:
        store = SessionStore()
        session = store.create()
        store.delete(session.id)
        assert store.get(session.id) is None


class TestWizardFlow:
    def test_full_happy_path_migrates_and_erases_session(self) -> None:
        harness = _harness()
        client = harness.client()
        csrf = _advance_to_plan(client, harness)

        page = client.get("/plan")
        assert page.status_code == 200
        assert b"Would create" in page.data

        session = harness.session_for(client)
        response = client.post("/migrate", data={"confirm": "migrate", "csrf": csrf})
        assert response.status_code == 302
        _wait_for_completion(session)

        assert session.status == "done"
        assert [e.id for _, e, _ in harness.destination.created] == ["evt-1", "evt-2"]
        assert all(send == "none" for _, _, send in harness.destination.created)

        page = client.get("/progress")
        assert b"Migration finished" in page.data

        response = client.post("/finish", data={"csrf": csrf})
        assert response.status_code == 200
        assert harness.store.get(session.id) is None
        # Any further navigation falls back to the start page.
        assert client.get("/plan").status_code == 302

    def test_source_scopes_are_readonly_and_destination_writable(self) -> None:
        harness = _harness()
        client = harness.client()
        _advance_to_plan(client, harness)
        (src_scopes, *_), (dst_scopes, *_) = harness.oauth.exchanged
        assert src_scopes == ("https://www.googleapis.com/auth/calendar.readonly",)
        assert "https://www.googleapis.com/auth/calendar.events" in dst_scopes

    def test_readonly_destination_calendar_is_rejected(self) -> None:
        harness = _harness()
        client = harness.client()
        assert client.post("/start").status_code == 302
        csrf = harness.session_for(client).csrf_token
        _login(client, harness, "source", "c1")
        client.post("/calendars/source", data={"calendar_id": "src-cal", "csrf": csrf})
        _login(client, harness, "destination", "c2")
        response = client.post(
            "/calendars/destination", data={"calendar_id": "dst-ro", "csrf": csrf}
        )
        assert response.status_code == 200
        assert b"needs &#39;writer&#39; or &#39;owner&#39;" in response.data
        assert harness.session_for(client).destination_calendar is None

    def test_wrong_confirmation_word_writes_nothing(self) -> None:
        harness = _harness()
        client = harness.client()
        csrf = _advance_to_plan(client, harness)
        response = client.post("/migrate", data={"confirm": "yes please", "csrf": csrf})
        assert response.status_code == 400
        assert harness.destination.created == []
        assert harness.session_for(client).status == "setup"


class TestSecurityBoundaries:
    def test_oauth_state_mismatch_is_rejected(self) -> None:
        harness = _harness()
        client = harness.client()
        client.post("/start")
        client.get("/login/source")
        response = client.get("/oauth/callback?code=stolen&state=forged-state")
        assert response.status_code == 400
        assert harness.oauth.exchanged == []

    def test_post_without_csrf_token_is_rejected(self) -> None:
        harness = _harness()
        client = harness.client()
        client.post("/start")
        _login(client, harness, "source", "c1")
        response = client.post("/calendars/source", data={"calendar_id": "src-cal"})
        assert response.status_code == 400
        assert harness.session_for(client).source_calendar is None

    def test_no_cookie_means_no_access(self) -> None:
        harness = _harness()
        client = harness.client()
        _advance_to_plan(client, harness)
        stranger = harness.client()
        for path in ("/plan", "/progress", "/range", "/calendars/source"):
            response = stranger.get(path)
            assert response.status_code == 302
            assert response.location.endswith("/")

    def test_forged_session_id_means_no_access(self) -> None:
        harness = _harness()
        client = harness.client()
        _advance_to_plan(client, harness)
        stranger = harness.client()
        stranger.set_cookie(COOKIE, "A" * 43, domain="localhost")
        response = stranger.get("/plan")
        assert response.status_code == 302
        assert response.location.endswith("/")

    def test_session_cookie_is_httponly(self) -> None:
        harness = _harness()
        client = harness.client()
        response = client.post("/start")
        header = response.headers.get("Set-Cookie", "")
        assert "HttpOnly" in header
        assert "SameSite=Lax" in header
