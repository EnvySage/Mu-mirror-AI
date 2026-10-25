"""Graph-only planning protocols shared by the provider adapters."""

from __future__ import annotations

import json
import re
import time

from mirror_core.errors import AiServiceError, ContentInvalidError, LlmTimeoutError, translate_llm_sdk_exception
from mirror_core.llm_json import parse_json

# A plan can carry an entire record body; the legacy 256-token route budget cannot.
PLAN_MAX_TOKENS = 8192


class PlanningProtocolUnsupported(AiServiceError):
    """The configured provider cannot honor the explicitly requested protocol."""


def capability_unsupported(exc: Exception, protocol: str) -> bool:
    """Only explicit protocol capability errors permit a different request."""
    if getattr(exc, "status_code", None) not in (400, 422):
        return False
    text = str(getattr(exc, "message", "") or exc).lower()
    if protocol == "native" and any(word in text for word in ("schema", "parameters", "arguments")):
        return False
    fields = ("tools", "tool_choice", "tool calling", "function calling") if protocol == "native" else ("json_schema", "response_format", "output_config")
    return any(field in text for field in fields) and any(
        hint in text for hint in ("not supported", "unsupported", "unknown parameter", "unrecognized", "not allowed", "does not support")
    )


def plan_schema(tools: list[dict]) -> dict:
    # Provider JSON-schema subsets differ. Keep a strict portable envelope and
    # leave actual argument constraints to the authoritative tool registry.
    names = [tool["name"] for tool in tools]
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "calls": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "tool": {"type": "string", **({"enum": names} if names else {})},
                    "args_json": {"type": "string"},
                },
                "required": ["tool", "args_json"],
            }},
            "done": {"type": "boolean"}, "reason": {"type": "string"},
        },
        "required": ["calls", "done", "reason"],
    }


def native_messages(messages: list[dict]) -> list[dict]:
    instruction = (
        "Planning transport for this request is native tool calling. Any earlier JSON plan example "
        "describes the plan concept only; it is not the response format for this request. "
        "Use the supplied native tools directly for planned calls. Do not print calls as JSON or XML text. "
        "When no further tool is needed, give a brief reason."
    )
    result = [dict(message) for message in messages]
    for index, message in enumerate(result):
        if message.get("role") == "system":
            result[index] = {**message, "content": message["content"] + "\n\n" + instruction}
            return result
    return [{"role": "system", "content": instruction}, *result]


def planning_messages(messages: list[dict], tools: list[dict], protocol: str = "text_json") -> list[dict]:
    call_format = '{"tool": name, "args_json": JSON-encoded argument object string}' if protocol == "json_schema" else '{"tool": name, "args": object}'
    instruction = (
        f'Return one JSON object with calls (array of {call_format}), '
        'done (boolean), and reason (string). Use only these tools: '
        + json.dumps(tools, ensure_ascii=False)
    )
    # Append rather than replace the caller's system context.
    return [*messages, {"role": "user", "content": instruction}]


def text_plan(text: str, protocol: str, finish_reason: str | None) -> dict:
    value = parse_json(text, ctx="graph plan")
    if not isinstance(value, dict) or not isinstance(value.get("calls"), list) or type(value.get("done")) is not bool or not isinstance(value.get("reason", ""), str):
        raise ContentInvalidError("Graph plan must contain calls, done, and reason")
    calls = []
    for call in value["calls"]:
        if not isinstance(call, dict) or not isinstance(call.get("tool"), str):
            raise ContentInvalidError("Graph plan call must contain a tool name")
        if protocol == "json_schema":
            try:
                args = json.loads(call["args_json"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ContentInvalidError("Graph plan args_json must encode an argument object") from exc
        else:
            args = call.get("args")
        if not isinstance(args, dict):
            raise ContentInvalidError("Graph plan args must be an object")
        calls.append({"tool": call["tool"], "args": args})
    return {"calls": calls, "done": value["done"], "reason": value.get("reason", ""), "protocol": protocol, "finish_reason": finish_reason}


def native_plan(calls: list[dict], reason: str, finish_reason: str | None) -> dict:
    if any(not isinstance(call.get("tool"), str) or not isinstance(call.get("args"), dict) for call in calls):
        raise ContentInvalidError("Native plan call must contain a tool name and argument object")
    if not calls and re.search(
        r'<(?:tool_calls?|tool_use|function_calls?|function|invoke)(?=[\s=/>])'
        r'|\[(?:工具_call|工具调用|tool_call|function_call)'
        r'|"calls"\s*:\s*\[\s*(?=[^\s\]])', reason, re.IGNORECASE,
    ):
        raise ContentInvalidError("Native planning returned textual tool calls instead of structured calls")
    return {
        "calls": calls,
        "done": not calls and finish_reason in ("stop", "end_turn", "stop_sequence"),
        "reason": reason, "protocol": "native", "finish_reason": finish_reason,
    }


def run_plan(request, messages, tools, *, protocol, timeout, max_retries, default_timeout):
    if protocol not in ("auto", "native", "json_schema", "text_json"):
        raise ValueError(f"Unknown planning protocol: {protocol}")
    deadline = time.monotonic() + (default_timeout if timeout is None else timeout)

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise LlmTimeoutError("Graph planning deadline exceeded")
        return value

    selected = "native" if protocol == "auto" else protocol
    try:
        result = request(messages, tools, selected, remaining(), max_retries)
        remaining()
        return result
    except Exception as exc:
        if capability_unsupported(exc, selected):
            if protocol != "auto":
                raise PlanningProtocolUnsupported(f"Planning protocol {selected} is not supported by this provider") from exc
        else:
            if isinstance(exc, (AiServiceError, ValueError, TypeError)):
                raise
            raise translate_llm_sdk_exception(exc) from exc
    try:
        result = request(messages, tools, "text_json", remaining(), max_retries)
        remaining()
        return result
    except Exception as exc:
        if isinstance(exc, (AiServiceError, ValueError, TypeError)):
            raise
        raise translate_llm_sdk_exception(exc) from exc
