"""MirrorChat 服务实现 — 意图提取 + 对话生成（协作清单 #5）

ExtractIntent（兼容回滚路径）：结构化/语义/组合检索路由 + 过滤条件
Chat：服务端流式 stream ChatChunk{content, done, sources, thinking}
（thinking=4 为 LLM 思考过程增量块，proto3 optional，模型不支持时永不为真）
无状态：配置随请求携带，用完即弃。
PlanTools 的工具执行结果（ChatRequest.tool_results，field 6）由 Chat 渲染成上下文块
（_format_tool_results，截断到 config plan_tools.max_tool_chars）；空列表零影响
（prompt 不留孤儿块，同 glossary 模式）。
"""

import json
from datetime import datetime
from types import SimpleNamespace

from generated import mirror_chat_pb2 as pb2
from generated import mirror_chat_pb2_grpc as pb2_grpc

from config import CONFIG
from errors import abort_with_mapped
from glossary_render import format_glossary
from llm.factory import create_llm
from llm_json import parse_json
from prompts_loader import loader

# 兼容回滚路径的 query_type（三选一，英文小写），非法值兜底 hybrid
QUERY_TYPES = {"structured", "semantic", "hybrid"}
DEFAULT_QUERY_TYPE = "hybrid"

CONTENT_TYPES = {"todo", "thought", "learning", "plan", "note", "work", "social", "health"}
MOODS = {"happy", "excited", "satisfied", "grateful", "expecting", "calm", "bored",
         "confused", "anxious", "sad", "angry", "exhausted", "stressed"}

ROLE_ASSISTANT = "assistant"


def assistant_context_text(context) -> str:
    """Render only current server-authoritative context; old chat claims never override it."""
    if context is None:
        return "当前日期/时区未提供；操作卡状态以服务端本段为准。"
    lines = [f"当前日期：{context.current_date or '未知'}；时区：{context.timezone or '未知'}",
             f"角色口吻：{context.role_tone or '自然、直接'}",
             "操作卡状态为服务端当前真实状态，优先级高于历史聊天中的描述；取消的卡不能重建或称为已执行。"]
    if context.action_status_summary:
        lines.append("当前会话操作卡：\n" + context.action_status_summary)
    else:
        lines.append("当前会话没有已准备的操作卡。")
    for memory in context.memories[:5]:
        date_label = f"（来源：{memory.source_date or '日期未知'}）"
        lines.append(f"授权记忆{date_label} [{memory.kind or 'fact'}]：{memory.content}")
    draw = context.divination if context.HasField("divination") else None
    if draw and (draw.draw_id or draw.drawn_at or draw.question or draw.cards):
        cards = "；".join(f"{card.card_name}（{card.orientation}）：{card.interpretation}" for card in draw.cards)
        lines.append(f"占卜上下文（仅供娱乐，不是事实或行动依据）：时间 {draw.drawn_at or '未知'}；问题 {draw.question or '未提供'}；{cards}")
    return "\n".join(lines)

# 工具结果渲染截断（config plan_tools.max_tool_chars，防 prompt 膨胀）
_MAX_TOOL_CHARS = int(CONFIG["plan_tools"]["max_tool_chars"])


def _format_tool_results(tool_results) -> str:
    """ToolResult 列表 → prompt 上下文块。

    文件类结果（find_item/recall_item 的 items/item）渲染为独立 [F编号] 行——
    LLM 引用文件时写 [F1]，B 侧 extractVaultRefs 只认 [F\\d+] 出文件卡（fix-batch B3）。
    search_records 的可见记录按 chunks 之后的连续编号渲染为 [n]，并建立真实 sources；
    统计等其他工具仍渲染为「工具 {tool} 返回：…」且不编号。
    禁止使用长得像 [n]/[F1] 的工具名方括号标签，避免模型把它抄成假引用。
    失败结果只渲染失败说明，不把失败 payload 喂给 LLM（防止拿错误输出当事实）。
    空列表返回 ""（prompt 不留孤儿块，同 glossary 模式）。
    """
    return _format_tool_results_with_sources(tool_results)[0]


