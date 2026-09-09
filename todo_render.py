"""待办清单 prompt 注入渲染（todo-registry-design.md §3.2 判别期）

ClassifyRequest.open_todos（repeated TodoHint，B 侧查 todo_registry 注入）。
本模块把待办列表渲染成 prompt 段落，供 classify / classify-single 两模板的
{open_todos} 占位符接线。

渲染模式照抄 glossary_render：**空列表返回空串，整块消失不留孤儿节头**——
节头（"## 用户未完成的待办清单"）写在模板里、渲染产物从清单行开始，
B 未升级（不传 open_todos）时 prompt 与旧版完全一致（零回归）。

Java 侧（TodoRegistryService）负责查未完成 registry（current_status != 'completed'、
最近优先、上限 20 条）+ 原始片段摘要；Python 侧只做渲染与 TodoRef 解析，
无状态、不查库（无状态铁律 #2）。
"""

from config import CONFIG

# 渲染防线（config todo_hint 段，照 extract_terms 的 max_* 模式）
_MAX_HINTS = int(CONFIG["todo_hint"]["max_hints"])          # 清单条数上限（B 侧同值 20，双保险）
_MAX_EXCERPT_CHARS = int(CONFIG["todo_hint"]["max_excerpt_chars"])  # 单条摘要截断长度（防 prompt 膨胀）

# 状态中文名（清单里 current_status 用小写枚举串；渲染成中文便于 LLM 理解）
_STATUS_CN = {"not_started": "未开始", "in_progress": "进行中", "completed": "已完成"}


def _fmt_status(status: str) -> str:
    """not_started → 未开始；未知值原样展示（不丢信息，B 侧只传未完成两态）。"""
    text = (status or "").strip().lower()
    return _STATUS_CN.get(text, text or "状态未知")


def _fmt_hint(hint) -> str:
    """单条 TodoHint → prompt 行：`- #7 补作业（登记于 9-01，未开始）：8-20 的日记说明天要补作业…`"""
    title = (hint.title or "").strip() or "（无标题）"
    date = (hint.created_at or "").strip()[:10] or "日期未知"
    excerpt = (hint.source_excerpt or "").strip()
    if len(excerpt) > _MAX_EXCERPT_CHARS:
        excerpt = excerpt[:_MAX_EXCERPT_CHARS] + "…"
    body = f"- #{hint.todo_id} {title}（登记于 {date}，{_fmt_status(hint.current_status)}）"
    if excerpt:
        body += f"：{excerpt}"
    return body


def format_open_todos(hints) -> str:
    """TodoHint 列表 → prompt 清单段落（不含节头）。

    空列表返回 ""（调用方拼进模板时整块消失，不留孤儿节头——glossary_render 同模式）。
    超过 max_hints 截断保留前 N 条（B 侧"最近优先 ≤20"之外的最后防线）。
    每行格式：`- #id 标题（登记于 日期，状态）：原始片段摘要`
    """
    if not hints:
        return ""
    lines = []
    for h in list(hints)[:_MAX_HINTS]:
        if int(getattr(h, "todo_id", 0) or 0) <= 0:
            continue  # 脏条目（无 id）跳过：LLM 拿到也填不出合法 TodoRef
        lines.append(_fmt_hint(h))
    if not lines:
        return ""
    return "以下是用户此前日记中登记的未完成待办：\n" + "\n".join(lines) + "\n"
