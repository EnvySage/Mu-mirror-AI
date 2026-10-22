"""glossary prompt 注入渲染（lexicon-design.md 第 4 节）

四个注入点：ClassifyRequest / ExtractIntentRequest / ChatRequest / GenerateProfileRequest
的 repeated GlossaryTerm glossary 字段。本模块把词条列表渲染成 prompt 段落。

设计稿第 4 节固定软约束话术（原样使用，不改写）：
  "以下用户个人词汇表仅供参考，解释可能过时；与近期记录矛盾时，以近期记录为准。"
每词条附 confirmed_at（如"论文（6月确认）：..."）。

Java 侧（GlossaryService）负责查 confirmed 词条 + query_hit_count 排序 + top 30 截断 +
60s 缓存；Python 侧只做渲染，无状态、不查库（核心哲学 #3）。
"""

from generated import common_pb2 as common

# 固定软约束话术（设计稿第 4 节，禁止 LLM 生成或改写）
GLOSSARY_SOFT_CONSTRAINT = "以下用户个人词汇表仅供参考，解释可能过时；与近期记录矛盾时，以近期记录为准。"


def _format_confirmed_at(confirmed_at: str) -> str:
    """ISO 日期 → "x月确认" 标注。解析不了就原样带上（不丢信息）。"""
    text = (confirmed_at or "").strip()
    if not text:
        return ""
    parts = text.split("-")
    # "2026-06-01" → "6月确认"；带时间（ISO datetime）截到日期部分再取月
    if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
        return f"{int(parts[1])}月确认"
    return f"{text}确认"


def _format_marks(aliases, confirmed_at: str) -> str:
    """括号标注拼接：又称 + 确认时间，如 "（又称：毕设、那个设计）（6月确认）"。"""
    out = []
    items = [a.strip() for a in (aliases or []) if a and a.strip()]
    if items:
        out.append(f"又称：{'、'.join(items)}")
    when = _format_confirmed_at(confirmed_at)
    if when:
        out.append(when)
    return f"（{'）（'.join(out)}）" if out else ""


def format_glossary(terms) -> str:
    """GlossaryTerm 列表 → prompt 段落。

    空列表返回 ""（调用方拼进 prompt 时不留空节头）。
    每行格式：- 词条（又称：…）（N月确认）：解释
    """
    if not terms:
        return ""
    lines = [GLOSSARY_SOFT_CONSTRAINT, ""]
    for t in terms:
        term = (t.term or "").strip()
        if not term:
            continue
        desc = (t.description or "").strip()
        marks = _format_marks(t.aliases, t.confirmed_at)
        body = f"{term}{marks}：{desc}" if desc else f"{term}{marks}"
        lines.append(f"- {body}")
    return "\n".join(lines)


def glossary_lines(terms) -> str:
    """format_glossary 的别名，语义化入口：渲染词条段落（不含节标题）。

    各 prompt 模板用 {glossary} 占位符接线；模板中的节标题自 带，
    无词条时整段为空串，不产生孤儿标题。
    """
    return format_glossary(terms)


def has_terms(terms) -> bool:
    """是否有非空词条（Java 未加字段/未传时为 False，渲染层据此短路）。"""
    return bool(terms) and any((t.term or "").strip() for t in terms)
