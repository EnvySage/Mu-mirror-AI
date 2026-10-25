from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agent_core.time_ranges import normalize_time
from agent_core.types import Clarification, TimeExpression, TimeWindow


REFERENCE_DATE = date(2026, 10, 20)
TIMEZONE = "Asia/Shanghai"
ZONE = ZoneInfo(TIMEZONE)


@pytest.mark.parametrize(
    ("expression", "start", "end"),
    [
        (TimeExpression(kind="day", offset=-1), datetime(2026, 10, 19), datetime(2026, 10, 20)),
        (TimeExpression(kind="calendar_week", offset=-1), datetime(2026, 10, 12), datetime(2026, 10, 19)),
        (TimeExpression(kind="month", month="2026-08"), datetime(2026, 8, 1), datetime(2026, 9, 1)),
        (TimeExpression(kind="rolling_days", days=7), datetime(2026, 10, 14), datetime(2026, 10, 21)),
    ],
)
def test_normalize_time_uses_half_open_local_calendar_windows(expression, start, end):
    result = normalize_time(expression, REFERENCE_DATE, TIMEZONE)

    assert result == TimeWindow(start.replace(tzinfo=ZONE), end.replace(tzinfo=ZONE))


def test_normalize_time_handles_leap_year_and_month_offset_across_year():
    leap_day = normalize_time(TimeExpression(kind="day", date="2024-02-29"), REFERENCE_DATE, TIMEZONE)
    previous_month = normalize_time(
        TimeExpression(kind="calendar_month", offset=-1), date(2026, 1, 15), TIMEZONE
    )

    assert leap_day == TimeWindow(
        datetime(2024, 2, 29, tzinfo=ZONE), datetime(2024, 3, 1, tzinfo=ZONE)
    )
    assert previous_month == TimeWindow(
        datetime(2025, 12, 1, tzinfo=ZONE), datetime(2026, 1, 1, tzinfo=ZONE)
    )


@pytest.mark.parametrize(
    ("expression", "code"),
    [
        (TimeExpression(kind="day", date="2026-02-30"), "invalid_date"),
        (TimeExpression(kind="month", month="08"), "ambiguous_year"),
        (TimeExpression(kind="rolling_days", days=-7), "invalid_interval"),
        (TimeExpression(kind="day", offset=True), "invalid_offset"),
        (TimeExpression(kind="none", date="2026-10-20"), "invalid_expression"),
        (TimeExpression(kind="unknown"), "invalid_kind"),
    ],
)
def test_invalid_or_ambiguous_time_returns_clarification(expression, code):
    result = normalize_time(expression, REFERENCE_DATE, TIMEZONE)

    assert isinstance(result, Clarification)
    assert result.code == code


def test_invalid_timezone_and_reversed_window_are_clarifications():
    bad_timezone = normalize_time(TimeExpression(kind="day", offset=0), REFERENCE_DATE, "Mars/Olympus")
    reversed_window = normalize_time(
        TimeExpression(kind="rolling_days", days=0), REFERENCE_DATE, TIMEZONE
    )

    assert isinstance(bad_timezone, Clarification)
    assert bad_timezone.code == "invalid_timezone"
    assert isinstance(reversed_window, Clarification)
    assert reversed_window.code == "invalid_interval"


def test_none_means_unbounded_window_and_timewindow_rejects_reversed_bounds():
    assert normalize_time(TimeExpression(kind="none"), REFERENCE_DATE, TIMEZONE) == TimeWindow(None, None)
    with pytest.raises(ValueError, match="start must be before end"):
        TimeWindow(datetime(2026, 10, 21, tzinfo=ZONE), datetime(2026, 10, 20, tzinfo=ZONE))
