"""Confirmed personal glossary reads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg_pool import ConnectionPool

from agent_data.pool import read_transaction
from agent_data.scope import ReadScope, bounded_limit, require_scope


@dataclass(frozen=True, slots=True)
class GlossaryTerm:
    id: int
    user_id: UUID
    term: str
    aliases: tuple[str, ...]
    description: str
    query_hit_count: int
    last_confirmed_at: datetime | None


class GlossaryRepository:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def top_confirmed(self, scope: ReadScope, limit: int = 30) -> list[GlossaryTerm]:
        scope = require_scope(scope)
        limit = bounded_limit(limit, maximum=30, default=30)
        with read_transaction(self._pool, scope) as connection:
            rows = connection.execute(
                "SELECT id, user_id, term, aliases, description, query_hit_count, last_confirmed_at "
                "FROM mirror_ai.glossary_terms WHERE user_id = %s "
                "ORDER BY query_hit_count DESC NULLS LAST, last_confirmed_at DESC NULLS LAST, id ASC "
                "LIMIT %s",
                (scope.user_id, limit),
            ).fetchall()
        return [
            GlossaryTerm(
                id=row["id"],
                user_id=row["user_id"],
                term=row["term"],
                aliases=tuple(_string_list(row["aliases"])),
                description=row["description"],
                query_hit_count=row["query_hit_count"] or 0,
                last_confirmed_at=row["last_confirmed_at"],
            )
            for row in rows
        ]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
