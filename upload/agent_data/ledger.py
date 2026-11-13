"""Authenticated ledger lookups against the backend's read-only money view."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from psycopg_pool import ConnectionPool

from agent_data.pool import read_transaction
from agent_data.scope import ReadScope, bounded_limit, require_scope


_VIEW = "mirror_ai.ledger_cashflows"
_COST = """
    CASE WHEN in_budget_window AND kind = 'EXPENSE' THEN amount
         WHEN in_budget_window AND kind = 'RECOVERY' THEN -amount
    END
"""


@dataclass(frozen=True, slots=True)
class LedgerFilters:
    start: date | None = None
    end: date | None = None
    category: str | None = None
    query: str | None = None
    record_id: UUID | None = None
    diary_id: int | None = None
    kind: str | None = None


class LedgerRepository:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def summary(self, scope: ReadScope, filters: LedgerFilters) -> dict:
        where, params = _scope_where(scope, filters)
        cash = _date_match("occurred_on", filters, params)
        cost = _date_match("budget_day", filters, params)
        sql = f"""
            WITH scoped AS (
                SELECT record_id,title,occurred_on,category_id,category_name,kind,amount,
                       ({cash}) AS in_cash_window, ({cost}) AS in_budget_window
                FROM {_VIEW}
                WHERE {where} AND (({cash}) OR ({cost}))
            ), source_records AS (
                SELECT record_id,title,max(occurred_on) AS latest
                FROM scoped
                GROUP BY record_id,title
                ORDER BY latest DESC, record_id
                LIMIT 4
            ), category_totals AS (
                SELECT category_id,category_name,COALESCE(sum({_COST}),0) AS net_expense
                FROM scoped
                WHERE in_budget_window AND kind IN ('EXPENSE','RECOVERY')
                GROUP BY category_id,category_name
                ORDER BY net_expense DESC,category_id
                LIMIT 8
            )
            SELECT COALESCE(sum(amount) FILTER (WHERE in_cash_window AND kind = 'EXPENSE'),0) AS expense,
                   COALESCE(sum(amount) FILTER (WHERE in_cash_window AND kind = 'INCOME'),0) AS income,
                   COALESCE(sum(amount) FILTER (WHERE in_cash_window AND kind = 'RECOVERY'),0) AS recovery,
                   COALESCE(sum({_COST}),0) AS net_expense,
                   count(*) FILTER (WHERE in_cash_window) AS flow_count,
                   count(*) AS matched_count,
                   (SELECT COALESCE(jsonb_agg(jsonb_build_object(
                       'record_id',record_id,'title',title) ORDER BY latest DESC,record_id), '[]')
                    FROM source_records) AS sources,
                   (SELECT COALESCE(jsonb_agg(jsonb_build_object(
                       'category_id',category_id,'name',category_name,'net_expense',net_expense::text)
                       ORDER BY net_expense DESC,category_id), '[]') FROM category_totals) AS categories
            FROM scoped
        """
        with read_transaction(self._pool, scope) as connection:
            return connection.execute(sql, params).fetchone()

    def list_flows(
        self, scope: ReadScope, filters: LedgerFilters, *, offset: int = 0, limit: int = 8,
    ) -> tuple[list[dict], bool]:
        where, params = _scope_where(scope, filters)
        date_match = _date_match("occurred_on", filters, params)
        limit = bounded_limit(limit, maximum=20, default=8)
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10_000:
            raise ValueError("账目分页位置无效")
        params.update(limit=limit + 1, offset=offset)
        sql = f"""
            SELECT record_id,flow_id,title,kind,amount,occurred_on,budget_day,category_id,category_name,note
            FROM {_VIEW}
            WHERE {where} AND ({date_match})
            ORDER BY occurred_on DESC, record_id, flow_id
            LIMIT %(limit)s OFFSET %(offset)s
        """
        with read_transaction(self._pool, scope) as connection:
            rows = connection.execute(sql, params).fetchall()
        return rows[:limit], len(rows) > limit


def _scope_where(scope: ReadScope, filters: LedgerFilters) -> tuple[str, dict]:
    params = {"owner": require_scope(scope).user_id}
    predicates = ["user_id = %(owner)s"]
    if filters.record_id is not None:
        params["record_id"] = filters.record_id
        predicates.append("record_id = %(record_id)s")
    if filters.diary_id is not None:
        params["diary_id"] = filters.diary_id
        predicates.append("%(diary_id)s = ANY(diary_ids)")
    if filters.category:
        params["category"] = filters.category
        predicates.append("(lower(category_name) = lower(%(category)s) OR category_id = %(category)s)")
    if filters.query:
        params["query"] = filters.query
        predicates.append("strpos(lower(concat_ws(' ', title, description, note)), lower(%(query)s)) > 0")
    if filters.kind:
        params["kind"] = filters.kind
        predicates.append("kind = %(kind)s")
    return " AND ".join(predicates), params


def _date_match(column: str, filters: LedgerFilters, params: dict) -> str:
    if filters.start is not None:
        params["start"] = filters.start
    if filters.end is not None:
        params["end"] = filters.end
    if filters.start is not None and filters.end is not None:
        return f"{column} BETWEEN %(start)s AND %(end)s"
    if filters.start is not None:
        return f"{column} >= %(start)s"
    if filters.end is not None:
        return f"{column} <= %(end)s"
    return "TRUE"
