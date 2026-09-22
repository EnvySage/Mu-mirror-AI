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
                              time_rule=format_time_rule(REF, single=(name == "classify_single")))
            assert "相对时间词消解" in p, name
            assert "这段记录写于 **2026-09-12（星期" in p, name
            assert re.findall(r"\{[a-z_]+\}", p) == [], name

    def test_rule_placement_is_right_after_output_format(self):
        """规则段必须紧贴「输出格式」（2026-09-22 真机实测：放 prompt 中部时模型频繁漏字段）"""
        for name, single in (("classify", False), ("classify_single", True)):
            p = loader.render(name, content="USER_TXT", glossary="", open_todos="", recent_context="",
                              time_rule=format_time_rule(REF, single=single))
            rule_at = p.find("相对时间词消解")
            # 规则在「输出格式」之后、且距用户输入不远（中间只隔 JSON 骨架与说明）
            assert p.find("输出格式") < rule_at, name
            assert rule_at < p.rfind("USER_TXT"), name
            assert p.rfind("USER_TXT") - rule_at < 1200, name

    def test_rule_wording_matches_mode(self):
        """单段模式 JSON 是平铺的，文案必须说"在这个 JSON 对象里"，不能说"每个 item 内部" """
        split_rule = format_time_rule(REF)
        single_rule = format_time_rule(REF, single=True)
        assert "每个 item 对象内部" in split_rule and "每个 item 对象内部" not in single_rule
        assert "在这个 JSON 对象里" in single_rule


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
        # 兜底扫描按原文出现顺序产出（2026-09-22 起固定词由本端独立扫描，不再依赖 LLM 顺序）
        assert out == [("今天", "12月31日"), ("明天", "2027年1月1日")]

    def test_shorter_word_contained_in_longer_is_skipped(self):
        """文本里"后天"与"大后天"并存：两者都必须替换，且"后天"不得吃掉"大后天"的前缀

        （2026-09-22 起由兜底扫描保证——LLM 是否列出都不影响）
        """
        text = "后天考试，大后天放假"
        out = resolve_substitutions([_sub("后天")], text, REF)
        assert sorted(out) == [("后天", "9月14日"), ("大后天", "9月15日")]
        # B 侧按长词优先执行替换后的实际文本（与 TimeSubstitutionApplier 同口径）
        applied = text
        for o, r in sorted(out, key=lambda x: -len(x[0])):
            applied = applied.replace(o, r)
        assert applied == "9月14日考试，9月15日放假"

    def test_scan_does_not_emit_shorter_word_when_only_longer_present(self):
        """文本里只有"大后天"：扫描按长词优先消费跨度，不得额外产出"后天" """
        out = resolve_substitutions([], "大后天放假", REF)
        assert out == [("大后天", "9月15日")]

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
        assert resolve_substitutions([_sub("明天")], "明天见", None) == []      # 无参照日期 → 全不产出
        # raw 畸形（非列表/含脏元素）不再导致整体放弃：固定词兜底独立于 LLM 输出
        assert resolve_substitutions("明天", "明天见", REF) == [("明天", "9月13日")]
        assert resolve_substitutions([None, "明天", 3], "明天见", REF) == [("明天", "9月13日")]

    def test_fixed_terms_substituted_even_when_llm_omits_them(self):
        """核心回归护栏（2026-09-22 真机实测 33% 成功率）：LLM 完全不给替换表时，固定词照样消解"""
        out = resolve_substitutions([], "昨天好累啊", REF)
        assert out == [("昨天", "9月11日")]
        out = resolve_substitutions(None, "今天把PPT做完了，明天开始补文献综述", REF)
        assert sorted(out) == [("今天", "9月12日"), ("明天", "9月13日")]

    def test_llm_result_never_overrides_deterministic_fixed_date(self):
        """LLM 把固定词日期算错（此处给 2030）：必须被本端确定性结果覆盖"""
        out = resolve_substitutions([_sub("明天", "2030-01-01")], "明天开会", REF)
        assert out == [("明天", "9月13日")]


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
        assert "这段记录写于 **2026-09-12（星期" in llm.prompt
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

    # ---- 外层字段容忍（2026-09-22 真机实测：模型单条时把字段提到顶层） ----

    def test_outer_level_subs_accepted_when_single_item(self):
        """模型把 time_substitutions 放到与 items 平级的顶层（单条场景）→ 采纳"""
        content = "昨天好累啊"
        llm = _SpyLlm(json.dumps({
            "skip": False, "split_content": content,
            "items": [{"title": "累", "summary": "累", "content_type": "THOUGHT", "moods": [],
                       "status": "", "keywords": []}],
            "time_substitutions": [_sub("昨天", "2026-09-11")],   # ← 在外层
        }, ensure_ascii=False))
        resp = _classify(llm, content, reference_date="2026-09-12")
        assert _subs(resp.items[0]) == [("昨天", "9月11日")]

    def test_outer_level_subs_rejected_when_multiple_items(self):
        """多条时外层字段归属不明：不采纳，只认 item 内（推理类词不受影响仍靠 item）"""
        content = "今天做A。明天做B"
        llm = _SpyLlm(json.dumps({
            "skip": False, "split_content": "今天做A|||明天做B",
            "items": [
                {"title": "A", "summary": "A", "content_type": "WORK", "moods": [], "status": "",
                 "keywords": [], "time_substitutions": [_sub("今天")]},
                {"title": "B", "summary": "B", "content_type": "PLAN", "moods": [], "status": "",
                 "keywords": []},
            ],
            "time_substitutions": [_sub("下周三", "2026-09-16")],   # ← 外层，多条时不认
        }, ensure_ascii=False))
        resp = _classify(llm, content, reference_date="2026-09-12")
        assert _subs(resp.items[0]) == [("今天", "9月12日")]
        # 外层"下周三"归属不明 → 不采纳；各条只拿自己段里的固定词（兜底扫描）
        assert _subs(resp.items[1]) == [("明天", "9月13日")]
        assert all("下周三" not in o for i in resp.items for o, _ in _subs(i))

    def test_fixed_words_substituted_even_when_model_says_nothing(self):
        """端到端护栏：模型一个替换都没给，固定词仍被消解（真机 33% 成功率的直接修复）"""
        content = "昨天好累啊"
        llm = _SpyLlm(json.dumps({
            "skip": False, "split_content": content,
            "items": [{"title": "累", "summary": "累", "content_type": "THOUGHT",
                       "moods": [], "status": "", "keywords": []}],
        }, ensure_ascii=False))     # ← 完全没有 time_substitutions 字段
        resp = _classify(llm, content, reference_date="2026-09-12")
        assert _subs(resp.items[0]) == [("昨天", "9月11日")]
