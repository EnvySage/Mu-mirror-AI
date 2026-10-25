"""Run the compiled graph through failures that previously looked like success."""
import json
from dataclasses import replace

from agent_core.runner import run_agent
from tests.agent_v2.agent_fakes import (
    FakeLlm, FakeRepositories, make_chunk, make_dependencies, make_recall,
    make_request, make_vault_item,
)


def run(llm, repos, *, question="分析这篇文章"):
    events = list(run_agent(make_request(question=question), make_dependencies(llm, repos)))
    terminal, = [event for event in events if event.kind in {"completed", "failed"}]
    return terminal, events


def body_intent():
    return dict(action_kind="read", route="hybrid", standalone_question="分析设计稿全文",
                content_type=None, moods=[], time_expression={"kind": "none"}, file_query_mode="body")


def plan(tool, args=None, *, done=True):
    return json.dumps({"calls": [{"tool": tool, "args": args or {}}], "done": done})


def test_optional_tool_error_still_analyzes_existing_authorized_evidence():
    llm = FakeLlm(plans=[plan("find_item", {"query": "设计稿"})], answer="记录说明项目已完成[1]。")
    terminal, _ = run(llm, FakeRepositories([make_chunk()], fail_find=True))
    assert terminal.kind == "completed"
    assert terminal.metadata["answer_text"].endswith("记录说明项目已完成[1]。")
    assert terminal.metadata["outcome"] == "partial_answer"
    assert "find_item:UNAVAILABLE" in terminal.metadata["metrics"]["degraded_reasons"]


def test_multiple_file_candidates_clarify_without_answer_model():
    llm = FakeLlm(intent=body_intent(), plans=[plan("find_item", {"query": "设计稿"})])
    terminal, _ = run(llm, FakeRepositories(items=[make_vault_item(31), make_vault_item(32)]))
    assert terminal.kind == "completed"
    assert terminal.metadata["outcome"] == "clarification"
    assert "哪" in terminal.metadata["answer_text"]
    assert llm.answer_calls == 0


def test_unconfirmed_file_body_never_reaches_analysis():
    item = make_vault_item(digest_status="pending")
    llm = FakeLlm(intent=body_intent(), plans=[plan("find_item", {"query": "设计稿"})])
    terminal, _ = run(llm, FakeRepositories(items=[item], recall=make_recall(item)))
    assert llm.answer_calls == 0
    assert "正文" in terminal.metadata["answer_text"]
    assert "没有找到" not in terminal.metadata["answer_text"]
    assert terminal.metadata["outcome"] == "insufficient_material"


def test_empty_answer_is_failure_even_with_valid_evidence():
    llm = FakeLlm(answer="")
    terminal, _ = run(llm, FakeRepositories([make_chunk()]))
    assert terminal.kind == "failed"
    assert terminal.metadata["code"] == "OUTPUT_EMPTY"


def test_invalid_done_call_is_corrected_instead_of_silent_fallback():
    llm = FakeLlm(plans=[plan("does_not_exist"), plan("search_records", {"query": "项目"})])
    repos = FakeRepositories([make_chunk()])
    terminal, events = run(llm, repos)
    assert terminal.kind == "completed"
    assert llm.plan_calls == 2
    assert [event.metadata["code"] for event in events if event.kind == "tool_finished"] == ["UNKNOWN", "OK"]


def test_no_material_tool_error_is_reported_as_read_failure():
    llm = FakeLlm(plans=[plan("find_item", {"query": "设计稿"})])
    terminal, _ = run(llm, FakeRepositories(fail_find=True))
    assert terminal.kind == "failed"
    assert terminal.metadata["code"] == "READ_UNAVAILABLE"
    assert llm.answer_calls == 0


def test_invalid_plan_shape_gets_bounded_correction():
    llm = FakeLlm(plans=['[]', plan("search_records", {"query": "项目"})])
    terminal, _ = run(llm, FakeRepositories([make_chunk()]))
    assert terminal.kind == "completed"
    assert llm.plan_calls == 2


def test_protocol_planner_is_used_and_recorded():
    class Native(FakeLlm):
        def plan(self, messages, tools, **kwargs):
            self.plan_calls += 1
            return {"calls": [], "done": True, "reason": "enough", "protocol": "native", "finish_reason": "stop"}
    llm = Native(answer="依据现有记录分析[1]。")
    terminal, _ = run(llm, FakeRepositories([make_chunk()]))
    assert terminal.kind == "completed"
    assert llm.plan_calls == 1
    assert terminal.metadata["metrics"]["diagnostics"]["planning_protocol"] == "native"


def test_body_pages_are_read_until_last_segment_is_available_to_model():
    from agent_data.vault import VaultRecall, VaultSnippet
    item = make_vault_item()
    class PagedVault:
        def __init__(self): self.offsets = []
        def find(self, *args, **kwargs): return [item]
        def recall(self, scope, item_id, query, embedder, *, offset=0, limit=3, **kwargs):
            self.offsets.append(offset)
            snippets = (VaultSnippet(55 + offset, 77, "开头事实" if offset == 0 else "结尾关键结论", item.created_at),)
            return VaultRecall(item, snippets, next_offset=1 if offset == 0 else None,
                               complete=offset == 1, content_scope="partial" if offset == 0 else "full")
    class Inspecting(FakeLlm):
        def chat_stream(self, messages, **kwargs):
            if "conversation engine" in messages[0]["content"]:
                assert "结尾关键结论" in messages[0]["content"]
            yield from super().chat_stream(messages, **kwargs)
    repos = FakeRepositories()
    repos.vault = PagedVault()
    llm = Inspecting(intent=body_intent(), plans=[plan("find_item", {"query": "设计稿"})], answer="文章结尾给出了关键结论[F1]。")
    terminal, _ = run(llm, repos)
    assert terminal.kind == "completed"
    assert repos.vault.offsets == [0, 1]
    assert terminal.metadata["metrics"]["diagnostics"]["material_status"] == "sufficient"


