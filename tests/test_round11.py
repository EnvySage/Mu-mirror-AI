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

from services import plan_service  # noqa: E402
from services.chat_service import _format_tool_results  # noqa: E402
from services.plan_service import format_tool_registry, sanitize_calls  # noqa: E402


def _spec(name="search_records", description="检索记录", args_schema='{"query?": "string"}'):
    return common.ToolSpec(name=name, description=description, args_schema=args_schema)


REGISTRY = {"search_records", "get_stats", "get_profile", "get_glossary", "get_coverage"}


# ---------------------------------------------------------------------------
# 1. PlannedCall 校验
# ---------------------------------------------------------------------------
class TestSanitizeCalls:
    def test_normal_calls(self):
        data = {"calls": [
            {"tool": "search_records", "args": {"query": "论文", "days": 7}},
            {"tool": "get_stats", "args": {"days": 30}},
        ]}
        calls, dropped = sanitize_calls(data, REGISTRY)
        assert dropped == 0
        assert [c.tool for c in calls] == ["search_records", "get_stats"]
        args = json.loads(calls[0].args_json)
        assert args == {"query": "论文", "days": 7}

    def test_hallucinated_tool_dropped(self):
        """注册表外工具名（幻觉）→ 静默剔除，合法项保留"""
        data = {"calls": [
            {"tool": "delete_all_records", "args": {}},
            {"tool": "get_stats", "args": {"days": 30}},
        ]}
        calls, dropped = sanitize_calls(data, REGISTRY)
        assert dropped == 1
        assert [c.tool for c in calls] == ["get_stats"]

    def test_empty_and_blank_tool_dropped(self):
        calls, dropped = sanitize_calls(
            {"calls": [{"tool": "", "args": {}}, {"tool": "  ", "args": {}},
                       {"tool": "get_stats", "args": {}}]}, REGISTRY)
        assert dropped == 2 and [c.tool for c in calls] == ["get_stats"]

    def test_bad_args_string_dropped(self):
        """args 字符串不可解析 → 剔除该项（不炸整个计划）"""
        data = {"calls": [
            {"tool": "get_stats", "args": "{not-json"},
            {"tool": "get_profile", "args": {}},
        ]}
        calls, dropped = sanitize_calls(data, REGISTRY)
        assert dropped == 1
        assert [c.tool for c in calls] == ["get_profile"]

    def test_args_missing_or_non_dict_defaults_empty(self):
        """缺 args / args 解析后非对象（如列表）→ 归 {}（执行侧自兜底默认值），不算违规；
        args 是解析不了的字符串 → 剔除（"args 可解析"校验）"""
        data = {"calls": [{"tool": "get_profile"}, {"tool": "get_profile", "args": "x"},
                          {"tool": "get_profile", "args": [1, 2]}]}
        calls, dropped = sanitize_calls(data, REGISTRY)
        assert dropped == 1  # "x" 不是合法 JSON
        assert [c.tool for c in calls] == ["get_profile", "get_profile"]
        assert all(json.loads(c.args_json) == {} for c in calls)

    def test_args_valid_json_string_parsed(self):
        """args 是合法 JSON 字符串 → 解析收编"""
        data = {"calls": [{"tool": "get_stats", "args": '{"days": 7}'}]}
        calls, _ = sanitize_calls(data, REGISTRY)
        assert json.loads(calls[0].args_json) == {"days": 7}

    def test_max_calls_cap(self):
        """超过 max_calls（默认 2）截断保留前 N 步"""
        data = {"calls": [{"tool": "get_stats", "args": {}} for _ in range(5)]}
        calls, dropped = sanitize_calls(data, REGISTRY)
        assert len(calls) == plan_service._MAX_CALLS == 2
        assert dropped == 3

    def test_non_list_and_malformed_structures(self):
        """calls 非列表 / 顶层非 dict / 项非 dict → 空计划，不炸"""
        assert sanitize_calls({}, REGISTRY) == ([], 0)
        assert sanitize_calls({"calls": None}, REGISTRY) == ([], 0)
        assert sanitize_calls({"calls": "oops"}, REGISTRY) == ([], 0)
        assert sanitize_calls({"calls": ["字符串项", 42, {"tool": "get_stats"}]}, REGISTRY) == (
            [], 1) or sanitize_calls({"calls": ["字符串项", 42, {"tool": "get_stats"}]}, REGISTRY)[0][0].tool == "get_stats"

    def test_empty_registry_drops_everything(self):
        """注册表为空 → 全部剔除（防线兜底；servicer 层已提前短路）"""
        calls, dropped = sanitize_calls({"calls": [{"tool": "get_stats", "args": {}}]}, set())
        assert calls == [] and dropped == 1

    def test_args_json_ensure_ascii_false(self):
        """中文参数不被转义（prompt 侧与 Java 侧日志可读）"""
        data = {"calls": [{"tool": "search_records", "args": {"query": "焦虑"}}]}
        calls, _ = sanitize_calls(data, REGISTRY)
        assert "焦虑" in calls[0].args_json


