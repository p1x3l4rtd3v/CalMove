"""Domain error hierarchy."""


class CalMoveError(Exception):
    """Base error for all CalMove failures."""


class InvalidDateRangeError(CalMoveError):
    """Raised when a date range's end does not lie after its start."""


class MigrationError(CalMoveError):
    """Raised when migrating an event to the destination calendar fails."""


class InvalidConfigError(CalMoveError):
    """Raised when user-supplied configuration (files or CLI rules) is malformed."""
