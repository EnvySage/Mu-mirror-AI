"""Keep model-written tool protocols out of answer deltas; never execute them."""

import json
import re


class GraphToolCallTextFilter:
    """Fail closed on answer protocols, including attributes and Markdown fences.

    Only a short possible opener is held between chunks. Once blocked, discard
    the rest of this generation; the caller decides whether repair is allowed.
    """

    _PREFIXES = ("<tool_call", "<tool_calls", "<function_call", "<function_calls",
                 "<function", "<tool_use", "<invoke", "[工具_call", "[工具调用", "[tool_call", "[function_call")
    _OPEN = re.compile(
        r"<(?:tool_calls?|function_calls?|tool_use|invoke)(?=[\s>/])|<function(?=[\s=/>])"
        r"|\[(?:工具_call|工具调用|tool_call|function_call)", re.IGNORECASE,
    )
    _JSON_LIMIT = 4096
    _JSON_START = re.compile(r'\{\s*(?:"|}|$)')
    _JSON_CALL = re.compile(
        r'\{\s*"(?:tool_calls?|function_calls?)"\s*:'
        r'|\{\s*"calls"\s*:\s*\[\s*\{\s*"(?:tool|function|name)"\s*:'
        r'|\{\s*"function"\s*:\s*\{\s*"name"\s*:'
        r'|\{\s*"(?:tool|name)"\s*:\s*"[^"\n]{1,128}"\s*,\s*"(?:args|arguments|parameters)"\s*:',
        re.IGNORECASE,
    )
    _JSON_POSSIBLE_CALL = re.compile(
        r'\{\s*(?:"(?:tool|name|args|arguments|parameters|calls|function|tool_calls?|function_calls?)"\s*:|$)',
        re.IGNORECASE,
    )

    def __init__(self, *, allow_protocol_examples=False):
        self.allow_protocol_examples = allow_protocol_examples
        self.pending = ""
        self.preamble = ""
        self.started = False
        self.blocked = False

    def feed(self, text: str) -> str:
        if self.allow_protocol_examples:
            return text
        if self.blocked:
            return ""
        self.pending += text
        match = self._OPEN.search(self.pending)
        json_block, json_hold = self._json_boundary()
        blocked_at = min((start for start in (match.start() if match else None, json_block)
                          if start is not None), default=None)
        if blocked_at is not None:
            safe = self.pending[:blocked_at]
            self.pending = ""
            self.blocked = True
            # A fence opening is presentation, not a visible answer prefix.
            self.preamble = ""
            return self._release(safe)
        lowered = self.pending.lower()
        keep = max((size for prefix in self._PREFIXES for size in range(1, len(prefix) + 1)
                    if lowered.endswith(prefix[:size])), default=0)
        if json_hold is not None:
            keep = max(keep, len(self.pending) - json_hold)
        safe = self.pending[:-keep] if keep else self.pending
        self.pending = self.pending[-keep:] if keep else ""
        return self._release(safe)

    def _json_boundary(self):
        """Hold a possible JSON object until its envelope is known, within a cap."""
        start = self.pending.find("{")
        while start >= 0:
            candidate = self.pending[start:]
            if not self._JSON_START.match(candidate):
                start = self.pending.find("{", start + 1)
                continue
            if self._JSON_CALL.match(candidate):
                return start, None
            try:
                value, end = json.JSONDecoder().raw_decode(candidate)
            except (ValueError, RecursionError):
                if len(candidate) <= self._JSON_LIMIT:
                    return None, start
                if self._JSON_POSSIBLE_CALL.match(candidate):
                    # Never release an unresolved call envelope when its cap is hit.
                    return start, None
                # Do not indefinitely buffer ordinary JSON or a prose brace.
                start = self.pending.find("{", start + 1)
                continue
            if isinstance(value, dict) and (
                any(key in value for key in ("tool_calls", "tool_call", "function_calls", "function_call"))
                or (any(key in value for key in ("tool", "name"))
                    and any(key in value for key in ("args", "arguments", "parameters")))
                or (isinstance(value.get("calls"), list) and any(
                    isinstance(call, dict) and ("tool" in call or "function" in call)
                    for call in value["calls"]))
                or (isinstance(value.get("function"), dict) and "name" in value["function"]
                    and any(key in value["function"] for key in ("args", "arguments", "parameters")))
            ):
                return start, None
            start = self.pending.find("{", start + end)
        return None, None

    def _release(self, safe):
        if self.started:
            return safe
        candidate = self.preamble + safe
        stripped = candidate.lstrip()
        partial_fence = stripped and any(fence.startswith(stripped) for fence in ("```", "~~~"))
        fence_header = stripped.startswith(("```", "~~~")) and (
            "\n" not in stripped or not stripped.split("\n", 1)[1].strip()
        )
        if (partial_fence or fence_header) and len(candidate) <= 256:
            self.preamble = candidate
            return ""
        self.preamble = ""
        self.started = bool(candidate.strip())
        return candidate

    def flush(self) -> str:
        if self.blocked:
            return ""
        tail, self.pending = self.pending, ""
        if len(tail) >= 5 and any(prefix.startswith(tail.lower()) for prefix in self._PREFIXES):
            self.blocked = True
            self.preamble = ""
            return ""
        out = self.preamble + tail
        self.preamble = ""
        return out


