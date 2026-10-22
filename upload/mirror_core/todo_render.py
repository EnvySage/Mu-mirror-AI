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

# 清单段落固定文案（只在清单非空时出现——空清单整节消失，不留孤儿节头）
_SECTION_HEADER = "## 用户未完成的待办清单"
_LIST_INTRO = "以下是用户此前日记中登记的未完成待办："

# 判别规则（todo-registry-design.md §3.2；两模板共用同一份，改口径只改这里）
# 设计要点：给可判定的判据（进度是否改变）而非形容词；few-shot 改用真实失败案例
# （"练琴" → 清单"准备学习吉他"应填；"练琴" 与 清单"计划补文献综述" 不填）校准口径。
# 双向兜底：有相关候选时必须填（不因措辞不同而漏判）；无相关候选时宁可不填、
# 不硬扯无关 id——两句话同时写明，避免偏向"一味填"或"一味放弃"任一边。
_RULES = """请逐条比对清单与当前片段，判断当前片段是否在推进其中某一项所指的那件事：

- **算同一件事（必须填）**：当前片段是清单那件事的**具体步骤、组成部分或阶段推进**。例如清单是"准备学习吉他"，当前写"今天练了琴"——练琴就是学吉他的具体推进（同一目标的下一步动作），**必须填**，suggested_status 给 IN_PROGRESS。
- **不算同一件事（不要填）**：两件事的目标不同、互不隶属；主题完全无关时更不要填。例如清单是"计划补文献综述"、当前写的是"练琴"——一个写论文一个弹乐器，主题无关、目标不同，不要填。
- **判据（拿不准时用它）**：把当前片段做的事去掉，清单那件事的进度会因此改变吗？会 → 是同一件事，要填；不会 → 不要填。
- **不要因为措辞不同就放弃判定**——同一件事换个说法很常见（"练琴"vs"练吉他"是同一条线），只按"目标是否同一"判断，不按字面是否雷同判断。
- **多个候选时选语义最接近的那一个**：若清单里同时有几项都沾边，只填与当前内容主题最贴近的一项，不要贪多。
- **没有实质关联时不要硬扯**：如果所有候选与当前内容的主题都没有实质关联，就**宁可不填**——refers_to_todo 输出 {"todo_id": 0, "suggested_status": "STATUS_UNKNOWN"}，也不要勉强关联一个不相关的 id。（这与上一条"相关候选存在时必须填"并不矛盾：有相关就填、无相关才不填，不要偏向任一边。）
- 只对清单里**真实存在**的 todo_id 填，禁止编造清单外的 id；每个 item 最多对应一个 todo_id。
- **refers_to_todo 是必填字段，无论是否有引用都必须输出它**：无引用时输出 {"todo_id": 0, "suggested_status": "STATUS_UNKNOWN"}，并给较低的 "todo_confidence"；有引用时给 "todo_confidence" 打分（0-1，判断把握），suggested_status 从 NOT_STARTED / IN_PROGRESS / COMPLETED 三选一。"""


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
    """TodoHint 列表 → 完整 prompt 段落（含节头与判别规则）。

    空列表返回 ""（调用方拼进模板时整块消失，不留孤儿节头——glossary_render 同模式）。
    清单非空时返回「节头 + 清单行 + 判别规则」三段，规则后带空行，保证与后续
    模板小节之间有换行分隔（旧版把规则写在模板里、清单行紧贴规则句，渲染产物以
    "\\n" 结尾会把清单行和规则句粘成一行）。

    规则文本只此一份（classify / classify-single 两模板共用 {open_todos} 占位符），
    避免两处各维护一遍导致口径漂移。
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
    return "\n".join([_SECTION_HEADER, "", _LIST_INTRO, *lines, "", _RULES, ""])
