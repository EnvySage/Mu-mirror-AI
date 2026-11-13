"""Selective-index suggestions stay conservative and preserve classification output."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from generated import common_pb2 as common  # noqa: E402
from generated import record_processor_pb2 as pb2  # noqa: E402
from services import record_processor  # noqa: E402


@pytest.mark.parametrize("item, expected", [
    ({"embedding_enabled": False}, False),
    ({"embedding_enabled": True}, True),
    ({}, True),
    ({"embedding_enabled": None}, True),
    ({"embedding_enabled": "false"}, True),
    ({"embedding_enabled": 0}, True),
    ({"embedding_enabled": 1}, True),
    ({"embedding_enabled": []}, True),
])
def test_parser_accepts_only_real_boolean(item, expected):
    from mirror_core.embedding_decision import _parse_embedding_decision

    assert _parse_embedding_decision(item) == (expected, "")


@pytest.mark.parametrize("reason, expected", [
    ("普通日常", "普通日常"),
    ("理" * 201, "理" * 200),
    (None, ""),
    (23, ""),
])
def test_parser_bounds_reason_to_200_characters(reason, expected):
    from mirror_core.embedding_decision import _parse_embedding_decision

    assert _parse_embedding_decision({"embedding_reason": reason}) == (True, expected)


class FakeLlm:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        return json.dumps(self.payload, ensure_ascii=False)


class FakeContext:
    def abort(self, code, details):
        pytest.fail(f"unexpected gRPC abort: {code}: {details}")


def classify(monkeypatch, payload, content, single=False, reference_date=""):
    llm = FakeLlm(payload)
    monkeypatch.setattr(record_processor, "create_llm", lambda **_: llm)
    response = record_processor.RecordProcessorServicer().Classify(
        pb2.ClassifyRequest(content=content, single=single, processing_authorized=True,
                            reference_date=reference_date), FakeContext())
    assert len(llm.calls) == 1
    return response


def test_split_keeps_mixed_suggestions_aligned_and_preserves_classification(monkeypatch):
    response = classify(monkeypatch, {
        "skip": False,
        "split_content": "今天好累|||今天去了陈记牛肉面，太咸，以后不去了",
        "items": [
            {"title": "疲惫", "content_type": "HEALTH", "moods": ["EXHAUSTED"],
             "embedding_enabled": False, "embedding_reason": "普通日常"},
            {"title": "餐厅评价", "content_type": "NOTE", "keywords": ["陈记牛肉面"],
             "embedding_enabled": True, "embedding_reason": "具体地点与后续偏好"},
        ],
    }, "今天好累。今天去了陈记牛肉面，太咸，以后不去了", reference_date="2026-11-12")

    assert not response.skip
    assert [item.content for item in response.items] == ["今天好累", "今天去了陈记牛肉面，太咸，以后不去了"]
    assert [item.embedding_enabled for item in response.items] == [False, True]
    assert all(item.HasField("embedding_enabled") for item in response.items)
    assert [item.embedding_reason for item in response.items] == ["普通日常", "具体地点与后续偏好"]
    assert response.items[0].content_type == common.ContentType.HEALTH
    assert list(response.items[0].moods) == [common.MoodType.EXHAUSTED]
    assert list(response.items[1].keywords) == ["陈记牛肉面"]
    assert [(sub.original, sub.resolved) for sub in response.items[0].time_substitutions] == [("今天", "11月12日")]


@pytest.mark.parametrize("wrapped", [False, True])
def test_single_false_has_presence_and_keeps_original_and_time_substitution(monkeypatch, wrapped):
    item = {"title": "日常疲惫", "content_type": "HEALTH", "moods": ["EXHAUSTED"],
            "embedding_enabled": False, "embedding_reason": "日常" * 101}
    payload = {"skip": False, "items": [item]} if wrapped else {"skip": False, **item}
    response = classify(monkeypatch, payload, "今天好累", single=True, reference_date="2026-11-12")

    assert not response.skip
    assert len(response.items) == 1
    item = response.items[0]
    assert item.content == "今天好累"
    assert item.HasField("embedding_enabled") and item.embedding_enabled is False
    assert item.embedding_reason == "日常" * 100
    assert [(sub.original, sub.resolved) for sub in item.time_substitutions] == [("今天", "11月12日")]


@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize("decision", [{}, {"embedding_enabled": "false"}])
def test_old_or_invalid_model_suggestion_defaults_to_explicit_true(monkeypatch, single, decision):
    item = {"title": "日常", "content_type": "NOTE", **decision}
    response = classify(monkeypatch, {"skip": False, "split_content": "中午吃饭", "items": [item]},
                        "中午吃饭", single=single)
    assert response.items[0].HasField("embedding_enabled")
    assert response.items[0].embedding_enabled is True


@pytest.mark.parametrize("single", [False, True])
def test_short_valid_diary_is_classified_instead_of_skipped(monkeypatch, single):
    response = classify(monkeypatch, {
        "skip": False, "split_content": "好累",
        "items": [{"title": "疲惫", "content_type": "HEALTH", "embedding_enabled": False}],
    }, "好累", single=single)
    assert not response.skip
    assert response.items[0].content == "好累"
    assert response.items[0].embedding_enabled is False


@pytest.mark.parametrize("single", [False, True])
def test_meaningless_model_skip_is_preserved(monkeypatch, single):
    response = classify(monkeypatch, {"skip": True, "skip_reason": "纯语气词"}, "啊啊啊", single=single)
    assert response.skip and response.skip_reason == "纯语气词"
    assert not response.items


def test_embedding_fields_wire_numbers_presence_and_round_trip():
    fields = pb2.ClassifyItem.DESCRIPTOR.fields_by_name
    assert fields["embedding_enabled"].number == 11
    assert fields["embedding_reason"].number == 12
    assert not pb2.ClassifyItem().HasField("embedding_enabled")
    false_item = pb2.ClassifyItem(embedding_enabled=False, embedding_reason="普通日常")
    restored = pb2.ClassifyItem.FromString(false_item.SerializeToString())
    assert restored.HasField("embedding_enabled")
    assert restored.embedding_enabled is False
    assert restored.embedding_reason == "普通日常"
