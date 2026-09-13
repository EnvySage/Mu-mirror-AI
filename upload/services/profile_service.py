"""MirrorProfile 服务实现 — 画像生成（协作清单 #6 + 第十二轮累计镜子）

输入：五维统计（todos/learnings/mood_stats/keywords/active_time）+ recent_chats + glossary
     + 累计镜子四块新输入（rolling-mirror-design.md §1）：
       ① prev_mirror      上一份镜子全文（空 = 首份镜子 genesis）
       ② records          本月原始记录（按回看深度档位由 B 侧闸门限流后传入）
       ③ correction_index 校正索引（仅回看深度=0 时传入，防低档误差累积）
       ④ stats_facts      待办/统计实况直查（唯一真源，LLM 只叙事不记账）
     ②④ 由 B 侧复用既有字段塞入：learnings 通道→records、todos 通道→stats_facts。
输出：六维分析（todo/learning/mood/rhythm）+ user_tags + overall_summary（累计语义）
无状态：配置随请求携带，用完即弃。
"""

from generated import mirror_profile_pb2 as pb2
from generated import mirror_profile_pb2_grpc as pb2_grpc

from config import CONFIG
from errors import abort_with_mapped
from glossary_render import format_glossary
from llm.factory import create_llm
from llm_json import parse_json
from prompts_loader import loader

VALID_TAGS_MIN = 1

# 累计镜子渲染上限（config.yml mirror 段；rolling-mirror-design.md §3 per_chunk_max_chars 对齐。
# B 侧另有四道防洪闸（条数/总字符/单条/递归链），Python 侧只是渲染层最后防线，防超长块撑爆 prompt。
# 注意：lookback 档位→原文条数的窗口闸门在 B 侧，Python 不做档位逻辑）
_MIRROR_CFG = CONFIG.get("mirror", {})
RECORD_MAX_CHARS = int(_MIRROR_CFG.get("record_max_chars", 2000))
PREV_MIRROR_MAX_CHARS = int(_MIRROR_CFG.get("prev_mirror_max_chars", 30000))
CORRECTION_INDEX_MAX_CHARS = int(_MIRROR_CFG.get("correction_index_max_chars", 8000))


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _fmt_todos(todos) -> str:
    """todos 通道在本轮承载 ④ 待办/统计实况直查（唯一真源）。

    空块 → 空串（模板"待办/统计实况"节内容为空；无未完成待办这一事实
    由"未完成待办"空列表本身表达，不渲染占位文字——空块不留孤儿内容，
    同 glossary 模式）。
    """
    if not todos:
        return ""
    lines = [f"- [{t.created_at}] {t.title}：{t.summary}" for t in todos]
    return "未完成待办：\n" + "\n".join(lines)


def _fmt_learnings(learnings) -> str:
    """learnings 通道在本轮承载 ② 本月原始记录（B 侧按回看深度档位带入的原文）。

    空块 → 空串（模板"本月记录原文"节不留孤儿内容，同 glossary 模式）。
    """
    if not learnings:
        return ""
    lines = []
    for l in learnings:
        kw = "/".join(l.keywords)
        suffix = f"（关键词：{kw}）" if kw else ""
        body = f"{l.title}：{l.summary}{suffix}" if l.title else f"{l.summary}{suffix}"
        lines.append(f"- [{l.created_at}] {_truncate(body, RECORD_MAX_CHARS)}")
    return "本月记录（按回看深度档位带入，B 侧已限流）：\n" + "\n".join(lines)


def _fmt_prev_mirror(prev_mirror: str) -> str:
    """① 上一份镜子全文。空 → 空串（模板节头保留，内容为空 = genesis 首份镜子语义）。"""
    return _truncate((prev_mirror or "").strip(), PREV_MIRROR_MAX_CHARS)


def _fmt_correction_index(correction_index: str) -> str:
    """③ 校正索引（仅 lookback=0 时传入）。空 → 空串，不留孤儿内容。

    非空时自带节头（模板中占位符独立成段，节头由本函数提供）——
    与 glossary_render 的"渲染器自带节头"模式一致。
    """
    text = (correction_index or "").strip()
    if not text:
        return ""
    return "## 校正索引（上月镜子涉及记录的清单，用于校准叙事与实际记录的一致性）\n\n" + \
        _truncate(text, CORRECTION_INDEX_MAX_CHARS)


