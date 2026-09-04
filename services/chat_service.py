"""MirrorChat 服务实现 — 意图提取 + 对话生成（协作清单 #5）

ExtractIntent：query_type 四选一（profile/structured/semantic/hybrid）+ 过滤条件 + rewritten_query
Chat：服务端流式 stream ChatChunk{content, done, sources}
无状态：配置随请求携带，用完即弃。
"""

import json

from generated import mirror_chat_pb2 as pb2
from generated import mirror_chat_pb2_grpc as pb2_grpc

from errors import ContentInvalidError, abort_with_mapped
from llm.factory import create_llm
from prompts_loader import loader

# query_type 四选一（英文小写），非法值兜底 hybrid
QUERY_TYPES = {"profile", "structured", "semantic", "hybrid"}
DEFAULT_QUERY_TYPE = "hybrid"

CONTENT_TYPES = {"todo", "thought", "learning", "plan", "note", "work", "social", "health"}
MOODS = {"happy", "excited", "satisfied", "grateful", "expecting", "calm", "bored",
         "confused", "anxious", "sad", "angry", "exhausted", "stressed"}

ROLE_ASSISTANT = "assistant"


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

            prompt = loader.render("intent", query=query)
            response_text = llm.chat([{"role": "user", "content": prompt}])
            print(f"[ExtractIntent] LLM 响应: {response_text[:200]}")

            result = _parse_json(response_text)
            return pb2.ExtractIntentResponse(
                query_type=result.get("query_type") if result.get("query_type") in QUERY_TYPES else DEFAULT_QUERY_TYPE,
                content_type=result.get("content_type") or None,
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

            prompt = loader.render(
                "chat",
                question=question,
                history=_format_history(request.history),
                context=context_text,
            )

            messages = [{"role": "system", "content": prompt}]
            messages += [{"role": m.role, "content": m.content} for m in request.history]
            messages.append({"role": "user", "content": question})

            # 流式输出：逐块 yield，最后一块 done=true 携带 sources
            buffer: list[str] = []
            for piece in llm.chat_stream(messages):
                buffer.append(piece)
                yield pb2.ChatChunk(content=piece, done=False, sources=[])

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
                quote=c.content[:100],
                date=c.created_at,
            )
    return list(sources.values())


def _find_markers(answer: str) -> list[int]:
    out = []
    for part in answer.split("[")[1:]:
        head = part.split("]")[0].strip()
        if head.isdigit():
            out.append(int(head))
    return out


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

    raise ContentInvalidError(f"LLM 响应无法解析为意图 JSON: {text[:200]}")
