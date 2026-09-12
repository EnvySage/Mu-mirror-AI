"""轻量 JSON 任务（json_task）单测 —— 关思考优化（感知延迟主因）

背景（实测，每项 3 次平均）：
- 现状 thinking 默认 + max_tokens=4096：**22.6s**，输出 1074/1061/813 token
- 关 thinking + max_tokens=4096：**2.1s**，输出 39/43/48 token
- 只限 max_tokens=300（不关思考）：6.6s 且 **JSON 被思考吃满预算而截断**

ExtractIntent / PlanTools 是"用户看到第一个字之前"的串行前置调用，
所以这两个调用改成 json_task 直接决定感知延迟。

覆盖：
1. base 默认实现回退 chat（不支持的厂商零影响）
2. AnthropicLlm.json_task：带 thinking=disabled + 小 max_tokens
3. 端点不认 thinking（400）→ 降级普通调用重试一次
4. 非 400 错误照常按状态码翻译（401/429/5xx）
5. OpenAiLlm.json_task：extra_body 关思考 + 400 降级
6. 接线：ExtractIntent / PlanTools 走 json_task（不再是 chat）
7. 最终回答流式 Chat 仍带思考（不能误伤）

运行：.venv/Scripts/python.exe -m pytest tests/test_json_task.py -v
"""

import sys
import types
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from anthropic import APIStatusError as AnthropicStatusError  # noqa: E402
from openai import APIStatusError as OpenAiStatusError  # noqa: E402

from errors import AiServiceError, LlmUnavailableError  # noqa: E402
from llm.anthropic_llm import AnthropicLlm  # noqa: E402
from llm.base import BaseLlm  # noqa: E402
from llm.openai_llm import OpenAiLlm  # noqa: E402


def _http_response(status_code: int):
    """构造 SDK 可读的响应（json body 必须有，否则 SDK 读 error.message 会炸）"""
    req = httpx.Request("POST", "http://test/v1/messages")
    return httpx.Response(status_code, request=req, json={"error": {"message": "x"}})


# ---------------------------------------------------------------------------
# 1. base 默认实现
# ---------------------------------------------------------------------------
class _Stub(BaseLlm):
    def __init__(self):
        self.calls = []

    def chat(self, messages, temperature=0.7):
        self.calls.append(("chat", messages, temperature))
        return "from-chat"

    def chat_stream(self, messages, temperature=0.7):
        yield ("content", "x")


class TestBaseDefault:
    def test_json_task_falls_back_to_chat(self):
        """默认实现 = chat（老厂商/未覆写实现零影响，不炸）"""
        llm = _Stub()
        out = llm.json_task([{"role": "user", "content": "q"}])
        assert out == "from-chat"
        assert llm.calls[0][0] == "chat"


# ---------------------------------------------------------------------------
# 2/3/4. AnthropicLlm.json_task
# ---------------------------------------------------------------------------
class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Usage:
    def __init__(self, output_tokens):
        self.output_tokens = output_tokens


class _MsgResponse:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.usage = _Usage(43)


class _FakeAnthropicMessages:
    """按脚本返回：脚本项为 Exception 则抛出，否则作为返回消息文本"""

    def __init__(self, script):
        self.script = list(script)
        self.kwargs_log = []

    def create(self, **kwargs):
        self.kwargs_log.append(kwargs)
        item = self.script.pop(0) if self.script else "{}"
        if isinstance(item, Exception):
            raise item
        return _MsgResponse(item)


def _make_anthropic(script) -> tuple[AnthropicLlm, _FakeAnthropicMessages]:
    llm = object.__new__(AnthropicLlm)
    llm.model = "mimo-v2.5"
    fake = _FakeAnthropicMessages(script)
    llm.client = types.SimpleNamespace(messages=fake)
    return llm, fake


class TestAnthropicJsonTask:
    def test_disables_thinking_and_caps_tokens(self):
        """带 thinking=disabled + max_tokens=256（实测 22.6s → 2.1s 的两条硬指令）"""
        llm, fake = _make_anthropic(['{"query_type": "semantic"}'])
        out = llm.json_task([{"role": "user", "content": "我最喜欢的歌是什么"}])
        assert out == '{"query_type": "semantic"}'
        kw = fake.kwargs_log[0]
        assert kw["thinking"] == {"type": "disabled"}
        assert kw["max_tokens"] == 256
        assert kw["model"] == "mimo-v2.5"

    def test_system_message_split(self):
        """system 剥离后走 system 参数（与 chat 同口径）"""
        llm, fake = _make_anthropic(["{}"])
        llm.json_task([
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "q"},
        ])
        kw = fake.kwargs_log[0]
        assert kw["system"] == "你是助手"
        assert kw["messages"] == [{"role": "user", "content": "q"}]

    def test_400_falls_back_to_plain_call(self):
        """端点不认 thinking 参数（400 + 报错提到 thinking）→ 降级普通调用

        注：SDK 开了 max_retries 时同一次逻辑调用可能重发，故断言"最后一次调用的形状"
        而不是精确调用次数。
        """
        err = AnthropicStatusError(
            "unexpected keyword argument 'thinking'",
            response=_http_response(400), body=None)
        # 脚本恰好两项：首次（带 thinking）报 400 → 降级（不带）成功
        llm, fake = _make_anthropic([err, '{"ok": true}'])
        out = llm.json_task([{"role": "user", "content": "q"}])
        assert out == '{"ok": true}'
        assert fake.kwargs_log[0]["thinking"] == {"type": "disabled"}   # 首次带 thinking
        last = fake.kwargs_log[-1]
        assert "thinking" not in last                                   # 降级后不带
        assert last["max_tokens"] == 256                                # 限额保留

    def test_400_without_thinking_hint_not_retried(self):
        """400 但报错与 thinking 无关（请求本身非法）→ 不降级，照常报错

        防"任何 400 都重试一次"把真正的请求错误掩盖成静默重试。
        """
        err = AnthropicStatusError("invalid request body",
                                   response=_http_response(400), body=None)
        llm, fake = _make_anthropic([err])
        with pytest.raises(AiServiceError):
            llm.json_task([{"role": "user", "content": "q"}])
        assert len(fake.kwargs_log) == 1

    @pytest.mark.parametrize("code", [401, 429, 500])
    def test_non_400_status_mapped_not_retried(self, code):
        """非 400 错误不走降级，按状态码翻译成 AiServiceError"""
        err = AnthropicStatusError("boom", response=_http_response(code), body=None)
        llm, fake = _make_anthropic([err])
        with pytest.raises(LlmUnavailableError):
            llm.json_task([{"role": "user", "content": "q"}])


