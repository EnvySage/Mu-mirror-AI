"""Receipt inference RPC on the existing model service."""

import grpc

from generated import ledger_pb2_grpc
from llm.factory import create_llm
from mirror_core.errors import map_exception
from mirror_core.ledger_receipt import recognize_receipt, validate_image


class LedgerVisionServicer(ledger_pb2_grpc.LedgerVisionServicer):
    def Recognize(self, request, context):
        if not context.is_active():
            context.abort(grpc.StatusCode.CANCELLED, "请求已取消")
        try:
            today = validate_image(request)
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        remaining = context.time_remaining()
        if remaining is not None and remaining <= 0:
            context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "账单识别超时")
        # json_task can fall back once when a compatible endpoint rejects thinking options.
        timeout = min(30.0, remaining / 2) if remaining is not None else 30.0
        config = request.llm_config
        try:
            llm = create_llm(provider=config.provider, api_key=config.api_key,
                             base_url=config.base_url, model=config.model, protocol=config.protocol)
            result = recognize_receipt(request, llm, today, timeout=timeout)
        except Exception as exc:
            mapped = map_exception(exc)
            context.abort(mapped.code, "当前模型无法识别这张图片，可以手动填写账目。")
        if not context.is_active():
            context.abort(grpc.StatusCode.CANCELLED, "请求已取消")
        return result
