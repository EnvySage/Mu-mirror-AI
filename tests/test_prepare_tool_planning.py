"""Preparation plans must fit record content and reject malformed call arguments."""

import pytest

from generated import common_pb2, mirror_chat_pb2
from services import plan_service
from services.plan_service import plan_next_step, sanitize_calls
from mirror_core.prompts_loader import PromptLoader


@pytest.mark.parametrize("mode", ["single_helper", "single_rpc", "loop_helper", "loop_rpc"])
def test_planner_allocates_room_for_record_payload(monkeypatch, mode):
    class Llm:
        def json_task(self, messages, **options):
            self.options = options
            return '{"calls":[]}'

        def chat_stream(self, messages, **options):
            self.options = options
            yield "content", '{"calls":[],"done":true}'

    llm = Llm()
    request_type = mirror_chat_pb2.PlanToolsRequest if mode.startswith("single") else mirror_chat_pb2.PlanNextStepRequest
    request = request_type(
        question="帮我写个日记，我今天改bug改到头晕",
        tools=[common_pb2.ToolSpec(name="prepare_record", effect="PREPARE_WRITE", description="记录卡")],
    )
    monkeypatch.setattr(plan_service, "create_llm", lambda **kwargs: llm)
    if mode == "single_helper":
        list(plan_next_step(request, llm, prompt_name="plan_tools_single"))
    elif mode == "single_rpc":
        plan_service.MirrorChatServicer().PlanTools(request, None)
    elif mode == "loop_helper":
        list(plan_next_step(request, llm))
    else:
        list(plan_service.MirrorChatServicer().PlanNextStep(request, None))
    assert llm.options["max_tokens"] >= 4096


@pytest.mark.parametrize("bad_args", [None, [], "[]", 5, True])
def test_malformed_arguments_are_not_silently_replaced_with_empty_object(bad_args):
    calls, dropped = sanitize_calls({"calls": [{"tool": "prepare_record", "args": bad_args}]}, {"prepare_record"})
    assert calls == []
    assert dropped == 1


def test_non_finite_json_arguments_are_rejected():
    calls, dropped = sanitize_calls({"calls": [{"tool": "prepare_record", "args": {"points": float("nan")}}]}, {"prepare_record"})
    assert calls == []
    assert dropped == 1


@pytest.mark.parametrize("name", ["agent_intent", "plan_tools", "plan_tools_single"])
def test_assistant_record_examples_default_to_analysis_with_explicit_save_only_exception(name):
    prompt = PromptLoader().load(name)
    assert "助手新增记录默认使用 AI_ASSISTED" in prompt
    assert "仅保存、不分析" in prompt
    assert '"mode":"ONLY_SAVE"' not in prompt
    assert "mode=ONLY_SAVE" not in prompt
