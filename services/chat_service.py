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
    日记类/汇总类工具结果仍是 [工具结果·{tool}] 汇总行，不加编号。
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
            lines.append(f"[工具结果·{tool}] （工具执行失败，以下回答不要依赖该工具的数据）")
            continue
        payload = _parse_payload(r.payload_json)
        items = _extract_file_items(tool, payload)
        if items:
            for it in items:
                f_no += 1
                lines.append(f"[F{f_no}] {_describe_file_item(it)}")
            body = (r.summary or "").strip()
            if body:
                lines.append(f"[工具结果·{tool}] {body}")
            continue
        body = (r.summary or "").strip()
        if not body:
            body = (r.payload_json or "").strip()
        if not body:
            body = "（无返回数据）"
        if len(body) > _MAX_TOOL_CHARS:
            body = body[:_MAX_TOOL_CHARS] + "…"
        lines.append(f"[工具结果·{tool}] {body}")
    return "\n".join(lines)


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


def _format_context(chunks) -> str:
    """RetrievedChunk 列表 → 带编号的资料文本（供 [n] 引用）"""
    if not chunks:
        return "（没有找到相关记录）"
    lines = []
    for i, c in enumerate(chunks, start=1):
        moods = "/".join(c.moods)
        suffix = f" 情绪:{moods}" if moods else ""
        lines.append(f"[{i}] {c.created_at} ({c.content_type}{suffix}): {c.content}")
    return "\n".join(lines)


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
            response_text = llm.chat([{"role": "user", "content": prompt}])
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

            # 检索为空 → 明确告知（6.6 兜底："没有找到相关记录"）
            context_text = _format_context(request.chunks)
            has_context = len(request.chunks) > 0

            tool_results_text = _format_tool_results(request.tool_results)
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
            for kind, text in llm.chat_stream(messages):
                if kind == "thinking":
                    yield pb2.ChatChunk(thinking=text)
                    continue
                buffer.append(text)
                yield pb2.ChatChunk(content=text, done=False, sources=[])

            sources = _extract_sources("".join(buffer), request.chunks) if has_context else []
            yield pb2.ChatChunk(content="", done=True, sources=sources)
            print(f"[Chat] 完成，共 {len(buffer)} 块，sources={len(sources)}")

        except Exception as e:
            print(f"[Chat] 错误: {e}")
            abort_with_mapped(context, e)


def _extract_sources(answer: str, chunks) -> list[pb2.Source]:
    """从回答中解析 [n] 引用标记 → Source{record_id, quote, date}"""
    sources: dict[int, pb2.Source] = {}
    for token in _find_markers(answer):
        idx = token - 1  # 回答里 [1] 对应 chunks[0]
        if 0 <= idx < len(chunks) and idx not in sources:
            c = chunks[idx]
            sources[idx] = pb2.Source(
                record_id=c.record_id,
                quote=c.title or c.content[:100],  # 对齐 Java 兜底路径：title 优先（ChatServiceImpl.deriveSources）
                date=c.created_at,
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