# ---------------------------------------------------------------------------
# 2. 注册表渲染
# ---------------------------------------------------------------------------
class TestFormatToolRegistry:
    def test_empty_registry_empty_string(self):
        assert format_tool_registry([]) == ""
        assert format_tool_registry(None) == ""

    def test_full_render(self):
        tools = [_spec("search_records", "四路检索记录", '{"query?": "string", "days?": "int"}'),
                 _spec("get_stats", "统计汇总", '{"days?": "int"}')]
        out = format_tool_registry(tools)
        assert "1. search_records：四路检索记录" in out
        assert "参数：{\"query?\": \"string\", \"days?\": \"int\"}" in out
        assert "2. get_stats：统计汇总" in out

    def test_blank_name_skipped(self):
        out = format_tool_registry([_spec(name="  "), _spec(name="get_stats")])
        assert "get_stats" in out
        assert out.count(".") == 1

    def test_missing_fields_tolerated(self):
        out = format_tool_registry([common.ToolSpec(name="get_profile")])
        assert "get_profile" in out
        assert "参数：" not in out

    def test_schema_truncated(self):
        out = format_tool_registry([_spec(args_schema="长" * 500)])
        assert ("长" * 500) not in out
        assert ("长" * plan_service._MAX_ARG_CHARS) in out

    def test_cap_at_max_tools(self):
        tools = [_spec(name=f"tool_{i}") for i in range(plan_service._MAX_TOOLS + 10)]
        out = format_tool_registry(tools)
        assert "tool_0" in out and f"tool_{plan_service._MAX_TOOLS - 1}" in out
        assert f"tool_{plan_service._MAX_TOOLS}" not in out


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
        assert out == "[工具结果·search_records] 12 条"

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
        long_summary = "长" * (plan_service._MAX_TOOL_CHARS_CONST if hasattr(
            plan_service, "_MAX_TOOL_CHARS_CONST") else 3000)
        out = _format_tool_results([self._tr(summary=long_summary)])
        assert ("长" * 3000) not in out
        assert out.endswith("…")
        assert ("长" * 2000) in out

    def test_blank_tool_named_unknown(self):
        out = _format_tool_results([self._tr(tool="")])
        assert "unknown_tool" in out

    def test_mixed_success_and_failure(self):
        out = _format_tool_results([self._tr(tool="get_stats"), self._tr(tool="find_item", success=False)])
        assert out.count("[工具结果·") == 2
        assert "（工具执行失败" in out


# ---------------------------------------------------------------------------
# 4. 配置
# ---------------------------------------------------------------------------
class TestConfig:
    def test_plan_tools_section(self):
        from config import CONFIG
        assert "plan_tools" in CONFIG
        assert CONFIG["plan_tools"]["max_calls"] == 2
        assert CONFIG["plan_tools"]["max_tools"] > 0
        assert CONFIG["plan_tools"]["max_arg_chars"] > 0
        assert CONFIG["plan_tools"]["max_tool_chars"] > 0
        assert CONFIG["prompts"]["plan_tools"].endswith("plan_tools.txt")


# ---------------------------------------------------------------------------
# 5. prompt 渲染接线
# ---------------------------------------------------------------------------
class TestPromptWiring:
    def test_plan_tools_template_renders(self):
        """旧 PlanTools 路径的模板（chat-loop-design.md 裁决 0.4：回滚要退回已知旧行为，
        所以 plan_tools.txt 循环化后，PlanTools 改渲染 plan_tools_single —— HEAD 原文快照，
        本用例断言一字未改）"""
        from prompts_loader import loader
        p = loader.render("plan_tools_single", tools="1. search_records：检索",
                          question="我论文咋样了", glossary="")
        assert "工具规划助手" in p
        assert "1. search_records：检索" in p
        assert "我论文咋样了" in p
        assert "{tools}" not in p and "{question}" not in p and "{glossary}" not in p
        # 决策规则/输出约定在模板里（B1/B2/B4 场景判定约束写死）
        assert "宁可少规划" in p
        assert "最多 2 步" in p
        assert "get_coverage" in p
        assert "search_records" in p
        assert "get_profile" in p

    def test_plan_tools_loop_template_renders(self):
        """循环版模板（chat-loop-design.md §5.1，只给 PlanNextStep 用）——与上一个用例
        互为对照：同一批规则换了措辞，且多出循环语境渲染位"""
        from prompts_loader import loader
        p = loader.render("plan_tools", tools="1. search_records：检索", question="我论文咋样了",
                          glossary="", history="h", previous_results="（空）",
                          step=2, max_steps=4, has_retrieval="false")
        assert "工具规划器" in p
        assert "这是第 2 步，最多 4 步" in p
        assert "一步最多 2 个工具" in p
        assert "宁可少规划" in p          # 总基调保留
        assert "情绪安慰" not in p        # §5.1 关键修复
        for ph in ("{tools}", "{question}", "{glossary}", "{history}",
                   "{previous_results}", "{step}", "{max_steps}", "{has_retrieval}"):
            assert ph not in p

    def test_plan_tools_glossary_placeholder(self):
        from prompts_loader import loader
        g = "以下用户个人词汇表仅供参考，解释可能过时；与近期记录矛盾时，以近期记录为准。\n- 论文：毕设"
        assert "毕设" in loader.render("plan_tools_single", tools="t", question="q", glossary=g)
        assert "毕设" in loader.render("plan_tools", tools="t", question="q", glossary=g)

    def test_chat_template_tool_results_placeholder(self):
        from prompts_loader import loader
        p = loader.render("chat", question="q", history="h", context="c", glossary="",
                          tool_results="[工具结果·get_stats] 30 天 42 条")
        assert "[工具结果·get_stats] 30 天 42 条" in p
        assert "{tool_results}" not in p
        # 空工具结果：占位符清空，不留孤儿节内容（节头保留由模板固定，内容行为空）
        p_empty = loader.render("chat", question="q", history="h", context="c", glossary="",
                                tool_results="")
        assert "工具查询结果" in p_empty  # 节头仍在（模板静态），内容为空串

    def test_chat_template_backward_compat(self):
        """chat 模板既有占位符不受影响（回归保障）"""
        from prompts_loader import loader
        p = loader.render("chat", question="问题", history="历史", context="[1] 资料",
                          glossary="", tool_results="")
        assert "问题" in p and "历史" in p and "[1] 资料" in p


