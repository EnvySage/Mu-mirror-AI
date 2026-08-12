"""MirrorChat 服务实现 — 意图提取 + 对话生成"""

from generated import mirror_chat_pb2 as pb2
from generated import mirror_chat_pb2_grpc as pb2_grpc


class MirrorChatServicer(pb2_grpc.MirrorChatServicer):
    """对话服务（测试阶段，返回硬编码数据）"""

    def ExtractIntent(self, request, context):
        query = request.query
        llm_config = request.llm_config

        print(f"[ExtractIntent] query: {query}")
        print(f"[ExtractIntent] LLM: {llm_config.provider}/{llm_config.model}")

        # 测试数据
        return pb2.ExtractIntentResponse(
            content_type="",
            moods=[],
            time_range="",
            rewritten_query=query,
        )

    def Chat(self, request, context):
        question = request.question
        llm_config = request.llm_config

        print(f"[Chat] question: {question}")
        print(f"[Chat] LLM: {llm_config.provider}/{llm_config.model}")

        # 测试数据：流式返回
        yield pb2.ChatChunk(
            content="这是一个测试回答。 ",
            done=False,
            sources=[],
        )
        yield pb2.ChatChunk(
            content="后续会接入真正的 AI 模型。",
            done=True,
            sources=[],
        )