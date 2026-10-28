"""Tarot RPC uses the caller's configured model and never invokes agent tools."""
import grpc
from generated import tarot_pb2_grpc
from llm.factory import create_llm
from mirror_core.errors import map_exception
from mirror_core.tarot_reading import analyze_reading


class TarotReadingServicer(tarot_pb2_grpc.TarotReadingServicer):
    def Analyze(self, request, context):
        if not context.is_active():
            context.abort(grpc.StatusCode.CANCELLED, "请求已取消")
        config = request.llm_config
        try:
            llm = create_llm(provider=config.provider, api_key=config.api_key,
                             base_url=config.base_url, model=config.model, protocol=config.protocol)
            remaining = context.time_remaining()
            timeout = min(55.0, remaining) if remaining is not None else 55.0
            if timeout <= 0:
                context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "解读超时")
            result = analyze_reading(request, llm, timeout=timeout)
        except Exception as exc:
            # Do not log source excerpts, credentials, or upstream error payloads.
            mapped = map_exception(exc)
            context.abort(mapped.code, "暂时无法生成解读，请稍后重试")
        if not context.is_active():
            context.abort(grpc.StatusCode.CANCELLED, "请求已取消")
        return result
