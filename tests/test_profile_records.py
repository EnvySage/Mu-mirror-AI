"""The profile RPC preserves the server's selected authorized record material."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from generated import mirror_profile_pb2 as pb  # noqa: E402
from services import profile_service as ps  # noqa: E402


class CapturingLlm:
    def __init__(self):
        self.messages = []

    def chat(self, messages):
        self.messages.append(messages)
        return json.dumps({"overall_summary": "镜子结果", "user_tags": ["日常记录"]})


class Context:
    def abort(self, code, details):
        pytest.fail(f"unexpected profile abort: {code}: {details}")


def generate_prompt(monkeypatch, request):
    llm = CapturingLlm()
    monkeypatch.setattr(ps, "create_llm", lambda **_: llm)
    response = ps.MirrorProfileServicer().GenerateProfile(request, Context())
    assert response.overall_summary == "镜子结果"
    assert len(llm.messages) == 1
    return llm.messages[0][0]["content"]


def test_records_field_has_number_16_and_explicit_empty_survives_wire_round_trip():
    field = pb.GenerateProfileRequest.DESCRIPTOR.fields_by_name["records"]
    assert field.number == 16 and field.has_presence
    assert not pb.GenerateProfileRequest().HasField("records")
    request = pb.GenerateProfileRequest(records="")
    restored = pb.GenerateProfileRequest.FromString(request.SerializeToString())
    assert restored.HasField("records")
    assert restored.records == ""


def test_records_presence_takes_priority_over_legacy_learning_summaries(monkeypatch):
    prompt = generate_prompt(monkeypatch, pb.GenerateProfileRequest(
        records="[2026-11-12] 中午吃饭，下午干活",
        learnings=[pb.LearningItem(title="旧学习标题", summary="LEGACY_SUMMARY_SHOULD_NOT_LEAK")],
    ))
    assert "[2026-11-12] 中午吃饭，下午干活" in prompt
    assert "LEGACY_SUMMARY_SHOULD_NOT_LEAK" not in prompt
    assert "旧学习标题" not in prompt


def test_explicit_empty_records_does_not_fall_back_to_learning_material(monkeypatch):
    request = pb.GenerateProfileRequest(
        records="", mirror_lookback=0, correction_index="校正清单保留",
        learnings=[pb.LearningItem(summary="LEGACY_MATERIAL_MUST_STAY_OUT")],
    )
    # Pass through protobuf serialization, as the real caller does.
    prompt = generate_prompt(monkeypatch, pb.GenerateProfileRequest.FromString(request.SerializeToString()))
    assert "LEGACY_MATERIAL_MUST_STAY_OUT" not in prompt
    assert "校正清单保留" in prompt
    assert "**本月记录原文：**\n\n\n" in prompt


def test_old_client_without_records_presence_keeps_learning_rendering(monkeypatch):
    request = pb.GenerateProfileRequest(learnings=[pb.LearningItem(
        record_id=7, title="旧学习", summary="旧客户端仍能回看", keywords=["兼容"], created_at="2026-11-11")])
    restored = pb.GenerateProfileRequest.FromString(request.SerializeToString())
    prompt = generate_prompt(monkeypatch, restored)
    assert "- [2026-11-11] 旧学习：旧客户端仍能回看（关键词：兼容）" in prompt


def test_multi_record_text_keeps_final_fact_past_legacy_per_item_limit(monkeypatch):
    # Three individually bounded chunks; the combined text exceeds the old 2,000-char item limit.
    records = ("[2026-11-10] " + "日常片段。" * 300 + "\n"
               "[2026-11-11] " + "工作片段。" * 300 + "\n"
               "[2026-11-12] 陈记牛肉面太咸，以后不去了\n")
    prompt = generate_prompt(monkeypatch, pb.GenerateProfileRequest(records=records, mirror_lookback=1))
    assert records in prompt
    assert "[2026-11-12] 陈记牛肉面太咸，以后不去了" in prompt