def _format_tool_results_with_sources(tool_results, citation_offset: int = 0) -> tuple[str, list[dict]]:
    """渲染工具结果，并为实际展示的 search_records 行分配与 chunks 连续的引用编号。"""
    if not tool_results:
        return "", []
    lines = []
    record_sources: list[dict] = []
    f_no = 0  # 文件编号独立计数（跨工具结果连续，[F1][F2]…）
    for r in tool_results:
        tool = (r.tool or "").strip() or "unknown_tool"
        if not r.success:
            lines.append(f"工具 {tool} 返回：（工具执行失败，以下回答不要依赖该工具的数据）")
            continue
        payload = _parse_payload(r.payload_json)
        items = _extract_file_items(tool, payload)
        if items:
            for it in items:
                f_no += 1
                lines.append(f"[F{f_no}] {_describe_file_item(it)}")
            # recall_item 的正文摘录（payload.quotes / quote）也要给回答模型看——原先只渲染文件元信息，
            # 2026-09-21 联调实测 recall_item 成功读到正文，回答仍说"只看到上传记录，读不到里面的字"
            body = "\n".join(x for x in ((r.summary or "").strip(), _render_file_excerpts(payload, f_no)) if x)
            if body:
                if len(body) > _MAX_TOOL_CHARS:
                    body = body[:_MAX_TOOL_CHARS] + "…"
                lines.append(f"工具 {tool} 返回：{body}")
            continue
        if tool == "search_records" and isinstance(payload.get("records"), list):
            summary = (r.summary or "").strip()
            body_lines = [summary] if summary else []
            visible_len = len("\n".join(body_lines))
            records = sorted((it for it in payload["records"] if isinstance(it, dict)),
                             key=lambda it: str(it.get("date") or ""))
            truncated = False
            for rec in records:
                record_id = _positive_int(rec.get("record_id"))
                n = citation_offset + len(record_sources) + 1 if record_id is not None else None
                date = str(rec.get("date") or "")[:16].replace("T", " ")
                ctype = str(rec.get("content_type") or "")
                title = str(rec.get("title") or "")
                quote = str(rec.get("quote") or "")
                head = f"{date}（{ctype}）" if ctype else date
                detail = f"{head} {title}：{quote}".replace(" ：", "：").strip()
                line = (f"[{n}]{_relevance_note(rec.get('score'))} {detail}"
                        if n is not None else detail)
                candidate_len = visible_len + (1 if body_lines else 0) + len(line)
                if candidate_len > _MAX_TOOL_CHARS:
                    truncated = True
                    break
                body_lines.append(line)
                visible_len = candidate_len
                if n is not None:
                    record_sources.append({
                        "n": n,
                        "record_id": record_id,
                        "quote": title or quote[:100],
                        "date": str(rec.get("date") or ""),
                    })
            if truncated and visible_len < _MAX_TOOL_CHARS:
                body_lines.append("…")
            body = "\n".join(body_lines) or "（无返回数据）"
            lines.append(f"工具 {tool} 返回：{body}")
            continue
        # summary 当标题、payload 当明细。原先 summary 非空就不渲染 payload——而 Java 侧每个工具
        # 都填 summary，于是回答模型只看得到 "search_records:12条" 这几个字、看不到任何一条内容
        # （违背 common.proto ToolResult.payload_json「完整结果 JSON（进 prompt 上下文块）」）。
        # 2026-09-21 联调实测：检索空、循环查到 12 条焦虑记录，回答却说"没有能拿出来聊的素材"。
        summary = (r.summary or "").strip()
        detail = _render_tool_payload(payload, r.payload_json)
        body = "\n".join(x for x in (summary, detail) if x)
        if not body:
            body = "（无返回数据）"
        if len(body) > _MAX_TOOL_CHARS:
            body = body[:_MAX_TOOL_CHARS] + "…"
        lines.append(f"工具 {tool} 返回：{body}")
    return "\n".join(lines), record_sources