def _fmt_stats_facts(todos, total_records: int) -> str:
    """④ 待办/统计实况：唯一真源块。

    B 侧复用 todos 通道塞"实况直查"（未完成/已完成待办），统计计数（本月记录数等）
    走 total_records/time_range 既有字段。全空 → 空串（不留孤儿节内容）。
    """
    todos_text = _fmt_todos(todos)
    parts = []
    if todos_text:
        parts.append(todos_text)
    if total_records > 0:
        parts.append(f"本月记录数：{total_records}")
    return "\n".join(parts)


def _fmt_mood_stats(stats) -> str:
    if not stats:
        return "（本月无情绪数据）"
    return "\n".join(f"- {s.mood}: {s.count} 次 ({s.percentage:.1f}%)" for s in stats)


def _fmt_keywords(keywords) -> str:
    if not keywords:
        return "（无关键词数据）"
    return "、".join(f"{k.keyword}({k.count})" for k in keywords[:20])


def _fmt_active_time(active) -> str:
    parts = []
    if active.hour_distribution:
        parts.append("小时分布: " + ", ".join(f"{h}点({c})" for h, c in sorted(active.hour_distribution.items())))
    if active.weekday_distribution:
        parts.append("星期分布: " + ", ".join(f"{d}({c})" for d, c in active.weekday_distribution.items()))
    if active.peak_hour:
        parts.append(f"峰值时段: {active.peak_hour}")
    parts.append(f"最近7天记录数: {active.records_last_7_days}")
    return "\n".join(parts) if parts else "（无活跃时段数据）"


def _fmt_recent_chats(chats) -> str:
    if not chats:
        return "（无对话记录）"
    lines = []
    for c in chats:
        lines.append(f"- [{c.created_at}] {c.role}: {c.content[:120]}")
    return "\n".join(lines)


class MirrorProfileServicer(pb2_grpc.MirrorProfileServicer):
    """画像服务：五维统计 + 累计镜子四块输入 → 六维分析（累计语义）"""

    def GenerateProfile(self, request, context):
        llm_config = request.llm_config

        lookback = request.mirror_lookback if request.HasField("mirror_lookback") else None
        print(f"[GenerateProfile] records: {request.total_records}, range: {request.time_range}, "
              f"todos={len(request.todos)}, learnings={len(request.learnings)}, "
              f"chats={len(request.recent_chats)}, prev_mirror={'有' if request.prev_mirror else '无(genesis)'}, "
              f"correction_index={'有' if request.correction_index else '无'}, "
              f"lookback={lookback if lookback is not None else '未传'}")
        print(f"[GenerateProfile] LLM: provider={llm_config.provider}, model={llm_config.model}, "
              f"protocol={llm_config.protocol}")

        try:
            llm = create_llm(
                provider=llm_config.provider,
                api_key=llm_config.api_key,
                base_url=llm_config.base_url,
                model=llm_config.model,
                protocol=llm_config.protocol,
            )

            prompt = loader.render(
                "profile",
                prev_mirror=_fmt_prev_mirror(request.prev_mirror),
                correction_index=_fmt_correction_index(request.correction_index),
                stats_facts=_fmt_stats_facts(request.todos, request.total_records),
                records=_fmt_learnings(request.learnings),
                mood_stats=_fmt_mood_stats(request.mood_stats),
                keywords=_fmt_keywords(request.keywords),
                active_time=_fmt_active_time(request.active_time),
                time_range=request.time_range or "（未指定）",
                recent_chats=_fmt_recent_chats(request.recent_chats),
                glossary=format_glossary(request.glossary),
            )

            response_text = llm.chat([{"role": "user", "content": prompt}])
            print(f"[GenerateProfile] LLM 响应: {response_text[:200]}")

            result = parse_json(response_text, ctx="画像")
            return pb2.GenerateProfileResponse(
                todo_analysis=result.get("todo_analysis", ""),
                learning_analysis=result.get("learning_analysis", ""),
                mood_analysis=result.get("mood_analysis", ""),
                user_tags=_sanitize_tags(result.get("user_tags", [])),
                rhythm_analysis=result.get("rhythm_analysis", ""),
                overall_summary=result.get("overall_summary", ""),
            )

        except Exception as e:
            print(f"[GenerateProfile] 错误: {e}")
            abort_with_mapped(context, e)


def _sanitize_tags(tags) -> list[str]:
    """user_tags 清洗：非空字符串、去重、最多 5 个"""
    out = []
    for t in tags:
        if isinstance(t, str):
            t = t.strip()
            if t and t not in out:
                out.append(t)
    return out[:5]
