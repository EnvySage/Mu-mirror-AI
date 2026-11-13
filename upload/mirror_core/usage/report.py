"""Thread-safe receipt container shared only within one RPC invocation."""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock
import json

_current = ContextVar("mirror_usage_collector", default=None)
MAX_BYTES = 48 * 1024
MAX_ATTEMPTS = 100


def current_collector():
    return _current.get()


@contextmanager
def bind_collector(collector):
    token = _current.set(collector)
    try:
        yield collector
    finally:
        _current.reset(token)


class UsageCollector:
    def __init__(self, invocation_id):
        self.invocation_id = invocation_id
        self.lock = RLock()
        self.attempts = []
        self.complete = True
        self.sealed = False

    def begin(self, protocol, provider, model):
        from .normalize import Observation
        with self.lock:
            if self.sealed or len(self.attempts) >= MAX_ATTEMPTS:
                self.complete = False
                return None
            observation = Observation(self, protocol, provider, model)
            self.attempts.append(observation)
            return observation

    def finish(self, *, cancelled=False):
        with self.lock:
            for attempt in self.attempts:
                if not attempt.finished:
                    attempt.finish("cancelled" if cancelled else "error")
                    self.complete = False
            self.sealed = True

    def to_bytes(self):
        with self.lock:
            report = {"version": 1, "invocation_id": self.invocation_id,
                      "complete": self.complete and all(a.finished for a in self.attempts), "attempts": []}
            for attempt in self.attempts:
                report["attempts"].append(attempt.receipt())
                if len(_encode(report)) > MAX_BYTES:
                    report["attempts"].pop()
                    report["complete"] = False
                    break
            return _encode(report)


def _encode(report):
    return json.dumps(report, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
