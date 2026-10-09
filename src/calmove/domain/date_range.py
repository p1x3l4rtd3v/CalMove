"""Date range selection for event fetching."""

from dataclasses import dataclass
from datetime import UTC, datetime

from calmove.domain.errors import InvalidDateRangeError


@dataclass(frozen=True, slots=True, kw_only=True)
class DateRange:
    """A half-open time window applied server-side via timeMin/timeMax."""

    time_min: datetime | None = None
    time_max: datetime | None = None

    @classmethod
    def future(cls, now: datetime | None = None) -> "DateRange":
        """From ``now`` (defaults to the current aware UTC time) onward."""
        return cls(time_min=now if now is not None else datetime.now(UTC), time_max=None)

    @classmethod
    def everything(cls) -> "DateRange":
        """The unbounded range."""
        return cls(time_min=None, time_max=None)

    @classmethod
    def between(cls, start: datetime, end: datetime) -> "DateRange":
        """An explicit window; ``end`` must lie strictly after ``start``."""
        if end <= start:
            raise InvalidDateRangeError(f"end ({end}) must be after start ({start})")
        return cls(time_min=start, time_max=end)
