"""异常 → gRPC 状态码映射（协作清单 #8 + 第九轮修订）

| 异常 | 状态码 |
|------|--------|
| LLM/Embedding 超时（本端等待上游超时） | DEADLINE_EXCEEDED |
| 内容不合规（无法解析/内容违规） | INVALID_ARGUMENT |
| 模型不可用（连接失败、鉴权失败、模型不存在、限流） | UNAVAILABLE |
| 其他内部错误 | INTERNAL |

第九轮修订：
1. isinstance 精确检查（AiServiceError 子类 / ValueError 族）挪到关键字匹配之前
   ——关键字只是最后兜底，避免 "apiconnectionerror" 这类文本误判超时。
2. _TIMEOUT_MARKERS 移除 "apiconnectionerror"（连接失败是 UNAVAILABLE，不是超时）。
3. abort_with_mapped 的 details 不再回传 str(exc) 原文（会泄露内部细节给客户端），
   只留安全摘要（错误类名 + 状态码）；原文打服务端日志。
4. 供 llm/openai_llm.py、llm/anthropic_llm.py 主动翻译 SDK 异常：
   translate_llm_sdk_exception() 把 openai/anthropic 的 SDK 异常按类型+status_code
   归类为 LlmTimeoutError / LlmUnavailableError，让这两个异常类有真实 raise 点。
"""

import json
import logging

import grpc

logger = logging.getLogger("mirror-ai.errors")


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


class ConfigMissingError(AiServiceError):
    """必需配置缺失（api_key / base_url / model 未由调用方显式提供）。

    历史版本在 llm/openai_llm.py、embedding/api_embedder.py 里按 provider 写死了
    默认 model/base_url（qwen-plus、text-embedding-v3 等）。后果是"用户没配模型"
    不会报错，而是被静默替换成另一个模型去真实调用并计费——账单上出现用户从未
    配置过的模型。契约改为：缺什么报什么，绝不替调用方猜。
    """

    def __init__(self, message: str = "缺少必需的模型配置"):
        super().__init__(message, grpc.StatusCode.INVALID_ARGUMENT)


def require_config(values: dict) -> None:
    """批量校验必需配置，缺任一项即抛 ConfigMissingError。

    @param values: {展示名: 实际值}，空字符串/None 视为缺失
    """
    missing = [name for name, val in values.items() if not val]
    if missing:
        raise ConfigMissingError(
            f"模型配置缺失：{'、'.join(missing)} 未配置（不提供默认值，请在设置页显式填写）"
        )


# 关键字表仅作最后兜底（非 SDK 异常的裸网络错误等），已降级为次优先级
_TIMEOUT_MARKERS = ("timeout", "timed out", "超时", "deadline")
_UNAVAILABLE_MARKERS = (
    "api key", "api_key", "unauthorized", "401", "403", "forbidden", "authentication",
    "not found", "404", "model_not_found", "connection error", "connection refused",
    "connect error", "connection reset", "unreachable", "rate limit", "429", "502",
    "503", "insufficient", "quota", "无可用", "连接失败", "鉴权",
)


def map_exception(exc: Exception) -> AiServiceError:
    """把底层异常归类为 AiServiceError（已归类则原样返回）。

    优先级：AiServiceError 子类 > ValueError 族（内容不合规）> 关键字兜底 > INTERNAL。
    """
    if isinstance(exc, AiServiceError):
        return exc
    if isinstance(exc, (json.JSONDecodeError, ValueError, KeyError, TypeError)):
        return ContentInvalidError(f"LLM 响应无法解析: {exc}")
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(m in text for m in _TIMEOUT_MARKERS):
        return LlmTimeoutError(str(exc))
    if any(m in text for m in _UNAVAILABLE_MARKERS):
        return LlmUnavailableError(str(exc))
    return AiServiceError(str(exc))


def translate_llm_sdk_exception(exc: Exception) -> AiServiceError:
    """把 openai / anthropic SDK 异常翻译成 AiServiceError 子类（第九轮 #3）。

    两家 SDK 的异常层级同构（APIError 基类，APIStatusError 带 status_code，
    APITimeoutError / APIConnectionError 继承自 APIError），用鸭子类型判断，
    不硬 import SDK（本模块被无 SDK 的测试环境引用时也能工作）：

    - APITimeoutError                    → LlmTimeoutError  (DEADLINE_EXCEEDED)
    - APIConnectionError（含超时子类）    → 已被上一行截获；纯连接失败 → LlmUnavailableError
    - APIStatusError 按 status_code 分流：
        401/403/404                      → LlmUnavailableError（鉴权/模型不存在）
        429                              → LlmUnavailableError（限流，附带可重试语义）
        5xx                              → LlmUnavailableError（上游故障）
        其他 4xx                         → 保持原语义：请求内容问题多为 4xx，
                                           但这里保守归 INTERNAL（非内容解析失败）
    - 其他未知异常                        → 交回 map_exception 兜底
    """
    cls_name = type(exc).__name__
    mro_names = {c.__name__ for c in type(exc).__mro__}

    if "APITimeoutError" in mro_names:
        return LlmTimeoutError(f"LLM 请求超时（{cls_name}）")

    if "APIConnectionError" in mro_names:
        # 网络层连接失败/断开：上游不可达，可重试 → UNAVAILABLE
        return LlmUnavailableError(f"LLM 连接失败（{cls_name}）")

    if "APIStatusError" in mro_names:
        status_code = getattr(exc, "status_code", None)
        if status_code in (401, 403, 404):
            return LlmUnavailableError(f"LLM 请求被拒绝（HTTP {status_code}，{cls_name}）")
        if status_code == 429:
            return LlmUnavailableError(f"LLM 限流（HTTP 429，{cls_name}），可稍后重试")
        if status_code is not None and 500 <= status_code < 600:
            return LlmUnavailableError(f"LLM 服务端错误（HTTP {status_code}，{cls_name}）")
        # 其他 4xx（400/409/413/422 等）：请求本身的问题，不归内容解析也不归不可用
        return AiServiceError(f"LLM 请求失败（HTTP {status_code}，{cls_name}）")

    # 非 SDK 异常（如 AssertionError / RuntimeError）→ 走通用兜底
    return map_exception(exc)


def safe_details(mapped: AiServiceError) -> str:
    """客户端可见的安全摘要：只露错误类名 + 状态码，不露异常原文/堆栈/URL。"""
    return f"{type(mapped).__name__} ({mapped.code.name})"


def abort_with_mapped(context, exc: Exception) -> None:
    """按映射规则中止 gRPC 调用。

    原文只进服务端日志；details 给客户端安全摘要（第九轮 #3，防内部信息泄露）。
    """
    mapped = translate_llm_sdk_exception(exc) if not isinstance(exc, AiServiceError) else exc
    if not isinstance(mapped, AiServiceError):  # 双保险（translate 内部已兜底）
        mapped = map_exception(exc)
    logger.error("gRPC abort: %s | original=%s: %s",
                 safe_details(mapped), type(exc).__name__, exc)
    context.abort(mapped.code, safe_details(mapped))
