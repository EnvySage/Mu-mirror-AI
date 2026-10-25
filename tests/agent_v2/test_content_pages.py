"""Complete body pages and schema validation at the read-tool boundary."""

from contextlib import contextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from agent_core.types import RuntimeContext, ToolCall
from agent_data.records import RecordChunk
from agent_data.scope import ReadScope
from agent_data.vault import VaultRepository
from agent_tools.read_tools import execute_read_tool


NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)
SCOPE = ReadScope(UUID(int=1), UUID(int=2), 10)


def runtime(repositories=None):
    return RuntimeContext(SCOPE.user_id, SCOPE.session_id, "run", 10, date(2026, 10, 20),
                          "Asia/Shanghai", SCOPE, repositories=repositories)


def test_ordered_vault_pages_keep_long_segments_and_last_segment(monkeypatch):
    texts = ["start " + "长" * 1100, "middle", "last sentence"]
    metadata = dict(id=303, user_id=SCOPE.user_id, original_name="long.pdf", mime="application/pdf",
                    size_bytes=100, category="note", description=None, digest_status="confirmed", created_at=NOW)

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchone(self):
            return self.rows[0]

        def fetchall(self):
            return self.rows

    class Connection:
        def execute(self, sql, params):
            if "vault_metadata" in sql:
                return Result([metadata])
            assert "mirror_ai.vault_content" in sql and "ORDER BY chunk_id ASC" in sql
            assert params[:2] == (SCOPE.user_id, 303)
            limit, offset = params[-2:]
            rows = [dict(chunk_id=i + 1, record_id=401, segment=text, created_at=NOW)
                    for i, text in enumerate(texts)]
            return Result(rows[offset:offset + limit])

    @contextmanager
    def transaction(pool, scope):
        assert scope == SCOPE
        yield Connection()

    monkeypatch.setattr("agent_data.vault.read_transaction", transaction)
    repo = VaultRepository(object())
    first = repo.recall(SCOPE, 303, None, None, limit=2)
    assert [snippet.text for snippet in first.snippets] == texts[:2]
    assert (first.next_offset, first.complete, first.content_scope) == (2, False, "partial")
    last = repo.recall(SCOPE, 303, None, None, offset=first.next_offset, limit=2)
    assert [snippet.text for snippet in last.snippets] == texts[2:]
    assert (last.next_offset, last.complete, last.content_scope) == (None, True, "full")


@pytest.mark.parametrize("tool,args", [
    ("search_records", {"query": None}),
    ("search_records", {"moods": ["calm", {}]}),
    ("search_records", {"moods": ["invented"]}),
    ("find_item", {"category": "invented"}),
    ("recall_item", {"vault_item_id": True}),
    ("recall_item", {"vault_item_id": 303, "offset": -1}),
    ("search_records", {"limit": 21}),
    ("get_stats", {"unknown": 1}),
    ("list_todos", {"status": "invented"}),
    ("read_record", {"record_id": "101"}),
])
def test_invalid_schema_is_rejected_before_repository_access(tool, args):
    observation = execute_read_tool(ToolCall("c1", tool, args), runtime())
    assert observation.code == "INVALID_ARGS"
    assert "repository context" not in observation.summary


def test_read_record_pages_use_authorized_fragments_and_report_coverage():
    texts = ["first" * 200, "second", "last"]

    class Records:
        def search(self, scope, filters=None, *, record_id=None, offset=0, limit=20):
            assert scope == SCOPE and record_id == 101
            return [RecordChunk(i + 1, 101, SCOPE.user_id, text, {}, None, NOW)
                    for i, text in enumerate(texts)][offset:offset + limit]

    repos = SimpleNamespace(records=Records())
    first = execute_read_tool(ToolCall("c1", "read_record", {"record_id": 101, "limit": 2}), runtime(repos))
    assert first.success and first.data["content_scope"] == "partial"
    assert first.data["next_offset"] == 2 and not first.data["complete"]
    assert [snippet["text"] for snippet in first.data["snippets"]] == texts[:2]
    last = execute_read_tool(ToolCall("c2", "read_record", {"record_id": 101, "offset": 2, "limit": 2}), runtime(repos))
    assert last.data["complete"] and last.data["next_offset"] is None
    assert [snippet["text"] for snippet in last.data["snippets"]] == texts[2:]
    assert last.evidence[0].origin == "read_record"


def test_recall_tool_preserves_page_contract():
    from agent_data.vault import VaultItem, VaultRecall, VaultSnippet

    class Vault:
        def recall(self, scope, item_id, query, embedder, **kwargs):
            assert kwargs["offset"] == 2 and kwargs["limit"] == 1
            item = VaultItem(item_id, scope.user_id, "long.pdf", "application/pdf", 100,
                             "note", None, "confirmed", NOW, "explicit_item")
            return VaultRecall(item, (VaultSnippet(3, 401, "end", NOW),), None, True, "full")

    observed = execute_read_tool(ToolCall("c1", "recall_item", {"vault_item_id": 303, "offset": 2, "limit": 1}),
                                 runtime(SimpleNamespace(vault=Vault())))
    assert observed.success
    assert observed.data["next_offset"] is None
    assert observed.data["complete"] and observed.data["body_available"]
    assert observed.data["content_scope"] == "full"
