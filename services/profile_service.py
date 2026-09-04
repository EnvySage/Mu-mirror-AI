"""MirrorProfile 服务实现 — 画像生成（协作清单 #6）

输入：五维统计（todos/learnings/mood_stats/keywords/active_time）+ recent_chats + 统计元数据 + llm_config
输出：六维分析（todo/learning/mood/rhythm）+ user_tags + overall_summary
无状态：配置随请求携带，用完即弃。
"""

import json

from generated import mirror_profile_pb2 as pb2
from generated import mirror_profile_pb2_grpc as pb2_grpc

from errors import ContentInvalidError, abort_with_mapped
from llm.factory import create_llm
from prompts_loader import loader

VALID_TAGS_MIN = 1


def _fmt_todos(todos) -> str:
    if not todos:
        return "（无未完成待办）"
    lines = []
    for t in todos:
        lines.append(f"- [{t.created_at}] {t.title}：{t.summary}")
    return "\n".join(lines)


def _fmt_learnings(learnings) -> str:
    if not learnings:
        return "（无学习记录）"
    lines = []
    for l in learnings:
        kw = "/".join(l.keywords)
        suffix = f"（关键词：{kw}）" if kw else ""
        lines.append(f"- [{l.created_at}] {l.title}：{l.summary}{suffix}")
    return "\n".join(lines)


def _fmt_mood_stats(stats) -> str:
    if not stats:
        return "（无情绪数据）"
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
    """画像服务：五维统计 + 最近对话 → 六维分析"""

    def GenerateProfile(self, request, context):
        llm_config = request.llm_config

        print(f"[GenerateProfile] records: {request.total_records}, range: {request.time_range}, "
              f"todos={len(request.todos)}, learnings={len(request.learnings)}, "
              f"chats={len(request.recent_chats)}")
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
                todos=_fmt_todos(request.todos),
                learnings=_fmt_learnings(request.learnings),
                mood_stats=_fmt_mood_stats(request.mood_stats),
                keywords=_fmt_keywords(request.keywords),
                active_time=_fmt_active_time(request.active_time),
                total_records=request.total_records,
                time_range=request.time_range or "（未指定）",
                recent_chats=_fmt_recent_chats(request.recent_chats),
            )

            response_text = llm.chat([{"role": "user", "content": prompt}])
            print(f"[GenerateProfile] LLM 响应: {response_text[:200]}")

            result = _parse_json(response_text)
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


def _parse_json(text: str) -> dict:
    """从 LLM 响应中提取 JSON"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start)
        return json.loads(text[start:end].strip())

    start = text.find("{")
    end = text.rfind("}") + 1
    if start != -1 and end > start:
        return json.loads(text[start:end])

    raise ContentInvalidError(f"LLM 响应无法解析为画像 JSON: {text[:200]}")
