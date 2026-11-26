"""Streaming inference and daily summarization used by Java background features."""

from generated import mirror_chat_pb2 as pb2
from generated import mirror_chat_pb2_grpc as pb2_grpc

from mirror_core.errors import abort_with_mapped
from llm.factory import create_llm
from services.chat_memory_service import ChatMemoryService

ROLE_ASSISTANT = "assistant"

from services.answer_service import (
    _CONTEXT_EMPTY_WITH_TOOLS,
    _CONTEXT_NOT_SEARCHED,
    _CONTEXT_SEARCH_EMPTY,
    _CONTEXT_SEARCH_FAILED,
    _FakeCiteFilter,
    _MAX_TOOL_CHARS,
    _context_text,
    _describe_file_item,
    _extract_file_items,
    _extract_sources,
    _find_markers,
    _format_context,
    _format_history,
    _format_tool_results,
    _format_tool_results_with_sources,
    assistant_context_text,
    _parse_payload,
    _relevance_note,
    _render_file_excerpts,
    _render_tool_payload,
    build_answer_bundle,
    build_answer_messages,
    stream_answer,
)
class MirrorChatServicer(pb2_grpc.MirrorChatServicer):


    def SummarizeDay(self, request, context):
        """Summarize supplied user-only messages and return unactivated memory candidates."""
        try:
            return ChatMemoryService().summarize_day(request, context)
        except Exception as e:
            print(f"[SummarizeDay] 错误: {e}")
            abort_with_mapped(context, e)

    def Chat(self, request, context):
        question = request.question
        llm_config = request.llm_config

        print(f"[Chat] question: {question[:80]} | chunks={len(request.chunks)} | history={len(request.history)}")
        print(f"[Chat] tool_results={len(request.tool_results)} payload_chars={sum(len(r.payload_json) for r in request.tool_results)}")
        print(f"[Chat] LLM: provider={llm_config.provider}, model={llm_config.model}")

        try:
            llm = create_llm(
                provider=llm_config.provider,
                api_key=llm_config.api_key,
                base_url=llm_config.base_url,
                model=llm_config.model,
                protocol=llm_config.protocol,
            )

            messages, tool_sources = build_answer_bundle(
                question,
                request.history,
                request.chunks,
                request.glossary,
                request.tool_results,
                assistant_context=assistant_context_text(request.assistant_context, include_divination=True),
            )
            if request.glossary:
                print(f"[Chat] glossary 注入 {len(request.glossary)} 条")

            # 流式输出：逐块 yield，最后一块 done=true 携带 sources。
            # thinking 通道（LLM 思考过程增量块）与正文分流：thinking 走 ChatChunk.thinking
            # （不进 buffer，不参与 [n] 引用解析），content 照旧。模型不发思考块时
            # 只有 content 分支 → wire 上与旧版完全一致（零回归）。
            buffer: list[str] = []
            source_count = 0
            for event in stream_answer(messages, request.chunks, request.tool_results, llm, tool_sources=tool_sources):
                if event.kind == "thinking":
                    yield pb2.ChatChunk(thinking=event.content)
                elif event.kind == "delta":
                    buffer.append(event.content)
                    yield pb2.ChatChunk(content=event.content, done=False, sources=[])
                else:
                    source_count = len(event.sources)
                    yield pb2.ChatChunk(content="", done=True, sources=event.sources)
            print(f"[Chat] 完成，共 {len(buffer)} 块，sources={source_count}")

        except Exception as e:
            print(f"[Chat] 错误: {e}")
            abort_with_mapped(context, e)