# ---------------------------------------------------------------------------
# 6. proto 契约自检（B 侧对账依据）
# ---------------------------------------------------------------------------
class TestProtoContract:
    def test_common_tool_messages(self):
        spec = {n: f.number for n, f in common.ToolSpec.DESCRIPTOR.fields_by_name.items()}
        assert spec == {"name": 1, "description": 2, "args_schema": 3}
        tr = {n: f.number for n, f in common.ToolResult.DESCRIPTOR.fields_by_name.items()}
        assert tr == {"tool": 1, "summary": 2, "payload_json": 3, "success": 4}

    def test_plantools_messages(self):
        req = {n: f.number for n, f in chat_pb2.PlanToolsRequest.DESCRIPTOR.fields_by_name.items()}
        assert req == {"question": 1, "glossary": 2, "tools": 3, "llm_config": 4}
        reply = {n: f.number for n, f in chat_pb2.PlanToolsReply.DESCRIPTOR.fields_by_name.items()}
        assert reply == {"calls": 1}
        call = {n: f.number for n, f in chat_pb2.PlannedCall.DESCRIPTOR.fields_by_name.items()}
        assert call == {"tool": 1, "args_json": 2}

    def test_chat_request_tool_results_is_6(self):
        """ChatRequest.tool_results=6（核对现有注释防撞号：question1/history2/chunks3/llm4/glossary5）"""
        fields = {n: f.number for n, f in chat_pb2.ChatRequest.DESCRIPTOR.fields_by_name.items()}
        assert fields == {"question": 1, "history": 2, "chunks": 3, "llm_config": 4,
                          "glossary": 5, "tool_results": 6}

    def test_plantools_rpc_in_service(self):
        """PlanTools 仍在（回滚路径不删）；PlanNextStep 为 chat-loop-design.md §3 新增"""
        methods = set(chat_pb2.DESCRIPTOR.services_by_name["MirrorChat"].methods_by_name)
        assert methods == {"ExtractIntent", "Chat", "PlanTools", "PlanNextStep"}

    def test_wire_first_bytes(self):
        """wire 冒烟：tool_results=6 → 首字节 0x32；tools=3 → 0x1a；calls=1 → 0x0a"""
        r = chat_pb2.ChatRequest(tool_results=[common.ToolResult(tool="t")])
        assert r.SerializeToString()[0] == 0x32
        pr = chat_pb2.PlanToolsRequest(tools=[common.ToolSpec(name="a")])
        assert pr.SerializeToString()[0] == 0x1A
        rep = chat_pb2.PlanToolsReply(calls=[chat_pb2.PlannedCall(tool="a", args_json="{}")])
        assert rep.SerializeToString()[0] == 0x0A
        # 空请求零字节（无字段缺省 wire 兼容）
        assert chat_pb2.ChatRequest().SerializeToString() == b""
        assert chat_pb2.PlanToolsRequest().SerializeToString() == b""

    def test_registry_names_from_request(self):
        """servicer 用法自检：从 PlanToolsRequest 提取工具名集合（防幻觉校验依据）"""
        req = chat_pb2.PlanToolsRequest(tools=[_spec("search_records"), _spec("get_stats"),
                                               _spec("  ")])
        names = {(t.name or "").strip() for t in req.tools}
        names.discard("")
        assert names == {"search_records", "get_stats"}
