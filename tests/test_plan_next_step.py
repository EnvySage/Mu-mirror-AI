"""PlanNextStep 循环版规划单测 —— chat-loop-design.md §3/§5.1

覆盖任务书验收点：
1. proto 契约：PlanNextStep 为 unary_stream；PlanStepChunk thinking=1/calls=2/done=3/final=4；
   PlanNextStepRequest 九个字段号与设计稿逐一对齐
2. 流式时序：thinking 块先到、终帧最后到（final=True 只有一帧，且只有它带 calls/done）
3. 注册表外的工具名（幻觉）被 sanitize_calls 剔除，注册表内的保留
4. {"calls":[],"done":true} → 终帧 calls 空 + done=True
5. 注册表为空 → 不调 LLM（create_llm 被替换成会炸的桩，调了就失败），直接终帧 done=True
6. done 语义一致性：calls 为空时 done 恒为 True（B 侧两个终止条件语义对齐）
7. prompt 渲染：五个新占位符（previous_results/step/max_steps/has_retrieval/history）
   全部落值，模板里不残留字面量 {step}
8. 用 chat_stream 而不是 json_task（黑屏根因修复的对账点）

运行：./venv/Scripts/python.exe -m pytest tests/test_plan_next_step.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from generated import common_pb2  # noqa: E402
from generated import mirror_chat_pb2 as chat_pb2  # noqa: E402
from generated import mirror_chat_pb2_grpc as chat_grpc  # noqa: E402

from errors import AiServiceError  # noqa: E402
from services.plan_service import (  # noqa: E402
    MirrorChatServicer,
    format_previous_results,
    resolve_done,
)

# ---------------------------------------------------------------------------
# 桩
# ---------------------------------------------------------------------------


class _ScriptedLlm:
    """预置 (kind, text) 序列的桩 LLM。

    记录 chat_stream / json_task 被调用的情况——PlanNextStep 必须走 chat_stream
    （json_task 非流式 + 关思考，是"黑屏 20s"的根因）。
    """

    def __init__(self, pieces):
        self._pieces = pieces
        self.stream_calls: list[list[dict]] = []
        self.thinking_budgets: list = []
        self.json_task_calls = 0

    def chat_stream(self, messages, temperature=0.7, thinking_budget=None):
        self.stream_calls.append(messages)
        self.thinking_budgets.append(thinking_budget)
        yield from self._pieces

    def json_task(self, messages, temperature=0.7):
        self.json_task_calls += 1
        raise AssertionError("PlanNextStep 不得走 json_task（非流式 + 关思考）")

    @property
    def prompt(self) -> str:
        return self.stream_calls[0][0]["content"]


class _JsonTaskLlm:
    """旧 PlanTools 走 json_task 的桩：捕获 prompt，返回空计划"""

    def __init__(self):
        self.prompt = ""

    def json_task(self, messages, temperature=0.7):
        self.prompt = messages[0]["content"]
        return '{"calls": []}'


class _ExplodingLlmFactory:
    """注册表为空时必须完全不碰 LLM——被调到就直接失败"""

    def __init__(self):
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        raise AssertionError("注册表为空时不应创建 LLM")


class _CapturedContext:
    """grpc.ServicerContext 最小桩（只服务于 abort 路径）"""

    def __init__(self):
        self.aborted = None

    def abort(self, code, details):
        self.aborted = (code, details)
        raise RuntimeError(f"abort: {code} {details}")


_REGISTRY = [
    common_pb2.ToolSpec(name="get_stats", description="统计汇总", args_schema='{"days": int}'),
    common_pb2.ToolSpec(name="search_records", description="检索记录", args_schema='{"moods": []}'),
]


def _request(**kw):
    params = dict(question="我最近特别焦虑，怎么办？", tools=_REGISTRY, step=1, max_steps=4)
    params.update(kw)
    return chat_pb2.PlanNextStepRequest(**params)


def _run(servicer, request, llm, context=None):
    """替换 create_llm 跑一次 PlanNextStep，返回 chunk 列表"""
    ctx = context or _CapturedContext()
    globals_ = servicer.PlanNextStep.__globals__
    orig = globals_["create_llm"]
    globals_["create_llm"] = llm if callable(llm) and not hasattr(llm, "chat_stream") \
        else (lambda **kwargs: llm)
    try:
        return list(servicer.PlanNextStep(request, ctx))
    finally:
        globals_["create_llm"] = orig


# ---------------------------------------------------------------------------
# 1. proto 契约
# ---------------------------------------------------------------------------
class TestProtoContract:
    def test_plan_next_step_is_unary_stream(self):
        """服务端流式（B 侧逐块读 thinking 的前提）"""
        method = chat_pb2.DESCRIPTOR.services_by_name["MirrorChat"] \
            .methods_by_name["PlanNextStep"]
        assert method.client_streaming is False
        assert method.server_streaming is True
        assert method.input_type.name == "PlanNextStepRequest"
        assert method.output_type.name == "PlanStepChunk"

    def test_servicer_registers_unary_stream_handler(self):
        """生成的 grpc 适配层把 PlanNextStep 挂成 unary_stream_rpc_method_handler"""
        captured = {}

        class _FakeServer:
            def add_generic_rpc_handlers(self, generic_handlers):
                pass

            def add_registered_method_handlers(self, service, handlers):
                captured.update(handlers)

        chat_grpc.add_MirrorChatServicer_to_server(MirrorChatServicer(), _FakeServer())
        h = captured["PlanNextStep"]
        assert h.request_streaming is False and h.response_streaming is True
        assert captured["PlanTools"].response_streaming is False  # 回滚路径仍是一元

    def test_plan_step_chunk_field_numbers(self):
        fields = chat_pb2.PlanStepChunk.DESCRIPTOR.fields_by_name
        assert fields["thinking"].number == 1
        assert fields["calls"].number == 2
        assert fields["done"].number == 3
        assert fields["final"].number == 4
        assert fields["thinking"].has_presence is True  # proto3 optional

    def test_plan_next_step_request_field_numbers(self):
        fields = chat_pb2.PlanNextStepRequest.DESCRIPTOR.fields_by_name
        expected = {"question": 1, "glossary": 2, "tools": 3, "llm_config": 4,
                    "history": 5, "previous_results": 6, "step": 7,
                    "max_steps": 8, "has_retrieval": 9}
        assert {k: fields[k].number for k in expected} == expected

    def test_plan_tools_kept(self):
        """回滚路径不许删：PlanTools 方法与消息都还在"""
        assert callable(getattr(MirrorChatServicer, "PlanTools", None))
        assert chat_pb2.PlanToolsRequest.DESCRIPTOR.fields_by_name["question"].number == 1


# ---------------------------------------------------------------------------
# 2. 流式时序：thinking 先到、终帧最后到
# ---------------------------------------------------------------------------
class TestStreamOrdering:
    def test_thinking_chunks_precede_final_frame(self):
        llm = _ScriptedLlm([
            ("thinking", "检索空了，"),
            ("thinking", "先摸底。"),
            ("content", '{"calls": [{"tool": "get_stats", "args": {"days": 30}}], '),
            ("content", '"done": false}'),
        ])
        chunks = _run(MirrorChatServicer(), _request(), llm)

        assert len(chunks) == 3
        # 前两块：纯 thinking，不带 calls/done/final
        assert [c.thinking for c in chunks[:2]] == ["检索空了，", "先摸底。"]
        assert all(c.HasField("thinking") for c in chunks[:2])
        assert all(not c.final and not c.done and len(c.calls) == 0 for c in chunks[:2])
        # 终帧：final=True，带 calls
        last = chunks[-1]
        assert last.final is True
        assert not last.HasField("thinking")
        assert [(c.tool, c.args_json) for c in last.calls] == [
            ("get_stats", '{"days": 30}')]
        assert last.done is False

    def test_exactly_one_final_frame(self):
        llm = _ScriptedLlm([("thinking", "a"), ("thinking", "b"), ("thinking", "c"),
                            ("content", '{"calls": [], "done": true}')])
        chunks = _run(MirrorChatServicer(), _request(), llm)
        assert sum(1 for c in chunks if c.final) == 1
        assert chunks[-1].final is True

    def test_no_thinking_model_yields_only_final_frame(self):
        """模型不发思考块（json_task 那类端点/不支持思考的模型）→ 只有终帧，B 侧零影响"""
        llm = _ScriptedLlm([("content", '{"calls": [], "done": true}')])
        chunks = _run(MirrorChatServicer(), _request(), llm)
        assert len(chunks) == 1 and chunks[0].final and chunks[0].done

    def test_content_not_leaked_as_thinking(self):
        """正文是 JSON，绝不能外推（否则 SSE thinking 里出现 {"calls":…}）"""
        llm = _ScriptedLlm([("content", '{"calls": [], '), ("content", '"done": true}')])
        chunks = _run(MirrorChatServicer(), _request(), llm)
        assert all(not c.HasField("thinking") for c in chunks)

    def test_uses_chat_stream_not_json_task(self):
        """黑屏根因修复的对账点：走 chat_stream，json_task 一次都不调"""
        llm = _ScriptedLlm([("content", '{"calls": [], "done": true}')])
        _run(MirrorChatServicer(), _request(), llm)
        assert len(llm.stream_calls) == 1
        assert llm.json_task_calls == 0


# ---------------------------------------------------------------------------
# 3. 注册表校验（防幻觉工具名）
# ---------------------------------------------------------------------------
class TestPlannerThinkingBudget:
    """2026-09-21 联调回归：规划器沿用全局 2048 思考预算时，mimo-v2.5（~36 token/s）
    光思考 ~57s，第 1 步必撞 B 侧 60s 单步 deadline，循环一个工具都执行不了。"""

    def test_plan_next_step_passes_planner_budget(self):
        import services.plan_service as ps
        llm = _ScriptedLlm([("content", '{"calls": [], "done": true}')])
        _run(MirrorChatServicer(), _request(), llm)
        assert llm.thinking_budgets == [ps._PLAN_THINKING_BUDGET]

    def test_planner_budget_smaller_than_answer_budget(self):
        """规划器预算必须小于最终回答预算，否则等于没修"""
        import services.plan_service as ps
        from config import CONFIG
        assert ps._PLAN_THINKING_BUDGET < int(CONFIG["llm"]["thinking_budget_tokens"])
        # 0（不传 thinking 参数）或 ≥1024（Anthropic extended thinking 下限），中间值会被端点拒
        assert ps._PLAN_THINKING_BUDGET == 0 or ps._PLAN_THINKING_BUDGET >= 1024

    def test_prompt_tells_model_thinking_is_user_visible(self):
        llm = _ScriptedLlm([("content", '{"calls": [], "done": true}')])
        _run(MirrorChatServicer(), _request(), llm)
        assert "思考过程会原样显示给用户看" in llm.prompt


class TestFinalBatchPrompt:
    """2026-09-21 联调后改定：done=true 可带 calls（查完这批即收尾），互不依赖的查询同一步做。
    实测 mimo 每轮规划 20~60s，"单独花一轮说够了"和"一步只查一个"都是纯等待。"""

    def test_prompt_teaches_final_batch_and_batching(self):
        llm = _ScriptedLlm([("content", '{"calls": [], "done": true}')])
        _run(MirrorChatServicer(), _request(), llm)
        p = llm.prompt
        assert "查完这批就能答" in p
        assert "互不依赖的查询放在同一步" in p
        # 情绪类示例：两个工具 + done=true 同帧
        assert '{"tool": "get_stats", "args": {"days": 30}}, {"tool": "search_records"' in p

    def test_done_with_calls_passes_through(self):
        """done=true + calls 非空 → 终帧原样透传（由 B 侧执行完即收尾）"""
        llm = _ScriptedLlm([("content",
            '{"calls": [{"tool": "get_stats", "args": {"days": 30}}], "done": true}')])
        last = _run(MirrorChatServicer(), _request(), llm)[-1]
        assert last.final and last.done
        assert [c.tool for c in last.calls] == ["get_stats"]


class TestTodayDate:
    """2026-09-21 联调：问"十二号弹了什么曲子"，规划器不知道今天几号，只能猜；
    工具也没有按日期查的参数，只好拿 query 逐字匹配"弹曲子"，0 条。"""

    def test_today_text_format_and_weekday(self):
        from datetime import datetime
        import services.plan_service as ps
        assert ps.today_text(datetime(2026, 9, 21, 10, 0, tzinfo=ps._CN_TZ)) == "2026-09-21（星期一）"
        assert ps.today_text(datetime(2026, 9, 20, 10, 0, tzinfo=ps._CN_TZ)) == "2026-09-20（星期日）"

    def test_today_uses_east8_not_utc(self):
        """UTC 16:30 已是北京时间次日 0:30——必须按东八区算"""
        from datetime import datetime, timezone
        import services.plan_service as ps
        assert ps.today_text(datetime(2026, 9, 20, 16, 30, tzinfo=timezone.utc)).startswith("2026-09-21")

    def test_prompt_contains_today(self):
        import services.plan_service as ps
        llm = _ScriptedLlm([("content", '{"calls": [], "done": true}')])
        _run(MirrorChatServicer(), _request(), llm)
        assert "今天是 **" + ps.today_text() + "**" in llm.prompt
        assert "{today}" not in llm.prompt

    def test_prompt_teaches_date_lookup_without_query(self):
        llm = _ScriptedLlm([("content", '{"calls": [], "done": true}')])
        _run(MirrorChatServicer(), _request(), llm)
        assert "不要带 query" in llm.prompt
        assert '{"tool": "search_records", "args": {"date": "2026-09-12"}}' in llm.prompt


class TestRegistrySanitize:
    def test_hallucinated_tool_dropped(self):
        llm = _ScriptedLlm([("content",
                             '{"calls": ['
                             '{"tool": "send_email", "args": {}},'
                             '{"tool": "get_stats", "args": {"days": 30}}'
                             '], "done": false}')])
        chunks = _run(MirrorChatServicer(), _request(), llm)
        final = chunks[-1]
        assert [c.tool for c in final.calls] == ["get_stats"]

    def test_all_calls_hallucinated_becomes_done(self):
        """全是编的工具名 → 剔完变空计划 → done 必须为 True（不能让循环白转）"""
        llm = _ScriptedLlm([("content",
                             '{"calls": [{"tool": "open_browser", "args": {}}], "done": false}')])
        chunks = _run(MirrorChatServicer(), _request(), llm)
        final = chunks[-1]
        assert len(final.calls) == 0
        assert final.done is True

    def test_calls_truncated_to_max(self):
        """一步最多 max_calls（默认 2）个工具，超出截断"""
        llm = _ScriptedLlm([("content",
                             '{"calls": ['
                             '{"tool": "get_stats", "args": {}},'
                             '{"tool": "search_records", "args": {}},'
                             '{"tool": "get_stats", "args": {"days": 7}}'
                             '], "done": false}')])
        chunks = _run(MirrorChatServicer(), _request(), llm)
        assert len(chunks[-1].calls) == 2


# ---------------------------------------------------------------------------
# 4. done 解析
# ---------------------------------------------------------------------------
class TestDoneSemantics:
    def test_empty_calls_done_true_parsed(self):
        llm = _ScriptedLlm([("content", '{"calls": [], "done": true}')])
        final = _run(MirrorChatServicer(), _request(step=2), llm)[-1]
        assert final.final is True and final.done is True and len(final.calls) == 0

    def test_empty_calls_with_done_false_coerced_true(self):
        """模型自相矛盾（空计划却说没完）→ 按空计划算收尾，与 B 侧终止条件语义一致"""
        llm = _ScriptedLlm([("content", '{"calls": [], "done": false}')])
        assert _run(MirrorChatServicer(), _request(), llm)[-1].done is True

    def test_done_missing_with_calls_defaults_false(self):
        llm = _ScriptedLlm([("content", '{"calls": [{"tool": "get_stats", "args": {}}]}')])
        assert _run(MirrorChatServicer(), _request(), llm)[-1].done is False

    def test_done_as_string_accepted(self):
        llm = _ScriptedLlm([("content",
                             '{"calls": [{"tool": "get_stats", "args": {}}], "done": "true"}')])
        assert _run(MirrorChatServicer(), _request(), llm)[-1].done is True

    def test_json_in_code_fence_parsed(self):
        """模型裹了 ```json 代码块：parse_json 已兜底"""
        llm = _ScriptedLlm([("thinking", "想"),
                            ("content", '```json\n{"calls": [], "done": true}\n```')])
        chunks = _run(MirrorChatServicer(), _request(), llm)
        assert chunks[-1].final and chunks[-1].done

    def test_resolve_done_unit(self):
        assert resolve_done({"done": False}, []) is True      # 空计划恒 True
        assert resolve_done({}, []) is True
        assert resolve_done({"done": True}, ["x"]) is True
        assert resolve_done({}, ["x"]) is False
        assert resolve_done({"done": "yes"}, ["x"]) is True
        assert resolve_done({"done": 1}, ["x"]) is False      # 非法类型不瞎猜


# ---------------------------------------------------------------------------
# 5. 注册表为空 → 不烧 LLM
# ---------------------------------------------------------------------------
class TestEmptyRegistry:
    def test_no_tools_skips_llm(self):
        factory = _ExplodingLlmFactory()
        chunks = _run(MirrorChatServicer(), _request(tools=[]), factory)
        assert factory.calls == 0
        assert len(chunks) == 1
        assert chunks[0].final is True and chunks[0].done is True and len(chunks[0].calls) == 0

    def test_blank_tool_names_treated_as_empty(self):
        factory = _ExplodingLlmFactory()
        blank = [common_pb2.ToolSpec(name="   ", description="脏数据")]
        chunks = _run(MirrorChatServicer(), _request(tools=blank), factory)
        assert factory.calls == 0 and chunks[0].done is True


# ---------------------------------------------------------------------------
# 6. prompt 渲染（循环语境五个新占位符）
# ---------------------------------------------------------------------------
class TestPromptRendering:
    def test_loop_context_rendered(self):
        llm = _ScriptedLlm([("content", '{"calls": [], "done": true}')])
        req = _request(
            step=2, max_steps=4, has_retrieval=False,
            history=[chat_pb2.ChatMessage(role="user", content="我上周写了什么"),
                     chat_pb2.ChatMessage(role="assistant", content="你写了三条待办")],
            previous_results=[common_pb2.ToolResult(
                tool="get_stats", summary="30 天共 42 条；anxious 11", success=True)],
        )
        _run(MirrorChatServicer(), req, llm)
        prompt = llm.prompt
        assert "这是第 2 步，最多 4 步" in prompt
        assert "**false**" in prompt                     # has_retrieval 小写字面量
        assert "1. get_stats → 30 天共 42 条；anxious 11" in prompt
        assert "我上周写了什么" in prompt and "你写了三条待办" in prompt
        assert "get_stats" in prompt and "search_records" in prompt  # 注册表
        assert "我最近特别焦虑，怎么办？" in prompt

    def test_no_literal_placeholder_left(self):
        """模板占位符全部落值——漏登记会让模型读到字面量 {step}"""
        llm = _ScriptedLlm([("content", '{"calls": [], "done": true}')])
        _run(MirrorChatServicer(), _request(), llm)
        prompt = llm.prompt
        for ph in ("{tools}", "{question}", "{glossary}", "{history}",
                   "{previous_results}", "{step}", "{max_steps}", "{has_retrieval}"):
            assert ph not in prompt, f"占位符未渲染: {ph}"

    def test_has_retrieval_true_rendered_lowercase(self):
        llm = _ScriptedLlm([("content", '{"calls": [], "done": true}')])
        _run(MirrorChatServicer(), _request(has_retrieval=True), llm)
        assert "**true**" in llm.prompt

    def test_first_step_previous_results_placeholder_text(self):
        llm = _ScriptedLlm([("content", '{"calls": [], "done": true}')])
        _run(MirrorChatServicer(), _request(), llm)
        assert "还没有执行过任何工具" in llm.prompt

    def test_failed_previous_result_hides_payload(self):
        """失败结果只渲染失败说明，不把失败 payload 喂给模型"""
        rendered = format_previous_results([common_pb2.ToolResult(
            tool="find_item", summary="炸了", payload_json='{"error": "boom"}', success=False)])
        assert "执行失败" in rendered
        assert "boom" not in rendered and "炸了" not in rendered

    def test_prompt_no_longer_forbids_emotional_support_in_loop_template(self):
        """§5.1 关键修复：规则 1 里不许再出现"情绪安慰 → 必须空计划"，且要有 get_stats 摸底规则"""
        llm = _ScriptedLlm([("content", '{"calls": [], "done": true}')])
        _run(MirrorChatServicer(), _request(), llm)
        prompt = llm.prompt
        assert "情绪安慰" not in prompt
        assert "宁可少规划，不可错规划" in prompt          # 总基调保留
        assert "禁止编造注册表里不存在的工具" in prompt      # 防幻觉基调保留
        assert "get_stats" in prompt and "摸底" in prompt   # 新规则：情绪类先摸底


# ---------------------------------------------------------------------------
# 7. 两套模板隔离（chat-loop-design.md 裁决 0.4 / §9 验收 6：回滚必须退回已知旧行为）
# ---------------------------------------------------------------------------
# 渲染后的循环版特征（不是模板字面量——占位符渲染完就没了）
_LOOP_MARKERS = ("已有材料", '"done"', "先自评", "本轮进度", "工具规划器", "这是第")
_SINGLE_MARKERS = ("工具规划助手", "情绪安慰", "最多 2 步")

# prompts/plan_tools_single.txt 必须与 `git show HEAD:prompts/plan_tools.txt` 逐字一致。
# 这里写死换行归一化后（read_text 的 universal newlines）的 sha256 + 字符数，
# 不在测试里跑 git：CI 上未必有仓库历史，且跑子进程会让单测依赖 git 环境。
# 它一旦对不上，就说明有人动了回滚路径的 prompt —— 那等于偷偷改掉"已知good的状态"。
_SINGLE_SHA256 = "d6ac1d1ca40e41c80b477dd3da0b15b1ae9ab4a234a6b18ba2c7965000d3e0cd"
_SINGLE_CHARS = 2093


class TestTemplateSeparation:
    def test_single_template_is_head_verbatim(self):
        """回滚模板 = HEAD 原文快照，逐字不动（含「情绪安慰」那条已知 bug，故意保留）"""
        import hashlib
        from prompts_loader import PROMPT_DIR
        text = (PROMPT_DIR / "plan_tools_single.txt").read_text(encoding="utf-8")
        assert len(text) == _SINGLE_CHARS
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == _SINGLE_SHA256
        # 旧 prompt 的三个特征句都还在（hash 挂了时给出人类可读的定位信息）
        for marker in _SINGLE_MARKERS:
            assert marker in text

    def test_plan_tools_renders_single_template(self):
        """PlanTools（回滚路径）渲染旧模板：不含任何循环版特征"""
        llm = _JsonTaskLlm()
        servicer = MirrorChatServicer()
        globals_ = servicer.PlanTools.__globals__
        orig = globals_["create_llm"]
        globals_["create_llm"] = lambda **kw: llm
        try:
            reply = servicer.PlanTools(
                chat_pb2.PlanToolsRequest(question="我最近特别焦虑，怎么办？", tools=_REGISTRY),
                _CapturedContext())
        finally:
            globals_["create_llm"] = orig
        assert len(reply.calls) == 0  # 旧路径行为不变（空计划照旧是合法输出）
        prompt = llm.prompt
        for marker in _LOOP_MARKERS:
            assert marker not in prompt, f"回滚路径 prompt 里混进了循环版特征: {marker}"
        for marker in _SINGLE_MARKERS:
            assert marker in prompt

    def test_plan_next_step_renders_loop_template(self):
        """PlanNextStep 渲染循环版：反过来——含循环特征，不含旧模板特征"""
        llm = _ScriptedLlm([("content", '{"calls": [], "done": true}')])
        _run(MirrorChatServicer(), _request(), llm)
        prompt = llm.prompt
        for marker in _LOOP_MARKERS:
            assert marker in prompt
        assert "工具规划助手" not in prompt
        assert "情绪安慰" not in prompt

    def test_two_templates_are_different_files(self):
        from prompts_loader import PROMPT_DIR, _FILENAME
        assert _FILENAME["plan_tools"] == "plan_tools"
        assert _FILENAME["plan_tools_single"] == "plan_tools_single"
        loop = (PROMPT_DIR / "plan_tools.txt").read_text(encoding="utf-8")
        single = (PROMPT_DIR / "plan_tools_single.txt").read_text(encoding="utf-8")
        assert loop != single

    def test_single_template_registered_in_config(self):
        from config import CONFIG
        assert CONFIG["prompts"]["plan_tools_single"] == "prompts/plan_tools_single.txt"
        assert CONFIG["prompts"]["plan_tools"] == "prompts/plan_tools.txt"


# ---------------------------------------------------------------------------
# 8. 异常路径
# ---------------------------------------------------------------------------
class TestErrorPath:
    def test_unparsable_json_aborts(self):
        """正文不是 JSON → parse_json 抛 ContentInvalidError → abort_with_mapped"""
        llm = _ScriptedLlm([("thinking", "想了半天"), ("content", "我觉得不需要工具")])
        ctx = _CapturedContext()
        with pytest.raises(RuntimeError, match="abort"):
            _run(MirrorChatServicer(), _request(), llm, context=ctx)
        assert ctx.aborted is not None

    def test_thinking_already_yielded_before_abort(self):
        """流式语义：已经外推的 thinking 不回滚（B 侧带着已有结果退出循环）"""
        llm = _ScriptedLlm([("thinking", "想"), ("content", "不是 JSON")])
        ctx = _CapturedContext()
        gen = MirrorChatServicer().PlanNextStep(_request(), ctx)
        globals_ = MirrorChatServicer.PlanNextStep.__globals__
        orig = globals_["create_llm"]
        globals_["create_llm"] = lambda **kw: llm
        try:
            first = next(gen)
            assert first.thinking == "想" and not first.final
            with pytest.raises(RuntimeError, match="abort"):
                next(gen)
        finally:
            globals_["create_llm"] = orig

    def test_llm_error_mapped(self):
        """LLM 层异常沿用 errors.py 映射（不裸抛）"""

        class _BoomLlm:
            def chat_stream(self, messages, temperature=0.7, thinking_budget=None):
                raise AiServiceError("炸了")
                yield  # pragma: no cover

        ctx = _CapturedContext()
        with pytest.raises(RuntimeError, match="abort"):
            _run(MirrorChatServicer(), _request(), _BoomLlm(), context=ctx)
        assert ctx.aborted is not None
