"""近期语境 prompt 注入渲染（recent-context）

ClassifyRequest.recent_context（repeated RecentHint，B 侧查近 7 天 chunks.metadata 注入）。
本模块把近期记录摘要渲染成 prompt 段落，供 classify / classify-single 两模板的
{recent_context} 占位符接线，让 LLM 正确消解当前内容里的指代与省略。

渲染模式照 todo_render / glossary_render：**空列表返回空串，整块消失不留孤儿节头**——
节头（"## 用户近期记录（最近 7 天）"）由本模块产出、从清单行开始，
B 未升级（不传 recent_context）时 prompt 与旧版完全一致（零回归）。

Java 侧负责查近 7 天记录摘要（date/title/keywords）；Python 侧只做渲染防线，
无状态、不查库（无状态铁律 #2）。
"""

from config import CONFIG

# 渲染防线（config recent_context 段，照 todo_hint 的 max_* 模式）
_MAX_ITEMS = int(CONFIG["recent_context"]["max_items"])            # 清单条数上限（B 侧同值 20，双保险）
_MAX_TITLE_CHARS = int(CONFIG["recent_context"]["max_title_chars"])  # 单条 title 截断长度（防 prompt 膨胀）
_MAX_KEYWORDS = int(CONFIG["recent_context"]["max_keywords"])       # 单条 keywords 渲染上限

# 清单段落固定文案（只在清单非空时出现——空清单整节消失，不留孤儿节头）
_SECTION_HEADER = "## 用户近期记录（最近 7 天）"
_LIST_INTRO = "以下是用户最近记录的主题清单（新→旧），用于理解当前内容中的指代与省略："

# 防幻觉提示（与模板开头的"忠实原文规则"呼应：清单只用于消解指代，不得当事实写出来）
_NOTE = ("注意：清单仅供理解语境；当前内容中的指代可以结合清单消解，"
         "但不得把清单里没有的具体信息（乐器、人名、地点、数量等）写进标题或摘要。")


def _fmt_date(date: str) -> str:
    """"2026-09-11" → "9-11"（月份去前导零省 token；日保留原两位，如 09 仍为 09）。"""
    text = (date or "").strip()[:10]
    parts = text.split("-")
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        return f"{int(parts[1])}-{parts[2]}"
    return text or "日期未知"


def _fmt_hint(hint) -> str:
    """单条 RecentHint → prompt 行：`- 9-11 学吉他：吉他、练习`"""
    title = (hint.title or "").strip() or "（无标题）"
    if len(title) > _MAX_TITLE_CHARS:
        title = title[:_MAX_TITLE_CHARS] + "…"
    keywords = [k.strip() for k in (hint.keywords or []) if k and k.strip()][:_MAX_KEYWORDS]
    body = f"- {_fmt_date(hint.date)} {title}"
    if keywords:
        body += f"：{'、'.join(keywords)}"
    return body


def format_recent_context(hints) -> str:
    """RecentHint 列表 → 完整 prompt 段落（含节头、清单行与防幻觉提示）。

    空列表返回 ""（调用方拼进模板时整块消失，不留孤儿节头——todo_render 同模式）。
    清单非空时返回「节头 + 清单行 + 提示」三段，末尾带换行，与后续模板小节有分隔。
    """
    if not hints:
        return ""
    lines = [_fmt_hint(h) for h in list(hints)[:_MAX_ITEMS]]
    if not lines:
        return ""
    return "\n".join([_SECTION_HEADER, "", _LIST_INTRO, *lines, "", _NOTE, ""])