def _positive_int(value):
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def _render_file_excerpts(payload: dict, f_no: int) -> str:
    """recall_item 读到的正文摘录 → 挂在对应 [F编号] 下的摘录行。

    只认 payload 顶层的 quotes（[{text: ...}]）/ quote（recall_item 的形态）；
    find_item 的 payload 没有这两个键，保持只出文件卡、不出正文（它本来就只负责找文件）。
    """
    texts = []
    for q in payload.get("quotes") or []:
        t = q.get("text") if isinstance(q, dict) else q
        t = str(t or "").strip()
        if t:
            texts.append(t)
    if not texts:
        t = str(payload.get("quote") or "").strip()
        if t:
            texts.append(t)
    out = []
    if texts:
        out.append(f"[F{f_no}] 正文摘录（引用时写 [F{f_no}]）：")
        out.extend(f"- {t}" for t in texts)
    note = str(payload.get("note") or "").strip()
    if note:
        out.append(f"（{note}）")
    return "\n".join(out)


def _render_tool_payload(payload: dict, raw: str) -> str:
    """非文件类工具的 payload → 给回答模型看的明细文本。

    - 带 records 列表 → 逐行 "- 日期（类型）标题：摘录"，比 JSON 省字且好读
    - 其余 dict（get_stats 等）→ 紧凑 JSON（ensure_ascii=False，中文不转义）
    - 不是 dict 的合法 JSON（如列表）→ 原文
    """
    import json
    records = payload.get("records") if isinstance(payload, dict) else None
    if isinstance(records, list) and records:
        rows = []
        # 按时间正序、保留到分钟：原先只留日期且沿用 SQL 的倒序，问"十二号做了什么"时模型看不出
        # 一天里的先后，实测把"先想直接练春日影→太难→改练小星星"讲反了
        recs = sorted((r for r in records if isinstance(r, dict)), key=lambda r: str(r.get("date") or ""))
        for rec in recs:
            date = str(rec.get("date") or "")[:16].replace("T", " ")
            ctype = rec.get("content_type") or ""
            title = rec.get("title") or ""
            quote = rec.get("quote") or ""
            head = f"{date}（{ctype}）" if ctype else date
            rows.append(f"- {head} {title}：{quote}".replace(" ：", "：").strip())
        return "\n".join(rows)
    if isinstance(payload, dict) and payload:
        return json.dumps(payload, ensure_ascii=False)
    raw = (raw or "").strip()
    return raw if raw and not raw.startswith("{") else ""


# 检索没命中但工具查到了数据：不能再写"（没有找到相关记录）"——那句话会让回答模型
# 以为真的没数据，和下方「工具查询结果」打架（联调实测它会信前者）
_CONTEXT_EMPTY_WITH_TOOLS = "（本轮没有预检索日记片段；下方工具结果是本轮已取得的依据）"
_CONTEXT_NOT_SEARCHED = "（本轮尚未检索日记记录；这不表示用户没有相关记录。普通对话可直接回应，涉及历史事实时不要猜）"
_CONTEXT_SEARCH_EMPTY = "（本轮已成功检索日记记录，但没有匹配片段；这只表示本次查询未命中，不表示用户从未记录）"
_CONTEXT_SEARCH_FAILED = "（本轮日记检索失败或部分失败；不能据此判断用户是否有相关记录。若有成功结果，只能依据实际返回的内容）"


