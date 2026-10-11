"""CalMove web: Flask wizard and second composition root.

Stateless by design: all per-user state (OAuth-backed gateways, calendar lists,
results) lives in the RAM-only :class:`SessionStore`. The service holds no admin or
service-account credentials; every request acts solely under the OAuth grants of the
browser session driving it (Constitution art. 6).
"""

import hmac
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue
from googleapiclient.discovery import build

from calmove.application.migrate_calendar import (
    MigrateCalendarUseCase,
    MigrationRequest,
)
from calmove.application.ports import CalendarReader, CalendarWriter
from calmove.application.reporting import NullReporter
from calmove.domain.date_range import DateRange
from calmove.domain.errors import CalMoveError, InvalidConfigError, InvalidDateRangeError
from calmove.domain.model import Calendar, primary_address
from calmove.infrastructure.config import load_address_map, parse_domain_rules
from calmove.infrastructure.google.auth import (
    DESTINATION_SCOPES,
    SOURCE_SCOPES,
    web_authorization_url,
    web_exchange_code,
)
from calmove.infrastructure.google.gateway import GoogleCalendarGateway
from calmove.web.sessions import (
    DEFAULT_TTL_SECONDS,
    SessionLimitError,
    SessionReporter,
    SessionStore,
    WebSession,
)

_COOKIE = "calmove_session"
_ROLES = ("source", "destination")
_WRITABLE_ROLES = frozenset({"writer", "owner"})


@dataclass(frozen=True, slots=True, kw_only=True)
class WebConfig:
    """Static service configuration; read once from the environment, never per user."""

    client_secret: Path
    base_url: str
    room_map: Mapping[str, str] = field(default_factory=dict)
    attendee_map: Mapping[str, str] = field(default_factory=dict)
    domain_map: Mapping[str, str] = field(default_factory=dict)
    session_ttl: float = DEFAULT_TTL_SECONDS
    host: str = "0.0.0.0"  # noqa: S104 - containerized service behind a reverse proxy
    port: int = 8080

    @classmethod
    def from_env(cls) -> "WebConfig":
        client_secret = Path(_require_env("CALMOVE_WEB_CLIENT_SECRET"))
        if not client_secret.is_file():
            raise SystemExit(f"OAuth web client secret not found: {client_secret}")
        base_url = _require_env("CALMOVE_WEB_BASE_URL").rstrip("/")
        rules = [r for r in os.environ.get("CALMOVE_WEB_DOMAIN_MAP", "").split(",") if r.strip()]
        try:
            room_map = _load_env_map("CALMOVE_WEB_ROOM_MAP", "Room map")
            attendee_map = _load_env_map("CALMOVE_WEB_ATTENDEE_MAP", "Attendee map")
            domain_map = parse_domain_rules(rules)
        except InvalidConfigError as error:
            raise SystemExit(str(error)) from error
        return cls(
            client_secret=client_secret,
            base_url=base_url,
            room_map=room_map,
            attendee_map=attendee_map,
            domain_map=domain_map,
            port=int(os.environ.get("CALMOVE_WEB_PORT", "8080")),
        )

    @property
    def redirect_uri(self) -> str:
        return f"{self.base_url}/oauth/callback"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Required environment variable {name} is not set.")
    return value


def _load_env_map(env_name: str, label: str) -> dict[str, str]:
    """Load an optional ``{old: new}`` address map from the path in ``env_name``."""
    path = os.environ.get(env_name)
    if not path:
        return {}
    return load_address_map(Path(path), label)


class GoogleWebOAuth:
    """Default OAuth adapter delegating to the web-app PKCE flow in infrastructure."""

    def __init__(self, client_secret: Path, redirect_uri: str) -> None:
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    def authorization_url(self, scopes: Sequence[str]) -> tuple[str, str, str]:
        return web_authorization_url(scopes, self._client_secret, self._redirect_uri)

    def exchange(
        self, scopes: Sequence[str], *, code: str, state: str, code_verifier: str
    ) -> object:
        return web_exchange_code(
            scopes,
            self._client_secret,
            self._redirect_uri,
            code=code,
            state=state,
            code_verifier=code_verifier,
        )


