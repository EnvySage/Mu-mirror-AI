"""MirrorChat 服务实现 — 意图提取 + 对话生成（协作清单 #5）

ExtractIntent：query_type 四选一（profile/structured/semantic/hybrid）+ 过滤条件 + rewritten_query
Chat：服务端流式 stream ChatChunk{content, done, sources, thinking}
（thinking=4 为 LLM 思考过程增量块，proto3 optional，模型不支持时永不为真）
无状态：配置随请求携带，用完即弃。
PlanTools 的工具执行结果（ChatRequest.tool_results，field 6）由 Chat 渲染成上下文块
（_format_tool_results，截断到 config plan_tools.max_tool_chars）；空列表零影响
（prompt 不留孤儿块，同 glossary 模式）。
"""

import json

from generated import mirror_chat_pb2 as pb2
from generated import mirror_chat_pb2_grpc as pb2_grpc

from config import CONFIG
from errors import abort_with_mapped
from glossary_render import format_glossary
from llm.factory import create_llm
from llm_json import parse_json
from prompts_loader import loader

# query_type 四选一（英文小写），非法值兜底 hybrid
QUERY_TYPES = {"profile", "structured", "semantic", "hybrid"}
DEFAULT_QUERY_TYPE = "hybrid"

CONTENT_TYPES = {"todo", "thought", "learning", "plan", "note", "work", "social", "health"}
MOODS = {"happy", "excited", "satisfied", "grateful", "expecting", "calm", "bored",
         "confused", "anxious", "sad", "angry", "exhausted", "stressed"}

ROLE_ASSISTANT = "assistant"

# 工具结果渲染截断（config plan_tools.max_tool_chars，防 prompt 膨胀）
_MAX_TOOL_CHARS = int(CONFIG["plan_tools"]["max_tool_chars"])


def _format_tool_results(tool_results) -> str:
    """ToolResult 列表 → prompt 上下文块。

    文件类结果（find_item/recall_item 的 items/item）渲染为独立 [F编号] 行——
    LLM 引用文件时写 [F1]，B 侧 extractVaultRefs 只认 [F\\d+] 出文件卡（fix-batch B3）。
    日记类/汇总类工具结果渲染为「工具 {tool} 返回：…」行，不加编号。
    （原先是 [工具结果·{tool}] 方括号标签——长得像 [n]/[F1] 引用编号，联调实测模型会把它
    原样抄进回答里当引用，前端显示出一串假标记；prompt 里禁止也拦不住，所以从格式上根治）
    失败结果只渲染失败说明，不把失败 payload 喂给 LLM（防止拿错误输出当事实）。
    空列表返回 ""（prompt 不留孤儿块，同 glossary 模式）。
    """
    if not tool_results:
        return ""
    lines = []
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
    return "\n".join(lines)


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

    - 带 records 列表（search_records）→ 逐行 "- 日期（类型）标题：摘录"，比 JSON 省字且好读
    - 其余 dict（get_stats 等）→ 紧凑 JSON（ensure_ascii=False，中文不转义）
    - 不是 dict 的合法 JSON（如列表）→ 原文
    """
    import json
    records = payload.get("records") if isinstance(payload, dict) else None
    if isinstance(records, list) and records:
        rows = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            date = str(rec.get("date") or "")[:10]
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
_CONTEXT_EMPTY_WITH_TOOLS = "（语义检索没有直接命中的日记片段——这一轮的依据在下方「工具查询结果」里，以它为准）"


def _context_text(chunks, tool_results_text: str) -> str:
    """{context} 渲染：有 chunks 照旧；无 chunks 时看工具有没有数据"""
    if not chunks and tool_results_text:
        return _CONTEXT_EMPTY_WITH_TOOLS
    return _format_context(chunks)


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
    """

    _MAX_HOLD = 40

    def __init__(self, tool_names):
        self._names = {(n or "").strip() for n in tool_names} - {""}
        self._hold = ""

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
                self._hold = ch
            else:
                out.append(ch)
        return "".join(out)

    def flush(self) -> str:
        held, self._hold = self._hold, ""
        return held

    def _is_fake(self, token: str) -> bool:
        inner = token[1:-1].strip()
        # 以"工具"开头的一律视为假标记：实测模型会从 prompt 章节名自造变体
        # （[工具结果]、[工具查询结果]…），逐个列举追不上
        if inner.startswith("工具"):
            return True
        return inner.split("·")[0].strip() in self._names


def _format_history(history) -> str:
    if not history:
        return "（无历史对话）"
    return "\n".join(f"{m.role}: {m.content}" for m in history)


