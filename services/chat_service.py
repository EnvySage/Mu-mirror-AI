"""MirrorChat 服务实现 — 意图提取 + 对话生成（协作清单 #5）

ExtractIntent：query_type 四选一（profile/structured/semantic/hybrid）+ 过滤条件 + rewritten_query
Chat：服务端流式 stream ChatChunk{content, done, sources}
无状态：配置随请求携带，用完即弃。
"""

from generated import mirror_chat_pb2 as pb2
from generated import mirror_chat_pb2_grpc as pb2_grpc

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

            prompt = loader.render(
                "chat",
                question=question,
                history=_format_history(request.history),
                context=context_text,
                glossary=format_glossary(request.glossary),
            )
            if request.glossary:
                print(f"[Chat] glossary 注入 {len(request.glossary)} 条")

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
