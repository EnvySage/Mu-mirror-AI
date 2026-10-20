from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

from agent_core.context_loader import load_context
from agent_data.glossary import GlossaryTerm
from agent_data.history import HistoryMessage
from agent_data.scope import ReadScope


USER = UUID("00000000-0000-0000-0000-000000000001")
OTHER_USER = UUID("00000000-0000-0000-0000-000000000002")
SESSION = UUID("10000000-0000-0000-0000-000000000001")
OTHER_SESSION = UUID("10000000-0000-0000-0000-000000000002")


def _term(term_id, *, hits=0, confirmed=None, term=None, aliases=()):
    return GlossaryTerm(
        id=term_id,
        user_id=USER,
        term=term or f"词条{term_id}",
        aliases=tuple(aliases),
        description=f"说明{term_id}",
        query_hit_count=hits,
        last_confirmed_at=confirmed,
    )


def _message(message_id, *, role="user", user_id=USER, session_id=SESSION, fallback=False):
    return HistoryMessage(
        id=message_id,
        user_id=user_id,
        session_id=session_id,
        role=role,
        content=f"消息{message_id}",
        is_fallback=fallback,
        created_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
    )


class FakeGlossaryRepository:
    def __init__(self, terms):
        self.terms = terms
        self.calls = []

    def top_confirmed(self, scope, limit):
        self.calls.append((scope, limit))
        return self.terms


class FakeHistoryRepository:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    def recent(self, scope, limit):
        self.calls.append((scope, limit))
        return self.messages


def _repositories(terms, messages):
    glossary = FakeGlossaryRepository(terms)
    history = FakeHistoryRepository(messages)
    return SimpleNamespace(glossary=glossary, history=history), glossary, history


def test_load_context_sorts_confirmed_terms_trims_to_30_and_matches_aliases():
    terms = [_term(i) for i in range(1, 33)]
    terms.extend([
        _term(40, hits=3, confirmed=datetime(2026, 1, 1, tzinfo=timezone.utc), term="方案"),
        _term(41, hits=3, confirmed=datetime(2026, 1, 1, tzinfo=timezone.utc), term="毕设", aliases=("毕业设计",)),
        _term(42, hits=3, confirmed=datetime(2026, 2, 1, tzinfo=timezone.utc), term="旧方案"),
    ])
    scope = ReadScope(USER, SESSION, 904)
    repositories, glossary_repo, history_repo = _repositories(terms, [])

    prepared = load_context(scope, "继续聊毕业设计的方案", repositories)

    assert len(prepared.glossary_top30) == 30
    assert [term.id for term in prepared.glossary_top30[:3]] == [42, 40, 41]
    assert [term.id for term in prepared.glossary_matched] == [40, 41]
    assert prepared.matched_ids == (40, 41)
    assert prepared.glossary_for_intent == prepared.glossary_matched
    assert glossary_repo.calls == [(scope, 30)]
    assert history_repo.calls == [(scope, 12)]


def test_load_context_falls_back_to_top30_when_no_term_or_alias_matches():
    scope = ReadScope(USER, SESSION, 904)
    terms = [_term(1, term="论文", aliases=("小论文",))]
    repositories, _, _ = _repositories(terms, [])

    prepared = load_context(scope, "今天心情怎么样", repositories)

    assert prepared.glossary_matched == ()
    assert prepared.matched_ids == ()
    assert prepared.glossary_for_intent == prepared.glossary_top30


def test_load_context_filters_cross_scope_current_future_and_fallback_history():
    scope = ReadScope(USER, SESSION, 904)
    messages = [
        _message(903, role="assistant"),
        _message(904),
        _message(905),
        _message(902, user_id=OTHER_USER),
        _message(901, session_id=OTHER_SESSION),
        _message(900, role="assistant", fallback=True),
        *[_message(i) for i in range(880, 868, -1)],
    ]
    repositories, _, history_repo = _repositories([], messages)

    prepared = load_context(scope, "继续", repositories)

    assert [message.id for message in prepared.history_plan] == list(range(870, 881)) + [903]
    assert [message.id for message in prepared.history_answer] == list(range(876, 881)) + [903]
    assert all(message.id < scope.user_message_id for message in prepared.history_plan)
    assert all(message.user_id == USER and message.session_id == SESSION for message in prepared.history_plan)
    assert not any(message.is_fallback for message in prepared.history_plan)
    assert history_repo.calls == [(scope, 12)]