def _default_gateway_factory(credentials: object) -> GoogleCalendarGateway:
    service: Any = build("calendar", "v3", credentials=credentials)
    return GoogleCalendarGateway(service)


def create_app(
    config: WebConfig,
    *,
    store: SessionStore | None = None,
    oauth: GoogleWebOAuth | None = None,
    gateway_factory: Callable[[object], Any] | None = None,
) -> Flask:
    """Build the Flask app; ``oauth``/``gateway_factory`` are injectable for tests."""
    app = Flask(__name__)
    sessions = store if store is not None else SessionStore(ttl_seconds=config.session_ttl)
    auth = oauth if oauth is not None else GoogleWebOAuth(config.client_secret, config.redirect_uri)
    make_gateway = gateway_factory if gateway_factory is not None else _default_gateway_factory
    secure_cookie = config.base_url.startswith("https")

    def current_session() -> WebSession | None:
        return sessions.get(request.cookies.get(_COOKIE))

    def require_session() -> WebSession:
        session = current_session()
        if session is None:
            abort(redirect(url_for("index")))
        return session

    def check_csrf(session: WebSession) -> None:
        if not hmac.compare_digest(request.form.get("csrf", ""), session.csrf_token):
            abort(400, description="Invalid CSRF token.")

    def reject_cross_site() -> None:
        """Block cross-site navigations into state-changing routes (CSRF pre-session)."""
        if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            abort(403, description="Cross-site request rejected.")

    def fail(message: str, status: int = 400) -> ResponseReturnValue:
        return render_template("error.html", message=message), status

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
            "form-action 'self' https://accounts.google.com; base-uri 'none'; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        # Tiny first-party scripts: never let a stale cached copy change form behavior.
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index() -> ResponseReturnValue:
        return render_template("start.html")

    @app.post("/start")
    def start() -> ResponseReturnValue:
        reject_cross_site()
        try:
            session = sessions.create()
        except SessionLimitError:
            return fail("The service is at capacity right now; please try again later.", 503)
        response = app.make_response(redirect(url_for("login", role="source")))
        response.set_cookie(
            _COOKIE,
            session.id,
            httponly=True,
            samesite="Lax",
            secure=secure_cookie,
            path="/",
        )
        return response

    @app.get("/login/<role>")
    def login(role: str) -> ResponseReturnValue:
        if role not in _ROLES:
            abort(404)
        reject_cross_site()
        session = require_session()
        scopes = SOURCE_SCOPES if role == "source" else DESTINATION_SCOPES
        url, state, verifier = auth.authorization_url(scopes)
        session.oauth_role = role
        session.oauth_state = state
        session.oauth_verifier = verifier
        return redirect(url)

    @app.get("/oauth/callback")
    def oauth_callback() -> ResponseReturnValue:
        session = require_session()
        if request.args.get("error"):
            reason = (
                "You declined access."
                if request.args["error"] == "access_denied"
                else "Google reported an error."
            )
            return fail(f"Google sign-in was not completed. {reason}")
        code = request.args.get("code")
        state = request.args.get("state")
        role = session.oauth_role
        if (
            not code
            or not state
            or role is None
            or session.oauth_state is None
            or session.oauth_verifier is None
            or state != session.oauth_state
        ):
            abort(400, description="OAuth state mismatch.")
        scopes = SOURCE_SCOPES if role == "source" else DESTINATION_SCOPES
        try:
            credentials = auth.exchange(
                scopes, code=code, state=state, code_verifier=session.oauth_verifier
            )
        except Exception:  # oauthlib raises library-specific errors; show one friendly page
            return fail("The sign-in could not be completed. Please start over.")
        session.oauth_role = None
        session.oauth_state = None
        session.oauth_verifier = None
        gateway = make_gateway(credentials)
        try:
            calendars = list(gateway.list_calendars())
        except CalMoveError as error:
            return fail(str(error))
        if role == "source":
            session.source = gateway
            session.forwarder = gateway
            session.source_calendars = calendars
            session.source_address = primary_address(calendars)
        else:
            session.destination = gateway
            session.responder = gateway
            session.destination_calendars = calendars
            # forward target is derived from the chosen destination calendar at run time
        return redirect(url_for("pick_calendar", role=role))

    @app.route("/calendars/<role>", methods=["GET", "POST"])
    def pick_calendar(role: str) -> ResponseReturnValue:
        if role not in _ROLES:
            abort(404)
        session = require_session()
        calendars = session.source_calendars if role == "source" else session.destination_calendars
        ready = session.source is not None if role == "source" else session.destination is not None
        if not ready:
            return redirect(url_for("index"))
        if not calendars:
            return fail(f"No calendars found for the {role} account.")
        message: str | None = None
        if request.method == "POST":
            check_csrf(session)
            chosen = _find_calendar(calendars, request.form.get("calendar_id"))
            if chosen is None:
                message = "Pick one of the listed calendars."
            elif role == "destination" and chosen.access_role not in _WRITABLE_ROLES:
                message = (
                    f"'{chosen.summary or chosen.id}' has access role '{chosen.access_role}'; "
                    "the destination needs 'writer' or 'owner'."
                )
            elif role == "source":
                session.source_calendar = chosen
                return redirect(url_for("login", role="destination"))
            else:
                session.destination_calendar = chosen
                return redirect(url_for("date_range"))
        return render_template(
            "calendars.html",
            role=role,
            calendars=calendars,
            csrf=session.csrf_token,
            message=message,
        )

    @app.route("/range", methods=["GET", "POST"])
    def date_range() -> ResponseReturnValue:
        session = require_session()
        if session.destination_calendar is None or session.source_calendar is None:
            return redirect(url_for("index"))
        message: str | None = None
        if request.method == "POST":
            check_csrf(session)
            try:
                session.date_range = _parse_range(request.form)
            except InvalidDateRangeError as error:
                message = str(error)
            else:
                source, destination = _gateways(session)
                plan_request = _build_request(session, config, dry_run=True)
                try:
                    session.plan = MigrateCalendarUseCase(
                        source, destination, NullReporter()
                    ).execute(plan_request)
                except CalMoveError as error:
                    return fail(str(error))
                return redirect(url_for("plan"))
        return render_template("range.html", csrf=session.csrf_token, message=message)

    @app.get("/plan")
    def plan() -> ResponseReturnValue:
        session = require_session()
        if session.plan is None:
            return redirect(url_for("index"))
        return render_template("plan.html", plan=session.plan, csrf=session.csrf_token)

    def rerender_plan(
        session: WebSession, message: str, *, notify: bool, resend: bool
    ) -> ResponseReturnValue:
        """Validation failures keep the user on the plan with state preserved."""
        return (
            render_template(
                "plan.html",
                plan=session.plan,
                csrf=session.csrf_token,
                message=message,
                notify_checked=notify,
                resend_checked=resend,
            ),
            400,
        )

    @app.post("/migrate")
    def migrate() -> ResponseReturnValue:
        session = require_session()
        check_csrf(session)
        notify = request.form.get("notify") == "on"
        resend = request.form.get("resend") == "on"
        if request.form.get("confirm", "").strip() != "migrate":
            return rerender_plan(
                session,
                "Type the word migrate into the confirmation field to start; nothing was written.",
                notify=notify,
                resend=resend,
            )
        if resend and not notify:
            # A silent identical rewrite accomplishes nothing; the plan.js dependency
            # prevents this in the browser, the server enforces it for everyone else.
            return rerender_plan(
                session,
                "Re-sending invitations requires the invitation e-mail option; "
                "tick both checkboxes.",
                notify=notify,
                resend=resend,
            )
        with session.lock:  # atomic check-and-transition: a double submit must not double-run
            if session.plan is None or session.status != "setup":
                return redirect(url_for("index"))
            session.status = "running"
        source, destination = _gateways(session)
        run_request = _build_request(
            session,
            config,
            dry_run=False,
            send_updates="all" if notify else "none",
            resend_invitations=resend,
        )
        use_case = MigrateCalendarUseCase(
            source,
            destination,
            SessionReporter(session),
            forwarder=session.forwarder,
            responder=session.responder,
        )

        def run() -> None:
            try:
                result = use_case.execute(run_request)
            except Exception as error:  # per-run guard: the thread must record, not die
                with session.lock:
                    session.error = str(error)
                    session.status = "failed"
            else:
                with session.lock:
                    session.report = result
                    session.status = "done"

        threading.Thread(target=run, daemon=True).start()
        return redirect(url_for("progress"))

    @app.get("/progress")
    def progress() -> ResponseReturnValue:
        session = require_session()
        if session.status == "setup":
            return redirect(url_for("index"))
        with session.lock:
            report = session.report if session.status in {"done", "failed"} else None
            placeholders: list[str] = []
            if report is not None:
                created, forwarded, updated = report.created, report.forwarded, report.updated
                deleted, skipped = report.deleted, report.skipped
                failed = list(report.failed)
                placeholders = list(report.placeholder_events)
            else:
                created, forwarded = session.progress_created, session.progress_forwarded
                updated, deleted = session.progress_updated, session.progress_deleted
                skipped, failed = session.progress_skipped, list(session.progress_failed)
            return render_template(
                "progress.html",
                status=session.status,
                created=created,
                forwarded=forwarded,
                updated=updated,
                deleted=deleted,
                skipped=skipped,
                failed=failed,
                placeholders=placeholders,
                error=session.error,
                csrf=session.csrf_token,
            )

    @app.post("/finish")
    def finish() -> ResponseReturnValue:
        session = require_session()
        check_csrf(session)
        sessions.delete(session.id)
        response = app.make_response(render_template("finished.html"))
        response.delete_cookie(_COOKIE, path="/")
        return response

    return app