def _context_text(chunks, tool_results) -> str:
    """区分未检索、成功零命中、检索失败和工具返回了其他依据。"""
    if chunks:
        return _format_context(chunks)
    if isinstance(tool_results, str):  # 旧调用兼容
        return _CONTEXT_EMPTY_WITH_TOOLS if tool_results else _CONTEXT_NOT_SEARCHED
    results = list(tool_results or [])
    searches = [r for r in results if (r.tool or "").strip() == "search_records"]
    if searches:
        failed = any(not r.success for r in searches)
        has_records = any(bool(_parse_payload(r.payload_json).get("records"))
                          for r in searches if r.success)
        if failed:
            return _CONTEXT_SEARCH_FAILED
        if has_records:
            return _CONTEXT_EMPTY_WITH_TOOLS
        return _CONTEXT_SEARCH_EMPTY
    if any(r.success for r in results):
        return _CONTEXT_EMPTY_WITH_TOOLS
    if results:
        return _CONTEXT_SEARCH_FAILED
    return _CONTEXT_NOT_SEARCHED


def _parse_payload(payload_json: str):
    """payload_json → dict（解析失败返回 {}）"""
    if not payload_json or not payload_json.strip():
        return {}
    try:
        import json
        data = json.loads(payload_json)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_file_items(tool: str, payload: dict) -> list:
    """从 find_item/recall_item 的 payload 提取文件卡列表（其余工具返回空）"""
    if tool not in ("find_item", "recall_item") or not payload:
        return []
    if "items" in payload and isinstance(payload["items"], list):
        return [it for it in payload["items"] if isinstance(it, dict)]
    if isinstance(payload.get("item"), dict):
        return [payload["item"]]
    return []


def _describe_file_item(item: dict) -> str:
    """文件卡 → 一行人类可读描述（供 LLM 引用 [Fn]）"""
    name = str(item.get("display_name") or item.get("original_name") or "未命名文件").strip()
    ftype = str(item.get("file_type") or "").strip()
    status = str(item.get("digest_status") or "").strip()
    desc = str(item.get("description") or "").strip()
    created = str(item.get("created_at") or "").strip()[:16]
    parts = [name]
    if ftype:
        parts.append(ftype)
    if status:
        parts.append(f"状态:{status}")
    if created:
        parts.append(created)
    line = " · ".join(parts)
    if desc:
        line += f" —— {desc}"
    vid = item.get("vault_item_id")
    if vid is not None:
        line += f"（vault_item_id={vid}）"
    return line


def _relevance_note(score) -> str:
    """相关度提示——让 LLM 判断证据强弱，避免拿擦边命中的记录硬凑答案。

    score 是 B 侧返回的**余弦距离**（越小越相关）；负数 = 无相似度信息
    （结构化命中 / 画像快照），此时不标注。数值只给模型看（prompt 已声明不要念给用户）。
    """
    try:
        distance = float(score)
    except (TypeError, ValueError):
        return ""
    if distance < 0:
        return ""
    similarity = max(0.0, min(1.0, 1.0 - distance))
    return f"〔相关度 {round(similarity * 100)}%〕"


def _format_context(chunks) -> str:
    """RetrievedChunk 列表 → 带编号与相关度的资料文本（供 [n] 引用）"""
    if not chunks:
        return "（没有找到相关记录）"
    lines = []
    for i, c in enumerate(chunks, start=1):
        moods = "/".join(c.moods)
        suffix = f" 情绪:{moods}" if moods else ""
        lines.append(f"[{i}]{_relevance_note(c.score)} {c.created_at} "
                     f"({c.content_type}{suffix}): {c.content}")
    return "\n".join(lines)


