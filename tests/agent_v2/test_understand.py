import json
from datetime import date
from types import SimpleNamespace

from agent_core.types import IntentResult, PreparedContext, RuntimeContext, TimeExpression
from agent_data.scope import ReadScope
from services import intent_service


class FakeLlm:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def json_task(self, messages):
        self.calls.append(messages)
        return json.dumps(self.response, ensure_ascii=False)


def _scope():
    from uuid import UUID
    return ReadScope(
        user_id=UUID("00000000-0000-0000-0000-000000000001"),
        session_id=UUID("10000000-0000-0000-0000-000000000001"),
        user_message_id=904,
    )


def _prepared(history=()):
    return PreparedContext(
        glossary_top30=(),
        glossary_matched=(),
        glossary_for_intent=(),
        matched_ids=(),
        history_plan=tuple(history),
        history_answer=tuple(history[-6:]),
    )


def _runtime(llm, **kwargs):
    llm = kwargs.pop("llm", llm)
    return RuntimeContext(
        user_id=_scope().user_id,
        session_id=_scope().session_id,
        run_id="run-1",
        user_message_id=904,
        reference_date=date(2026, 10, 20),
        timezone="Asia/Shanghai",
        read_scope=_scope(),
        llm=llm,
        llm_config=kwargs.pop("llm_config", None),
        **kwargs,
    )


def test_understand_rewrites_followup_with_context_history_once(monkeypatch):
    llm = FakeLlm({
        "action_kind": "read",
        "route": "hybrid",
        "standalone_question": "继续分析刚才讨论的方案",
        "content_type": None,
        "moods": [],
        "time_expression": {"kind": "none"},
        "file_query_mode": "none",
    })
    rendered = {}

    def render(name, **kwargs):
        rendered["name"] = name
        rendered.update(kwargs)
        return "agent intent prompt"

    monkeypatch.setattr(intent_service.loader, "render", render)
    previous = SimpleNamespace(role="assistant", content="之前讨论的方案是先做小范围验证")
    result = intent_service.understand("继续分析", _prepared([previous]), _runtime(llm))

    assert isinstance(result, IntentResult)
    assert result.standalone_question == "继续分析刚才讨论的方案"
    assert result.time_expression == TimeExpression(kind="none")
    assert llm.calls == [[{"role": "user", "content": "agent intent prompt"}]]
    assert rendered["name"] == "agent_intent"
    assert rendered["question"] == "继续分析"
    assert rendered["history"] == "assistant: 之前讨论的方案是先做小范围验证"


def test_agent_intent_prompt_is_separate_from_legacy_prompt():
    from prompts_loader import loader

    agent_prompt = loader.render("agent_intent", question="当前问题", history="历史", glossary="词典")
    legacy_prompt = loader.load("intent")

    assert "graph_v2" in agent_prompt
    assert "当前问题" in agent_prompt
    assert "当前问题" not in legacy_prompt


def test_understand_builds_existing_llm_client_from_runtime_config(monkeypatch):
    config = SimpleNamespace(
        provider="openai", api_key="request-key", base_url="https://api.test",
        model="test-model", protocol=1,
    )
    llm = FakeLlm({
        "action_kind": "conversation",
        "route": "hybrid",
        "standalone_question": "打个招呼",
        "content_type": None,
        "moods": [],
        "time_expression": {"kind": "none"},
        "file_query_mode": "none",
    })
    seen = {}

    def factory(**kwargs):
        seen.update(kwargs)
        return llm

    monkeypatch.setattr(intent_service.loader, "render", lambda name, **kwargs: "prompt")
    runtime = _runtime(None, llm_config=config)
    intent_service.understand("你好", _prepared(), runtime, llm_factory=factory)

    assert seen == {
        "provider": "openai", "api_key": "request-key", "base_url": "https://api.test",
        "model": "test-model", "protocol": 1,
    }
    assert len(llm.calls) == 1


def test_understand_validates_dirty_model_fields(monkeypatch):
    llm = FakeLlm({
        "action_kind": [],
        "route": [],
        "standalone_question": "",
        "content_type": ["note"],
        "moods": "happy",
        "time_expression": {"kind": "surprise", "days": "seven"},
        "file_query_mode": "upload",
    })
    monkeypatch.setattr(intent_service.loader, "render", lambda name, **kwargs: "prompt")

    result = intent_service.understand("原问题", _prepared(), _runtime(llm))

    assert result.action_kind == "clarification"
    assert result.route == "hybrid"
    assert result.standalone_question == "原问题"
    assert result.content_type is None
    assert result.moods == ()
    assert result.time_expression == TimeExpression(kind="none")
    assert result.file_query_mode == "none"
    assert result.validation_issues


def test_understand_rejects_time_fields_that_conflict_with_kind(monkeypatch):
    llm = FakeLlm({
        "action_kind": "read",
        "route": "structured",
        "standalone_question": "查询",
        "content_type": None,
        "moods": [],
        "time_expression": {"kind": "none", "date": "2026-10-20"},
        "file_query_mode": "none",
    })
    monkeypatch.setattr(intent_service.loader, "render", lambda name, **kwargs: "prompt")

    result = intent_service.understand("查询", _prepared(), _runtime(llm))

    assert result.action_kind == "clarification"
    assert result.time_expression == TimeExpression(kind="none")
    assert "invalid_time_expression" in result.validation_issues


def test_authorized_record_content_and_grant_take_deterministic_path(monkeypatch):
    def forbidden_factory(**kwargs):
        raise AssertionError("authorized explicit create must not call the LLM")

    monkeypatch.setattr(intent_service, "create_llm", forbidden_factory)
    runtime = _runtime(
        llm=None,
        llm_config=None,
        command_grant="trusted-java-grant",
        authorized_record_content="原文内容",
        allowed_write_tools=("create_record",),
    )

    result = intent_service.understand("帮我保存", _prepared(), runtime)

    assert result.action_kind == "create_record"
    assert result.standalone_question == "帮我保存"
    assert result.time_expression == TimeExpression(kind="none")


def test_partial_or_malformed_write_authority_does_not_take_deterministic_path(monkeypatch):
    llm = FakeLlm({
        "action_kind": "create_record",
        "route": "structured",
        "standalone_question": "创建一条日记",
        "content_type": "note",
        "moods": [],
        "time_expression": {"kind": "none"},
        "file_query_mode": "none",
    })
    monkeypatch.setattr(intent_service.loader, "render", lambda name, **kwargs: "prompt")
    runtime = _runtime(
        llm,
        command_grant="trusted-java-grant",
        authorized_record_content="原文内容",
        allowed_write_tools="create_record",
    )

    result = intent_service.understand("帮我保存", _prepared(), runtime)

    assert result.action_kind == "clarification"
    assert "unauthorized_create_record" in result.validation_issues
    assert len(llm.calls) == 1


def test_model_create_suggestion_without_java_grant_is_downgraded(monkeypatch):
    llm = FakeLlm({
        "action_kind": "create_record",
        "route": "structured",
        "standalone_question": "创建一条日记",
        "content_type": "note",
        "moods": [],
        "time_expression": {"kind": "none"},
        "file_query_mode": "none",
    })
    monkeypatch.setattr(intent_service.loader, "render", lambda name, **kwargs: "prompt")

    result = intent_service.understand("帮我记下", _prepared(), _runtime(llm))

    assert result.action_kind == "clarification"
    assert result.file_query_mode == "none"
    assert "unauthorized_create_record" in result.validation_issues
    assert len(llm.calls) == 1

\n