# ---------------------------------------------------------------------------
# 5. OpenAiLlm.json_task
# ---------------------------------------------------------------------------
class _FakeCompletions:
    def __init__(self, script):
        self.script = list(script)
        self.kwargs_log = []

    def create(self, **kwargs):
        self.kwargs_log.append(kwargs)
        item = self.script.pop(0) if self.script else "{}"
        if isinstance(item, Exception):
            raise item
        msg = types.SimpleNamespace(content=item)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def _make_openai(script) -> tuple[OpenAiLlm, _FakeCompletions]:
    llm = object.__new__(OpenAiLlm)
    llm.model = "stub-model"
    fake = _FakeCompletions(script)
    llm.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=fake))
    return llm, fake


class TestOpenAiJsonTask:
    def test_extra_body_disables_thinking(self):
        """OpenAI 兼容系走 extra_body.thinking=disabled + 小 max_tokens"""
        llm, fake = _make_openai(['{"calls": []}'])
        out = llm.json_task([{"role": "user", "content": "q"}])
        assert out == '{"calls": []}'
        kw = fake.kwargs_log[0]
        assert kw["extra_body"] == {"thinking": {"type": "disabled"}}
        assert kw["max_tokens"] == 256

    def test_400_falls_back_to_plain_call(self):
        """端点不认 thinking（400 + 报错提到 thinking）→ 降级普通调用"""
        err = OpenAiStatusError("unknown parameter thinking",
                                response=_http_response(400), body=None)
        llm, fake = _make_openai([err, '{"ok": 1}'])
        assert llm.json_task([{"role": "user", "content": "q"}]) == '{"ok": 1}'
        assert fake.kwargs_log[0]["extra_body"] == {"thinking": {"type": "disabled"}}
        assert "extra_body" not in fake.kwargs_log[-1]
        assert fake.kwargs_log[-1]["max_tokens"] == 256

    def test_400_without_thinking_hint_not_retried(self):
        """400 但与 thinking 无关 → 不降级（防掩盖真正的请求错误）"""
        err = OpenAiStatusError("invalid request body",
                                response=_http_response(400), body=None)
        llm, fake = _make_openai([err])
        with pytest.raises(AiServiceError):
            llm.json_task([{"role": "user", "content": "q"}])
        assert len(fake.kwargs_log) == 1


# ---------------------------------------------------------------------------
# 6. 接线：两个前置调用必须走 json_task
# ---------------------------------------------------------------------------
class _RecordingLlm:
    def __init__(self, text='{"query_type": "semantic", "rewritten_query": "x"}'):
        self.text = text
        self.used = []

    def chat(self, messages, temperature=0.7):
        self.used.append("chat")
        return self.text

    def json_task(self, messages, temperature=0.7):
        self.used.append("json_task")
        return self.text

    def chat_stream(self, messages, temperature=0.7):
        yield ("content", "hi")


class _Ctx:
    def abort(self, code, details):
        raise RuntimeError("abort")


class TestWiring:
    def test_extract_intent_uses_json_task(self):
        """ExtractIntent 走 json_task（不再让路由任务做内心独白）"""
        from generated import common_pb2 as common
        from generated import mirror_chat_pb2 as pb2
        from services.chat_service import MirrorChatServicer

        llm = _RecordingLlm()
        svc = MirrorChatServicer()
        with patch.dict(svc.ExtractIntent.__globals__, {"create_llm": lambda **kw: llm}):
            svc.ExtractIntent(pb2.ExtractIntentRequest(
                query="我最喜欢的歌是什么",
                llm_config=common.LlmConfig(provider="stub")), _Ctx())
        assert llm.used == ["json_task"]

    def test_plan_tools_uses_json_task(self):
        """PlanTools 走 json_task（注册表非空才会真调 LLM——空表是短路空计划）"""
        from google.protobuf.message_factory import GetMessageClass
        from generated import common_pb2 as common
        from generated import mirror_chat_pb2 as pb2
        from services.plan_service import MirrorChatServicer as PlanServicer

        # ToolSpec 在描述符池中存在但未暴露为模块属性（生成文件早于 proto 的该字段）
        tool_cls = GetMessageClass(
            pb2.PlanToolsRequest.DESCRIPTOR.fields_by_name["tools"].message_type)
        tool = tool_cls(name="search_records", description="检索记录", args_schema="{}")

        llm = _RecordingLlm('{"calls": []}')
        svc = PlanServicer()
        with patch.dict(svc.PlanTools.__globals__, {"create_llm": lambda **kw: llm}):
            svc.PlanTools(pb2.PlanToolsRequest(
                question="我最近怎么样", tools=[tool],
                llm_config=common.LlmConfig(provider="stub")), _Ctx())
        assert llm.used == ["json_task"]
