"""Content fingerprint used to decide skip/update on synchronizing re-runs.

Pure and deterministic: the fingerprint is computed from the *source* event (after
room mapping) on every run and compared against the copy stored on the destination
event; destination drift (fresh Meet links, Google-side normalization) never
causes spurious updates because the destination content itself is never hashed.

The hash covers an *explicit, versioned projection* of exactly the synchronized
fields rather than ``dataclasses.asdict``: adding a field to the domain model no
longer silently changes every fingerprint (forcing a full re-update of all
previously migrated events). Evolving the projection (adding/removing a field or
changing a serialization) MUST be done deliberately by bumping the ``"v"`` key.

Attendees are serialized sorted by e-mail, so attendee *order* never affects the
fingerprint. ``is_organizer`` is deliberately excluded: it describes the viewer's
relationship to the event, not synchronized content.
"""

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from calmove.domain.model import Attendee, CalendarEvent, EventTime, Reminder

_VERSION = 1


def _time_payload(time: EventTime) -> dict[str, Any]:
    return {
        "date_time": time.date_time.isoformat() if time.date_time is not None else None,
        "date": time.date.isoformat() if time.date is not None else None,
        "time_zone": time.time_zone,
    }


def _attendee_payload(attendee: Attendee) -> dict[str, Any]:
    return {
        "email": attendee.email,
        "display_name": attendee.display_name,
        "optional": attendee.optional,
        "is_resource": attendee.is_resource,
        "response_status": attendee.response_status,
    }


def _reminder_payload(reminder: Reminder) -> dict[str, Any]:
    return {"method": reminder.method, "minutes": reminder.minutes}


def _event_payload(event: CalendarEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "summary": event.summary,
        "description": event.description,
        "location": event.location,
        "start": _time_payload(event.start),
        "end": _time_payload(event.end),
        "attendees": [_attendee_payload(a) for a in sorted(event.attendees, key=lambda a: a.email)],
        "recurrence": list(event.recurrence),
        "recurring_event_id": event.recurring_event_id,
        "original_start": (
            _time_payload(event.original_start) if event.original_start is not None else None
        ),
        "status": event.status,
        "visibility": event.visibility,
        "transparency": event.transparency,
        "reminders": [_reminder_payload(r) for r in event.reminders],
        "reminders_use_default": event.reminders_use_default,
        "conference_link": event.conference_link,
    }


def event_fingerprint(event: CalendarEvent, exceptions: Sequence[CalendarEvent] = ()) -> str:
    """SHA-256 over the versioned canonical projection of ``event`` and its ``exceptions``.

    The unit of synchronization is the top-level event: a change inside any of a
    recurring master's exceptions changes the master's fingerprint.
    """
    payload = {
        "v": _VERSION,
        "event": _event_payload(event),
        "exceptions": [_event_payload(e) for e in sorted(exceptions, key=lambda e: e.id)],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
