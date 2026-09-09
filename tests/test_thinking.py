"""Chat 流式思考过程捕获单测 — ChatChunk.thinking（字段号 4，B 侧对账依据）

覆盖任务书验证标准：
1. proto 契约：ChatChunk.thinking=4（proto3 optional，区分"未传"与空串，
   同 mirror_lookback=13 模式）；既有字段 content=1/done=2/sources=3 不动，wire 向后兼容
2. anthropic_llm.chat_stream 事件流 mock：thinking_delta → ("thinking", ...)、
   text_delta → ("content", ...)；其他事件类型跳过
3. openai_llm.chat_stream mock：reasoning_content 与 content 分流；
   无 reasoning_content 属性的 delta（SDK 标准模型）不炸 → 只有 content；
   空 choices 防御
4. Chat RPC：thinking chunk 与 content chunk 分离 yield（mock llm）；
   thinking 不进 buffer（不参与 [n] 引用解析）；模型无思考块时 wire 与旧版一致
5. wire 冒烟：thinking=4 首字节 0x22（field4/len-delimited）、未传零字节、round-trip

运行：.venv/Scripts/python.exe -m pytest tests/test_thinking.py -v
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from generated import mirror_chat_pb2 as chat_pb2  # noqa: E402

from llm.anthropic_llm import AnthropicLlm  # noqa: E402
from llm.openai_llm import OpenAiLlm  # noqa: E402
from services.chat_service import MirrorChatServicer  # noqa: E402


# ---------------------------------------------------------------------------
# 1. proto 契约（B 侧对账依据）
# ---------------------------------------------------------------------------
class TestProtoContract:
    def test_thinking_is_field_4(self):
        """ChatChunk.thinking 字段号 4（与 B 仓同步登记 shared-protocol.md）"""
        fields = chat_pb2.ChatChunk.DESCRIPTOR.fields_by_name
        assert fields["thinking"].number == 4

    def test_existing_fields_unchanged(self):
        """既有字段号不动（1/2/3），wire 向后兼容"""
        fields = chat_pb2.ChatChunk.DESCRIPTOR.fields_by_name
        assert fields["content"].number == 1
        assert fields["done"].number == 2
        assert fields["sources"].number == 3

    def test_thinking_is_optional_string(self):
        """proto3 optional string：has_presence=True（区分"未传"与空串）"""
        f = chat_pb2.ChatChunk.DESCRIPTOR.fields_by_name["thinking"]
        assert f.type == 9  # TYPE_STRING
        assert f.has_presence is True

    def test_optional_presence_semantics(self):
        """未传 → HasField=False 且取值空串；显式传空串 → HasField=True"""
        unset = chat_pb2.ChatChunk(content="hi")
        assert not unset.HasField("thinking")
        assert unset.thinking == ""

        empty = chat_pb2.ChatChunk(content="hi", thinking="")
        assert empty.HasField("thinking")

    def test_wire_first_byte(self):
        """wire 冒烟：thinking → 0x22（field 4 / len-delimited）；未传 → 零字节"""
        chunk = chat_pb2.ChatChunk(thinking="思考")
        assert chunk.SerializeToString()[0] == 0x22
        assert chat_pb2.ChatChunk(content="hi").SerializeToString() != b""
        assert b"\x22" not in chat_pb2.ChatChunk(content="hi", done=False).SerializeToString()

    def test_wire_round_trip(self):
        """序列化 round-trip：thinking 与 content 互不干扰"""
        chunk = chat_pb2.ChatChunk(content="正文", thinking="思考片段")
        back = chat_pb2.ChatChunk()
        back.ParseFromString(chunk.SerializeToString())
        assert back.content == "正文"
        assert back.thinking == "思考片段"
        assert back.HasField("thinking")

    def test_unset_thinking_gets_empty_string(self):
        """proto3 optional 语义：未传 thinking 的 chunk 反序列化后 getThinking()==""（B 侧 Java 语义一致）"""
        chunk = chat_pb2.ChatChunk(content="hi")
        back = chat_pb2.ChatChunk()
        back.ParseFromString(chunk.SerializeToString())
        assert back.thinking == ""
        assert not back.HasField("thinking")


# ---------------------------------------------------------------------------
# 2. anthropic_llm 事件流 mock
# ---------------------------------------------------------------------------
def _anthropic_event(event_type: str, delta):
    """构造 anthropic RawMessageStreamEvent 形状的事件（types 冻结类不可直接构造，用 SimpleNamespace）"""
    return types.SimpleNamespace(type=event_type, delta=delta)


def _delta(delta_type: str, **kw):
    return types.SimpleNamespace(type=delta_type, **kw)


class _FakeAnthropicStream:
    """模拟 MessageStream：可迭代事件序列（with __enter__ 用法）"""

    def __init__(self, events):
        self._events = events

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __iter__(self):
        return iter(self._events)


class _FakeAnthropicMessages:
    def __init__(self, events):
        self._events = events

    def stream(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeAnthropicStream(self._events)


def _make_anthropic_llm(events) -> AnthropicLlm:
    llm = object.__new__(AnthropicLlm)  # 绕过 __init__（不建真实 client）
    llm.model = "test-model"
    llm.client = types.SimpleNamespace(messages=_FakeAnthropicMessages(events))
    return llm


class TestAnthropicEventStream:
    def test_thinking_delta_then_text_delta(self):
        """content_block_delta：thinking_delta → ("thinking",...)、text_delta → ("content",...)"""
        events = [
            _anthropic_event("message_start", _delta("message_start")),
            _anthropic_event("content_block_start", _delta("content_block_start")),
            _anthropic_event("content_block_delta", _delta("thinking_delta", thinking="思考 A")),
            _anthropic_event("content_block_delta", _delta("thinking_delta", thinking="思考 B")),
            _anthropic_event("content_block_delta", _delta("text_delta", text="正文 1")),
            _anthropic_event("content_block_delta", _delta("text_delta", text="正文 2")),
            _anthropic_event("message_stop", _delta("message_stop")),
        ]
        llm = _make_anthropic_llm(events)
        out = list(llm.chat_stream([{"role": "user", "content": "q"}]))
        assert out == [("thinking", "思考 A"), ("thinking", "思考 B"),
                       ("content", "正文 1"), ("content", "正文 2")]

    def test_non_delta_events_skipped(self):
        """message_start/content_block_start 等非 delta 事件不产出"""
        events = [
            _anthropic_event("message_start", None),
            _anthropic_event("content_block_delta", _delta("text_delta", text="hi")),
            _anthropic_event("message_delta", None),
        ]
        llm = _make_anthropic_llm(events)
        assert list(llm.chat_stream([{"role": "user", "content": "q"}])) == [("content", "hi")]

    def test_only_text_delta_no_thinking(self):
        """mimo 等走 anthropic 协议但无 thinking：只有 content（与旧版行为一致）"""
        events = [
            _anthropic_event("content_block_delta", _delta("text_delta", text="a")),
            _anthropic_event("content_block_delta", _delta("text_delta", text="b")),
        ]
        llm = _make_anthropic_llm(events)
        out = list(llm.chat_stream([{"role": "user", "content": "q"}]))
        assert out == [("content", "a"), ("content", "b")]
        assert all(k == "content" for k, _ in out)

    def test_empty_delta_values_skipped(self):
        """thinking/text 为空串或 None 不产出（防脏事件）"""
        events = [
            _anthropic_event("content_block_delta", _delta("thinking_delta", thinking="")),
            _anthropic_event("content_block_delta", _delta("text_delta", text=None)),
            _anthropic_event("content_block_delta", _delta("text_delta", text="ok")),
        ]
        llm = _make_anthropic_llm(events)
        assert list(llm.chat_stream([{"role": "user", "content": "q"}])) == [("content", "ok")]

    def test_system_message_split_passthrough(self):
        """system 剥离逻辑不变（messages.stream kwargs 收到剥离后的 messages）"""
        events = [_anthropic_event("content_block_delta", _delta("text_delta", text="x"))]
        llm = _make_anthropic_llm(events)
        list(llm.chat_stream([{"role": "system", "content": "S"}, {"role": "user", "content": "Q"}]))
        fake = llm.client.messages
        assert fake.last_kwargs["system"] == "S"
        assert fake.last_kwargs["messages"] == [{"role": "user", "content": "Q"}]


# ---------------------------------------------------------------------------
# 3. openai_llm mock
# ---------------------------------------------------------------------------
def _openai_chunk(**delta_kw):
    """构造 ChatCompletionChunk 形状：delta 用标准类型构造（extra 字段走 model_extra）"""
    from openai.types.chat import ChatCompletionChunk
    from openai.types.chat.chat_completion_chunk import Choice, ChoiceDelta
    delta = ChoiceDelta(**{k: v for k, v in delta_kw.items()
                           if k in ChoiceDelta.model_fields})
    # reasoning_content 不在标准字段里 → 塞 model_extra（与 DeepSeek 等真实 SDK 行为一致）
    extras = {k: v for k, v in delta_kw.items() if k not in ChoiceDelta.model_fields}
    if extras:
        delta = ChoiceDelta.model_construct(**{**ChoiceDelta().model_dump(), **delta_kw})
    return ChatCompletionChunk(id="c", object="chat.completion.chunk", created=1, model="m",
                               choices=[Choice(index=0, delta=delta, finish_reason=None)])


class _FakeOpenAIStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __iter__(self):
        return iter(self._chunks)


class _FakeCompletions:
    def __init__(self, chunks):
        self._chunks = chunks

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeOpenAIStream(self._chunks)


def _make_openai_llm(chunks) -> OpenAiLlm:
    llm = object.__new__(OpenAiLlm)
    llm.model = "test-model"
    llm.client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=_FakeCompletions(chunks)))
    return llm


class TestOpenAIReasoningSplit:
    def test_reasoning_content_then_content(self):
        """reasoning_content → ("thinking",...)、content → ("content",...)，顺序保持"""
        chunks = [
            _openai_chunk(reasoning_content="想一下"),
            _openai_chunk(reasoning_content="再想一下"),
            _openai_chunk(content="回答"),
            _openai_chunk(content="完了"),
        ]
        llm = _make_openai_llm(chunks)
        out = list(llm.chat_stream([{"role": "user", "content": "q"}]))
        assert out == [("thinking", "想一下"), ("thinking", "再想一下"),
                       ("content", "回答"), ("content", "完了")]

    def test_delta_without_reasoning_attribute_no_crash(self):
        """SDK 标准模型没有 reasoning_content 属性：getattr 兜底 None，不炸只有 content"""
        chunks = [_openai_chunk(content="正文"), _openai_chunk(content="继续")]
        llm = _make_openai_llm(chunks)
        assert list(llm.chat_stream([{"role": "user", "content": "q"}])) == [
            ("content", "正文"), ("content", "继续")]

    def test_empty_choices_skipped(self):
        """choices 空列表/缺 delta 的 chunk 跳过（部分网关会发 usage-only chunk）"""
        from openai.types.chat import ChatCompletionChunk
        from openai.types.chat.chat_completion_chunk import Choice, ChoiceDelta
        usage_chunk = ChatCompletionChunk(id="c", object="chat.completion.chunk", created=1,
                                          model="m", choices=[])
        chunks = [usage_chunk, _openai_chunk(content="ok")]
        llm = _make_openai_llm(chunks)
        assert list(llm.chat_stream([{"role": "user", "content": "q"}])) == [("content", "ok")]

    def test_null_content_and_reasoning_skipped(self):
        """delta.content / reasoning_content 为 None（finish chunk）不产出"""
        chunks = [_openai_chunk(reasoning_content=None, content=None)]
        llm = _make_openai_llm(chunks)
        assert list(llm.chat_stream([{"role": "user", "content": "q"}])) == []

    def test_stream_kwargs_unchanged(self):
        """create 参数不变（model/messages/temperature/stream=True）"""
        llm = _make_openai_llm([])
        list(llm.chat_stream([{"role": "user", "content": "q"}], temperature=0.3))
        kw = llm.client.chat.completions.last_kwargs
        assert kw["model"] == "test-model" and kw["stream"] is True and kw["temperature"] == 0.3


# ---------------------------------------------------------------------------
# 4. Chat RPC：thinking 与 content 分离 yield
# ---------------------------------------------------------------------------
class _ScriptedLlm:
    """预置 (kind, text) 序列的桩 LLM"""

    def __init__(self, pieces):
        self._pieces = pieces

    def chat(self, messages, temperature=0.7):
        return "".join(t for k, t in self._pieces if k == "content")

    def chat_stream(self, messages, temperature=0.7):
        yield from self._pieces


class _CapturedContext:
    """grpc.ServicerContext 最小桩（只服务于 abort 路径，正常流用不到）"""

    def abort(self, code, details):
        raise RuntimeError(f"abort: {code} {details}")


def _chat_request():
    return chat_pb2.ChatRequest(
        question="q",
        chunks=[chat_pb2.RetrievedChunk(record_id=7, content="记录一", title="t",
                                        created_at="2026-09-01")],
        llm_config=chat_pb2.ChatRequest().llm_config,
    )


class TestChatRpcThinkingSplit:
    def test_thinking_and_content_separate_chunks(self):
        """thinking → ChatChunk(thinking=...)；content → ChatChunk(content=..., done=False)"""
        servicer = MirrorChatServicer()
        orig = servicer.Chat.__globals__["create_llm"]
        servicer.Chat.__globals__["create_llm"] = \
            lambda **kw: _ScriptedLlm([("thinking", "想"), ("content", "答[1]")])
        try:
            chunks = list(servicer.Chat(_chat_request(), _CapturedContext()))
        finally:
            servicer.Chat.__globals__["create_llm"] = orig

        assert len(chunks) == 3
        assert chunks[0].HasField("thinking") and chunks[0].thinking == "想"
        assert chunks[0].content == ""  # proto3 裸 string 无 presence：未设恒为空串
        assert chunks[1].content == "答[1]" and not chunks[1].done
        assert not chunks[1].HasField("thinking")
        # 终块：done=true + sources（thinking 不进 buffer，[1] 引用照常解析）
        assert chunks[2].done and chunks[2].sources[0].record_id == 7

    def test_thinking_not_in_source_buffer(self):
        """thinking 块不参与 [n] 引用解析：thinking 里写 [1] 不产生 sources，content 里才产生"""
        servicer = MirrorChatServicer()
        orig = servicer.Chat.__globals__["create_llm"]
        servicer.Chat.__globals__["create_llm"] = \
            lambda **kw: _ScriptedLlm([("thinking", "引用 [1] 在思考里不算"),
                                       ("content", "正文无引用")])
        try:
            chunks = list(servicer.Chat(_chat_request(), _CapturedContext()))
        finally:
            servicer.Chat.__globals__["create_llm"] = orig
        assert chunks[0].thinking == "引用 [1] 在思考里不算"
        assert chunks[-1].done and len(chunks[-1].sources) == 0

    def test_no_thinking_model_wire_identical_to_legacy(self):
        """模型无思考块（只有 content）：wire 上只有 content 块 + done 终块，与旧版一致"""
        servicer = MirrorChatServicer()
        orig = servicer.Chat.__globals__["create_llm"]
        servicer.Chat.__globals__["create_llm"] = \
            lambda **kw: _ScriptedLlm([("content", "a"), ("content", "b")])
        try:
            chunks = list(servicer.Chat(_chat_request(), _CapturedContext()))
        finally:
            servicer.Chat.__globals__["create_llm"] = orig
        assert len(chunks) == 3
        assert [c.content for c in chunks[:2]] == ["a", "b"]
        assert all(not c.HasField("thinking") for c in chunks)
        assert chunks[2].done

    def test_thinking_chunk_has_no_content_field(self):
        """thinking chunk 不携带 content（互斥语义，B 侧分流 SSE 事件的依据）"""
        servicer = MirrorChatServicer()
        orig = servicer.Chat.__globals__["create_llm"]
        servicer.Chat.__globals__["create_llm"] = \
            lambda **kw: _ScriptedLlm([("thinking", "思考"), ("content", "正文")])
        try:
            chunks = list(servicer.Chat(_chat_request(), _CapturedContext()))
        finally:
            servicer.Chat.__globals__["create_llm"] = orig
        assert chunks[0].content == "" and chunks[0].HasField("thinking") and chunks[0].thinking == "思考"
        assert chunks[1].thinking == "" and not chunks[1].HasField("thinking") and chunks[1].content == "正文"
