"""Integration checks against the disposable DB preloaded from agent_data.sql.

Setup uses a PostgreSQL administrator only for the isolated database:
  1. psql -d mirror_ai_agent_v2_test -f tests/agent_v2/fixtures/agent_data.sql
  2. From Mu-mirror-B, apply migration-v11-agent-v2.sql (fixture includes its v8/v10 prerequisites).
  3. From Mu-mirror-B, run deploy/agent-reader-role.sql -v reader_role=mirror_ai_reader
  4. set MIRROR_TEST_READ_DSN to the mirror_ai_reader connection string.

The data fixture refuses to run unless the database has its exact test name.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import ExitStack
from datetime import datetime
from uuid import UUID

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import dict_row
from psycopg_pool import PoolTimeout

from agent_data.glossary import GlossaryRepository
from agent_data.history import HistoryRepository
from agent_data.pool import POOL_MAX_SIZE, read_transaction
from agent_data.profiles import ProfileRepository
from agent_data.records import RecordRepository, RecordWindow
from agent_data.scope import ReadScope
from agent_data.todos import TodoRepository
from agent_data.vault import VaultRepository
USER_A = UUID("00000000-0000-0000-0000-000000000001")
USER_B = UUID("00000000-0000-0000-0000-000000000002")
SESSION_B = UUID("10000000-0000-0000-0000-000000000002")
READER_ROLE = os.getenv("MIRROR_TEST_READER_ROLE", "mirror_ai_reader")


class _FixedEmbedder:
    def embed(self, _text: str) -> list[float]:
        return [1.0] + [0.0] * 1023


class _WrongDimensionEmbedder:
    def embed(self, _text: str) -> list[float]:
        return [1.0] + [0.0] * 1022


def test_reader_identity_and_exact_view_grants(read_dsn, agent_pool):
    with psycopg.connect(read_dsn, row_factory=dict_row) as connection:
        row = connection.execute(
            "SELECT current_user AS user_name, count(*) AS view_count "
            "FROM information_schema.role_table_grants "
            "WHERE grantee = current_user AND table_schema = 'mirror_ai' AND privilege_type = 'SELECT' "
            "GROUP BY current_user"
        ).fetchone()
    assert row["user_name"] == READER_ROLE
    assert row["view_count"] == 8


def test_record_search_scope_approval_filters_and_limit(agent_pool, read_scope):
    repository = RecordRepository(agent_pool)

    chunks = repository.search(read_scope, {"keyword": "approved coverage phrase"})
    chunk_ids = {chunk.chunk_id for chunk in chunks}
    assert {1001, 1010}.issubset(chunk_ids)
    assert 2001 not in chunk_ids  # same body exists for user B
    assert not {1002, 1003, 1004, 1005, 1006, 1008, 1009, 1011}.intersection(chunk_ids)
    assert all(chunk.segment != "secret unauthorized segment" for chunk in chunks)
    assert all("FULL RECORD RAW TEXT" not in chunk.segment for chunk in chunks)
    assert repository.search(read_scope, {"content_type": "learning"}) == chunks
    assert repository.search(read_scope, {"moods": ["anxious"]}) == []

    capped = repository.search(read_scope, limit=1000)
    assert len(capped) == 20
    assert repository.search(read_scope, {"keyword": "' OR 1=1 --"}) == []
    with pytest.raises(ValueError):
        repository.search(read_scope, limit=0)
    with pytest.raises(ValueError):
        repository.search(read_scope, {"user_id": USER_B})
    with pytest.raises(ValueError):
        repository.search(read_scope, {"content_type": "not-a-real-type"})


def test_record_stats_count_records_and_coverage_counts_local_dates(agent_pool, read_scope):
    repository = RecordRepository(agent_pool)
    stats = repository.stats(read_scope)
    assert stats.record_count == 22
    assert stats.mood_counts["calm"] == 1  # duplicate chunks in record 101 count once
    assert stats.mood_counts["happy"] == 21

    window = RecordWindow(
        start_at=datetime.fromisoformat("2026-10-01T00:00:00+08:00"),
        end_at=datetime.fromisoformat("2026-10-02T00:00:00+08:00"),
    )
    assert repository.stats(read_scope, window).record_count == 1
    coverage = repository.coverage(read_scope, "coverage phrase")
    assert coverage.record_count == 3
    assert coverage.distinct_dates == 2
    assert coverage.first_date.isoformat() == "2026-10-01"
    assert coverage.latest_date.isoformat() == "2026-10-10"


def test_confirmed_glossary_and_session_history(agent_pool, read_scope):
    terms = GlossaryRepository(agent_pool).top_confirmed(read_scope)
    assert [term.id for term in terms] == [701]
    assert terms[0].aliases == ("alias-A",)

    history = HistoryRepository(agent_pool).recent(read_scope, limit=12)
    assert [message.id for message in history] == [899, 900]
    assert all(message.user_id == USER_A for message in history)
    assert all(message.session_id != SESSION_B for message in history)
    assert all(message.content not in {"forgotten question", "dependent answer", "fallback answer", "current user question", "B private answer"} for message in history)


def test_profiles_hide_invalidated_and_other_user_snapshots(agent_pool, read_scope):
    repository = ProfileRepository(agent_pool)
    profiles = repository.recent(read_scope)
    assert [profile.id for profile in profiles] == [801]
    assert profiles[0].overall_summary == "valid A profile"
    assert repository.compare(read_scope, 801, 803) is None
    with pytest.raises(ValueError):
        repository.compare(read_scope, 801, 801)


def test_vault_metadata_and_body_require_scope_and_confirmation(agent_pool, read_scope):
    repository = VaultRepository(agent_pool)

    shared = repository.find(read_scope, "shared", None, None)
    assert [item.id for item in shared] == [301]
    assert shared[0].digest_status == "pending"
    assert not hasattr(shared[0], "storage_key")
    assert repository.recall(read_scope, 301, None, None).snippets == ()
    assert repository.find(read_scope, "' OR 1=1 --", None, None) == []
    with pytest.raises(ValueError):
        repository.find(read_scope, "file", None, None, limit=0)
    with pytest.raises(ValueError):
        repository.find(read_scope, "file", {"session_id": SESSION_B}, None)

    semantic = repository.find(read_scope, "vector probe", None, _FixedEmbedder())
    assert [item.id for item in semantic] == [303]
    assert semantic[0].match_layer == "confirmed_key_vector"
    assert semantic[0].distance == pytest.approx(0.0)
    with pytest.raises(ValueError, match="1024"):
        repository.find(read_scope, "missing-vector-probe", None, _WrongDimensionEmbedder())

    alias_match = repository.find(
        read_scope,
        "unmatched query",
        {"aliases": ["specification"]},
        None,
    )
    assert [item.id for item in alias_match] == [303]
    assert alias_match[0].match_layer == "glossary_alias"
    fallback = repository.find(read_scope, None, {"category": "learning"}, None)
    assert [item.id for item in fallback] == [303]

    recalled = repository.recall(read_scope, 303, None, None)
    assert recalled is not None
    assert [snippet.text for snippet in recalled.snippets] == [
        "approved vault body segment alpha",
        "approved vault body segment beta",
    ]
    assert all("file key" not in snippet.text for snippet in recalled.snippets)

    queried = repository.recall(read_scope, 303, "body query", _FixedEmbedder())
    assert queried is not None
    assert len(queried.snippets) == 2
    assert all(len(snippet.text) <= 500 for snippet in queried.snippets)
    assert repository.recall(read_scope, 302, None, None) is None  # item belongs to user B


def test_todo_stats_use_separate_todo_approval(agent_pool, read_scope):
    stats = TodoRepository(agent_pool).stats(read_scope)
    assert stats.total == 1
    assert stats.by_status == {"not_started": 0, "in_progress": 1, "completed": 0}


def test_ordered_body_pages_and_query_excerpts_keep_authorization(agent_pool, read_scope):
    repository = VaultRepository(agent_pool)
    first = repository.recall(read_scope, 303, None, None, limit=1)
    assert [snippet.chunk_id for snippet in first.snippets] == [4002]
    assert (first.next_offset, first.complete, first.content_scope) == (1, False, "partial")
    last = repository.recall(read_scope, 303, None, None, offset=1, limit=1)
    assert [snippet.chunk_id for snippet in last.snippets] == [4003]
    assert (last.next_offset, last.complete, last.content_scope) == (None, True, "full")
    assert [snippet.text for snippet in (*first.snippets, *last.snippets)] == [
        "approved vault body segment alpha", "approved vault body segment beta",
    ]
    excerpts = repository.recall(read_scope, 303, "query", _FixedEmbedder(), limit=1)
    assert excerpts.content_scope == "excerpt" and not excerpts.complete
    assert excerpts.next_offset == 1
    pending = repository.recall(read_scope, 301, None, None, offset=1, limit=1)
    assert pending.snippets == () and not pending.complete
    assert repository.recall(read_scope, 302, None, None, offset=1, limit=1) is None


def test_record_fragment_and_todo_pages_use_only_scoped_views(agent_pool, read_scope):
    repository = RecordRepository(agent_pool)
    assert [chunk.chunk_id for chunk in repository.search(read_scope, record_id=101, limit=1)] == [1001]
    assert [chunk.chunk_id for chunk in repository.search(read_scope, record_id=101, offset=1, limit=1)] == [1010]
    assert repository.search(read_scope, record_id=101, offset=2) == []
    for record_id in (102, 103, 104, 105, 106, 107, 108, 201, 401):
        assert repository.search(read_scope, record_id=record_id) == []
    for kwargs in ({"offset": -1}, {"offset": True}, {"record_id": False}):
        with pytest.raises(ValueError):
            repository.search(read_scope, **kwargs)

    todos = TodoRepository(agent_pool)
    assert [(todo.id, todo.title, todo.record_id) for todo in todos.list(read_scope)] == [
        (601, "independently approved todo", 106),
    ]
    assert [todo.id for todo in todos.list(read_scope, status="in_progress")] == [601]
    assert todos.list(read_scope, status="completed") == []
    assert todos.list(read_scope, offset=1, limit=1) == []


def test_body_and_todo_tools_run_against_real_authorized_repositories(agent_pool, read_scope):
    from datetime import date
    from agent_core.types import RuntimeContext, ToolCall
    from agent_tools.read_tools import execute_read_tool

    runtime = RuntimeContext(read_scope.user_id, read_scope.session_id, "pages-test", read_scope.user_message_id,
                             date(2026, 10, 20), "Asia/Shanghai", read_scope, read_pool=agent_pool)
    first = execute_read_tool(ToolCall("r1", "read_record", {"record_id": 101, "limit": 1}), runtime)
    assert first.success and first.data["record_id"] == 101
    assert first.data["next_offset"] == 1 and not first.data["complete"]
    last = execute_read_tool(ToolCall("r2", "read_record", {"record_id": 101, "offset": 1, "limit": 1}), runtime)
    assert last.success and last.data["offset"] == 1
    assert last.data["next_offset"] is None and last.data["complete"]
    assert [piece["text"] for piece in first.data["snippets"] + last.data["snippets"]] == [
        "approved coverage phrase one", "approved coverage phrase two",
    ]
    forbidden = execute_read_tool(ToolCall("r3", "read_record", {"record_id": 201}), runtime)
    assert forbidden.code == "EMPTY" and not forbidden.has_material and not forbidden.data["body_available"]
    listed = execute_read_tool(ToolCall("t1", "list_todos", {"status": "in_progress", "limit": 1}), runtime)
    assert listed.success and listed.data["complete"]
    assert [todo["id"] for todo in listed.data["todos"]] == [601]
    assert listed.evidence[0].record_id == 106


def test_read_role_cannot_write_or_read_private_base_tables(read_dsn):
    statements = (
        "UPDATE public.records SET content = 'not allowed' WHERE id = 101",
        "INSERT INTO public.records (id, user_id, content, source, status, created_at) "
        "VALUES (9999, '00000000-0000-0000-0000-000000000001', 'not allowed', 'user', 'done', now())",
        "SELECT * FROM public.user_settings",
        "SELECT * FROM public.vault_blobs",
        "SELECT * FROM public.users",
    )
    for statement in statements:
        with psycopg.connect(read_dsn, autocommit=True) as connection:
            with pytest.raises(InsufficientPrivilege) as error:
                connection.execute(statement)
        assert error.value.sqlstate == "42501"


def test_pool_wait_is_bounded_and_transactions_return_connections(agent_pool, read_scope):
    with ExitStack() as stack:
        for _ in range(POOL_MAX_SIZE):
            stack.enter_context(agent_pool.connection(timeout=1))
        started = time.monotonic()
        with pytest.raises(PoolTimeout):
            with agent_pool.connection(timeout=0.25):
                pass
        assert time.monotonic() - started < 2

    with pytest.raises(asyncio.CancelledError):
        with read_transaction(agent_pool, read_scope):
            raise asyncio.CancelledError()
    with read_transaction(agent_pool, read_scope) as connection:
        row = connection.execute(
            "SELECT current_user AS user_name, "
            "current_setting('transaction_read_only') AS read_only, "
            "current_setting('statement_timeout') AS statement_timeout, "
            "current_setting('lock_timeout') AS lock_timeout"
        ).fetchone()
        assert row["user_name"] == READER_ROLE
        assert row["read_only"] == "on"
        assert row["statement_timeout"] == "3s"
        assert row["lock_timeout"] == "1s"
