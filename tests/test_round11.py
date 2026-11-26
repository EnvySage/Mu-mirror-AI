"""第十一轮（toolcalling sprint）单测 — PlanTools + tool_results 渲染

覆盖任务书验证标准：
1. PlannedCall 校验（sanitize_calls：注册表外工具名剔除/args 可解析/步数≤2/坏结构不炸）
2. 注册表渲染（format_tool_registry：逐条 name/description/args_schema、空注册表短路、超限截断）
3. tool_results 渲染（_format_tool_results：正常/失败隔离/summary 回退 payload/截断/空列表零影响）
4. 配置（plan_tools 段 max_calls/max_tools/max_arg_chars/max_tool_chars + prompt 路径）
5. prompt 渲染（plan_tools 模板占位符接线 + chat 模板 {tool_results} 占位符）
6. proto 契约自检（ToolSpec/ToolResult/PlanToolsRequest/PlanToolsReply/PlannedCall 字段号 + ChatRequest.tool_results=6，B 侧对账依据）

运行：.venv/Scripts/python.exe -m pytest tests/test_round11.py -v
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from generated import common_pb2 as common  # noqa: E402
from generated import mirror_chat_pb2 as chat_pb2  # noqa: E402

from services.chat_service import _format_tool_results  # noqa: E402


def _spec(name="search_records", description="检索记录", args_schema='{"query?": "string"}', effect=""):
    return common.ToolSpec(name=name, description=description, args_schema=args_schema, effect=effect)


REGISTRY = {"search_records", "get_stats", "get_profile", "get_glossary", "get_coverage"}


# ---------------------------------------------------------------------------
# 1. PlannedCall 校验
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2. 注册表渲染
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 3. tool_results 渲染
# ---------------------------------------------------------------------------
class TestFormatToolResults:
    def _tr(self, tool="search_records", summary="12 条", payload="", success=True):
        return common.ToolResult(tool=tool, summary=summary, payload_json=payload, success=success)

    def test_empty_zero_impact(self):
        """空列表 → 空串（prompt 不留孤儿块，同 glossary 模式）"""
        assert _format_tool_results([]) == ""
        assert _format_tool_results(None) == ""

    def test_summary_rendered(self):
        out = _format_tool_results([self._tr()])
        assert out == "工具 search_records 返回：12 条"

    def test_payload_fallback_when_summary_empty(self):
        out = _format_tool_results([self._tr(summary="", payload='[{"date":"9-01"}]')])
        assert '[{"date":"9-01"}]' in out

    def test_both_empty_placeholder(self):
        out = _format_tool_results([self._tr(summary="", payload="")])
        assert "（无返回数据）" in out

    def test_failed_tool_isolated(self):
        """失败结果只渲染失败说明，不把失败 payload 喂给 LLM"""
        out = _format_tool_results([self._tr(summary="敏感错误堆栈", success=False)])
        assert "敏感错误堆栈" not in out
        assert "工具执行失败" in out
        assert "search_records" in out

    def test_truncation_to_max_tool_chars(self):
        long_summary = "长" * 3000
        out = _format_tool_results([self._tr(summary=long_summary)])
        assert ("长" * 3000) not in out
        assert out.endswith("…")
        assert ("长" * 2000) in out

    def test_blank_tool_named_unknown(self):
        out = _format_tool_results([self._tr(tool="")])
        assert "unknown_tool" in out

    def test_payload_rendered_alongside_summary(self):
        """2026-09-21 联调回归：summary 非空时 payload 也必须渲染（原先被吞，回答模型只看到"12条"）"""
        payload = ('{"count": 2, "records": ['
                   '{"record_id": 7, "title": "开题", "quote": "导师说框架要重做，好焦虑", '
                   '"date": "2026-09-02T23:10:00", "content_type": "work"},'
                   '{"record_id": 9, "title": "", "quote": "又失眠了", "date": "2026-09-05", "content_type": "health"}]}')
        out = _format_tool_results([self._tr(summary="search_records:2条", payload=payload)])
        assert out.startswith("工具 search_records 返回：search_records:2条\n")
        assert "[1] 2026-09-02 23:10（work） 开题：导师说框架要重做，好焦虑" in out  # 保留到分钟
        assert "[2] 2026-09-05（health）：又失眠了" in out  # 无标题不留孤儿空格

    def test_records_rendered_chronologically_with_time(self):
        """问"某一天做了什么"时先后顺序是关键：SQL 是倒序，渲染要转正序并保留时刻"""
        payload = json.dumps({"count": 3, "records": [
            {"record_id": 3, "quote": "我终于能弹出小星星了", "date": "2026-09-12T20:29:24", "content_type": "note"},
            {"record_id": 2, "quote": "春日影还是太难了，从小星星开始吧", "date": "2026-09-12T14:56:01", "content_type": "note"},
            {"record_id": 1, "quote": "直接开始尝试演奏春日影", "date": "2026-09-12T14:34:53", "content_type": "note"},
        ]}, ensure_ascii=False)
        out = _format_tool_results([self._tr(summary="search_records:3条（2026-09-12）", payload=payload)])
        i1, i2, i3 = out.index("14:34"), out.index("14:56"), out.index("20:29")
        assert i1 < i2 < i3

    def test_stats_payload_rendered_as_json_unescaped(self):
        payload = '{"days": 30, "record_count": 12, "moods": {"anxious": 5, "calm": 2}}'
        out = _format_tool_results([self._tr(tool="get_stats", summary="get_stats:记录12条/30天", payload=payload)])
        assert '"anxious": 5' in out and "record_count" in out

    def test_failed_tool_payload_still_hidden(self):
        """失败结果即使带 payload 也不渲染（隔离不因本次改动放松）"""
        out = _format_tool_results([self._tr(summary="x", payload='{"records":[{"quote":"泄露"}]}', success=False)])
        assert "泄露" not in out

    def test_recall_item_excerpts_rendered(self):
        """2026-09-21 联调回归：recall_item 读到正文，回答仍说"只看到上传记录"——摘录原先被丢"""
        payload = json.dumps({
            "item": {"vault_item_id": 3, "display_name": "mirror项目的设计文档.md", "file_type": "md"},
            "quote": "首段", "digest_status": "confirmed",
            "quotes": [{"text": "架构分 B/F/AI 三仓"}, {"text": "gRPC 打通 Java 与 Python"}],
        }, ensure_ascii=False)
        out = _format_tool_results([self._tr(tool="recall_item", summary="recall_item:mirror项目的设计文档.md",
                                             payload=payload)])
        assert "[F1] mirror项目的设计文档.md" in out
        assert "[F1] 正文摘录" in out
        assert "- 架构分 B/F/AI 三仓" in out and "- gRPC 打通 Java 与 Python" in out
        assert "首段" not in out  # 有 quotes 时不重复塞 quote

    def test_find_item_still_metadata_only(self):
        """find_item 只负责找文件，不出正文摘录（payload 没有顶层 quotes/quote）"""
        payload = json.dumps({"count": 1, "items": [
            {"vault_item_id": 3, "display_name": "设计.md", "quote": "命中片段"}]}, ensure_ascii=False)
        out = _format_tool_results([self._tr(tool="find_item", summary="find_item:1个文件", payload=payload)])
        assert "[F1] 设计.md" in out and "正文摘录" not in out

    def test_context_text_when_only_tools_have_data(self):
        from services.chat_service import (
            _context_text, _CONTEXT_EMPTY_WITH_TOOLS, _CONTEXT_NOT_SEARCHED,
            _CONTEXT_SEARCH_EMPTY, _CONTEXT_SEARCH_FAILED,
        )
        stats = common.ToolResult(tool="get_stats", success=True)
        assert _context_text([], [stats]) == _CONTEXT_EMPTY_WITH_TOOLS
        assert _context_text([], []) == _CONTEXT_NOT_SEARCHED
        no_match = common.ToolResult(tool="search_records", success=True,
                                     payload_json='{"count":0,"records":[]}')
        assert _context_text([], [no_match]) == _CONTEXT_SEARCH_EMPTY
        failed = common.ToolResult(tool="search_records", success=False)
        assert _context_text([], [failed]) == _CONTEXT_SEARCH_FAILED

    def test_tool_records_share_citation_sequence_with_rag_chunks(self):
        from services.chat_service import _format_tool_results_with_sources, _extract_sources
        payload = json.dumps({"records": [
            {"record_id": 7, "title": "开题", "quote": "导师让我重做", "date": "2026-09-02",
             "content_type": "work", "score": 0.2},
            {"record_id": 9, "title": "复盘", "quote": "终于理清了", "date": "2026-09-05",
             "content_type": "learning", "score": 0.3},
        ]}, ensure_ascii=False)
        rendered, tool_sources = _format_tool_results_with_sources(
            [self._tr(summary="search_records:2条", payload=payload)], citation_offset=2)
        assert "[3]〔相关度 80%〕" in rendered and "[4]〔相关度 70%〕" in rendered
        chunks = [type("C", (), {"record_id": 1, "title": "旧记录", "content": "x",
                                 "created_at": "2026-09-01"})(),
                  type("C", (), {"record_id": 2, "title": "近期记录", "content": "y",
                                 "created_at": "2026-09-02"})()]
        sources = _extract_sources("参照 [2]、[3] 和 [4]", chunks, tool_sources)
        assert [(s.n, s.record_id) for s in sources] == [(2, 2), (3, 7), (4, 9)]

    def test_truncated_tool_records_do_not_create_invisible_citations(self):
        from services.chat_service import _format_tool_results_with_sources
        payload = json.dumps({"records": [
            {"record_id": 7, "quote": "可见", "date": "2026-09-02"},
            {"record_id": 9, "quote": "隐藏" * 3000, "date": "2026-09-05"},
        ]}, ensure_ascii=False)
        rendered, sources = _format_tool_results_with_sources(
            [self._tr(summary="search_records:2条", payload=payload)])
        assert "[1]" in rendered and "[2]" not in rendered
        assert [s["record_id"] for s in sources] == [7]

    def test_mixed_success_and_failure(self):
        out = _format_tool_results([self._tr(tool="get_stats"), self._tr(tool="find_item", success=False)])
        assert out.count("工具 ") == 2 and "[" not in out
        assert "（工具执行失败" in out


class TestFakeCiteFilter:
    """2026-09-21 联调回归：模型把工具数据"引用"成 [get_stats] 写进回答，prompt 禁止也拦不住"""

    def _run(self, pieces, tools=("get_stats", "search_records")):
        from services.chat_service import _FakeCiteFilter
        f = _FakeCiteFilter(tools)
        return "".join(f.feed(p) for p in pieces) + f.flush()

    def test_drops_tool_name_markers(self):
        assert self._run(["12条焦虑[get_stats]。还有[search_records]一条"]) == "12条焦虑。还有一条"

    def test_drops_tool_result_prefix_and_dot_variant(self):
        assert self._run(["a[工具结果]b[工具结果·get_stats]c[search_records·x]d"]) == "abcd"
        # 实测变体：模型从 prompt 章节标题「工具查询结果」自造
        assert self._run(["空空如也 [工具查询结果]。"]) == "空空如也。"  # 前导空格一起删

    def test_keeps_real_citations(self):
        assert self._run(["你写过 [2] 和 [F1]，还有[12]"]) == "你写过 [2] 和 [F1]，还有[12]"

    def test_keeps_unknown_brackets(self):
        """只拦本轮真实用过的工具名，口语里的 [笑] 之类不误伤"""
        assert self._run(["哈哈[笑]，[find_item]"]) == "哈哈[笑]，[find_item]"

    def test_marker_split_across_chunks(self):
        assert self._run(["统计显示[get_", "sta", "ts]，嗯"]) == "统计显示，嗯"

    def test_unclosed_bracket_flushed_at_end(self):
        assert self._run(["结尾是个 [未闭合"]) == "结尾是个 [未闭合"

    def _run_cap(self, pieces, max_cite):
        from services.chat_service import _FakeCiteFilter
        f = _FakeCiteFilter(("search_records",), max_cite=max_cite)
        return "".join(f.feed(p) for p in pieces) + f.flush()

    def test_out_of_range_citations_dropped(self):
        """2026-09-21 联调实测：检索 0 条、证据全来自工具结果时，模型自编 [4][6][7]"""
        assert self._run_cap(["春日影还是太难了 [6][7]。突飞猛进 [4]。"], 0) == "春日影还是太难了。突飞猛进。"

    def test_in_range_citations_kept(self):
        assert self._run_cap(["你写过 [3]，还有 [6] 和 [0]"], 5) == "你写过 [3]，还有 和"
        assert self._run_cap(["文件 [F2] 不受编号上限影响"], 0) == "文件 [F2] 不受编号上限影响"

    def test_file_citation_capped_by_real_file_count(self):
        """实测：没查任何文件时模型写出 [F1]"""
        from services.chat_service import _FakeCiteFilter
        f = _FakeCiteFilter((), max_cite=0, max_file_cite=0)
        assert f.feed("还记着第二天要问导师 [F1]。") + f.flush() == "还记着第二天要问导师。"
        f = _FakeCiteFilter((), max_cite=0, max_file_cite=2)
        assert f.feed("见 [F2]，另见 [F3]") + f.flush() == "见 [F2]，另见"

    def test_space_kept_when_bracket_is_real(self):
        from services.chat_service import _FakeCiteFilter
        f = _FakeCiteFilter((), max_cite=3)
        assert "".join(f.feed(p) for p in ["A ", "[2] B  C "]) + f.flush() == "A [2] B  C "

    def test_newline_or_overlong_releases_hold(self):
        assert self._run(["[不是标记\n下一行"]) == "[不是标记\n下一行"
        long = "[" + "长" * 60
        assert self._run([long]) == long


# ---------------------------------------------------------------------------
# 4. 配置
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 5. prompt 渲染接线
# ---------------------------------------------------------------------------
class TestPromptWiring:



    def test_chat_template_tool_results_placeholder(self):
        from mirror_core.prompts_loader import loader
        p = loader.render("chat", question="CHAT_QUESTION_42", history="CHAT_HISTORY_77",
                          context="c", glossary="",
                          tool_results="工具 get_stats 返回：30 天 42 条")
        assert "工具 get_stats 返回：30 天 42 条" in p
        assert "CHAT_QUESTION_42" not in p and "CHAT_HISTORY_77" not in p
        assert "{tool_results}" not in p
        # 空工具结果：占位符清空，不留孤儿节内容（节头保留由模板固定，内容行为空）
        p_empty = loader.render("chat", question="q", history="h", context="c", glossary="",
                                tool_results="")
        assert "本轮工具结果" in p_empty  # 节头仍在（模板静态），内容为空串

    def test_chat_template_backward_compat(self):
        """chat 模板既有占位符不受影响（回归保障）"""
        from mirror_core.prompts_loader import loader
        p = loader.render("chat", question="CHAT_USER_Q_MARKER", history="CHAT_HISTORY_MARKER",
                          context="[1] 资料",
                          glossary="", tool_results="")
        assert "CHAT_USER_Q_MARKER" not in p and "CHAT_HISTORY_MARKER" not in p and "[1] 资料" in p


# ---------------------------------------------------------------------------
# 6. proto 契约自检（B 侧对账依据）
# ---------------------------------------------------------------------------
class TestProtoContract:
    def test_common_tool_messages(self):
        spec = {n: f.number for n, f in common.ToolSpec.DESCRIPTOR.fields_by_name.items()}
        assert spec == {"name": 1, "description": 2, "args_schema": 3, "effect": 4}
        tr = {n: f.number for n, f in common.ToolResult.DESCRIPTOR.fields_by_name.items()}
        assert tr == {"tool": 1, "summary": 2, "payload_json": 3, "success": 4}


    def test_chat_request_tool_results_is_6(self):
        """ChatRequest.tool_results=6（核对现有注释防撞号：question1/history2/chunks3/llm4/glossary5）"""
        fields = {n: f.number for n, f in chat_pb2.ChatRequest.DESCRIPTOR.fields_by_name.items()}
        assert fields == {"question": 1, "history": 2, "chunks": 3, "llm_config": 4,
                          "glossary": 5, "tool_results": 6, "assistant_context": 7}

    def test_shared_inference_rpc_methods(self):
        methods = set(chat_pb2.DESCRIPTOR.services_by_name["MirrorChat"].methods_by_name)
        assert methods == {"Chat", "SummarizeDay"}
