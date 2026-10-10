"""Content fingerprint used to decide skip/update on synchronizing re-runs.

Pure and deterministic: the fingerprint is computed from the *source* event (after
room mapping) on every run and compared against the copy stored on the destination
event — destination drift (fresh Meet links, Google-side normalization) never
causes spurious updates because the destination content itself is never hashed.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict

from calmove.domain.model import CalendarEvent


def event_fingerprint(event: CalendarEvent, exceptions: Sequence[CalendarEvent] = ()) -> str:
    """SHA-256 over a canonical serialization of ``event`` and its ``exceptions``.

    The unit of synchronization is the top-level event: a change inside any of a
    recurring master's exceptions changes the master's fingerprint.
    """
    payload = {
        "event": asdict(event),
        "exceptions": [asdict(e) for e in sorted(exceptions, key=lambda e: e.id)],
    }
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
