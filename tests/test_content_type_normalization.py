import pytest

from generated import common_pb2 as common
from services.record_processor import RecordProcessorServicer
from mirror_core.prompts_loader import loader


@pytest.mark.parametrize("category", ["TODO", "PLAN", "plan"])
def test_plans_and_todos_share_the_todo_category_and_keep_task_status(category):
    result = RecordProcessorServicer()._build_item(
        {"content_type": category, "status": "IN_PROGRESS", "title": "修复日记问题"},
        "修复日记问题",
    )
    assert result.content_type == common.ContentType.TODO
    assert result.status == common.TaskStatus.IN_PROGRESS


@pytest.mark.parametrize("name", ["classify", "classify_single"])
def test_classification_prompts_offer_one_todo_category(name):
    prompt = loader.load(name)
    assert "- TODO（待办" in prompt
    assert "- PLAN（计划）" not in prompt