class MirrorChatServicer(pb2_grpc.MirrorChatServicer):

    def ExtractIntent(self, request, context):
        query = request.query
        llm_config = request.llm_config

        print(f"[ExtractIntent] query: {query}")
        print(f"[ExtractIntent] LLM: provider={llm_config.provider}, model={llm_config.model}")

        try:
            llm = create_llm(
                provider=llm_config.provider,
                api_key=llm_config.api_key,
                base_url=llm_config.base_url,
                model=llm_config.model,
                protocol=llm_config.protocol,
            )

            prompt = loader.render(
                "intent",
                query=query,
                glossary=format_glossary(request.glossary),
            )
            if request.glossary:
                print(f"[ExtractIntent] glossary 注入 {len(request.glossary)} 条")
            # json_task：关思考（四选一路由不需要思维链）。实测 22.6s → 2.1s，
            # 这是"用户看到第一个字之前"的串行前置调用，直接决定感知延迟。
            response_text = llm.json_task([{"role": "user", "content": prompt}])
            print(f"[ExtractIntent] LLM 响应: {response_text[:200]}")

            result = parse_json(response_text, ctx="意图")
            ct = result.get("content_type")
            return pb2.ExtractIntentResponse(
                query_type=result.get("query_type") if result.get("query_type") in QUERY_TYPES else DEFAULT_QUERY_TYPE,
                content_type=ct if ct in CONTENT_TYPES else None,  # 白名单校验：脏值透传会让 Java SQL 等值过滤静默空结果
                moods=[m for m in result.get("moods", []) if m in MOODS],
                time_range=result.get("time_range", ""),
                rewritten_query=result.get("rewritten_query") or query,  # 兜底：改写失败用原 query
            )

        except Exception as e:
            print(f"[ExtractIntent] 错误: {e}")
            abort_with_mapped(context, e)

    def Chat(self, request, context):
        question = request.question
        llm_config = request.llm_config

        print(f"[Chat] question: {question[:80]} | chunks={len(request.chunks)} | history={len(request.history)}")
        print(f"[Chat] LLM: provider={llm_config.provider}, model={llm_config.model}")

        try:
            llm = create_llm(
                provider=llm_config.provider,
                api_key=llm_config.api_key,
                base_url=llm_config.base_url,
                model=llm_config.model,
                protocol=llm_config.protocol,
            )

            tool_results_text = _format_tool_results(request.tool_results)
            # 检索为空 → 明确告知（6.6 兜底）；但工具有数据时改口，别让两段互相打架
            context_text = _context_text(request.chunks, tool_results_text)
            has_context = len(request.chunks) > 0

            if tool_results_text:
                print(f"[Chat] tool_results 注入 {len(request.tool_results)} 条")

            prompt = loader.render(
                "chat",
                question=question,
                history=_format_history(request.history),
                context=context_text,
                glossary=format_glossary(request.glossary),
                tool_results=tool_results_text,
            )
            if request.glossary:
                print(f"[Chat] glossary 注入 {len(request.glossary)} 条")

            messages = [{"role": "system", "content": prompt}]
            messages += [{"role": m.role, "content": m.content} for m in request.history]
            messages.append({"role": "user", "content": question})

            # 流式输出：逐块 yield，最后一块 done=true 携带 sources。
            # thinking 通道（LLM 思考过程增量块）与正文分流：thinking 走 ChatChunk.thinking
            # （不进 buffer，不参与 [n] 引用解析），content 照旧。模型不发思考块时
            # 只有 content 分支 → wire 上与旧版完全一致（零回归）。
            buffer: list[str] = []
            fake_cites = _FakeCiteFilter(r.tool for r in request.tool_results)
            for kind, text in llm.chat_stream(messages):
                if kind == "thinking":
                    yield pb2.ChatChunk(thinking=text)
                    continue
                text = fake_cites.feed(text)
                if not text:
                    continue
                buffer.append(text)
                yield pb2.ChatChunk(content=text, done=False, sources=[])
            tail = fake_cites.flush()
            if tail:
                buffer.append(tail)
                yield pb2.ChatChunk(content=tail, done=False, sources=[])

            sources = _extract_sources("".join(buffer), request.chunks) if has_context else []
            yield pb2.ChatChunk(content="", done=True, sources=sources)
            print(f"[Chat] 完成，共 {len(buffer)} 块，sources={len(sources)}")

        except Exception as e:
            print(f"[Chat] 错误: {e}")
            abort_with_mapped(context, e)


def _extract_sources(answer: str, chunks) -> list[pb2.Source]:
    """从回答中解析 [n] 引用标记 → Source{record_id, quote, date, n}

    n 是正文里的原始引用编号（1-based）：本函数只返回"被引用到的"子集，
    编号会跳号（如只引用了 [1] 和 [5] → 列表长度 2）——消费方必须按 n 定位，
    不能用数组下标 sources[n-1]。
    """
    sources: dict[int, pb2.Source] = {}
    for token in _find_markers(answer):
        idx = token - 1  # 回答里 [1] 对应 chunks[0]
        if 0 <= idx < len(chunks) and idx not in sources:
            c = chunks[idx]
            sources[idx] = pb2.Source(
                record_id=c.record_id,
                quote=c.title or c.content[:100],  # 对齐 Java 兜底路径：title 优先（ChatServiceImpl.deriveSources）
                date=c.created_at,
                n=idx + 1,                        # 引用编号回传（B 侧/前端据此对齐）
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
