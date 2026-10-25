"""Scoped metadata discovery and confirmed vault-segment reads."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping, Sequence
from uuid import UUID

from psycopg_pool import ConnectionPool

from agent_data.pool import read_transaction
from agent_data.records import CONTENT_TYPES, _validate_window
from agent_data.scope import ReadScope, bounded_limit, reject_scope_overrides, require_scope


VECTOR_DIMENSION = 1024
MAX_FIND_LIMIT = 10
MAX_RECALL_SEGMENTS = 3
MAX_RECALL_LIMIT = 20


@dataclass(frozen=True, slots=True)
class VaultFilters:
    category: str | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VaultItem:
    id: int
    user_id: UUID
    original_name: str
    mime: str
    size_bytes: int
    category: str | None
    description: str | None
    digest_status: str
    created_at: datetime
    match_layer: str
    distance: float | None = None


@dataclass(frozen=True, slots=True)
class VaultSnippet:
    chunk_id: int
    record_id: int
    text: str
    created_at: datetime
    distance: float | None = None


@dataclass(frozen=True, slots=True)
class VaultRecall:
    item: VaultItem
    snippets: tuple[VaultSnippet, ...]
    next_offset: int | None = None
    complete: bool = False
    content_scope: str = "excerpt"


def _normalize_filters(filters: VaultFilters | Mapping[str, object] | None) -> VaultFilters:
    if filters is None:
        normalized = VaultFilters()
    elif isinstance(filters, VaultFilters):
        normalized = filters
    elif isinstance(filters, Mapping):
        reject_scope_overrides(filters)
        allowed = {"category", "created_after", "created_before", "aliases"}
        unknown = set(filters) - allowed
        if unknown:
            raise ValueError(f"unsupported vault filters: {', '.join(sorted(unknown))}")
        values = dict(filters)
        if "aliases" in values:
            values["aliases"] = tuple(values["aliases"] or ())  # type: ignore[arg-type]
        normalized = VaultFilters(**values)  # type: ignore[arg-type]
    else:
        raise TypeError("filters must be VaultFilters, a mapping, or None")

    if normalized.category is not None and normalized.category not in CONTENT_TYPES:
        raise ValueError("category is not a supported content type")
    if isinstance(normalized.aliases, str):
        raise TypeError("aliases must be a sequence of strings")
    aliases = tuple(dict.fromkeys(normalized.aliases))
    if any(not isinstance(alias, str) or not alias.strip() or len(alias) > 256 for alias in aliases):
        raise ValueError("aliases must contain non-empty strings up to 256 characters")
    _validate_window(normalized.created_after, normalized.created_before)
    return VaultFilters(
        category=normalized.category,
        created_after=normalized.created_after,
        created_before=normalized.created_before,
        aliases=aliases,
    )


def _metadata_filters(filters: VaultFilters, *, alias: str = "m") -> tuple[list[str], list[object]]:
    conditions: list[str] = []
    params: list[object] = []
    if filters.category:
        conditions.append(f"{alias}.category = %s")
        params.append(filters.category)
    if filters.created_after:
        conditions.append(f"{alias}.created_at >= %s")
        params.append(filters.created_after)
    if filters.created_before:
        conditions.append(f"{alias}.created_at < %s")
        params.append(filters.created_before)
    return conditions, params


def _query_text(query: str | None) -> str | None:
    if query is None:
        return None
    if not isinstance(query, str):
        raise TypeError("query must be a string or None")
    normalized = query.strip()
    if len(normalized) > 256:
        raise ValueError("query must be at most 256 characters")
    return normalized or None


def _embedding_literal(
    embedder: Callable[[str], Sequence[float]] | object,
    query: str,
    *,
    timeout: float | None = None,
    max_retries: int | None = None,
) -> str:
    method = getattr(embedder, "embed", None)
    if not callable(method):
        raise TypeError("embedder must provide embed(text)")
    options = {}
    if timeout is not None:
        options["timeout"] = timeout
    if max_retries is not None:
        options["max_retries"] = max_retries
    vector = method(query, **options)
    if not isinstance(vector, Sequence) or isinstance(vector, (str, bytes)):
        raise TypeError("embedder result must be a numeric sequence")
    if len(vector) != VECTOR_DIMENSION:
        raise ValueError(f"embedding must contain exactly {VECTOR_DIMENSION} values")
    values: list[str] = []
    for item in vector:
        try:
            number = float(item)
        except (TypeError, ValueError) as exc:
            raise ValueError("embedding values must be finite numbers") from exc
        if not math.isfinite(number):
            raise ValueError("embedding values must be finite numbers")
        values.append(repr(number))
    return "[" + ",".join(values) + "]"


def _item(row: Mapping[str, object], match_layer: str, distance: float | None = None) -> VaultItem:
    return VaultItem(
        id=row["id"],  # type: ignore[arg-type]
        user_id=row["user_id"],  # type: ignore[arg-type]
        original_name=row["original_name"],  # type: ignore[arg-type]
        mime=row["mime"],  # type: ignore[arg-type]
        size_bytes=row["size_bytes"],  # type: ignore[arg-type]
        category=row["category"],  # type: ignore[arg-type]
        description=row["description"],  # type: ignore[arg-type]
        digest_status=row["digest_status"],  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
        match_layer=match_layer,
        distance=distance,
    )


class VaultRepository:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def find(
        self,
        scope: ReadScope,
        query: str | None,
        filters: VaultFilters | Mapping[str, object] | None,
        embedder: Callable[[str], Sequence[float]] | object | None,
        limit: int = 10,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> list[VaultItem]:
        scope = require_scope(scope)
        limit = bounded_limit(limit, maximum=MAX_FIND_LIMIT, default=10)
        query = _query_text(query)
        normalized = _normalize_filters(filters)
        candidates: list[VaultItem] = []
        seen: set[int] = set()

        terms = tuple(dict.fromkeys(([query] if query else []) + list(normalized.aliases)))
        base_conditions, base_params = _metadata_filters(normalized)
        conditions = ["m.user_id = %s", *base_conditions]
        params: list[object] = [scope.user_id, *base_params]
        if terms:
            matching_terms = []
            for term in terms:
                matching_terms.append(
                    "(strpos(lower(m.original_name), lower(%s)) > 0 "
                    "OR strpos(lower(COALESCE(m.description, '')), lower(%s)) > 0)"
                )
                params.extend((term, term))
            conditions.append("(" + " OR ".join(matching_terms) + ")")
        exact_order = ""
        order_params: list[object] = []
        if query:
            exact_order = (
                "CASE WHEN lower(m.original_name) = lower(%s) "
                "OR lower(COALESCE(m.description, '')) = lower(%s) THEN 0 ELSE 1 END, "
            )
            order_params.extend((query, query))
        alias_order = ""
        if normalized.aliases:
            alias_matches = []
            for alias_term in normalized.aliases:
                alias_matches.append(
                    "(strpos(lower(m.original_name), lower(%s)) > 0 "
                    "OR strpos(lower(COALESCE(m.description, '')), lower(%s)) > 0)"
                )
                order_params.extend((alias_term, alias_term))
            alias_order = "CASE WHEN " + " OR ".join(alias_matches) + " THEN 0 ELSE 1 END, "
        params.extend(order_params)
        params.append(limit)
        with read_transaction(self._pool, scope) as connection:
            metadata_rows = connection.execute(
                "SELECT m.id, m.user_id, m.original_name, m.mime, m.size_bytes, m.category, "
                "m.description, m.digest_status, m.created_at "
                "FROM mirror_ai.vault_metadata AS m WHERE "
                + " AND ".join(conditions)
                + " ORDER BY " + exact_order + alias_order + "m.created_at DESC, m.id DESC LIMIT %s",
                params,
            ).fetchall()
        for row in metadata_rows:
            name = row["original_name"].casefold()
            description = (row["description"] or "").casefold()
            if query and (name == query.casefold() or description == query.casefold()):
                layer = "metadata_exact"
            elif any(term.casefold() in name or term.casefold() in description for term in normalized.aliases):
                layer = "glossary_alias"
            else:
                layer = "metadata_keyword"
            candidate = _item(row, layer)
            candidates.append(candidate)
            seen.add(candidate.id)

        remaining = limit - len(candidates)
        can_vector_search = bool(query and embedder is not None and remaining > 0)
        if can_vector_search:
            # Embedding calls can reach a model API. Perform them before borrowing
            # a database connection so no read transaction spans model latency.
            vector_literal = _embedding_literal(embedder, query, timeout=timeout, max_retries=max_retries)
            with read_transaction(self._pool, scope) as connection:
                key_hits = self._vector_hits(
                    connection,
                    scope,
                    normalized,
                    vector_literal,
                    "mirror_ai.vault_keys",
                    "confirmed_key_vector",
                    0.50,
                    remaining,
                )
                for candidate in key_hits:
                    if candidate.id not in seen:
                        candidates.append(candidate)
                        seen.add(candidate.id)
                remaining = limit - len(candidates)
                if remaining > 0:
                    body_hits = self._vector_hits(
                        connection,
                        scope,
                        normalized,
                        vector_literal,
                        "mirror_ai.vault_content",
                        "confirmed_body_vector",
                        0.35,
                        remaining,
                    )
                    for candidate in body_hits:
                        if candidate.id not in seen:
                            candidates.append(candidate)
                            seen.add(candidate.id)

        remaining = limit - len(candidates)
        can_fallback = remaining > 0 and (
            query is None or normalized.category is not None
            or normalized.created_after is not None or normalized.created_before is not None
        )
        if can_fallback:
            with read_transaction(self._pool, scope) as connection:
                fallback_conditions = ["m.user_id = %s", *base_conditions]
                fallback_params: list[object] = [scope.user_id, *base_params]
                if seen:
                    fallback_conditions.append("m.id <> ALL(%s::bigint[])")
                    fallback_params.append(sorted(seen))
                fallback_params.append(remaining)
                fallback_rows = connection.execute(
                    "SELECT m.id, m.user_id, m.original_name, m.mime, m.size_bytes, m.category, "
                    "m.description, m.digest_status, m.created_at "
                    "FROM mirror_ai.vault_metadata AS m WHERE "
                    + " AND ".join(fallback_conditions)
                    + " ORDER BY m.created_at DESC, m.id DESC LIMIT %s",
                    fallback_params,
                ).fetchall()
                candidates.extend(_item(row, "type_date_fallback") for row in fallback_rows)
        return candidates[:limit]

    def _vector_hits(
        self,
        connection,
        scope: ReadScope,
        filters: VaultFilters,
        vector_literal: str,
        view_name: str,
        match_layer: str,
        threshold: float,
        limit: int,
    ) -> list[VaultItem]:
        conditions, filter_params = _metadata_filters(filters)
        vector_column = "v.embedding"
        # The project installs pgvector in public; explicit qualification keeps
        # public out of the role's search_path and grants no table access there.
        vector_distance = f"{vector_column} OPERATOR(public.<=>) %s::public.vector"
        conditions.extend(("v.user_id = %s", f"{vector_column} IS NOT NULL", f"({vector_distance}) <= %s"))
        params: list[object] = [vector_literal]
        params.extend(filter_params)
        params.extend((scope.user_id, vector_literal, threshold, limit))
        rows = connection.execute(
            "SELECT ranked.id, ranked.user_id, ranked.original_name, ranked.mime, ranked.size_bytes, "
            "ranked.category, ranked.description, ranked.digest_status, ranked.created_at, ranked.distance "
            "FROM ("
            "SELECT DISTINCT ON (v.vault_item_id) m.id, m.user_id, m.original_name, m.mime, m.size_bytes, "
            "m.category, m.description, m.digest_status, m.created_at, "
            f"({vector_distance}) AS distance "
            f"FROM {view_name} AS v "
            "JOIN mirror_ai.vault_metadata AS m ON m.id = v.vault_item_id AND m.user_id = v.user_id "
            "WHERE " + " AND ".join(conditions) + " "
            "ORDER BY v.vault_item_id, distance ASC, v.chunk_id ASC"
            ") AS ranked ORDER BY ranked.distance ASC, ranked.id ASC LIMIT %s",
            params,
        ).fetchall()
        return [_item(row, match_layer, float(row["distance"])) for row in rows]

    def recall(
        self,
        scope: ReadScope,
        item_id: int,
        query: str | None,
        embedder: Callable[[str], Sequence[float]] | object | None,
        *,
        offset: int = 0,
        limit: int = MAX_RECALL_SEGMENTS,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> VaultRecall | None:
        scope = require_scope(scope)
        if isinstance(item_id, bool) or not isinstance(item_id, int):
            raise TypeError("item_id must be an integer")
        if item_id < 1:
            raise ValueError("item_id must be positive")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        limit = bounded_limit(limit, maximum=MAX_RECALL_LIMIT, default=MAX_RECALL_SEGMENTS)
        query = _query_text(query)
        with read_transaction(self._pool, scope) as connection:
            row = connection.execute(
                "SELECT id, user_id, original_name, mime, size_bytes, category, description, "
                "digest_status, created_at FROM mirror_ai.vault_metadata "
                "WHERE user_id = %s AND id = %s",
                (scope.user_id, item_id),
            ).fetchone()
            if row is None:
                return None
            item = _item(row, "explicit_item")
        if item.digest_status != "confirmed":
            return VaultRecall(item=item, snippets=())

        if query is None:
            with read_transaction(self._pool, scope) as connection:
                rows = connection.execute(
                    "SELECT chunk_id, record_id, segment, created_at "
                    "FROM mirror_ai.vault_content "
                    "WHERE user_id = %s AND vault_item_id = %s "
                    "ORDER BY chunk_id ASC LIMIT %s OFFSET %s",
                    (scope.user_id, item_id, limit + 1, offset),
                ).fetchall()
                snippets = tuple(
                    VaultSnippet(
                        chunk_id=result["chunk_id"],
                        record_id=result["record_id"],
                        text=result["segment"],
                        created_at=result["created_at"],
                    )
                    for result in rows[:limit]
                )
            has_more = len(rows) > limit
            return VaultRecall(item=item, snippets=snippets,
                               next_offset=offset + limit if has_more else None,
                               complete=not has_more, content_scope="partial" if has_more else "full")

        if embedder is None:
            raise ValueError("an embedder is required for query-based vault recall")
        vector_literal = _embedding_literal(embedder, query, timeout=timeout, max_retries=max_retries)
        with read_transaction(self._pool, scope) as connection:
            rows = connection.execute(
                "SELECT chunk_id, record_id, segment, created_at, "
                "(embedding OPERATOR(public.<=>) %s::public.vector) AS distance "
                "FROM mirror_ai.vault_content "
                "WHERE user_id = %s AND vault_item_id = %s AND embedding IS NOT NULL "
                "ORDER BY distance ASC, chunk_id ASC LIMIT %s OFFSET %s",
                (vector_literal, scope.user_id, item_id, limit + 1, offset),
            ).fetchall()
        snippets = tuple(
            VaultSnippet(
                chunk_id=result["chunk_id"],
                record_id=result["record_id"],
                text=result["segment"][:500],
                created_at=result["created_at"],
                distance=float(result["distance"]),
            )
            for result in rows[:limit]
        )
        return VaultRecall(item=item, snippets=snippets,
                           next_offset=offset + limit if len(rows) > limit else None)

    def metadata(self, scope: ReadScope, item_id: int) -> VaultItem | None:
        """Return scoped file metadata without reading extracted body content."""
        scope = require_scope(scope)
        if isinstance(item_id, bool) or not isinstance(item_id, int):
            raise TypeError("item_id must be an integer")
        if item_id < 1:
            raise ValueError("item_id must be positive")
        with read_transaction(self._pool, scope) as connection:
            row = connection.execute(
                "SELECT id, user_id, original_name, mime, size_bytes, category, description, "
                "digest_status, created_at FROM mirror_ai.vault_metadata "
                "WHERE user_id = %s AND id = %s",
                (scope.user_id, item_id),
            ).fetchone()
        return _item(row, "explicit_item") if row is not None else None