class _FakeCiteFilter:
    """流式剔除假引用标记：[工具名]、[工具名·xxx]、[工具结果…]。

    2026-09-21 联调实测：模型会把工具数据"引用"成 [get_stats]、[search_records]、
    [工具结果·xxx] 写进回答，前端原样显示成一串乱码似的标记。prompt 里点名禁止也拦不住
    （它照着被禁止的例子写），只能在输出端确定性过滤。

    只拦**本轮真实用过的工具名**和「工具结果」前缀——[n]、[F1] 是真引用原样放行；
    其他方括号内容（如口语里的 [笑]）也放行，避免误伤。
    跨块安全：遇到 "[" 先扣住，等到 "]" 再判；超长或遇换行说明不是标记，原样吐出。

    max_cite：本轮可引用资料编号上限（chunks + 实际渲染出来的 search_records 结果数）。
    超出的 [n] 是模型自己编的号——检索 0 条、
    证据全来自没有编号的统计工具时，它会自行编出 [4][6][7]，
    前端渲染成点不开的死引用。None = 不校验编号（兼容旧调用）。
    """

    _MAX_HOLD = 40

    def __init__(self, tool_names, max_cite: int | None = None, max_file_cite: int | None = None):
        self._names = {(n or "").strip() for n in tool_names} - {""}
        self._max_cite = max_cite
        self._max_file_cite = max_file_cite
        self._hold = ""
        self._space = ""  # 扣住的一个空格：后面若是假标记，连同这个空格一起删，不留"弹出来了 ，"

    def feed(self, text: str) -> str:
        out = []
        for ch in text:
            if self._hold:
                self._hold += ch
                if ch == "]":
                    if not self._is_fake(self._hold):
                        out.append(self._hold)
                    self._hold = ""
                elif ch in "\r\n" or len(self._hold) > self._MAX_HOLD:
                    out.append(self._hold)
                    self._hold = ""
            elif ch == "[":
                self._hold = self._space + ch
                self._space = ""
            elif ch == " ":
                out.append(self._space)
                self._space = ch
            else:
                out.append(self._space + ch)
                self._space = ""
        return "".join(out)

    def flush(self) -> str:
        held = self._space + self._hold
        self._space = self._hold = ""
        return held

    def _is_fake(self, token: str) -> bool:
        inner = token.strip()[1:-1].strip()
        # [Fn] 超出本轮真实文件数 = 模型编的文件引用（实测没查任何文件时写出 [F1]）
        if self._max_file_cite is not None and inner[:1] in "Ff" and inner[1:].isdigit():
            n = int(inner[1:])
            return n < 1 or n > self._max_file_cite
        # 以"工具"开头的一律视为假标记：实测模型会从 prompt 章节名自造变体
        # （[工具结果]、[工具查询结果]…），逐个列举追不上
        if inner.startswith("工具"):
            return True
        if self._max_cite is not None and inner.isdigit():
            n = int(inner)
            return n < 1 or n > self._max_cite
        return inner.split("·")[0].strip() in self._names


def _format_history(history) -> str:
    if not history:
        return "（无历史对话）"
    return "\n".join(f"{m.role}: {m.content}" for m in history)



from dataclasses import dataclass, field
from typing import Callable
from agent_core.evidence import CitationIndex, assign_citations, render_evidence, render_observations

@dataclass(frozen=True)
class AnswerEvent:
    """One legacy-compatible answer stream event."""

    kind: str
    content: str = ""
    sources: list = field(default_factory=list)
    vault_refs: list = field(default_factory=list)


def build_answer_bundle(
    question, history, chunks, glossary, tool_results, *,
    prompt_name="chat", assistant_context=None,
):
    """Build the current legacy prompt and return its real citation source map."""
    tool_results_text, tool_sources = _format_tool_results_with_sources(
        tool_results, citation_offset=len(chunks)
    )
    prompt = loader.render(
        prompt_name,
        question=question,
        history=_format_history(history),
        context=_context_text(chunks, tool_results),
        glossary=format_glossary(glossary),
        tool_results=tool_results_text,
        **({"assistant_context": assistant_context} if assistant_context is not None else {}),
    )
    messages = [{"role": "system", "content": prompt}]
    messages += [{"role": m.role, "content": m.content} for m in history]
    messages.append({"role": "user", "content": question})
    return messages, tool_sources


