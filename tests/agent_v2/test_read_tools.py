from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from agent_core.types import ToolCall, RuntimeContext
from agent_data.glossary import GlossaryTerm
from agent_data.profiles import ProfileSnapshot
from agent_data.records import Coverage, RecordChunk, RecordStats
from agent_data.scope import ReadScope
from agent_data.todos import TodoStats
from agent_data.vault import VaultItem, VaultRecall, VaultSnippet
from agent_tools.registry import READ_TOOL_REGISTRY, get_read_tool_specs
from agent_tools.read_tools import execute_read_tool


SCOPE = ReadScope(UUID(int=1), UUID(int=2), 10)
NOW = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)


class Repo:
    def __init__(self):
        self.search_call = None
        self.vector_calls = []
        self.last_recall = None
        self.last_metadata_id = None

    def search(self, scope, filters=None, *, limit=20):
        self.search_call = (scope, filters, limit)
        return [RecordChunk(12, 3, SCOPE.user_id, "approved note", {"contentType": "note", "mood": ["calm"]}, None, NOW)]

    def stats(self, scope, window=None):
        return RecordStats(0, {}, window.start_at if window else None, window.end_at if window else None)

    def coverage(self, scope, keyword):
        return Coverage(date(2026, 1, 1), date(2026, 10, 1), 2, 4)


class Glossary:
    def top_confirmed(self, scope, limit=30):
        return [GlossaryTerm(1, SCOPE.user_id, "星环", ("环",), "项目代称", 2, NOW)]


class Profiles:
    def recent(self, scope, limit=2, *, month=None):
        self.month = month
        return [ProfileSnapshot(80, SCOPE.user_id, "manual", None, "mood", "learn", "todo", "rhythm",
                                ["tag"], "summary", None, NOW)]

    def compare(self, scope, first_id, second_id):
        a = self.recent(scope)[0]
        return a, ProfileSnapshot(81, SCOPE.user_id, "monthly", "2026-09", "m2", "l2", "t2", "r2", [], "old", None, NOW)


class Vault:
    def __init__(self):
        self.find_call = None
        self.recall_call = None
        self.metadata_call = None

    def find(self, scope, query, filters, embedder, limit=10, **kwargs):
        self.find_call = (scope, query, filters, embedder, limit, kwargs)
        return [VaultItem(303, SCOPE.user_id, "设计稿.pdf", "application/pdf", 100, "learning", "镜子项目", "confirmed", NOW, "glossary_alias", 0.2)]

    def recall(self, scope, item_id, query, embedder, **kwargs):
        self.recall_call = (scope, item_id, query, embedder, kwargs)
        item = VaultItem(item_id, SCOPE.user_id, "设计稿.pdf", "application/pdf", 100, "learning", "镜子项目", "confirmed", NOW, "explicit_item")
        snippets = (VaultSnippet(120, 30, "body quote", NOW, 0.1),) if self.body_enabled else ()
        return VaultRecall(item, snippets)

    def metadata(self, scope, item_id):
        self.metadata_call = (scope, item_id)
        return VaultItem(item_id, SCOPE.user_id, "设计稿.pdf", "application/pdf", 100, "learning", "镜子项目", "pending", NOW, "explicit_item")

    body_enabled = True


class Todos:
    def stats(self, scope):
        return TodoStats(SCOPE.user_id, 0, {"not_started": 0, "in_progress": 0, "completed": 0})


def runtime(repositories):
    return RuntimeContext(SCOPE.user_id, SCOPE.session_id, "run", SCOPE.user_message_id, date(2026, 10, 20),
                          "Asia/Shanghai", SCOPE, repositories=repositories)


def repositories():
    return SimpleNamespace(records=Repo(), todos=Todos(), profiles=Profiles(), glossary=Glossary(), vault=Vault())


def call(tool, args=None):
    return ToolCall("c1", tool, args or {})


