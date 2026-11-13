"""Incremental JSON projection. Content strings are skipped, never buffered.

Only model/type and usage scalar paths survive parsing. Memory is bounded even
for a single multi-megabyte content string or an adversarial SSE data line.
"""
import codecs
import json
from decimal import Decimal


class JsonProjection:
    def __init__(self, emit):
        self.emit = emit
        self.stack = []
        self.mode = None
        self.buffer = ""
        self.escape = False
        self.keep = False
        self.path = ()
        self.failed = False

    def value_path(self):
        if not self.stack:
            return ()
        kind, path, key, _ = self.stack[-1]
        return path + ((key,) if kind == "object" else ("[]",))

    def feed(self, text):
        if self.failed:
            return
        for char in text:
            if self.mode == "string":
                if self.keep:
                    self.buffer += char
                    if len(self.buffer) > 512:
                        self.keep = False
                        self.buffer = ""
                if self.escape:
                    self.escape = False
                elif char == "\\":
                    self.escape = True
                elif char == '"':
                    value = None
                    if self.keep:
                        try:
                            value = json.loads(self.buffer)
                        except ValueError:
                            self.failed = True
                    self.mode = None
                    self.buffer = ""
                    if self.stack and self.stack[-1][3] == "key":
                        self.stack[-1][2:] = [value, "colon"]
                    elif self.keep:
                        self.emit(self.path, value)
                continue
            if self.mode == "scalar":
                if char not in ",]} \t\r\n":
                    if self.keep:
                        self.buffer += char
                        if len(self.buffer) > 128:
                            self.keep = False
                            self.buffer = ""
                    continue
                if self.keep:
                    try:
                        self.emit(self.path, json.loads(self.buffer, parse_float=Decimal))
                    except ValueError:
                        self.failed = True
                self.mode = None
                self.buffer = ""
            if char.isspace():
                continue
            if char == '"':
                self.path = self.value_path()
                self.keep = bool(self.stack and self.stack[-1][3] == "key") or _selected(self.path)
                self.buffer = '"' if self.keep else ""
                self.mode = "string"
            elif char in "{[":
                if len(self.stack) >= 32:
                    self.failed = True
                    return
                self.stack.append(["object" if char == "{" else "array", self.value_path(), None,
                                   "key" if char == "{" else "value"])
            elif char in "}]":
                if self.stack:
                    self.stack.pop()
            elif char == ":":
                if self.stack:
                    self.stack[-1][3] = "value"
            elif char == ",":
                if self.stack:
                    self.stack[-1][2:] = [None, "key" if self.stack[-1][0] == "object" else "value"]
            else:
                self.path = self.value_path()
                self.keep = _selected(self.path)
                self.buffer = char if self.keep else ""
                self.mode = "scalar"


def _selected(path):
    if path in {("model",), ("message", "model"), ("type",)}:
        return True
    key = path[2:] if path[:2] == ("message", "usage") else path[1:] if path[:1] == ("usage",) else ()
    return key in {("input_tokens",), ("output_tokens",), ("total_tokens",),
                   ("prompt_tokens",), ("completion_tokens",), ("cache_read_input_tokens",),
                   ("cache_creation_input_tokens",), ("prompt_tokens_details", "cached_tokens"),
                   ("completion_tokens_details", "reasoning_tokens"), ("credits_consumed",),
                   ("consumed_credits",), ("points_consumed",), ("consumed_points",), ("credit_unit",)}


class ResponseProjection:
    def __init__(self, observation, sse):
        self.observation = observation
        self.sse = sse
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.parser = JsonProjection(observation.value)
        self.prefix = ""
        self.data = False
        self.first = False
        self.done = ""
        self.last_cr = False

    def feed(self, data):
        text = self.decoder.decode(data)
        if not self.sse:
            self.parser.feed(text)
            return
        for char in text:
            if char == "\n" and self.last_cr:
                self.last_cr = False
                continue
            self.last_cr = char == "\r"
            if char in "\r\n":
                if self.data:
                    if self.done.strip() == "[DONE]":
                        self.observation.terminal = True
                    self.parser.feed("\n")
                elif not self.prefix:
                    self.parser = JsonProjection(self.observation.value)
                self.prefix = ""
                self.data = False
                self.first = False
                self.done = ""
            elif self.data:
                if not self.first:
                    self.first = True
                    if char == " ":
                        continue
                if len(self.done) < 7:
                    self.done += char
                self.parser.feed(char)
            elif len(self.prefix) < 6:
                self.prefix += char
                if self.prefix == "data:":
                    self.data = True

    def eof(self):
        self.feed(b"\n" if self.sse else b" ")
