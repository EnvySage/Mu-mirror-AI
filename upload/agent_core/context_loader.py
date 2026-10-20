"""Load bounded, user-scoped preparation context for graph_v2."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent_core.types import PreparedContext
from agent_data.scope import ReadScope, require_scope


GLOSSARY_LIMIT = 30
PLAN_HISTORY_LIMIT = 12
ANSWER_HISTORY_LIMIT = 6


def load_context(scope: ReadScope, question: str, repositories: Any) -> PreparedContext:
    """Load glossary and history through repositories; no model calls or writes occur here."""
    scope = require_scope(scope)
    if not isinstance(question, str):
        raise TypeError("question must be a string")

    glossary_repo = getattr(repositories, "glossary", None)
    history_repo = getattr(repositories, "history", None)
    if glossary_repo is None or history_repo is None:
        raise TypeError("repositories must provide glossary and history repositories")

    glossary = list(glossary_repo.top_confirmed(scope, limit=GLOSSARY_LIMIT) or ())
    glossary.sort(key=lambda term: term.id)
    glossary.sort(key=_confirmed_sort_value, reverse=True)
    glossary.sort(key=_query_hit_sort_value, reverse=True)
    top30 = tuple(glossary[:GLOSSARY_LIMIT])

    folded_question = question.casefold()
    matched = tuple(
        term for term in top30
        if any(value and value.casefold() in folded_question for value in (term.term, *term.aliases))
    )

    history = list(history_repo.recent(scope, limit=PLAN_HISTORY_LIMIT) or ())
    history = [
        message for message in history
        if message.user_id == scope.user_id
        and message.session_id == scope.session_id
        and message.id < scope.user_message_id
        and message.role in {"user", "assistant"}
        and not message.is_fallback
    ]
    history.sort(key=lambda message: message.id)
    plan_history = tuple(history[-PLAN_HISTORY_LIMIT:])
    answer_history = tuple(plan_history[-ANSWER_HISTORY_LIMIT:])

    return PreparedContext(
        glossary_top30=top30,
        glossary_matched=matched,
        glossary_for_intent=matched or top30,
        matched_ids=tuple(term.id for term in matched),
        history_plan=plan_history,
        history_answer=answer_history,
    )


def _query_hit_sort_value(term: Any) -> tuple[bool, int]:
    count = getattr(term, "query_hit_count", None)
    return count is not None, count if isinstance(count, int) else 0


def _confirmed_sort_value(term: Any) -> tuple[bool, datetime]:
    confirmed_at = getattr(term, "last_confirmed_at", None)
    if confirmed_at is None:
        return False, datetime.min.replace(tzinfo=timezone.utc)
    if not isinstance(confirmed_at, datetime):
        return True, datetime.min.replace(tzinfo=timezone.utc)
    if confirmed_at.tzinfo is None:
        confirmed_at = confirmed_at.replace(tzinfo=timezone.utc)
    return True, confirmed_at

\n