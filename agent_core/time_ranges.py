"""Deterministic normalization of model-extracted time expressions."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_core.types import Clarification, TimeExpression, TimeWindow


_KINDS = {"none", "day", "month", "calendar_week", "calendar_month", "rolling_days"}
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")
_MAX_DAY_OFFSET = 36500
_MAX_MONTH_OFFSET = 1200


def normalize_time(
    expr: TimeExpression,
    reference_date: date,
    timezone: str,
) -> TimeWindow | Clarification:
    """Resolve a constrained time expression into a timezone-aware [start, end) window."""
    expression, error = _coerce_expression(expr)
    if error:
        return error
    if type(reference_date) is not date:
        return Clarification("invalid_reference_date", "无法确定查询日期，请重新说明时间范围。")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        return Clarification("invalid_timezone", "无法确定本地时区，请重新尝试。")

    if expression.kind not in _KINDS:
        return Clarification("invalid_kind", "无法识别时间范围，请重新说明。")
    allowed_fields = {
        "none": set(),
        "day": {"date", "offset"},
        "month": {"month", "offset"},
        "calendar_week": {"offset"},
        "calendar_month": {"offset"},
        "rolling_days": {"days", "offset"},
    }[expression.kind]
    supplied_fields = {
        name for name in ("date", "month", "offset", "days")
        if getattr(expression, name) is not None
    }
    if supplied_fields - allowed_fields:
        return Clarification("invalid_expression", "时间范围字段与类型不匹配，请重新说明。")
    if expression.kind == "none":
        return TimeWindow(None, None)

    if expression.kind == "day":
        day, error = _resolve_day(expression, reference_date)
        if error:
            return error
        try:
            return _make_window(day, day + timedelta(days=1), zone)
        except OverflowError:
            return Clarification("invalid_interval", "时间范围超出可计算日期，请重新说明。")

    if expression.kind == "month":
        month_start, error = _resolve_month(expression.month, expression.offset)
        if error:
            return error
        try:
            next_month = _shift_month(month_start, 1)
        except ValueError:
            return Clarification("invalid_interval", "时间范围超出可计算日期，请重新说明。")
        return _make_window(month_start, next_month, zone)

    if expression.kind == "calendar_week":
        offset, error = _bounded_int(expression.offset, default=0, maximum=_MAX_DAY_OFFSET // 7)
        if error:
            return error
        try:
            week_start = reference_date - timedelta(days=reference_date.weekday()) + timedelta(weeks=offset)
            return _make_window(week_start, week_start + timedelta(days=7), zone)
        except OverflowError:
            return Clarification("invalid_interval", "时间范围超出可计算日期，请重新说明。")

    if expression.kind == "calendar_month":
        offset, error = _bounded_int(expression.offset, default=0, maximum=_MAX_MONTH_OFFSET)
        if error:
            return error
        month_start = date(reference_date.year, reference_date.month, 1)
        month_start, error = _apply_month_offset(month_start, offset)
        if error:
            return error
        try:
            next_month = _shift_month(month_start, 1)
        except ValueError:
            return Clarification("invalid_interval", "时间范围超出可计算日期，请重新说明。")
        return _make_window(month_start, next_month, zone)

    if expression.kind == "rolling_days":
        days, error = _bounded_int(expression.days, default=None, maximum=_MAX_DAY_OFFSET)
        if error:
            return Clarification("invalid_interval", "天数范围无效，请重新说明要查询的时间段。")
        if days < 1:
            return Clarification("invalid_interval", "结束日期必须晚于开始日期，请重新说明时间范围。")
        offset, error = _bounded_int(expression.offset, default=0, maximum=_MAX_DAY_OFFSET)
        if error:
            return error
        try:
            end_day = reference_date + timedelta(days=offset + 1)
            start_day = end_day - timedelta(days=days)
            return _make_window(start_day, end_day, zone)
        except OverflowError:
            return Clarification("invalid_interval", "时间范围超出可计算日期，请重新说明。")

    return Clarification("invalid_kind", "无法识别时间范围，请重新说明。")


def _coerce_expression(value: TimeExpression | Mapping[str, object]) -> tuple[TimeExpression, Clarification | None]:
    if isinstance(value, TimeExpression):
        return value, None
    if not isinstance(value, Mapping):
        return TimeExpression(kind="none"), Clarification("invalid_expression", "时间条件格式无效，请重新说明。")
    allowed = {"kind", "date", "month", "offset", "days"}
    unknown = set(value) - allowed
    if unknown:
        return TimeExpression(kind="none"), Clarification("invalid_expression", "时间条件包含无法识别的字段，请重新说明。")
    kind = value.get("kind")
    if not isinstance(kind, str):
        return TimeExpression(kind="none"), Clarification("invalid_kind", "无法识别时间范围，请重新说明。")
    date_value = value.get("date")
    month_value = value.get("month")
    if date_value is not None and not isinstance(date_value, str):
        return TimeExpression(kind=kind), Clarification("invalid_date", "日期格式无效，请重新说明。")
    if month_value is not None and not isinstance(month_value, str):
        return TimeExpression(kind=kind), Clarification("invalid_month", "月份格式无效，请重新说明。")
    offset = value.get("offset")
    days = value.get("days")
    if offset is not None and (isinstance(offset, bool) or not isinstance(offset, int)):
        return TimeExpression(kind=kind), Clarification("invalid_offset", "相对时间偏移无效，请重新说明。")
    if days is not None and (isinstance(days, bool) or not isinstance(days, int)):
        return TimeExpression(kind=kind), Clarification("invalid_interval", "天数范围无效，请重新说明。")
    return TimeExpression(kind=kind, date=date_value, month=month_value, offset=offset, days=days), None


def _resolve_day(expression: TimeExpression, reference_date: date) -> tuple[date, Clarification | None]:
    if expression.date is None:
        offset, error = _bounded_int(expression.offset, default=0, maximum=_MAX_DAY_OFFSET)
        if error:
            return reference_date, error
        try:
            return reference_date + timedelta(days=offset), None
        except OverflowError:
            return reference_date, Clarification("invalid_offset", "相对时间超出可计算范围，请重新说明。")
    if not _DATE_PATTERN.fullmatch(expression.date):
        return reference_date, Clarification("invalid_date", "日期格式无效，请按年-月-日重新说明。")
    try:
        day = date.fromisoformat(expression.date)
    except ValueError:
        return reference_date, Clarification("invalid_date", "日期不存在，请重新说明。")
    offset, error = _bounded_int(expression.offset, default=0, maximum=_MAX_DAY_OFFSET)
    if error:
        return reference_date, error
    try:
        return day + timedelta(days=offset), None
    except OverflowError:
        return reference_date, Clarification("invalid_offset", "相对时间超出可计算范围，请重新说明。")


def _resolve_month(month_text: str | None, offset_value: int | None) -> tuple[date, Clarification | None]:
    if month_text is None:
        return date(1, 1, 1), Clarification("ambiguous_year", "请补充月份对应的年份。")
    if not _MONTH_PATTERN.fullmatch(month_text):
        if re.fullmatch(r"\d{1,2}", month_text):
            return date(1, 1, 1), Clarification("ambiguous_year", "请补充月份对应的年份。")
        return date(1, 1, 1), Clarification("invalid_month", "月份格式无效，请按年-月重新说明。")
    year, month = (int(part) for part in month_text.split("-"))
    if year < 1 or month < 1 or month > 12:
        return date(1, 1, 1), Clarification("invalid_month", "月份不存在，请重新说明。")
    month_start = date(year, month, 1)
    offset, error = _bounded_int(offset_value, default=0, maximum=_MAX_MONTH_OFFSET)
    if error:
        return month_start, error
    return _apply_month_offset(month_start, offset)


def _apply_month_offset(month_start: date, offset: int) -> tuple[date, Clarification | None]:
    month_index = month_start.year * 12 + month_start.month - 1 + offset
    year, month_index = divmod(month_index, 12)
    if year < 1 or year > 9999:
        return month_start, Clarification("invalid_offset", "相对月份超出可计算范围，请重新说明。")
    return date(year, month_index + 1, 1), None


def _shift_month(month_start: date, offset: int) -> date:
    month_index = month_start.year * 12 + month_start.month - 1 + offset
    year, month_index = divmod(month_index, 12)
    return date(year, month_index + 1, 1)


def _bounded_int(value: int | None, *, default: int | None, maximum: int) -> tuple[int | None, Clarification | None]:
    if value is None:
        if default is None:
            return None, Clarification("invalid_offset", "时间偏移无效，请重新说明。")
        value = default
    if isinstance(value, bool) or not isinstance(value, int) or abs(value) > maximum:
        return None, Clarification("invalid_offset", "时间偏移超出可计算范围，请重新说明。")
    return value, None


def _make_window(start: date, end: date, zone: ZoneInfo) -> TimeWindow | Clarification:
    if end <= start:
        return Clarification("invalid_interval", "结束日期必须晚于开始日期，请重新说明时间范围。")
    try:
        return TimeWindow(
            datetime.combine(start, datetime.min.time(), tzinfo=zone),
            datetime.combine(end, datetime.min.time(), tzinfo=zone),
        )
    except (OverflowError, ValueError):
        return Clarification("invalid_interval", "时间范围超出可计算日期，请重新说明。")
