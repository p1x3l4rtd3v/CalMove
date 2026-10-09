"""DateRange factory methods and validation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from calmove.domain.date_range import DateRange
from calmove.domain.errors import CalMoveError, InvalidDateRangeError


class TestFuture:
    def test_uses_given_now(self) -> None:
        now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
        r = DateRange.future(now=now)
        assert r.time_min == now
        assert r.time_max is None

    def test_defaults_to_aware_current_time(self) -> None:
        before = datetime.now(UTC)
        r = DateRange.future()
        after = datetime.now(UTC)
        assert r.time_min is not None
        assert r.time_min.tzinfo is not None
        assert before <= r.time_min <= after
        assert r.time_max is None


class TestEverything:
    def test_both_bounds_none(self) -> None:
        r = DateRange.everything()
        assert r.time_min is None
        assert r.time_max is None


class TestBetween:
    def test_happy_path(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 2, 1, tzinfo=UTC)
        r = DateRange.between(start, end)
        assert r.time_min == start
        assert r.time_max == end

    def test_raises_when_end_before_start(self) -> None:
        start = datetime(2026, 2, 1, tzinfo=UTC)
        end = datetime(2026, 1, 1, tzinfo=UTC)
        with pytest.raises(InvalidDateRangeError):
            DateRange.between(start, end)

    def test_raises_when_end_equals_start(self) -> None:
        moment = datetime(2026, 1, 1, tzinfo=UTC)
        with pytest.raises(InvalidDateRangeError):
            DateRange.between(moment, moment)


def test_invalid_date_range_error_is_calmove_error() -> None:
    assert issubclass(InvalidDateRangeError, CalMoveError)
