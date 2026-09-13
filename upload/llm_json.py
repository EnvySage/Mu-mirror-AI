"""LLM JSON 响应解析 — 公共模块（第九轮协作清单）

此前 chat_service / profile_service / record_processor 各持一份复制粘贴的
_parse_json，record 版已漂移成裸 ValueError（→ INTERNAL 而非 INVALID_ARGUMENT）。
现收编为单一 parse_json：统一 raise ContentInvalidError（→ INVALID_ARGUMENT）。

ctx 用于错误消息区分类别（"意图"/"画像"/"分类"），不参与解析逻辑。
"""

import json

from errors import ContentInvalidError


def parse_json(text: str, ctx: str = "LLM") -> dict:
    """从 LLM 响应中提取 JSON 对象。

    依次尝试：直接解析 → ```json 代码块 → 首尾大括号截取。
    任何路径失败（包括截取后的坏 JSON）统一抛 ContentInvalidError。
    """
    try:
        return _extract(text)
    except ContentInvalidError:
        raise
    except (json.JSONDecodeError, ValueError) as e:
        # ```json 块/括号截取命中但内容仍是坏 JSON：与完全无 JSON 同罪，
        # 都是"内容不合规"而非服务器内部错误
        raise ContentInvalidError(f"{ctx}响应 JSON 解析失败: {e}; 原文片段: {text[:200]}") from e


def _extract(text: str) -> dict:
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

    raise ContentInvalidError(f"LLM 响应无法解析为 JSON: {text[:200]}")
