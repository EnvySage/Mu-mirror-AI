"""recent-context 近期语境注入单测 — proto 契约 + 渲染 + prompt 接线 + Classify 集成

覆盖任务书验证标准：
1. proto 契约（ClassifyRequest.recent_context=6；RecentHint date=1/title=2/keywords=3；
   撞号核查：ClassifyRequest 现有字段 1-5 无冲突）
2. 渲染（recent_context_render.format_recent_context）：空列表 == ""、日期转换、title 40 截断、
   keywords 5 截断、无 keywords 省略"："、脏日期/空标题兜底
3. prompt 接线：两模板 {recent_context} 占位符、B 未传零回归、空清单无孤儿节头
4. Classify 集成：请求带 recent_context → 渲染进 prompt（捕获桩 LLM 收到的消息验证）

不真调 LLM（无有效配置），只做渲染层与桩调用验证。

运行：.venv/Scripts/python.exe -m pytest tests/test_recent_context.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from generated import common_pb2 as common  # noqa: E402
from generated import record_processor_pb2 as rp_pb2  # noqa: E402

from prompts_loader import loader  # noqa: E402
from recent_context_render import format_recent_context  # noqa: E402
from services import record_processor as rps  # noqa: E402


def _hint(date="2026-09-11", title="学吉他", keywords=None):
    return rp_pb2.RecentHint(
        date=date, title=title,
        keywords=(keywords if keywords is not None else ["吉他", "练习"]),
    )


# 任务书给定的两条样例（新→旧）
SAMPLE = [
    _hint(date="2026-09-11", title="学吉他", keywords=["吉他", "练习"]),
    _hint(date="2026-09-09", title="买吉他谱", keywords=["吉他谱", "乐理"]),
]


# ---------------------------------------------------------------------------
# 1. proto 契约自检（B 侧对账依据）
# ---------------------------------------------------------------------------
class TestProtoContract:
    def test_classify_request_field_numbers(self):
        """recent_context=6（open_todos=5 顺延，无撞号）；reference_date=7（相对时间消解，对齐 B 仓）"""
        fields = rp_pb2.ClassifyRequest.DESCRIPTOR.fields_by_name
        assert {n: f.number for n, f in fields.items()} == {
            "content": 1, "llm_config": 2, "single": 3, "glossary": 4,
            "open_todos": 5, "recent_context": 6, "reference_date": 7,
        }

    def test_recent_hint_fields(self):
        fields = rp_pb2.RecentHint.DESCRIPTOR.fields_by_name
        assert {n: f.number for n, f in fields.items()} == {"date": 1, "title": 2, "keywords": 3}

    def test_recent_hint_types(self):
        fields = rp_pb2.RecentHint.DESCRIPTOR.fields_by_name
        assert fields["date"].type == 9          # TYPE_STRING
        assert fields["title"].type == 9         # TYPE_STRING
        assert fields["keywords"].type == 9      # TYPE_STRING
        req_f = rp_pb2.ClassifyRequest.DESCRIPTOR.fields_by_name
        assert req_f["recent_context"].message_type.full_name == "mirror.RecentHint"

    def test_repeated_fields_functional(self):
        """repeated 语义：keywords 多值、recent_context 多条目均保留顺序"""
        h = rp_pb2.RecentHint(date="2026-09-11", title="学吉他", keywords=["吉他", "练习"])
        assert list(h.keywords) == ["吉他", "练习"]
        req = rp_pb2.ClassifyRequest(recent_context=[_hint(title="a"), _hint(title="b")])
        assert [r.title for r in req.recent_context] == ["a", "b"]

    def test_wire_first_bytes(self):
        """wire 冒烟：recent_context=6 → 0x32"""
        req = rp_pb2.ClassifyRequest(recent_context=[_hint()])
        assert req.SerializeToString()[0] == 0x32

    def test_backward_compat_old_client(self):
        """B 未升级（不传 recent_context）→ wire 无新字段，旧解析零影响"""
        req = rp_pb2.ClassifyRequest(content="今天很累", single=True)
        back = rp_pb2.ClassifyRequest()
        back.ParseFromString(req.SerializeToString())
        assert len(back.recent_context) == 0 and back.content == "今天很累"

    def test_service_rpcs_unchanged(self):
        methods = set(rp_pb2.DESCRIPTOR.services_by_name["RecordProcessor"].methods_by_name)
        assert methods == {"Classify", "ExtractTerms"}


# ---------------------------------------------------------------------------
# 2. 渲染
# ---------------------------------------------------------------------------
class TestFormatRecentContext:
    def test_empty_is_empty_string(self):
        """空清单 → 空串（整块消失不留孤儿——todo_render 同模式）"""
        assert format_recent_context([]) == ""
        assert format_recent_context(None) == ""

    def test_sample_render(self):
        out = format_recent_context(SAMPLE)
        assert out.startswith("## 用户近期记录（最近 7 天）\n")
        assert "以下是用户最近记录的主题清单（新→旧），用于理解当前内容中的指代与省略：" in out
        assert "- 9-11 学吉他：吉他、练习" in out
        assert "- 9-09 买吉他谱：吉他谱、乐理" in out
        assert "注意：清单仅供理解语境" in out
        assert "但不得把清单里没有的具体信息" in out

    def test_render_ends_with_newline(self):
        assert format_recent_context(SAMPLE).endswith("\n")

    def test_title_truncation_at_config(self):
        """超 40 字符 title 截断（config recent_context.max_title_chars）"""
        out = format_recent_context([_hint(title="长" * 60, keywords=[])])
        line = next(l for l in out.split("\n") if l.startswith("- 9-11"))
        assert ("长" * 40) in line
        assert ("长" * 41) not in line
        assert line.endswith("…"), "截断后应有省略号"

    def test_keywords_cap(self):
        """超 5 个 keywords 截断保留前 5（config recent_context.max_keywords）"""
        kws = [f"k{i}" for i in range(8)]
        out = format_recent_context([_hint(keywords=kws)])
        line = next(l for l in out.split("\n") if l.startswith("- 9-11"))
        for i in range(5):
            assert f"k{i}" in line
        for i in range(5, 8):
            assert f"k{i}" not in line

    def test_empty_keywords_omits_colon(self):
        out = format_recent_context([_hint(keywords=[])])
        line = next(l for l in out.split("\n") if l.startswith("- 9-11"))
        assert "：" not in line

    def test_no_title_fallback(self):
        out = format_recent_context([_hint(title="")])
        assert "（无标题）" in out

    def test_dirty_date_fallback(self):
        out = format_recent_context([_hint(date="")])
        assert "日期未知" in out

    def test_date_iso_datetime_sliced(self):
        out = format_recent_context([_hint(date="2026-09-11T08:00:00")])
        assert "- 9-11 " in out

    def test_cap_at_max_items(self):
        from recent_context_render import _MAX_ITEMS
        hints = [_hint(title=f"t{i}") for i in range(_MAX_ITEMS + 5)]
        out = format_recent_context(hints)
        assert f"t{_MAX_ITEMS - 1}" in out
        assert f"t{_MAX_ITEMS}" not in out


# ---------------------------------------------------------------------------
# 3. prompt 模板接线（classify / classify-single 两模板）
# ---------------------------------------------------------------------------
class TestPromptWiring:
    def test_both_templates_have_placeholder(self):
        for name in ("classify", "classify_single"):
            assert "{recent_context}" in loader.load(name), f"{name} 缺 recent_context 占位符"

    def test_fidelity_rule_in_both_templates(self):
        """防幻觉约束：两模板各自维护一份忠实原文规则节"""
        for name in ("classify", "classify_single"):
            text = loader.load(name)
            assert "## 忠实原文规则（重要！）" in text, name
            assert "禁止臆测或补充原文未提及的具体细节" in text, name
            assert "不得把近期记录里的信息当成当前内容的事实写出来" in text, name

    def test_render_injected_in_both(self):
        text = format_recent_context(SAMPLE)
        for name in ("classify", "classify_single"):
            p = loader.render(name, content="练了会曲子", glossary="", open_todos="",
                              recent_context=text)
            assert "## 用户近期记录（最近 7 天）" in p, name
            assert "- 9-11 学吉他：吉他、练习" in p, name
            assert "{recent_context}" not in p, name

    def test_empty_no_orphan_section(self):
        """空清单：节头/清单行/提示全部消失（整节由渲染器产出）"""
        for name in ("classify", "classify_single"):
            p = loader.render(name, content="今天很累", glossary="", open_todos="",
                              recent_context=format_recent_context([]))
            assert "## 用户近期记录（最近 7 天）" not in p, name
            assert "用户最近记录的主题清单" not in p, name
            assert "清单仅供理解语境" not in p, name

    def test_zero_regression_without_new_arg(self):
        """旧调用形状（不传 recent_context）→ 占位符填空串，不炸且无残留"""
        for name in ("classify", "classify_single"):
            p = loader.render(name, content="正文", glossary="", open_todos="")
            assert "{recent_context}" not in p, name

    def test_empty_both_no_extra_blank_line(self):
        """空 open_todos + 空 recent_context：不引入多余空行（零回归防线）"""
        p = loader.render("classify", content="今天很累", glossary="", open_todos="",
                          recent_context="")
        assert "\n\n\n\n## 拆分输出规则" not in p

    def test_no_unreplaced_placeholders(self):
        import re
        for name in ("classify", "classify_single"):
            p = loader.render(name, content="x", glossary="", open_todos="",
                              recent_context=format_recent_context(SAMPLE))
            assert re.findall(r"\{[a-z_]+\}", p) == [], name


# ---------------------------------------------------------------------------
# 4. Classify 集成（桩 LLM 捕获 prompt）
# ---------------------------------------------------------------------------
class _ScriptedLlm:
    def __init__(self, text):
        self._text = text

    def chat(self, messages, temperature=0.7):
        return self._text


class _CapturedContext:
    def abort(self, code, details):
        raise RuntimeError(f"abort: {code} {details}")


SPLIT_JSON = ('{"skip": false, "split_content": "练了会曲子", "items": ['
              '{"title": "练琴", "summary": "练了会曲子", "content_type": "LEARNING", '
              '"moods": [], "status": "STATUS_UNKNOWN", "keywords": ["吉他"]}]}')

SINGLE_JSON = ('{"skip": false, "title": "练琴", "summary": "练了会曲子", '
               '"content_type": "LEARNING", "moods": [], "status": "STATUS_UNKNOWN", '
               '"keywords": ["吉他"]}')


def _classify(llm, **req_kw):
    servicer = rps.RecordProcessorServicer()
    orig = servicer.Classify.__globals__["create_llm"]
    servicer.Classify.__globals__["create_llm"] = lambda **kw: llm
    try:
        return servicer.Classify(rp_pb2.ClassifyRequest(
            content="练了会曲子",
            llm_config=common.LlmConfig(provider="stub"),
            **req_kw,
        ), _CapturedContext())
    finally:
        servicer.Classify.__globals__["create_llm"] = orig


def _spy(output, captured):
    class _Spy(_ScriptedLlm):
        def chat(self, messages, temperature=0.7):
            captured["prompt"] = messages[0]["content"]
            return super().chat(messages, temperature)
    return _Spy(output)


class TestServiceInjection:
    def test_split_reaches_prompt(self):
        captured = {}
        resp = _classify(_spy(SPLIT_JSON, captured), recent_context=SAMPLE)
        assert not resp.skip and len(resp.items) == 1
        assert "## 用户近期记录（最近 7 天）" in captured["prompt"]
        assert "- 9-11 学吉他：吉他、练习" in captured["prompt"]
        assert "- 9-09 买吉他谱：吉他谱、乐理" in captured["prompt"]

    def test_single_reaches_prompt(self):
        captured = {}
        _classify(_spy(SINGLE_JSON, captured), single=True, recent_context=SAMPLE)
        assert "## 用户近期记录（最近 7 天）" in captured["prompt"]
        assert "- 9-11 学吉他：吉他、练习" in captured["prompt"]

    def test_no_recent_context_prompt_unchanged(self):
        """请求不带 recent_context → prompt 无清单段落（零回归）"""
        captured = {}
        _classify(_spy(SINGLE_JSON, captured), single=True)
        assert "## 用户近期记录（最近 7 天）" not in captured["prompt"]
        assert "用户最近记录的主题清单" not in captured["prompt"]


# ---------------------------------------------------------------------------
# 5. config recent_context 段
# ---------------------------------------------------------------------------
class TestConfig:
    def test_recent_context_section(self):
        from config import CONFIG
        assert CONFIG["recent_context"]["max_items"] == 20
        assert CONFIG["recent_context"]["max_title_chars"] == 40
        assert CONFIG["recent_context"]["max_keywords"] == 5

    def test_render_limits_follow_config(self):
        from recent_context_render import _MAX_ITEMS, _MAX_KEYWORDS, _MAX_TITLE_CHARS
        assert _MAX_ITEMS == 20 and _MAX_TITLE_CHARS == 40 and _MAX_KEYWORDS == 5
