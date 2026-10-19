"""Growth context and per-record suggestion validation."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from generated import common_pb2 as common  # noqa: E402
from generated import record_processor_pb2 as rp_pb2  # noqa: E402
from prompts_loader import loader  # noqa: E402
from services.record_processor import RecordProcessorServicer, _parse_growth_suggestions  # noqa: E402


AXES = [
    common.GrowthAxis(id="consistency", name="持续行动", description="规律投入", definition_version=3),
    common.GrowthAxis(id="reflection", name="复盘", description="从经历中总结", definition_version=8),
]


def test_growth_context_is_rendered_unchanged_in_both_prompts():
    for template in ("classify", "classify_single"):
        prompt = loader.render(template, growth_context=RecordProcessorServicer._format_growth_context(AXES))
        assert "consistency" in prompt and "definition_version=3" in prompt
        assert "reflection" in prompt and "definition_version=8" in prompt
        assert "every axis can be zero" in prompt


def test_user_tags_are_separate_from_keywords():
    item = RecordProcessorServicer()._build_item({
        "content": "今天练习了钢琴", "keywords": ["钢琴"], "user_tags": ["音乐", "练习"],
    }, fallback_content="今天练习了钢琴")
    assert list(item.keywords) == ["钢琴"]
    assert list(item.user_tags) == ["音乐", "练习"]


def test_no_evidence_does_not_invent_contribution():
    suggestions = _parse_growth_suggestions({}, AXES)
    assert [(s.dimension_id, s.points) for s in suggestions] == [
        ("consistency", 0), ("reflection", 0)
    ]


def test_unknown_and_out_of_range_suggestions_are_omitted():
    suggestions = _parse_growth_suggestions({"growth_suggestions": [
        {"dimension_id": "consistency", "points": 6, "reason": "out of range"},
        {"dimension_id": "invented", "points": 2, "reason": "unknown"},
        {"dimension_id": "reflection", "points": -1, "reason": "negative"},
        {"dimension_id": ["reflection"], "points": 1, "reason": "malformed"},
    ]}, AXES)
    assert [(s.dimension_id, s.points) for s in suggestions] == [
        ("consistency", 0), ("reflection", 0)
    ]


def test_protocol_field_numbers_are_append_only():
    req = rp_pb2.ClassifyRequest.DESCRIPTOR.fields_by_name
    resp = rp_pb2.ClassifyResponse.DESCRIPTOR.fields_by_name
    item = rp_pb2.ClassifyItem.DESCRIPTOR.fields_by_name
    assert req["growth_context"].number == 9
    assert resp["growth_suggestions"].number == 4
    assert item["user_tags"].number == 10
    assert {name: field.number for name, field in common.GrowthAxis.DESCRIPTOR.fields_by_name.items()} == {
        "id": 1, "name": 2, "description": 3, "definition_version": 4,
    }
    assert common.GrowthAxis.DESCRIPTOR.fields_by_name["definition_version"].type == 3  # TYPE_INT64, matching B's long.
    assert {name: field.number for name, field in common.GrowthContext.DESCRIPTOR.fields_by_name.items()} == {
        "axes": 1,
    }
    assert {name: field.number for name, field in common.GrowthSuggestion.DESCRIPTOR.fields_by_name.items()} == {
        "dimension_id": 1, "points": 2, "reason": 3,
    }
