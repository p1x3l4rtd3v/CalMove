"""RAM-only, TTL-evicted per-browser-session state.

Privacy contract (Constitution art. 6): everything in here — OAuth-backed gateways,
calendar lists, migration results — lives exclusively in process memory, keyed by an
unguessable session ID. Nothing is ever written to disk, a database, or logs, and no
code path exposes one session to another.
"""

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from calmove.application.migrate_calendar import MigrationResult
from calmove.application.ports import CalendarReader, CalendarWriter
from calmove.domain.date_range import DateRange
from calmove.domain.model import Calendar, CalendarEvent

DEFAULT_TTL_SECONDS = 30 * 60


@dataclass
class WebSession:
    """Mutable wizard state for one browser session (presentation-layer only)."""

    id: str
    csrf_token: str
    deadline: float
    oauth_role: str | None = None
    oauth_state: str | None = None
    oauth_verifier: str | None = None
    source: CalendarReader | None = None
    destination: CalendarWriter | None = None
    source_calendars: list[Calendar] = field(default_factory=list)
    destination_calendars: list[Calendar] = field(default_factory=list)
    source_calendar: Calendar | None = None
    destination_calendar: Calendar | None = None
    date_range: DateRange | None = None
    plan: MigrationResult | None = None
    status: str = "setup"  # setup | running | done | failed
    progress_created: int = 0
    progress_skipped: int = 0
    progress_failed: list[tuple[str, str]] = field(default_factory=list)
    report: MigrationResult | None = None
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class SessionReporter:
    """MigrationReporter sink updating session counters under the session lock."""

    def __init__(self, session: WebSession) -> None:
        self._session = session

    def event_created(self, event: CalendarEvent) -> None:
        with self._session.lock:
            self._session.progress_created += 1

    def event_skipped(self, event: CalendarEvent) -> None:
        with self._session.lock:
            self._session.progress_skipped += 1

    def event_failed(self, event: CalendarEvent, error: Exception) -> None:
        with self._session.lock:
            self._session.progress_failed.append((event.id, str(error)))


class SessionStore:
    """Thread-safe in-memory store with lazy TTL eviction; no persistence anywhere."""

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._sessions: dict[str, WebSession] = {}
        self._lock = threading.Lock()

    def create(self) -> WebSession:
        session = WebSession(
            id=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            deadline=self._clock() + self._ttl,
        )
        with self._lock:
            self._evict(self._clock())
            self._sessions[session.id] = session
        return session

    def get(self, session_id: str | None) -> WebSession | None:
        """Return the live session for ``session_id`` and extend its deadline."""
        if not session_id:
            return None
        now = self._clock()
        with self._lock:
            self._evict(now)
            session = self._sessions.get(session_id)
            if session is not None:
                session.deadline = now + self._ttl
            return session

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _evict(self, now: float) -> None:
        expired = [sid for sid, s in self._sessions.items() if s.deadline <= now]
        for sid in expired:
            del self._sessions[sid]
