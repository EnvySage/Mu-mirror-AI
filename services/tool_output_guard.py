"""Keep model-written tool protocols out of answer deltas; never execute them."""


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
