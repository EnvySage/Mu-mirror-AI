"""相对时间消解单测 — ClassifyRequest.reference_date → ClassifyItem.time_substitutions

背景：B 侧 2026-09-20（bc54a8c）上线了 TimeSubstitutionApplier 并开始传 reference_date，
本端漏做——proto 无字段、prompt 无要求、代码不产出，B 拿到的替换表恒为空，功能上线但
一个字都没替换过。本文件覆盖：
1. proto 契约（字段号与 B 仓对齐）
2. 参照日期解析 / prompt 规则段渲染（无参照日期 = 零回归）
3. 替换表校验规整（固定词确定性计算、收窄、LLM 结果校验、长短词包含、上限）
4. Classify 集成（桩 LLM：单段 / 拆分 / 无参照日期）

运行：./venv/Scripts/python.exe -m pytest tests/test_time_substitution.py -v
"""

import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from generated import common_pb2 as common  # noqa: E402
from generated import record_processor_pb2 as rp_pb2  # noqa: E402

from prompts_loader import loader  # noqa: E402
from services import record_processor as rps  # noqa: E402
from time_substitution import (  # noqa: E402
    MAX_SUBSTITUTIONS,
    format_time_rule,
    parse_reference_date,
    resolve_substitutions,
)

REF = date(2026, 9, 12)  # 星期六


def _sub(original, resolved=""):
    return {"original": original, "resolved": resolved}


# ---------------------------------------------------------------------------
# 1. proto 契约
# ---------------------------------------------------------------------------
class TestProtoContract:
    def test_field_numbers_match_b(self):
        """与 B 仓 record_processor.proto 对齐：reference_date=7、time_substitutions=9、original=1/resolved=2"""
        req = rp_pb2.ClassifyRequest.DESCRIPTOR.fields_by_name
        item = rp_pb2.ClassifyItem.DESCRIPTOR.fields_by_name
        ts = rp_pb2.TimeSubstitution.DESCRIPTOR.fields_by_name
        assert req["reference_date"].number == 7
        assert item["time_substitutions"].number == 9
        assert ts["original"].number == 1 and ts["resolved"].number == 2


# ---------------------------------------------------------------------------
# 2. 参照日期 / prompt 规则段
# ---------------------------------------------------------------------------
class TestReferenceDateAndRule:
    def test_parse_reference_date(self):
        assert parse_reference_date("2026-09-12") == REF
        assert parse_reference_date("2026-9-2") == date(2026, 9, 2)
        assert parse_reference_date("") is None
        assert parse_reference_date("十二号") is None
        assert parse_reference_date("2026-02-30") is None

    def test_rule_empty_without_reference(self):
        assert format_time_rule(None) == ""

    def test_rule_contains_date_and_weekday(self):
        rule = format_time_rule(REF)
        assert "2026-09-12（星期六）" in rule
        assert "time_substitutions" in rule
        assert '{"original": "明天", "resolved": "2026-09-13"}' in rule

    def test_templates_zero_regression_without_reference(self):
        """没传参照日期：两模板都不出现本节、不残留占位符（与 B 未升级时一致）"""
        for name in ("classify", "classify_single"):
            p = loader.render(name, content="x", glossary="", open_todos="", recent_context="")
            assert "相对时间消解" not in p and "time_substitutions" not in p, name
            assert re.findall(r"\{[a-z_]+\}", p) == [], name

    def test_templates_render_rule_with_reference(self):
        for name in ("classify", "classify_single"):
            p = loader.render(name, content="x", glossary="", open_todos="", recent_context="",
                              time_rule=format_time_rule(REF))
            assert "这条记录写于 2026-09-12（星期六）" in p, name
            assert re.findall(r"\{[a-z_]+\}", p) == [], name