def _find_calendar(calendars: list[Calendar], calendar_id: str | None) -> Calendar | None:
    for calendar in calendars:
        if calendar.id == calendar_id:
            return calendar
    return None


def _parse_range(form: Mapping[str, str]) -> DateRange:
    preset = form.get("preset", "future")
    if preset == "everything":
        return DateRange.everything()
    if preset == "custom":
        start = _parse_date(form.get("start"))
        end = _parse_date(form.get("end")) + timedelta(days=1)
        return DateRange.between(start, end)
    return DateRange.future()


def _parse_date(raw: str | None) -> datetime:
    if not raw:
        raise InvalidDateRangeError("Both custom dates are required (YYYY-MM-DD).")
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        raise InvalidDateRangeError(f"Invalid date: {raw!r}; use YYYY-MM-DD.") from None


def _gateways(session: WebSession) -> tuple[CalendarReader, CalendarWriter]:
    """Both gateways, guaranteed by route order; defensive error otherwise."""
    if session.source is None or session.destination is None:
        raise RuntimeError("Session is not fully authenticated.")
    return session.source, session.destination


def _build_request(
    session: WebSession,
    config: WebConfig,
    *,
    dry_run: bool,
    send_updates: str = "none",
    resend_invitations: bool = False,
) -> MigrationRequest:
    if (
        session.source_calendar is None
        or session.destination_calendar is None
        or session.date_range is None
    ):
        raise RuntimeError("Wizard state is incomplete.")
    return MigrationRequest(
        source_calendar_id=session.source_calendar.id,
        destination_calendar_id=session.destination_calendar.id,
        date_range=session.date_range,
        dry_run=dry_run,
        send_updates=send_updates,
        room_map=config.room_map,
        attendee_map=config.attendee_map,
        domain_map=config.domain_map,
        source_address=session.source_address,
        # Forward invitations to the chosen destination calendar's address: the account
        # itself for a primary-calendar migration, the group calendar for a team one.
        forward_address=session.destination_calendar.id,
        mirror_rsvp=session.destination_calendar.primary,
        resend_invitations=resend_invitations,
    )


def main() -> None:
    """Entry point for the ``calmove-web`` script (waitress, single process)."""
    from waitress import serve

    config = WebConfig.from_env()
    app = create_app(config)
    serve(app, host=config.host, port=config.port, threads=8)


__all__ = ["GoogleWebOAuth", "WebConfig", "create_app", "main"]
