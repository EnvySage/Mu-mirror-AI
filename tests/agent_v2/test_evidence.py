from datetime import datetime, timezone

from agent_core.evidence import assign_citations, merge_evidence, render_observations
from agent_core.types import Evidence, ToolObservation


def test_find_and_recall_for_one_vault_item_share_file_citation():
    found = Evidence("vault:303", "vault", vault_item_id=303, text="design.pdf", origin="find_item")
    body = Evidence("vault:303:chunk:120", "vault", record_id=30, chunk_id=120,
                    vault_item_id=303, text="design body", origin="recall_item")

    index = assign_citations([found, body])

    assert index.marker_for(found.evidence_id) == "[F1]"
    assert index.marker_for(body.evidence_id) == "[F1]"
    assert len(index.vault_items) == 1


def test_merge_deduplicates_chunks_but_keeps_different_chunks_of_same_record():
    first = Evidence("chunk:10", "diary", record_id=1, chunk_id=10, text="a")
    duplicate = Evidence("chunk:10", "diary", record_id=1, chunk_id=10, text="duplicate")
    other = Evidence("chunk:11", "diary", record_id=1, chunk_id=11, text="b")

    merged = merge_evidence([first], [duplicate, other])
    index = assign_citations(merged)

    assert [item.chunk_id for item in merged] == [10, 11]
    assert index.marker_for("chunk:10") == "[1]"
    assert index.marker_for("chunk:11") == "[2]"
    assert index.source_for("[2]").record_id == 1


def test_profiles_are_not_sources_and_cited_sources_keep_sparse_numbers():
    profile = Evidence("profile:80", "profile", record_id=None, text="profile text")
    one = Evidence("chunk:1", "diary", record_id=1, chunk_id=1, text="first")
    two = Evidence("chunk:2", "diary", record_id=2, chunk_id=2, text="second")
    index = assign_citations([profile, one, two])

    assert index.marker_for("profile:80") is None
    sources = index.sources_for("结论来自 [2]。")
    assert [source.n for source in sources] == [2]
    assert sources[0].record_id == 2
    assert "[get_stats]" not in index.sources_for("[get_stats]")


def test_observation_rendering_includes_useful_data_and_marks_truncation():
    observation = ToolObservation("call", "get_stats", True, "OK", "有统计数据", {
        "records": {"count": 0, "moods": {"calm": 2}},
        "window": {"start": "2026-10-01", "end": "2026-10-20"},
    }, has_material=True)
    rendered = render_observations([observation], max_chars=1000)
    assert "calm" in rendered and "0" in rendered and "2026-10-01" in rendered
    second = ToolObservation("call-2", "get_profile", False, "UNAVAILABLE", "database unavailable")
    clipped = render_observations([observation, second], max_chars=40).split("\n\n")
    assert len(clipped) == 2
    assert all(len(block) <= 40 for block in clipped)
    assert "truncated" in clipped[0]


def test_agent_answer_prompt_has_history_once_and_filters_fake_citations_per_chunk(monkeypatch):
    from types import SimpleNamespace

    from services.answer_service import build_agent_answer_messages, stream_agent_answer

    history = [SimpleNamespace(role="assistant", content="以前提到的方案")]
    diary = Evidence("chunk:1", "diary", record_id=1, chunk_id=1, text="支持事实")
    vault = Evidence("vault:3", "vault", vault_item_id=3, text="file.pdf")
    evidence = [diary, vault]
    index = assign_citations(evidence)
    messages = build_agent_answer_messages("当前问题", history, evidence, [], citation_index=index)
    assert sum(message["content"].count("以前提到的方案") for message in messages) == 1
    assert "[1]" in messages[0]["content"] and "[F1]" in messages[0]["content"]

    class FakeLlm:
        def __init__(self):
            self.options = None

        def chat_stream(self, _messages, *, timeout=None, max_retries=None):
            self.options = (timeout, max_retries)
            yield "content", "[1]支持事实"
            yield "content", " [save_item]错误标记 [F1]文件依据 [F9]"

    llm = FakeLlm()
    fake_tool = ToolObservation("tool", "save_item", True, "OK", "read only", has_material=True)
    events = list(stream_agent_answer(messages, evidence, [fake_tool], llm, citation_index=index, timeout=1.5))
    done = events[-1]
    answer = "".join(event.content for event in events if event.kind == "delta")
    assert answer == "[1]支持事实错误标记 [F1]文件依据"
    assert [source.n for source in done.sources] == [1]
    assert [ref.vault_item_id for ref in done.vault_refs] == [3]
    assert llm.options == (1.5, 0)


def test_agent_planner_prompt_contains_retrieval_file_ids_and_mood_distribution():
    from types import SimpleNamespace

    from services.answer_service import build_agent_plan_messages

    observation = ToolObservation("c1", "get_stats", True, "OK", "统计", {
        "records": {"count": 2, "moods": {"calm": 2}},
    }, has_material=True)
    file_observation = ToolObservation("c2", "find_item", True, "OK", "文件", {
        "items": [{"vault_item_id": 303}],
    }, has_material=True)
    messages = build_agent_plan_messages(
        "分析一下", [SimpleNamespace(role="user", content="之前的问题")], [], [],
        [observation, file_observation], retrieval_summary="HYBRID 命中2段",
        evidence=[Evidence("chunk:9", "diary", record_id=90, chunk_id=9, text="初始检索摘录")],
        step=2, max_steps=4, today="2026-10-20",
    )
    prompt = messages[0]["content"]
    assert "HYBRID 命中2段" in prompt
    assert "303" in prompt and "calm" in prompt
    assert "之前的问题" in prompt
    assert "初始检索摘录" in prompt


def test_agent_answer_cancellation_closes_the_underlying_stream():
    from services.answer_service import stream_agent_answer

    evidence = [Evidence("chunk:1", "diary", record_id=1, chunk_id=1, text="fact")]

    class FakeLlm:
        def __init__(self):
            self.closed = False

        def chat_stream(self, _messages, *, timeout=None, max_retries=None):
            try:
                yield "content", "first chunk"
                yield "content", "second chunk"
            finally:
                self.closed = True

    llm = FakeLlm()
    stream = stream_agent_answer([], evidence, [], llm)
    assert next(stream).content == "first chunk"
    stream.close()
    assert llm.closed is True