def test_registry_has_exactly_eleven_read_only_tools_and_rejects_scope_fields():
    assert set(READ_TOOL_REGISTRY) == {
        "search_records", "get_stats", "get_profile", "get_coverage", "get_glossary",
        "compare_snapshots", "find_item", "recall_item", "save_item",
        "read_record", "list_todos",
    }
    specs = get_read_tool_specs()
    assert len(specs) == 11
    assert {spec["effect"] for spec in specs} == {"read"}
    rejected = execute_read_tool(call("search_records", {"user_id": str(UUID(int=9))}), runtime(repositories()))
    assert rejected.code == "INVALID_ARGS"


def test_search_records_applies_short_keyword_and_inclusive_dates_before_repository_limit():
    repos = repositories()
    observation = execute_read_tool(
        call("search_records", {"date_from": "2026-10-01", "date_to": "2026-10-03", "query": "镜子", "limit": 5}),
        runtime(repos),
    )
    scope, filters, limit = repos.records.search_call
    assert filters.keyword == "镜子"
    assert filters.start_at.isoformat() == "2026-10-01T00:00:00+08:00"
    assert filters.end_at.isoformat() == "2026-10-04T00:00:00+08:00"
    assert limit == 5
    assert observation.evidence[0].evidence_id == "chunk:12"
    assert observation.evidence[0].origin == "search_records"
    assert observation.has_material is True


def test_stats_zero_is_material_and_coverage_counts_dates():
    repos = repositories()
    stats = execute_read_tool(call("get_stats", {"days": 30}), runtime(repos))
    assert stats.success and stats.has_material and stats.data["records"]["count"] == 0
    assert stats.data["todos"]["count"] == 0
    coverage = execute_read_tool(call("get_coverage", {"keyword": "计划"}), runtime(repos))
    assert coverage.data["distinct_dates"] == 2
    assert coverage.data["record_count"] == 4


def test_glossary_profile_and_compare_return_ordered_facts():
    repos = repositories()
    glossary = execute_read_tool(call("get_glossary"), runtime(repos))
    assert glossary.data["terms"][0]["term"] == "星环"
    profile = execute_read_tool(call("get_profile", {"month": "2026-09"}), runtime(repos))
    assert repos.profiles.month == "2026-09"
    assert profile.evidence[0].evidence_id == "profile:80"
    assert profile.evidence[0].record_id is None
    compare = execute_read_tool(call("compare_snapshots", {"first_id": 80, "second_id": 81}), runtime(repos))
    assert list(compare.data["first"]["dimensions"]) == ["mood", "learning", "todo", "rhythm", "summary"]


def test_find_alias_recall_confirmation_and_save_item_are_read_only():
    repos = repositories()
    found = execute_read_tool(call("find_item", {"query": "星环"}), runtime(repos))
    assert found.data["items"][0]["vault_item_id"] == 303
    assert found.data["items"][0]["match_layer"] == "glossary_alias"
    assert repos.vault.find_call[2].aliases == ("环",)

    recalled = execute_read_tool(call("recall_item", {"vault_item_id": 303, "query": "概要"}), runtime(repos))
    assert recalled.evidence[0].evidence_id == "vault:303:chunk:120"
    assert recalled.data["snippets"][0]["text"] == "body quote"

    repos.vault.body_enabled = False
    hidden = execute_read_tool(call("recall_item", {"vault_item_id": 303}), runtime(repos))
    assert hidden.evidence == ()

    recall_call_before_save = repos.vault.recall_call
    saved = execute_read_tool(call("save_item", {"vault_item_id": 303}), runtime(repos))
    assert saved.data["saved"] is True
    assert repos.vault.metadata_call == (SCOPE, 303)
    assert repos.vault.recall_call == recall_call_before_save
    assert saved.evidence[0].kind == "vault"


@pytest.mark.parametrize("args", [{"days": 0}, {"query": "x" * 257}, {"user_id": "other"}, {"date": "2026-02-30"}])
def test_tool_validation_is_controlled(args):
    observation = execute_read_tool(call("search_records", args), runtime(repositories()))
    assert observation.code == "INVALID_ARGS"
    assert observation.success is False