def build_answer_messages(
    question, history, chunks, glossary, tool_results, *,
    prompt_name="chat", assistant_context=None,
) -> list[dict]:
    """Build the legacy chat messages; kept as a small compatibility wrapper."""
    messages, _ = build_answer_bundle(
        question, history, chunks, glossary, tool_results,
        prompt_name=prompt_name, assistant_context=assistant_context,
    )
    return messages


def stream_answer(
    messages, chunks, tool_results, llm, *,
    tool_sources=(), cancel: Callable[[], bool] | None = None,
):
    """Yield filtered thinking/delta/final events using the legacy wire semantics."""
    buffer: list[str] = []
    n_files = sum(
        len(_extract_file_items(r.tool, _parse_payload(r.payload_json)))
        for r in tool_results if r.success
    )
    fake_cites = _FakeCiteFilter(
        (r.tool for r in tool_results),
        max_cite=len(chunks) + len(tool_sources),
        max_file_cite=n_files,
    )
    for kind, text in llm.chat_stream(messages):
        if cancel is not None and cancel():
            return
        if kind == "thinking":
            yield AnswerEvent(kind="thinking", content=text)
            continue
        text = fake_cites.feed(text)
        if not text:
            continue
        buffer.append(text)
        yield AnswerEvent(kind="delta", content=text)
    tail = fake_cites.flush()
    if tail:
        buffer.append(tail)
        yield AnswerEvent(kind="delta", content=tail)

    sources = _extract_sources("".join(buffer), chunks, tool_sources)
    yield AnswerEvent(kind="done", sources=sources)

def _extract_sources(answer: str, chunks, tool_sources=()) -> list[pb2.Source]:
    """从回答中解析 [n] 引用标记 → Source{record_id, quote, date, n}

    n 是正文里的原始引用编号（1-based）：本函数只返回"被引用到的"子集，
    编号会跳号（如只引用了 [1] 和 [5] → 列表长度 2）——消费方必须按 n 定位，
    不能用数组下标 sources[n-1]。
    """
    sources: dict[int, pb2.Source] = {}
    for token in _find_markers(answer):
        idx = token - 1  # 回答里 [1] 对应 chunks[0]
        if idx in sources:
            continue
        if 0 <= idx < len(chunks):
            c = chunks[idx]
            sources[idx] = pb2.Source(
                record_id=c.record_id,
                quote=c.title or c.content[:100],  # 对齐 Java 兜底路径：title 优先（ChatServiceImpl.deriveSources）
                date=c.created_at,
                n=idx + 1,                        # 引用编号回传（B 侧/前端据此对齐）
            )
        else:
            tool_idx = idx - len(chunks)
            if 0 <= tool_idx < len(tool_sources):
                source = tool_sources[tool_idx]
                sources[idx] = pb2.Source(
                    record_id=source["record_id"],
                    quote=source["quote"],
                    date=source["date"],
                    n=source["n"],
                )
    return list(sources.values())


def _find_markers(answer: str) -> list[int]:
    """提取独立方括号引用标记 [n]。

    排除两类非引用形态（第九轮修复）：
    - markdown 链接：[1](http://...) / [text](url) —— 方括号后紧跟圆括号
    - 列表编号："1. xxx" / "1、xxx" 不匹配；但 "\\[3\\] 学习了..."（转义方括号
      后跟编号，部分模型输出）同样不是引用
    只保留 [数字] 且后面不是 "(" 的独立标记。
    """
    out = []
    for part in answer.split("[")[1:]:
        head = part.split("]", 1)[0].strip()
        if not head.isdigit():
            continue
        rest = part.split("]", 1)[1] if "]" in part else ""
        if rest.startswith("("):  # markdown 链接 [n](url)
            continue
        out.append(int(head))
    return out