# ---------------------------------------------------------------------------
# 3. 替换表校验规整
# ---------------------------------------------------------------------------
class TestResolveSubstitutions:
    def test_fixed_words_computed_deterministically(self):
        """固定偏移词由本端算，LLM 给错的日期被覆盖"""
        text = "明天开始补文献综述，昨天没睡好，大后天考试"
        out = resolve_substitutions(
            [_sub("明天", "2026-09-20"), _sub("昨天", "瞎写的"), _sub("大后天")], text, REF)
        assert out == [("明天", "9月13日"), ("昨天", "9月11日"), ("大后天", "9月15日")]

    def test_time_of_day_suffix_kept(self):
        out = resolve_substitutions([_sub("今晚"), _sub("明早")], "今晚跑步，明早开会", REF)
        assert out == [("今晚", "9月12日晚上"), ("明早", "9月13日早上")]

    def test_narrowed_to_day_word(self):
        """"明天上午"只替换"明天"，"上午"留在原文里"""
        out = resolve_substitutions([_sub("明天上午", "2026-09-13")], "明天上午去开会", REF)
        assert out == [("明天", "9月13日")]

    def test_llm_computed_accepted_when_valid(self):
        text = "下周三交初稿，这周日休息"
        out = resolve_substitutions(
            [_sub("下周三", "2026-09-16"), _sub("这周日", "9月13日")], text, REF)
        assert out == [("下周三", "9月16日"), ("这周日", "9月13日")]

    def test_llm_computed_dropped_when_bad(self):
        text = "下周三交初稿，开题报告下午写，9月13日要交"
        out = resolve_substitutions([
            _sub("下周三", "下周三"),          # 没算出日期
            _sub("开题报告", "2026-09-13"),    # 不是时间词
            _sub("下午", "2026-09-12"),        # 时段不是某一天
            _sub("9月13日", "2026-09-13"),     # 已经是绝对日期
            _sub("下下周五", "2027-12-31"),    # 距参照日期超过一年
            _sub("上周一", "2026-09-07"),      # 原文里没有
        ], text, REF)
        assert out == []

    def test_month_day_resolves_to_nearest_year(self):
        ref = date(2026, 12, 30)
        out = resolve_substitutions([_sub("下周六", "1月2日")], "下周六回家", ref)
        assert out == [("下周六", "2027年1月2日")]

    def test_cross_year_fixed_word_gets_year(self):
        ref = date(2026, 12, 31)
        out = resolve_substitutions([_sub("明天"), _sub("今天")], "今天跨年，明天放假", ref)
        assert out == [("明天", "2027年1月1日"), ("今天", "12月31日")]

    def test_shorter_word_contained_in_longer_is_skipped(self):
        """文本里有"大后天"而替换表只有"后天"：替换"后天"会把"大后天"改成"大9月14日"，跳过"""
        text = "后天考试，大后天放假"
        assert resolve_substitutions([_sub("后天")], text, REF) == []
        both = resolve_substitutions([_sub("后天"), _sub("大后天")], text, REF)
        assert sorted(both) == [("后天", "9月14日"), ("大后天", "9月15日")]

    def test_duplicates_and_cap(self):
        text = "今天、明天、后天、昨天、前天都在忙，今晚也是"
        raw = [_sub("今天"), _sub("今天"), _sub("明天"), _sub("后天"),
               _sub("昨天"), _sub("前天"), _sub("今晚")]
        out = resolve_substitutions(raw, text, REF)
        assert len(out) == MAX_SUBSTITUTIONS
        assert [o for o, _ in out].count("今天") == 1

    def test_cap_never_leaves_shorter_word_without_its_longer_one(self):
        """"大前天"排在第 6 条被截掉时，"前天"不能单独留下（否则 B 替换出"大9月10日"）"""
        text = "今天、明天、后天、昨天、前天、大前天都在忙"
        raw = [_sub("今天"), _sub("明天"), _sub("后天"), _sub("昨天"), _sub("前天"), _sub("大前天")]
        originals = [o for o, _ in resolve_substitutions(raw, text, REF)]
        assert "前天" not in originals or "大前天" in originals

    def test_no_reference_or_malformed_input(self):
        assert resolve_substitutions([_sub("明天")], "明天见", None) == []
        assert resolve_substitutions("明天", "明天见", REF) == []
        assert resolve_substitutions([None, "明天", 3], "明天见", REF) == []


# ---------------------------------------------------------------------------
# 4. Classify 集成（桩 LLM 捕获 prompt）
# ---------------------------------------------------------------------------
class _SpyLlm:
    def __init__(self, text):
        self._text = text
        self.prompt = ""

    def chat(self, messages, temperature=0.7):
        self.prompt = messages[0]["content"]
        return self._text


class _Ctx:
    def abort(self, code, details):
        raise RuntimeError(f"abort: {code} {details}")


def _classify(llm, content, **req_kw):
    servicer = rps.RecordProcessorServicer()
    orig = servicer.Classify.__globals__["create_llm"]
    servicer.Classify.__globals__["create_llm"] = lambda **kw: llm
    try:
        return servicer.Classify(rp_pb2.ClassifyRequest(
            content=content, llm_config=common.LlmConfig(provider="stub"), **req_kw), _Ctx())
    finally:
        servicer.Classify.__globals__["create_llm"] = orig


def _subs(item):
    return [(s.original, s.resolved) for s in item.time_substitutions]


class TestClassifyIntegration:
    def test_single_mode_emits_substitutions(self):
        content = "今天把PPT做完了，明天开始补文献综述"
        llm = _SpyLlm(json.dumps({
            "skip": False, "title": "毕设进度", "summary": "PPT做完", "content_type": "WORK",
            "moods": [], "status": "STATUS_UNKNOWN", "keywords": ["毕设"],
            "time_substitutions": [_sub("今天", "2026-09-12"), _sub("明天", "2026-09-13")],
        }, ensure_ascii=False))
        resp = _classify(llm, content, single=True, reference_date="2026-09-12")
        assert "这条记录写于 2026-09-12（星期六）" in llm.prompt
        assert _subs(resp.items[0]) == [("今天", "9月12日"), ("明天", "9月13日")]

    def test_split_mode_validates_against_each_part(self):
        """拆分模式：每条的替换只认它自己那段原文（B 侧对 ClassifyItem.content 执行替换）"""
        content = "今天把PPT做完了。明天去剪头"
        llm = _SpyLlm(json.dumps({
            "skip": False, "split_content": "今天把PPT做完了。|||明天去剪头",
            "items": [
                {"title": "PPT", "summary": "做完", "content_type": "WORK", "moods": [], "status": "",
                 "keywords": [], "time_substitutions": [_sub("今天"), _sub("明天")]},
                {"title": "剪头", "summary": "去剪头", "content_type": "NOTE", "moods": [], "status": "",
                 "keywords": [], "time_substitutions": [_sub("明天")]},
            ],
        }, ensure_ascii=False))
        resp = _classify(llm, content, reference_date="2026-09-12")
        assert _subs(resp.items[0]) == [("今天", "9月12日")]   # "明天"不在第一段里，丢弃
        assert _subs(resp.items[1]) == [("明天", "9月13日")]

    def test_no_reference_date_is_zero_regression(self):
        """B 未传参照日期：prompt 无本节，LLM 就算吐了替换表也不产出"""
        llm = _SpyLlm(json.dumps({
            "skip": False, "title": "t", "summary": "s", "content_type": "NOTE", "moods": [],
            "status": "", "keywords": [], "time_substitutions": [_sub("明天", "2026-09-13")],
        }, ensure_ascii=False))
        resp = _classify(llm, "明天去剪头", single=True)
        assert "相对时间消解" not in llm.prompt
        assert _subs(resp.items[0]) == []
