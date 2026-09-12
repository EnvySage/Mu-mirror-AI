"""classify prompt 调优单测 — 情绪标签放宽 + 标题/摘要叙事规则 + 待办判别 few-shot

锁定本次 prompt 调优的口径（真实失败案例驱动：妈妈晚归吐槽 + 多出练琴时间）：
1. 两模板均含"合理推测"情绪规则（明确情绪必须打 / 语气态度可推测 / 纯事实不打 / 每条 1-2 个）
2. 两模板均含"标题与摘要规则"（标题抓主题、摘要还原转折）+ 真实案例示范
3. todo_render 渲染出的判别规则：练琴↔"准备学习吉他"正例、练琴↔"计划补文献综述"反例、
   多候选取语义最近 + 无实质关联不硬扯 的双向约束
4. 占位符无残留

不真调 LLM，只做渲染层验证。
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from generated import record_processor_pb2 as rp_pb2  # noqa: E402

from prompts_loader import loader  # noqa: E402
from todo_render import format_open_todos  # noqa: E402

TEMPLATES = ("classify", "classify_single")


def _hint(**kw):
    base = dict(todo_id=1, title="准备学习吉他", source_excerpt="最近准备打算开始学习吉他",
                created_at="2026-09-10T10:00:00", current_status="not_started")
    base.update(kw)
    return rp_pb2.TodoHint(**base)


class TestMoodRule:
    def test_relaxed_inference_rule_in_both(self):
        """放宽口径：合理推测（明确情绪必须打 + 语气态度可推测），不再一律留空"""
        for name in TEMPLATES:
            text = loader.load(name)
            assert "情绪标签规则" in text, name
            assert "合理推测" in text, name
            assert "必须打" in text, name
            assert "不要为凑数堆标签" in text, name

    def test_pure_fact_still_no_tag(self):
        """放宽不等于滥标：完全平铺直叙的纯事实仍不打标签"""
        for name in TEMPLATES:
            assert "平铺直叙的纯事实" in loader.load(name), name


class TestTitleSummaryRule:
    def test_rule_section_in_both(self):
        for name in TEMPLATES:
            text = loader.load(name)
            assert "标题与摘要规则" in text, name
            assert "10 字以内" in text and "30 字以内" in text, name

    def test_real_case_demo_in_both(self):
        """真实案例示范：体现"坏事变好事"的转折与基调，而非只抓单点"""
        for name in TEMPLATES:
            text = loader.load(name)
            assert "晚吃饭换来的练琴时间" in text, name
            assert "多出的时间让练琴突飞猛进" in text, name


class TestTodoFewShot:
    def test_positive_and_negative_examples(self):
        rules = format_open_todos([_hint()])
        assert "准备学习吉他" in rules and "练了琴" in rules    # 正例：必须填
        assert "计划补文献综述" in rules                        # 反例：不要填

    def test_two_way_guard(self):
        """双向兜底：有相关候选必填 / 无实质关联宁可不填——不偏向任一边"""
        rules = format_open_todos([_hint()])
        assert "语义最接近" in rules        # 多候选取最近
        assert "宁可不填" in rules          # 无关联不硬扯

    def test_no_unreplaced_placeholders(self):
        todos = format_open_todos([_hint()])
        for name in TEMPLATES:
            p = loader.render(name, content="今天练了琴", glossary="",
                              open_todos=todos, recent_context="")
            assert re.findall(r"\{[a-z_]+\}", p) == [], name
