"""Normalize only confirmed token and decimal consumption fields."""
from decimal import Decimal, InvalidOperation
import time
from uuid import uuid4

MAX_INT = 10**12
ALIASES = (("deepseek", "deepseek"), ("gemini", "gemini"), ("claude", "anthropic"),
           ("anthropic", "anthropic"), ("gpt", "openai"), ("grok", "grok"), ("glm", "zhipu"),
           ("zhipu", "zhipu"), ("kimi", "moonshot"), ("moonshot", "moonshot"), ("qwen", "qwen"))


def safe_name(value, maximum=256):
    # B validates Java String.length(), so bound UTF-16 units, not code points.
    # Gateway aliases may legitimately contain brackets, symbols or punctuation.
    return value if (isinstance(value, str) and value and value.isprintable()
                     and len(value.encode("utf-16-le")) // 2 <= maximum) else None


def token(value):
    return value if type(value) is int and 0 <= value <= MAX_INT else None


class Observation:
    def __init__(self, collector, protocol, provider, model):
        self.collector = collector
        self.protocol = protocol
        self.provider = safe_name(provider, 128) or "unknown"
        self.model = safe_name(model) or "unknown"
        self.id = str(uuid4())
        self.started = time.monotonic()
        self.latency = None
        self.http_status = None
        self.values = {}
        self.finished = False
        self.terminal = False
        self.streaming = False
        self.error_event = False
        self.status = "error"
        self.generation_revision = 0
        self.usage_revision = -1
        self.event_generation = False
        self.event_usage = False

    def output_value(self, path):
        """Observe presence of generated strings, never their text."""
        if ((path[:3] == ("choices", "[]", "delta") and path[3:] != ("role",))
                or path in {("content_block", "text"), ("message", "content", "[]", "text")}):
            with self.collector.lock:
                if not self.collector.sealed:
                    self.event_generation = True

    def end_event(self, event_name):
        """An event's usage covers that event regardless of JSON property order."""
        with self.collector.lock:
            if self.collector.sealed:
                return
            self.terminal = self.terminal or event_name == "message_stop"
            self.error_event = self.error_event or event_name == "error"
            if self.event_generation or event_name == "content_block_delta":
                self.generation_revision += 1
            if self.event_usage:
                self.usage_revision = self.generation_revision
            self.event_generation = False
            self.event_usage = False

    def value(self, path, value):
        with self.collector.lock:
            if self.collector.sealed:
                return
            if path in {("model",), ("message", "model")}:
                self.model = safe_name(value) or self.model
            elif path == ("type",):
                self.terminal = self.terminal or value == "message_stop"
                self.error_event = self.error_event or value == "error"
                self.event_generation = self.event_generation or value == "content_block_delta"
            else:
                key = path[2:] if path[:2] == ("message", "usage") else path[1:]
                if key in {("input_tokens",), ("output_tokens",), ("total_tokens",),
                           ("prompt_tokens",), ("completion_tokens",), ("cache_read_input_tokens",),
                           ("cache_creation_input_tokens",), ("prompt_tokens_details", "cached_tokens"),
                           ("completion_tokens_details", "reasoning_tokens")}:
                    count = token(value)
                    if count is not None:
                        self.values[key] = count
                        self.event_usage = self.event_usage or key in {
                            ("output_tokens",), ("completion_tokens",), ("total_tokens",)}
                elif key in {("credits_consumed",), ("consumed_credits",), ("points_consumed",), ("consumed_points",)}:
                    # credit / quota / balance have no confirmed consumption semantics.
                    if isinstance(value, (str, int, float, Decimal)) and not isinstance(value, bool):
                        try:
                            amount = Decimal(str(value))
                            if amount.is_finite() and amount >= 0 and amount.adjusted() < 26 and amount.as_tuple().exponent >= -12:
                                self.values[("credits",)] = format(amount.copy_abs(), "f")
                                self.event_usage = True
                        except InvalidOperation:
                            pass
                elif key == ("credit_unit",) and safe_name(value, 32):
                    self.values[key] = value

    def finish(self, status, *, force=False):
        with self.collector.lock:
            if not self.collector.sealed and (not self.finished or force):
                self.finished = True
                self.status = "error" if self.error_event else status
                self.latency = min(MAX_INT, max(0, int((time.monotonic() - self.started) * 1000)))

    def receipt(self):
        v = self.values
        if self.protocol == "anthropic":
            input_count = v.get(("input_tokens",))
            if input_count is not None:
                input_count += v.get(("cache_read_input_tokens",), 0) + v.get(("cache_creation_input_tokens",), 0)
            output = v.get(("output_tokens",))
        else:
            input_count = v.get(("prompt_tokens",), v.get(("input_tokens",)))
            output = v.get(("completion_tokens",), v.get(("output_tokens",)))
        total = v.get(("total_tokens",))
        if total is None and input_count is not None and output is not None:
            total = input_count + output
        credits = v.get(("credits",))
        known = any(x is not None for x in (input_count, output, total, credits))
        complete = (self.finished and self.status == "success" and (not self.streaming or (
                        self.terminal and self.usage_revision == self.generation_revision))
                    and (total is not None or credits is not None))
        provider = next((p for alias, p in ALIASES if alias in (self.model or "").lower()), self.provider)
        receipt = {"id": self.id, "status": self.status, "provider": provider, "model": self.model,
                   "input_tokens": token(input_count), "output_tokens": token(output), "total_tokens": token(total),
                   "credits": credits, "credit_unit": v.get(("credit_unit",), "积分") if credits is not None else None,
                   "usage_state": "complete" if complete else "partial" if known else "unknown",
                   "latency_ms": self.latency, "http_status": self.http_status}
        for target, key in (("cache_read_tokens", ("cache_read_input_tokens",)),
                            ("cache_creation_tokens", ("cache_creation_input_tokens",)),
                            ("cache_read_tokens", ("prompt_tokens_details", "cached_tokens")),
                            ("reasoning_tokens", ("completion_tokens_details", "reasoning_tokens"))):
            if key in v:
                receipt[target] = v[key]
        return receipt