def build_agent_plan_messages(
    question,
    history,
    glossary,
    tools,
    observations,
    *,
    retrieval_summary="",
    evidence=(),
    step=1,
    max_steps=4,
    today="",
    assistant_context="",
):
    """Build a planner prompt with retrieval facts, file IDs, moods, and prior observations."""
    import json

    rendered_observations = render_observations(observations)
    file_ids = []
    moods = {}
    for observation in observations:
        if not observation.success or not isinstance(observation.data, dict):
            continue
        for item in observation.data.get("items", ()) or ():
            if isinstance(item, dict) and item.get("vault_item_id") is not None:
                file_ids.append(item["vault_item_id"])
        records = observation.data.get("records")
        if isinstance(records, dict) and isinstance(records.get("moods"), dict):
            moods.update(records["moods"])
    if evidence:
        rendered_observations += "\n\n初始检索证据：\n" + render_evidence(evidence)
    prompt = loader.render(
        "agent_plan",
        question=question,
        history=_format_history(history),
        glossary=_format_agent_glossary(glossary),
        tools=json.dumps(tools, ensure_ascii=False),
        retrieval_summary=retrieval_summary,
        file_candidates=json.dumps(list(dict.fromkeys(file_ids)), ensure_ascii=False),
        mood_distribution=json.dumps(moods, ensure_ascii=False),
        observations=rendered_observations,
        step=step,
        max_steps=max_steps,
        today=today,
        assistant_context=assistant_context,
    )
    return [{"role": "system", "content": prompt}, {"role": "user", "content": question}]


def _format_agent_glossary(glossary):
    """Adapt read-view glossary timestamps to the shared prompt renderer."""
    adapted = []
    for term in glossary:
        confirmed_at = getattr(term, "confirmed_at", None)
        if confirmed_at is None:
            confirmed_at = getattr(term, "last_confirmed_at", None)
        if isinstance(confirmed_at, datetime):
            confirmed_at = confirmed_at.isoformat()
        adapted.append(SimpleNamespace(
            term=term.term,
            aliases=term.aliases,
            description=term.description,
            confirmed_at=confirmed_at or "",
        ))
    return format_glossary(adapted)


def build_agent_answer_messages(
    question,
    history,
    evidence,
    observations,
    *,
    citation_index: CitationIndex | None = None,
    glossary=(),
    prompt_name="agent_chat",
    assistant_context="",
):
    """Build graph_v2 answer messages; history appears only once in the message list."""
    index = citation_index or assign_citations(evidence)
    prompt = loader.render(
        prompt_name,
        evidence=render_evidence(evidence, index),
        observations=render_observations(observations),
        glossary=_format_agent_glossary(glossary),
        assistant_context=assistant_context,
    )
    messages = [{"role": "system", "content": prompt}]
    messages.extend({"role": message.role, "content": message.content} for message in history)
    messages.append({"role": "user", "content": question})
    return messages


def stream_agent_answer(
    messages,
    evidence,
    observations,
    llm,
    *,
    citation_index: CitationIndex | None = None,
    cancel: Callable[[], bool] | None = None,
    timeout: float | None = None,
):
    """Stream graph_v2 output with per-chunk fake-citation filtering and frozen sources."""
    evidence = tuple(evidence)
    index = citation_index or assign_citations(evidence)
    tool_names = [observation.tool for observation in observations]
    fake_cites = _FakeCiteFilter(
        tool_names, max_cite=len(index.diary), max_file_cite=len(index.vault_items),
    )
    buffer = []
    stream = llm.chat_stream(messages, timeout=timeout, max_retries=0)
    try:
        for kind, text in stream:
            if cancel is not None and cancel():
                return
            if kind == "thinking":
                yield AnswerEvent(kind="thinking", content=text)
                continue
            filtered = fake_cites.feed(text)
            if filtered:
                buffer.append(filtered)
                yield AnswerEvent(kind="delta", content=filtered)
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    tail = fake_cites.flush()
    if tail:
        buffer.append(tail)
        yield AnswerEvent(kind="delta", content=tail)
    answer = "".join(buffer)
    yield AnswerEvent(
        kind="done",
        sources=index.sources_for(answer),
        vault_refs=index.vault_refs_for(answer),
    )