def test_repeated_bad_arguments_do_not_masquerade_as_empty_search():
    llm = FakeLlm(plans=[plan("search_records", {"unsupported": True})] * 4)
    terminal, _ = run(llm, FakeRepositories())
    assert llm.plan_calls == 4
    assert terminal.kind == "completed"
    assert "工具" in terminal.metadata["answer_text"]
    assert "没有找到" not in terminal.metadata["answer_text"]


def test_zero_todos_is_an_answerable_result_not_missing_diary_material():
    class Todos:
        def list(self, *args, **kwargs): return []
    repos = FakeRepositories()
    repos.todos = Todos()
    llm = FakeLlm(plans=[plan("list_todos")], answer="当前没有待办事项。")
    terminal, _ = run(llm, repos, question="我有哪些待办？")
    assert terminal.metadata["answer_text"] == "当前没有待办事项。"
    assert terminal.metadata["outcome"] == "answer"


def test_body_context_limit_never_claims_full_analysis():
    item = make_vault_item()
    recall = make_recall(item)
    recall = replace(recall, snippets=(replace(recall.snippets[0], text="正文" * 40_000),))
    llm = FakeLlm(intent=body_intent(), plans=[plan("recall_item", {"vault_item_id": item.id})], answer="片段揭示了核心论点[F1]。")
    terminal, _ = run(llm, FakeRepositories(items=[item], recall=recall))
    assert terminal.metadata["outcome"] == "partial_answer"
    assert "不能代表全文" in terminal.metadata["answer_text"]


def test_record_page_from_middle_is_not_accepted_as_whole_record():
    class Records:
        def search(self, scope, filters=None, *, limit=20, record_id=None, offset=0):
            if record_id is None: return []
            return [make_chunk(1, text="第一段"), make_chunk(2, text="最后一段")][offset:offset + limit]
        def search_vector(self, *args, **kwargs): return []
    repos = FakeRepositories()
    repos.records = Records()
    class Inspect(FakeLlm):
        def chat_stream(self, messages, **kwargs):
            if "conversation engine" in messages[0]["content"]:
                assert "第一段" in messages[0]["content"]
            yield from super().chat_stream(messages, **kwargs)
    llm = Inspect(plans=[plan("read_record", {"record_id": 10, "offset": 1})], answer="分析了完整记录[1]。")
    terminal, _ = run(llm, repos)
    assert terminal.kind == "completed"
    assert terminal.metadata["tool_call_count"] == 2


def test_planner_service_error_does_not_retry_like_malformed_json():
    from mirror_core.errors import LlmUnavailableError
    class Unavailable(FakeLlm):
        def plan(self, messages, tools, **kwargs):
            self.plan_calls += 1
            raise LlmUnavailableError("synthetic unavailable")
    llm = Unavailable(answer="已有记录仍可分析[1]。")
    terminal, _ = run(llm, FakeRepositories([make_chunk()]))
    assert llm.plan_calls == 1
    assert terminal.kind == "completed"
    assert terminal.metadata["outcome"] == "partial_answer"


def test_initial_vector_query_failure_can_recover_through_keyword_tool():
    class Records:
        def search_vector(self, *args, **kwargs): raise RuntimeError("vector unavailable")
        def search(self, *args, **kwargs): return [make_chunk()]
    repos = FakeRepositories()
    repos.records = Records()
    llm = FakeLlm(plans=[plan("search_records", {"query": "项目"})])
    terminal, _ = run(llm, repos)
    assert terminal.kind == "completed"
    assert terminal.metadata["sources"][0].record_id == 10
    assert terminal.metadata["metrics"]["degraded_reasons"]


def test_ordered_body_read_replaces_earlier_excerpt_of_the_same_chunk():
    item = make_vault_item()
    full = "开头" * 2000 + "关键结论在文章最后，不得遗漏。"
    class Vault:
        def recall(self, scope, item_id, query, embedder, **kwargs):
            result = make_recall(item)
            snippet = replace(result.snippets[0], text=full[:500] if query else full)
            return replace(result, snippets=(snippet,), complete=not query,
                           content_scope="excerpt" if query else "full")
    class Inspect(FakeLlm):
        def chat_stream(self, messages, **kwargs):
            if "conversation engine" in messages[0]["content"]:
                assert "关键结论在文章最后，不得遗漏。" in messages[0]["content"]
            yield from super().chat_stream(messages, **kwargs)
    repos = FakeRepositories()
    repos.vault = Vault()
    llm = Inspect(intent=body_intent(), plans=[plan("recall_item", {"vault_item_id": item.id, "query": "结论"})])
    terminal, _ = run(llm, repos)
    assert terminal.kind == "completed"
    assert terminal.metadata["metrics"]["diagnostics"]["material_status"] == "sufficient"


def test_answer_transport_failure_preserves_visible_partial_status():
    from mirror_core.errors import LlmUnavailableError
    class Disconnect(FakeLlm):
        def answer_stream(self, messages, **kwargs):
            yield "content", "已经生成的分析正文。"
            raise LlmUnavailableError("synthetic disconnection")
    terminal, events = run(Disconnect(), FakeRepositories([make_chunk()]))
    assert any(event.kind == "delta" for event in events)
    assert terminal.kind == "failed"
    assert terminal.metadata["has_partial_answer"] is True
    assert terminal.metadata["stop_reason"] == "output_error"
    assert terminal.metadata["code"] == "OUTPUT_UNAVAILABLE"