class ToolCallTextFilter:
    """Incremental, bounded filter with support for split tags and fenced examples."""

    MESSAGE = "（工具调用未完成，未生成可用的操作卡，请重试。）"
    _CALLS = {
        "<tool_call>": "</tool_call>",
        "<tool_calls>": "</tool_calls>",
        "<function_calls>": "</function_calls>",
        "<function=": "</function>",
        # 部分第三方模型把未执行的工具调用写成方括号占位符
        "[工具_call": "]",
        "[工具调用": "]",
        "[tool_call": "]",
        "[function_call": "]",
    }
    _FENCES = ("```", "~~~")

    def __init__(self, emit_block_message: bool = True):
        self.pending = ""
        self.closing = ""
        self.fence = ""
        self.blocked = False
        self.emit_block_message = emit_block_message

    def feed(self, text: str) -> str:
        self.pending += text
        output = []
        while self.pending:
            lowered = self.pending.lower()
            if self.closing:
                end = lowered.find(self.closing)
                if end < 0:
                    # Retain only a possible partial closing tag, never the payload.
                    self.pending = self.pending[-(len(self.closing) - 1):]
                    break
                self.pending = self.pending[end + len(self.closing):]
                self.closing = ""
                continue

            tokens = (self.fence,) if self.fence else (*self._CALLS, *self._FENCES)
            matches = [(lowered.find(token), token) for token in tokens if token in lowered]
            if matches:
                start, token = min(matches)
                output.append(self.pending[:start])
                self.pending = self.pending[start + len(token):]
                if token in self._FENCES:
                    self.fence = "" if self.fence else token
                    output.append(token)
                else:
                    self.closing = self._CALLS[token]
                    if not self.blocked:
                        self.blocked = True
                        if self.emit_block_message:
                            output.append(self.MESSAGE)
                        print("[Answer] blocked model-written tool protocol in answer content")
                continue

            # A tag or fence may begin in this chunk and finish in the next one.
            keep = max((size for token in tokens for size in range(1, len(token))
                        if lowered.endswith(token[:size])), default=0)
            output.append(self.pending[:-keep] if keep else self.pending)
            self.pending = self.pending[-keep:] if keep else ""
            break
        return "".join(output)

    def flush(self) -> str:
        tail, self.pending = self.pending, ""
        if self.closing:
            return ""
        if not self.fence and any(token.startswith(tail.lower()) and len(tail) >= 5
                                  for token in self._CALLS):
            if self.blocked:
                return ""
            self.blocked = True
            return self.MESSAGE if self.emit_block_message else ""
        return tail
