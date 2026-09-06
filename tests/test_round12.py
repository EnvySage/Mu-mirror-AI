"""第十二轮（rolling-mirror 累计镜子）单测 — GenerateProfile prompt 重写 + 四块输入渲染

覆盖任务书验证标准：
1. proto 契约（GenerateProfileRequest 新增 prev_mirror=11 / correction_index=12 / mirror_lookback=13，
   与 shared-protocol.md 2026-09-06 登记行一致——B 侧对账依据固化在此；
   附设计稿 §4-B 原提案 10/11/12 与 glossary=10 撞号的修正声明）
2. 四块渲染：prev_mirror / records（learnings 通道）/ correction_index / stats_facts（todos 通道）
3. 空块不留孤儿节头（correction_index 自带节头随块存在，同 glossary_render 模式）
4. 档位语义传递：mirror_lookback optional 三态（未传→按缺省 1 / 0 显式 / 2-3 显式），
   correction_index 仅 0 档出现由 B 侧保证，Python 侧只渲染传入内容
5. genesis 分支：prev_mirror 空 → 空串渲染（模板节头保留、内容为空 = 首份镜子语义）
6. prompt 第 6 套语义声明（累计画像/唯一真源/本月完成/genesis）
7. 截断防线（prev_mirror/correction_index/单条记录）

运行：.venv/Scripts/python.exe -m pytest tests/test_round12.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from generated import mirror_profile_pb2 as pb  # noqa: E402

from services import profile_service as ps  # noqa: E402
from services.profile_service import (  # noqa: E402
    _fmt_correction_index,
    _fmt_learnings,
    _fmt_prev_mirror,
    _fmt_stats_facts,
)


def _render(req: pb.GenerateProfileRequest) -> str:
    """复刻 servicer 的 prompt 组装（渲染函数单一真源在 profile_service，此处只接线）"""
    return ps.loader.render(
        "profile",
        prev_mirror=_fmt_prev_mirror(req.prev_mirror),
        correction_index=_fmt_correction_index(req.correction_index),
        stats_facts=_fmt_stats_facts(req.todos, req.total_records),
        records=_fmt_learnings(req.learnings),
        mood_stats=ps._fmt_mood_stats(req.mood_stats),
        keywords=ps._fmt_keywords(req.keywords),
        active_time=ps._fmt_active_time(req.active_time),
        total_records=req.total_records,
        time_range=req.time_range or "（未指定）",
        recent_chats=ps._fmt_recent_chats(req.recent_chats),
        glossary=ps.format_glossary(req.glossary),
    )


# ---------------------------------------------------------------------------
# 1. proto 契约自检（B 侧对账依据）
# ---------------------------------------------------------------------------
class TestProtoContract:
    def test_new_fields_numbers(self):
        """prev_mirror=11 / correction_index=12 / mirror_lookback=13
        （设计稿 §4-B 原提案 10/11/12 与 glossary=10 撞号，确认修正——shared-protocol 2026-09-06 行）"""
        fields = pb.GenerateProfileRequest.DESCRIPTOR.fields_by_name
        assert fields["prev_mirror"].number == 11
        assert fields["correction_index"].number == 12
        assert fields["mirror_lookback"].number == 13

    def test_glossary_unchanged_no_collision(self):
        """词典轮 glossary=10 保持不变（撞号修正的前提）"""
        fields = pb.GenerateProfileRequest.DESCRIPTOR.fields_by_name
        assert fields["glossary"].number == 10

    def test_full_field_map(self):
        """全字段号快照（1-9 既有字段不动，wire 向后兼容）"""
        fields = pb.GenerateProfileRequest.DESCRIPTOR.fields_by_name
        assert {n: f.number for n, f in fields.items()} == {
            "todos": 1, "learnings": 2, "mood_stats": 3, "keywords": 4, "active_time": 5,
            "total_records": 6, "time_range": 7, "llm_config": 8, "recent_chats": 9,
            "glossary": 10, "prev_mirror": 11, "correction_index": 12, "mirror_lookback": 13,
        }

    def test_mirror_lookback_is_optional(self):
        """optional 语义：未传 HasField=False（B 未升级仍可用），显式 0 HasField=True（0 档纯继承可表达）"""
        assert not pb.GenerateProfileRequest().HasField("mirror_lookback")
        assert pb.GenerateProfileRequest(mirror_lookback=0).HasField("mirror_lookback")
        assert pb.GenerateProfileRequest(mirror_lookback=3).HasField("mirror_lookback")
        assert pb.GenerateProfileRequest.DESCRIPTOR.fields_by_name[
            "mirror_lookback"].has_presence  # proto3 optional = 单数 + presence

    def test_types(self):
        fields = pb.GenerateProfileRequest.DESCRIPTOR.fields_by_name
        assert fields["prev_mirror"].type == 9   # TYPE_STRING
        assert fields["correction_index"].type == 9
        assert fields["mirror_lookback"].type == 5  # TYPE_INT32

    def test_wire_first_bytes(self):
        """wire 冒烟：prev_mirror=11 → 0x5A；lookback=0 → 0x68 0x00；未传 → 零字节；round-trip"""
        req = pb.GenerateProfileRequest(prev_mirror="上月镜子全文")
        assert req.SerializeToString()[0] == 0x5A

        req0 = pb.GenerateProfileRequest(mirror_lookback=0)
        assert req0.SerializeToString() == bytes([0x68, 0x00])  # field13 varint 0（显式 0 档上 wire）

        assert pb.GenerateProfileRequest().SerializeToString() == b""  # 未传零字节

        req2 = pb.GenerateProfileRequest(prev_mirror="M", correction_index="- [9-01] x",
                                         mirror_lookback=2, total_records=7)
        back = pb.GenerateProfileRequest()
        back.ParseFromString(req2.SerializeToString())
        assert back.prev_mirror == "M" and back.correction_index == "- [9-01] x"
        assert back.mirror_lookback == 2 and back.total_records == 7

    def test_service_unary_unchanged(self):
        """GenerateProfile 仍是唯一方法（无新增 RPC）"""
        methods = set(pb.DESCRIPTOR.services_by_name["MirrorProfile"].methods_by_name)
        assert methods == {"GenerateProfile"}


# ---------------------------------------------------------------------------
# 2. 四块渲染
# ---------------------------------------------------------------------------
class TestPrevMirror:
    def test_empty_is_empty_string(self):
        """genesis：prev_mirror 空 → 空串（模板节头保留、内容为空 = 首份镜子语义）"""
        assert _fmt_prev_mirror("") == ""
        assert _fmt_prev_mirror("   \n ") == ""
        assert _fmt_prev_mirror(None) == ""

    def test_content_passthrough(self):
        assert _fmt_prev_mirror("上月镜子：Three.js 学习主线。") == "上月镜子：Three.js 学习主线。"

    def test_truncation(self):
        long_text = "长" * (ps.PREV_MIRROR_MAX_CHARS + 500)
        out = _fmt_prev_mirror(long_text)
        assert out.endswith("…")
        assert len(out) == ps.PREV_MIRROR_MAX_CHARS + 1


class TestCorrectionIndex:
    def test_empty_no_orphan_header(self):
        """空块 → 空串（不留孤儿节头，glossary 同模式）"""
        assert _fmt_correction_index("") == ""
        assert _fmt_correction_index(None) == ""
        assert _fmt_correction_index("  \n") == ""

    def test_nonempty_carries_section_header(self):
        """非空 → 自带节头（模板占位符独立成段，节头随块存在）"""
        out = _fmt_correction_index("- [9-01] 论文开题")
        assert out.startswith("## 校正索引")
        assert "- [9-01] 论文开题" in out

    def test_truncation(self):
        out = _fmt_correction_index("- " + "长" * (ps.CORRECTION_INDEX_MAX_CHARS + 100))
        assert out.endswith("…")
        assert ("长" * (ps.CORRECTION_INDEX_MAX_CHARS + 100)) not in out


class TestRecordsChannel:
    """learnings 通道本轮承载 ② 本月原始记录"""

    def _rec(self, title="粒子系统", summary="完成 Three.js 粒子系统", created="9-03"):
        return pb.LearningItem(record_id=2, title=title, summary=summary, keywords=["Three.js"],
                               created_at=created)

    def test_empty_is_empty_string(self):
        """空块 → 空串（模板"本月记录原文"节不留孤儿内容）"""
        assert _fmt_learnings([]) == ""

    def test_render_lines(self):
        out = _fmt_learnings([self._rec()])
        assert "[9-03]" in out
        assert "粒子系统：完成 Three.js 粒子系统" in out
        assert "关键词：Three.js" in out

    def test_no_title_falls_back_to_summary(self):
        out = _fmt_learnings([self._rec(title="", summary="只有正文")])
        assert "只有正文" in out

    def test_per_chunk_truncation(self):
        """单条渲染截断防线（per_chunk_max_chars=2000 同源；B 侧四闸之外的最后防线）"""
        out = _fmt_learnings([self._rec(summary="长" * 3000)])
        assert ("长" * 3000) not in out
        # 截断保留前 2000 字符 + 省略号（省略号占 1 位，正文实际保留 2000）
        assert out.endswith("…")
        assert ("长" * ps.RECORD_MAX_CHARS) not in out
        assert ("长" * (ps.RECORD_MAX_CHARS - 10)) in out


class TestStatsFactsChannel:
    """todos 通道本轮承载 ④ 待办/统计实况（唯一真源）"""

    def _todo(self, title="写周报", summary="还没写", created="9-05"):
        return pb.TodoItem(record_id=1, title=title, summary=summary, created_at=created)

    def test_all_empty_is_empty_string(self):
        """全空 → 空串（不留孤儿节内容；未完成清单为空即"无未完成"事实本身）"""
        assert _fmt_stats_facts([], 0) == ""

    def test_todos_rendered_with_marker(self):
        out = _fmt_stats_facts([self._todo()], 0)
        assert "未完成待办" in out
        assert "[9-05] 写周报：还没写" in out

    def test_total_records_line(self):
        out = _fmt_stats_facts([], 12)
        assert "本月记录数：12" in out

    def test_zero_records_no_line(self):
        assert "本月记录数" not in _fmt_stats_facts([], 0)

    def test_combined(self):
        out = _fmt_stats_facts([self._todo()], 12)
        assert "未完成待办" in out and "本月记录数：12" in out


# ---------------------------------------------------------------------------
# 3. prompt 模板语义与渲染（第 6 套 profile.txt）
# ---------------------------------------------------------------------------
class TestPromptSemantics:
    def test_template_has_all_block_placeholders(self):
        text = ps.loader.load("profile")
        for ph in ("{prev_mirror}", "{correction_index}", "{stats_facts}", "{records}",
                   "{glossary}", "{total_records}", "{time_range}", "{recent_chats}"):
            assert ph in text, f"缺占位符 {ph}"

    def test_semantic_declarations(self):
        """任务书硬指令逐条落在模板里"""
        text = ps.loader.load("profile")
        assert "累计画像" in text                      # 语义声明：截至本月的累计画像
        assert "承续" in text                          # 承续上月镜子
        assert "唯一真源" in text                      # stats_facts 唯一真源
        assert "不从上一份镜子继承旧说法" in text       # 不继承旧说法
        assert "本月完成" in text                      # 日记未提但实况完成 → 写"本月完成"
        assert "首份镜子" in text                      # genesis 语义
        assert "不虚构历史事实" in text

    def test_no_absolute_month_directive(self):
        """无月份字段 → 文案用相对表述，不指示 LLM 写具体年月"""
        text = ps.loader.load("profile")
        assert "不要写具体年月数字" in text

    def test_genesis_render_no_inheritance_content(self):
        """genesis 渲染：块全空，'上一份镜子'节内容为空，无孤儿校正索引节"""
        p = _render(pb.GenerateProfileRequest(total_records=0))
        assert "## 上一份镜子\n\n\n" in p          # 节头保留、内容为空
        assert "校正索引（上月镜子涉及记录" not in p  # 空块无孤儿节头
        assert "{prev_mirror}" not in p and "{correction_index}" not in p

    def test_rolling_render_with_prev_mirror(self):
        """承续渲染：prev_mirror 有内容 + 实况 + 记录原文"""
        req = pb.GenerateProfileRequest(
            prev_mirror="上月镜子：学习主线 Three.js，待办 3 条未完成。",
            todos=[pb.TodoItem(record_id=1, title="写周报", summary="还没写", created_at="9-05")],
            learnings=[pb.LearningItem(record_id=2, title="粒子系统", summary="完成 Three.js 粒子系统",
                                       keywords=["Three.js"], created_at="9-03")],
            total_records=12, time_range="2026年9月",
        )
        p = _render(req)
        assert "上月镜子：学习主线 Three.js" in p
        assert "未完成待办" in p and "写周报" in p
        assert "本月记录数：12" in p
        assert "粒子系统" in p
        assert "校正索引" not in p  # correction_index 未传（lookback≠0）

    def test_lookback_zero_scenario_correction_index_present(self):
        """0 档场景：correction_index 有内容 → 自带节头渲染（无孤儿）"""
        req = pb.GenerateProfileRequest(
            prev_mirror="上月镜子全文…",
            correction_index="- [8-01] 健身计划\n- [8-12] 论文开题",
            mirror_lookback=0,
        )
        p = _render(req)
        assert "## 校正索引" in p
        assert "- [8-01] 健身计划" in p and "- [8-12] 论文开题" in p

    def test_glossary_block_zero_impact(self):
        """glossary 空词条零影响（既有行为回归保障；GlossaryTerm 定义在 common.proto）"""
        from generated import common_pb2 as common
        p = _render(pb.GenerateProfileRequest())
        assert "仅供参考" not in p
        req = pb.GenerateProfileRequest(glossary=[common.GlossaryTerm(term="论文", description="毕设")])
        assert "毕设" in _render(req)


# ---------------------------------------------------------------------------
# 4. 档位语义传递
# ---------------------------------------------------------------------------
class TestLookbackSemantics:
    def test_default_when_absent(self):
        """未传（B 旧版客户端）→ Python 按 1（默认档）处理；prompt 文案不区分档位"""
        req = pb.GenerateProfileRequest()
        lookback = req.mirror_lookback if req.HasField("mirror_lookback") else 1
        assert lookback == 1

    def test_three_states(self):
        assert not pb.GenerateProfileRequest().HasField("mirror_lookback")
        assert pb.GenerateProfileRequest(mirror_lookback=0).mirror_lookback == 0
        assert pb.GenerateProfileRequest(mirror_lookback=3).mirror_lookback == 3

    def test_correction_index_only_lookback_zero_contract(self):
        """③ 仅 0 档带上（设计稿 §2）：契约由 B 侧保证，Python 侧验证 wire 层可表达"""
        # B 未传 correction_index（1-3 档）→ 空串渲染
        req = pb.GenerateProfileRequest(mirror_lookback=1, prev_mirror="上月镜子")
        assert _fmt_correction_index(req.correction_index) == ""
        # 0 档 → 有内容渲染
        req0 = pb.GenerateProfileRequest(mirror_lookback=0, prev_mirror="上月镜子",
                                         correction_index="- [8-01] 健身计划")
        assert "健身计划" in _fmt_correction_index(req0.correction_index)

    def test_no_python_side_lookback_logic(self):
        """闸门在 B 侧：Python 不做档位→截断条数映射。config mirror 段只有渲染层截断值
        （prev_mirror/correction_index/record max_chars），无档位窗口逻辑"""
        from config import CONFIG
        assert "mirror" in CONFIG
        assert CONFIG["mirror"]["record_max_chars"] == 2000
        assert CONFIG["mirror"]["prev_mirror_max_chars"] > 0
        assert CONFIG["mirror"]["correction_index_max_chars"] > 0
        # 渲染函数只做 max_chars 防线（prompt 撑爆保护），与 lookback 档位无关：
        # _fmt_prev_mirror 对 lookback=0/1/2/3 的请求行为一致（透传 + 截断）
        for lb in (0, 1, 2, 3):
            req = pb.GenerateProfileRequest(prev_mirror="上月镜子", mirror_lookback=lb)
            assert _fmt_prev_mirror(req.prev_mirror) == "上月镜子"


# ---------------------------------------------------------------------------
# 5. promp​ts_loader 别名接线（旧名 → 新占位符）
# ---------------------------------------------------------------------------
class TestLoaderAliases:
    def test_legacy_names_map_to_new_placeholders(self):
        """代码侧旧名 todos/learnings 经别名渲染进模板新占位符 stats_facts/records
        （prompts_loader._ALIASES["profile"]：todos→stats_facts、learnings→records）"""
        from prompts_loader import loader
        p = loader.render("profile", todos="- 未完成待办：x", learnings="- 记录原文",
                          mood_stats="", keywords="", active_time="",
                          total_records=1, time_range="", recent_chats="",
                          glossary="")
        assert "- 未完成待办：x" in p   # → {stats_facts}
        assert "- 记录原文" in p        # → {records}

    def test_no_unreplaced_placeholders(self):
        import re
        p = ps.loader.render("profile", prev_mirror="", correction_index="", stats_facts="",
                             records="", mood_stats="", keywords="", active_time="",
                             total_records=0, time_range="", recent_chats="", glossary="")
        assert re.findall(r"\{[a-z_]+\}", p) == []


# ---------------------------------------------------------------------------
# 6. 兼容性：旧请求（三字段缺省）渲染结果与既有通道不受影响
# ---------------------------------------------------------------------------
class TestBackwardCompat:
    def test_old_style_request_renders(self):
        """B 侧未升级（不传三新字段）→ 正常渲染，六维输出路径不变"""
        req = pb.GenerateProfileRequest(
            todos=[pb.TodoItem(record_id=1, title="写周报", summary="还没写", created_at="9-01")],
            learnings=[pb.LearningItem(record_id=2, title="gRPC", summary="学习了流式 RPC",
                                       keywords=["gRPC"], created_at="9-02")],
            mood_stats=[pb.MoodStat(mood="anxious", count=3, percentage=30.0)],
            keywords=[pb.KeywordStat(keyword="工作", count=10)],
            total_records=120, time_range="最近30天",
        )
        p = _render(req)
        assert "写周报" in p and "学习了流式 RPC" in p
        assert "anxious: 3 次 (30.0%)" in p and "工作(10)" in p
        assert "最近30天" in p
        assert "## 上一份镜子\n\n\n" in p  # genesis 语义

    def test_render_functions_exist(self):
        """渲染函数为模块级（单测可注入）；servicer 内接线不重复实现"""
        assert callable(ps._fmt_prev_mirror)
        assert callable(ps._fmt_correction_index)
        assert callable(ps._fmt_learnings)
        assert callable(ps._fmt_stats_facts)
