"""第十轮（词典 sprint）单测 — ExtractTerms + glossary 渲染

覆盖任务书验证标准：
1. candidates 解析（_sanitize_candidates：term 去重/空 term 剔除/kind 白名单兜底/aliases 清洗/
   source_chunk_id 类型防御/上限截断）
2. kind 判定（VALID_KINDS 三选一，非法值兜底 new）
3. glossary 渲染格式（固定软约束话术原文、confirmed_at "N月确认"、aliases、空列表短路）
4. 超长 chunks 截断（truncate_chunks 保留最近 + sort_chunks_by_time 时间排序）
5. prompt 渲染（extract_terms 模板占位符、四注入点 glossary 接线、空词条不留孤儿话术）

运行：.venv/Scripts/python.exe -m pytest tests/test_round10.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from generated import common_pb2 as common  # noqa: E402
from generated import record_processor_pb2 as rp_pb2  # noqa: E402

from glossary_render import (  # noqa: E402
    GLOSSARY_SOFT_CONSTRAINT,
    format_glossary,
    glossary_lines,
    has_terms,
)
from services import lexicon_service  # noqa: E402
from services.lexicon_service import (  # noqa: E402
    _fmt_chunks,
    _fmt_existing_terms,
    _sanitize_candidates,
    sort_chunks_by_time,
    truncate_chunks,
)


def _chunk(chunk_id=1, segment="语料", created_at="2026-09-01T10:00:00", user_edited=False, **kw):
    return common.ChunkDTO(chunk_id=chunk_id, segment=segment, created_at=created_at,
                           user_edited=user_edited, **kw)


def _term(term="论文", **kw):
    return common.GlossaryTerm(term=term, **kw)


# ---------------------------------------------------------------------------
# 1. candidates 解析
# ---------------------------------------------------------------------------
class TestSanitizeCandidates:
    def test_normal_candidates(self):
        data = {"candidates": [
            {"term": "论文", "aliases": ["毕设"], "description": "毕设 RAG 方向",
             "kind": "new", "evidence": "近14天出现3次", "source_chunk_id": 101},
        ]}
        out = _sanitize_candidates(data)
        assert len(out) == 1
        c = out[0]
        assert c.term == "论文"
        assert list(c.aliases) == ["毕设"]
        assert c.kind == "new"
        assert c.evidence == "近14天出现3次"
        assert c.source_chunk_id == 101

    def test_empty_and_missing(self):
        assert _sanitize_candidates({}) == []
        assert _sanitize_candidates({"candidates": []}) == []
        assert _sanitize_candidates({"candidates": None}) == []
        # candidates 不是列表（LLM 抽风返回字符串）→ 不炸，返回空
        assert _sanitize_candidates({"candidates": "oops"}) == []

    def test_non_dict_items_skipped(self):
        out = _sanitize_candidates({"candidates": ["字符串项", 42, {"term": "论文"}]})
        assert len(out) == 1 and out[0].term == "论文"

    def test_empty_term_skipped(self):
        out = _sanitize_candidates({"candidates": [{"term": "  "}, {"term": ""}, {"term": "游戏"}]})
        assert [c.term for c in out] == ["游戏"]

    def test_duplicate_terms_deduped(self):
        out = _sanitize_candidates({"candidates": [
            {"term": "论文", "kind": "new"},
            {"term": "论文", "kind": "evidence"},  # 重复 term 剔除（保留首个）
            {"term": " 论文 ", "kind": "update"},  # 空白差异也算重复
        ]})
        assert [c.term for c in out] == ["论文"]

    def test_source_chunk_id_type_defense(self):
        """source_chunk_id 脏值（字符串数字/非数字/None）不炸，非法归 0"""
        out = _sanitize_candidates({"candidates": [
            {"term": "a", "source_chunk_id": "123"},
            {"term": "b", "source_chunk_id": "abc"},
            {"term": "c", "source_chunk_id": None},
            {"term": "d"},
        ]})
        assert [c.source_chunk_id for c in out] == [123, 0, 0, 0]

    def test_aliases_cleaned(self):
        out = _sanitize_candidates({"candidates": [
            {"term": "x", "aliases": [" a ", "", "b", 123, None]},
        ]})
        assert list(out[0].aliases) == ["a", "b", "123"]

    # ---- 语料内校验（2026-09-23：挡住模型抄行号/写错数字后挂到别人 chunk 上）----

    def test_chunk_id_outside_corpus_zeroed(self):
        """chunk_id 不在本次语料内 → 归 0（该整数必然命中库里另一条 chunk）"""
        out = _sanitize_candidates({"candidates": [
            {"term": "论文", "source_chunk_id": 999},      # 不在语料里
            {"term": "毕设", "source_chunk_id": 3},        # 在语料里
        ]}, valid_chunk_ids={3, 7, 8})
        assert [c.source_chunk_id for c in out] == [0, 3]

    def test_chunk_id_local_index_zeroed(self):
        """模型误填本地行号（1/2/3）→ 不在语料内 → 归 0，绝不挂到同号 chunk 上"""
        out = _sanitize_candidates({"candidates": [
            {"term": "论文", "source_chunk_id": 1},
        ]}, valid_chunk_ids={152, 153})
        assert out[0].source_chunk_id == 0

    def test_chunk_id_not_validated_without_corpus(self):
        """不传 valid_chunk_ids → 不校验（历史行为，兼容直接调用）"""
        out = _sanitize_candidates({"candidates": [
            {"term": "论文", "source_chunk_id": 999},
        ]})
        assert out[0].source_chunk_id == 999

    def test_chunk_id_zero_stays_zero_with_corpus(self):
        """填 0（无对应片段）= 合法值，不因校验被改"""
        out = _sanitize_candidates({"candidates": [
            {"term": "论文", "source_chunk_id": 0},
        ]}, valid_chunk_ids={3})
        assert out[0].source_chunk_id == 0

    def test_cap_at_max_candidates(self):
        """超过 max_candidates 截断（默认 10，宁缺毋滥防线）"""
        raw = [{"term": f"词{i}"} for i in range(50)]
        out = _sanitize_candidates({"candidates": raw})
        assert len(out) == lexicon_service._MAX_CANDIDATES


# ---------------------------------------------------------------------------
# 2. kind 判定
# ---------------------------------------------------------------------------
class TestKindValidation:
    def test_valid_kinds_pass(self):
        for kind in ("new", "evidence", "update"):
            out = _sanitize_candidates({"candidates": [{"term": "t", "kind": kind}]})
            assert out[0].kind == kind

    def test_invalid_kind_defaults_to_new(self):
        for dirty in ("NEW", "New", "unknown", "", "删除", None):
            out = _sanitize_candidates({"candidates": [{"term": "t", "kind": dirty}]})
            assert out[0].kind == "new", dirty

    def test_kind_whitespace_and_case_normalized(self):
        out = _sanitize_candidates({"candidates": [{"term": "t", "kind": " Update "}]})
        assert out[0].kind == "update"

    def test_missing_kind_defaults_to_new(self):
        out = _sanitize_candidates({"candidates": [{"term": "t"}]})
        assert out[0].kind == "new"


# ---------------------------------------------------------------------------
# 3. glossary 渲染格式
# ---------------------------------------------------------------------------
class TestGlossaryRender:
    def test_soft_constraint_exact_text(self):
        """固定软约束话术与设计稿第 4 节逐字一致"""
        assert GLOSSARY_SOFT_CONSTRAINT == "以下用户个人词汇表仅供参考，解释可能过时；与近期记录矛盾时，以近期记录为准。"

    def test_empty_glossary_returns_empty_string(self):
        """无词条 → 空串（调用方拼 prompt 不留孤儿节头）"""
        assert format_glossary([]) == ""
        assert format_glossary(None) == ""
        assert glossary_lines([]) == ""

    def test_full_render_with_aliases_and_month(self):
        t = common.GlossaryTerm(term="论文", description="毕设《AI日记镜子系统》",
                                aliases=["毕设", "那个设计"], confirmed_at="2026-06-01")
        out = format_glossary([t])
        assert out.startswith(GLOSSARY_SOFT_CONSTRAINT)
        assert "- 论文（又称：毕设、那个设计）（6月确认）：毕设《AI日记镜子系统》" in out

    def test_confirmed_at_month_extraction(self):
        t = common.GlossaryTerm(term="游戏", description="明日方舟", confirmed_at="2026-03-15")
        assert "（3月确认）" in format_glossary([t])

        t2 = common.GlossaryTerm(term="游戏", description="x", confirmed_at="2026-03-15T22:00:00")
        assert "（3月确认）" in format_glossary([t2])

    def test_confirmed_at_unparseable_kept_verbatim(self):
        t = common.GlossaryTerm(term="旧词", description="x", confirmed_at="2026/07/02")
        assert "（2026/07/02确认）" in format_glossary([t])

    def test_no_confirmed_at_no_mark(self):
        t = common.GlossaryTerm(term="镜子", description="这个 AI 助手")
        out = format_glossary([t])
        assert "- 镜子：这个 AI 助手" in out
        assert "确认" not in out.replace(GLOSSARY_SOFT_CONSTRAINT, "")

    def test_blank_term_lines_skipped(self):
        out = format_glossary([_term(""), _term("  "), _term("论文")])
        assert "论文" in out
        assert out.count("- ") == 1

    def test_has_terms(self):
        assert not has_terms([])
        assert not has_terms([_term("")])
        assert has_terms([_term("论文")])

    def test_empty_description_allowed(self):
        t = common.GlossaryTerm(term="镜子", confirmed_at="2026-06-01")
        out = format_glossary([t])
        assert "- 镜子（6月确认）" in out


# ---------------------------------------------------------------------------
# 4. 超长 chunks 截断 + 时间排序
# ---------------------------------------------------------------------------
class TestChunkTruncation:
    def test_within_limit_no_truncation(self):
        chunks = [_chunk(chunk_id=i) for i in range(10)]
        kept, dropped = truncate_chunks(chunks)
        assert dropped == 0
        assert [c.chunk_id for c in kept] == list(range(10))

    def test_over_limit_keeps_latest(self):
        """超出 max_chunks 丢弃最早的（时间窗缩窗语义）"""
        n = lexicon_service._MAX_CHUNKS + 25
        chunks = [_chunk(chunk_id=i, created_at=f"2026-08-{(i % 28) + 1:02d}T00:00:00") for i in range(n)]
        ordered = sort_chunks_by_time(chunks)
        kept, dropped = truncate_chunks(ordered)
        assert dropped == 25
        assert len(kept) == lexicon_service._MAX_CHUNKS
        # 保留的是 ordered 的尾部（最近的）
        assert [c.chunk_id for c in kept] == [c.chunk_id for c in ordered[-lexicon_service._MAX_CHUNKS:]]

    def test_sort_by_time_ascending(self):
        chunks = [
            _chunk(chunk_id=3, created_at="2026-09-03T10:00:00"),
            _chunk(chunk_id=1, created_at="2026-09-01T10:00:00"),
            _chunk(chunk_id=2, created_at="2026-09-02T10:00:00"),
        ]
        out = sort_chunks_by_time(chunks)
        assert [c.chunk_id for c in out] == [1, 2, 3]

    def test_missing_created_at_sorted_last(self):
        chunks = [
            _chunk(chunk_id=2, created_at=""),
            _chunk(chunk_id=1, created_at="2026-09-01T10:00:00"),
            _chunk(chunk_id=3, created_at=""),
        ]
        out = sort_chunks_by_time(chunks)
        assert out[0].chunk_id == 1
        assert {c.chunk_id for c in out[1:]} == {2, 3}

    def test_stable_sort_same_time(self):
        chunks = [_chunk(chunk_id=5, created_at="2026-09-01"), _chunk(chunk_id=4, created_at="2026-09-01")]
        out = sort_chunks_by_time(chunks)
        assert [c.chunk_id for c in out] == [5, 4]

    def test_fmt_chunks_marks_user_edited(self):
        """user_edited=true 的语料前置标注"用户手动修改过"（任务书要求）"""
        chunks = [
            _chunk(chunk_id=7, segment="论文开题", created_at="2026-09-01", user_edited=True),
            _chunk(chunk_id=8, segment="日常", created_at="2026-09-02", user_edited=False),
        ]
        text = _fmt_chunks(chunks)
        assert "[用户手动修改过] 2026-09-01 chunk_id=7：论文开题" in text
        assert "2026-09-02 chunk_id=8：日常" in text

    def test_fmt_chunks_has_single_number_per_line(self):
        """每行只有一个可抄的数字 = chunk_id（2026-09-23 去掉本地行号 [1]）

        原先渲染 `[1] ... chunk_id=7`，"编号"两解；模型若抄行号，那个小整数会指向库里
        另一条真实 chunk（chunks.id 全局自增），静默挂错佐证。
        """
        import re
        chunks = [
            _chunk(chunk_id=7, segment="论文开题", created_at="2026-09-01"),
            _chunk(chunk_id=8, segment="日常", created_at="2026-09-02"),
        ]
        for line in _fmt_chunks(chunks).splitlines():
            rest = re.sub(r"\d{4}-\d{2}-\d{2}", "", line)   # 去日期
            rest = re.sub(r"chunk_id=\d+", "", rest)        # 去 chunk_id
            assert not re.search(r"\d", rest), f"行内残留数字: {line}"

    def test_fmt_chunks_segment_fallback_and_truncate(self):
        """segment 空 → 回退 content；超长截断到 max_chunk_chars"""
        long_seg = "长" * 500
        chunks = [
            _chunk(chunk_id=1, segment="", content="回退正文", created_at="2026-09-01"),
            _chunk(chunk_id=2, segment=long_seg, created_at="2026-09-02"),
        ]
        text = _fmt_chunks(chunks)
        assert "回退正文" in text
        assert ("长" * 500) not in text
        assert ("长" * lexicon_service._MAX_CHUNK_CHARS) in text

    def test_fmt_existing_terms_empty(self):
        assert "还是空的" in _fmt_existing_terms([])
        out = _fmt_existing_terms([_term("论文", aliases=["毕设"], description="RAG 方向")])
        assert "- 论文（又称：毕设）：RAG 方向" in out


# ---------------------------------------------------------------------------
# 5. prompt 渲染接线
# ---------------------------------------------------------------------------
class TestPromptWiring:
    def test_extract_terms_template_renders(self):
        from prompts_loader import loader
        p = loader.render("extract_terms",
                          chunks="[1] 2026-09-01 chunk_id=1：语料",
                          existing_terms="- 论文：x")
        assert "个人词典抽取助手" in p
        assert "chunk_id=1" in p
        assert "{chunks}" not in p and "{existing_terms}" not in p

    def test_classify_glossary_placeholder(self):
        from prompts_loader import loader
        p = loader.render("classify", content="正文", glossary=format_glossary([_term("论文")]))
        assert "论文" in p and "{glossary}" not in p
        p_empty = loader.render("classify", content="正文", glossary="")
        assert "仅供参考" not in p_empty  # 空词条不留孤儿话术

    def test_intent_chat_profile_glossary_placeholder(self):
        from prompts_loader import loader
        g = format_glossary([_term("论文", description="毕设")])
        assert "毕设" in loader.render("intent", query="q", glossary=g)
        assert "毕设" in loader.render("chat", question="q", history="h", context="c", glossary=g)
        assert "毕设" in loader.render("profile", todos="", learnings="", mood_stats="",
                                       keywords="", active_time="", total_records=0,
                                       time_range="", recent_chats="", glossary=g)

    def test_config_has_extract_terms_section(self):
        from config import CONFIG
        assert "extract_terms" in CONFIG
        assert CONFIG["extract_terms"]["max_chunks"] > 0
        assert CONFIG["extract_terms"]["max_chunk_chars"] > 0
        assert CONFIG["extract_terms"]["max_candidates"] > 0
        assert CONFIG["prompts"]["extract_terms"].endswith("extract_terms.txt")

    def test_proto_fields_present(self):
        """proto 契约自检：ExtractTerms + 四注入点 glossary 字段（B 侧对账依据）"""
        req_fields = rp_pb2.ExtractTermsRequest.DESCRIPTOR.fields_by_name
        assert [f.number for f in sorted(req_fields.values(), key=lambda f: f.number)] == [1, 2, 3]
        assert set(req_fields) == {"chunks", "existing_terms", "llm_config"}
        reply = rp_pb2.ExtractTermsReply.TermCandidate.DESCRIPTOR.fields_by_name
        assert set(reply) == {"term", "aliases", "description", "kind", "evidence", "source_chunk_id"}
        assert reply["source_chunk_id"].number == 6

        assert common_pb2_glossary_number() == {
            "ClassifyRequest": 4, "ExtractIntentRequest": 3, "ChatRequest": 5, "GenerateProfileRequest": 10,
        }


def common_pb2_glossary_number():
    from generated import mirror_chat_pb2 as chat
    from generated import mirror_profile_pb2 as profile
    from generated import record_processor_pb2 as rp
    return {
        "ClassifyRequest": rp.ClassifyRequest.DESCRIPTOR.fields_by_name["glossary"].number,
        "ExtractIntentRequest": chat.ExtractIntentRequest.DESCRIPTOR.fields_by_name["glossary"].number,
        "ChatRequest": chat.ChatRequest.DESCRIPTOR.fields_by_name["glossary"].number,
        "GenerateProfileRequest": profile.GenerateProfileRequest.DESCRIPTOR.fields_by_name["glossary"].number,
    }
