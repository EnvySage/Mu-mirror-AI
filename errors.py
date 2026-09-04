"""异常 → gRPC 状态码映射（协作清单 #8）

| 异常 | 状态码 |
|------|--------|
| LLM/Embedding 超时（本端等待上游超时） | DEADLINE_EXCEEDED |
| 内容不合规（无法解析/内容违规） | INVALID_ARGUMENT |
| 模型不可用（连接失败、鉴权失败、模型不存在） | UNAVAILABLE |
| 其他内部错误 | INTERNAL |
"""

import json

import grpc


class AiServiceError(Exception):
    """业务异常基类，携带应映射的 gRPC 状态码"""

    def __init__(self, message: str, code: grpc.StatusCode = grpc.StatusCode.INTERNAL):
        super().__init__(message)
        self.code = code


class LlmTimeoutError(AiServiceError):
    def __init__(self, message: str = "LLM 调用超时"):
        super().__init__(message, grpc.StatusCode.DEADLINE_EXCEEDED)


class LlmUnavailableError(AiServiceError):
    def __init__(self, message: str = "模型不可用"):
        super().__init__(message, grpc.StatusCode.UNAVAILABLE)


class ContentInvalidError(AiServiceError):
    def __init__(self, message: str = "内容不合规或无法解析"):
        super().__init__(message, grpc.StatusCode.INVALID_ARGUMENT)


_TIMEOUT_MARKERS = ("timeout", "timed out", "超时", "deadline", "apiconnectionerror")
_UNAVAILABLE_MARKERS = (
    "api key", "api_key", "unauthorized", "401", "403", "forbidden", "authentication",
    "not found", "404", "model_not_found", "connection error", "connection refused",
    "connect error", "unreachable", "rate limit", "429", "502", "503", "insufficient",
    "quota", "无可用", "连接失败", "鉴权",
)


def map_exception(exc: Exception) -> AiServiceError:
    """把底层异常归类为 AiServiceError（已归类则原样返回）"""
    if isinstance(exc, AiServiceError):
        return exc
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(m in text for m in _TIMEOUT_MARKERS):
        return LlmTimeoutError(str(exc))
    if any(m in text for m in _UNAVAILABLE_MARKERS):
        return LlmUnavailableError(str(exc))
    if isinstance(exc, (json.JSONDecodeError, ValueError, KeyError, TypeError)):
        return ContentInvalidError(f"LLM 响应无法解析: {exc}")
    return AiServiceError(str(exc))


def abort_with_mapped(context, exc: Exception) -> None:
    """按映射规则中止 gRPC 调用"""
    mapped = map_exception(exc)
    context.abort(mapped.code, str(mapped))
