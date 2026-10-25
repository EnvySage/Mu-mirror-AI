"""Small deterministic collaborators for graph_v2 state-machine tests."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import UUID

from agent_core.runner import AgentDependencies
from agent_data.records import Coverage, RecordChunk, RecordStats
from agent_data.vault import VaultItem, VaultRecall, VaultSnippet
from generated import agent_pb2, common_pb2


USER_ID = UUID("00000000-0000-0000-0000-000000000001")
SESSION_ID = UUID("10000000-0000-0000-0000-000000000001")


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds: float):
        self.now += seconds


class FakeLlm:
    def __init__(self, *, intent=None, plans=(), answer="可以从这条记录看出来[1]。", clock=None, plan_advance=0, on_plan_yield=None):
        self.intent = intent or {
            "action_kind": "read",
            "route": "semantic",
            "standalone_question": "测试问题",
            "content_type": None,
            "moods": [],
            "time_expression": {"kind": "none"},
            "file_query_mode": "none",
        }
        self.plans = list(plans)
        self.answer = answer
        self.clock = clock
        self.plan_advance = plan_advance
        self.on_plan_yield = on_plan_yield
        self.plan_calls = 0
        self.answer_calls = 0
        self.closed_streams = 0
        self.closed = False

    def json_task(self, messages, **kwargs):
        self.intent_kwargs = kwargs
        return json.dumps(self.intent, ensure_ascii=False)

    def chat_stream(self, messages, **kwargs):
        system = messages[0]["content"]
        is_plan = "read-only planner for Mirror" in system

        def generate():
            try:
                if is_plan:
                    self.plan_calls += 1
                    raw = self.plans.pop(0) if self.plans else '{"calls": [], "done": true}'
                    if isinstance(raw, BaseException):
                        raise raw
                    if self.clock is not None and self.plan_advance:
                        self.clock.advance(self.plan_advance)
                    if self.on_plan_yield is not None:
                        self.on_plan_yield()
                    yield "content", str(raw)
                else:
                    self.answer_calls += 1
                    yield "content", self.answer
            finally:
                self.closed_streams += 1

        return generate()

    def close(self):
        self.closed = True


class FakeEmbedder:
    def __init__(self):
        self.calls = []
        self.closed = False

    def embed(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return [0.0] * 1024

    def close(self):
        self.closed = True


class FakeGlossary:
    def __init__(self, terms=()):
        self.terms = list(terms)
        self.calls = []

    def top_confirmed(self, scope, *, limit):
        self.calls.append((scope, limit))
        return list(self.terms[:limit])


class FakeHistory:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.calls = []

    def recent(self, scope, limit):
        self.calls.append((scope, limit))
        return list(self.messages[-limit:])


class FakeRecords:
    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.search_calls = []
        self.vector_calls = []

    def search(self, scope, filters=None, *, limit=20, record_id=None, offset=0):
        self.search_calls.append((scope, filters, limit))
        rows = self.chunks
        if record_id is not None:
            rows = [row for row in rows if row.record_id == record_id]
        keyword = getattr(filters, "keyword", None)
        if keyword:
            rows = [row for row in rows if keyword.casefold() in row.segment.casefold()]
        return list(rows[offset:offset + limit])

    def search_vector(self, scope, vector, filters=None, **kwargs):
        self.vector_calls.append((scope, vector, filters, kwargs))
        rows = self.chunks
        keyword = getattr(filters, "keyword", None)
        if keyword:
            rows = [row for row in rows if keyword.casefold() in row.segment.casefold()]
        return list(rows[:kwargs.get("limit", 5)])

    def stats(self, scope, window=None):
        return RecordStats(len(self.chunks), {"calm": len(self.chunks)}, window.start_at, window.end_at)

    def coverage(self, scope, keyword):
        return Coverage(None, None, 0, 0)


class FakeTodos:
    def stats(self, scope):
        return SimpleNamespace(total=0, by_status={})


class FakeProfiles:
    def recent(self, scope, *, limit, month=None):
        return []

    def compare(self, scope, first_id, second_id):
        return None


class FakeVault:
    def __init__(self, items=(), *, recall=None, fail_find=False):
        self.items = list(items)
        self.recall_result = recall
        self.fail_find = fail_find
        self.find_calls = []
        self.recall_calls = []

    def find(self, scope, query, filters, embedder, **kwargs):
        self.find_calls.append((scope, query, embedder, kwargs))
        if self.fail_find:
            raise RuntimeError("database is unavailable")
        return list(self.items[:kwargs.get("limit", 10)])

    def recall(self, scope, item_id, query, embedder, **kwargs):
        self.recall_calls.append((scope, item_id, query, embedder, kwargs))
        if self.recall_result is None or self.recall_result.item.id != item_id:
            return None
        return self.recall_result

    def metadata(self, scope, item_id):
        return next((item for item in self.items if item.id == item_id), None)


class FakeRepositories:
    def __init__(self, chunks=(), items=(), *, recall=None, fail_find=False):
        self.records = FakeRecords(chunks)
        self.vault = FakeVault(items, recall=recall, fail_find=fail_find)
        self.glossary = FakeGlossary()
        self.history = FakeHistory()
        self.todos = FakeTodos()
        self.profiles = FakeProfiles()


def make_chunk(chunk_id=1, record_id=10, text="镜子项目完成了", *, created_at=None):
    return RecordChunk(
        chunk_id=chunk_id,
        record_id=record_id,
        user_id=USER_ID,
        segment=text,
        metadata={"contentType": "work", "mood": ["calm"], "title": "工作记录"},
        embedding=[0.0] * 1024,
        created_at=created_at or datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc),
    )


def make_vault_item(item_id=31, *, digest_status="confirmed"):
    return VaultItem(
        id=item_id,
        user_id=USER_ID,
        original_name="设计稿.pdf",
        mime="application/pdf",
        size_bytes=2048,
        category="work",
        description="AI架构设计稿",
        digest_status=digest_status,
        created_at=datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc),
        match_layer="metadata",
    )


def make_recall(item=None):
    item = item or make_vault_item()
    snippet = VaultSnippet(
        chunk_id=55,
        record_id=77,
        text="架构分成 Java 入口与 Python Agent。",
        created_at=item.created_at,
        distance=0.1,
    )
    return VaultRecall(item=item, snippets=(snippet,), complete=True, content_scope="full")


def make_request(*, run_id="run-1", question="查一下我的记录", intent=None, embedding=True, **kwargs):
    llm_config = common_pb2.LlmConfig(provider="openai", protocol=common_pb2.OPENAI, api_key="test-key", base_url="https://example.test", model="test-model")
    embedding_config = common_pb2.EmbeddingConfig(source="api", api_provider="openai", api_key="embed-key", api_model="embed-model", base_url="https://embed.test") if embedding else common_pb2.EmbeddingConfig()
    request = agent_pb2.AgentRunRequest(
        run_id=run_id,
        user_id=str(USER_ID),
        session_id=str(SESSION_ID),
        user_message_id=904,
        question=question,
        llm_config=llm_config,
        embedding_config=embedding_config,
        reference_date="2026-10-20",
        timezone="Asia/Shanghai",
        max_plan_rounds=4,
        total_budget_ms=480_000,
        loop_budget_ms=180_000,
        half_life_days=30.0,
        **kwargs,
    )
    return request


def make_dependencies(llm: FakeLlm, repositories: FakeRepositories, *, clock=None, embedding=True):
    return AgentDependencies(
        repositories=repositories,
        llm_factory=lambda **kwargs: llm,
        embedding_factory=(lambda **kwargs: FakeEmbedder()) if embedding else (lambda **kwargs: None),
        clock=clock or __import__("time").monotonic,
    )
