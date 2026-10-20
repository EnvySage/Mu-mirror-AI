"""Trusted user/session scope used by every read repository."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ReadScope:
    """Authenticated identity and current-message boundary for one run."""

    user_id: UUID
    session_id: UUID
    user_message_id: int

    def __post_init__(self) -> None:
        if self.user_id is None or self.session_id is None or self.user_message_id is None:
            raise ValueError("ReadScope requires user_id, session_id, and user_message_id")
        if not isinstance(self.user_id, UUID):
            raise TypeError("user_id must be a UUID")
        if not isinstance(self.session_id, UUID):
            raise TypeError("session_id must be a UUID")
        if isinstance(self.user_message_id, bool) or not isinstance(self.user_message_id, int):
            raise TypeError("user_message_id must be an integer")
        if self.user_message_id < 1:
            raise ValueError("user_message_id must be positive")


def require_scope(scope: ReadScope) -> ReadScope:
    if not isinstance(scope, ReadScope):
        raise TypeError("repository access requires a ReadScope")
    return scope


def bounded_limit(value: int, *, maximum: int, default: int) -> int:
    """Reject non-positive limits and cap otherwise valid requests."""
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("limit must be an integer")
    if value < 1:
        raise ValueError("limit must be at least 1")
    if maximum < 1:
        raise ValueError("maximum must be at least 1")
    return min(value, maximum)


def reject_scope_overrides(values: Mapping[str, object]) -> None:
    """Reject scope fields in model/tool filters; trusted scope is positional."""
    forbidden = {"user_id", "session_id"}.intersection(values)
    if forbidden:
        raise ValueError(f"scope fields cannot come from filters: {', '.join(sorted(forbidden))}")
