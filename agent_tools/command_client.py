"""Named Java command client with receipt lookup for uncertain POST outcomes."""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID, uuid5


CREATE_RECORD_SLOT = "create_record:0"
MAX_CONTENT_UTF16_UNITS = 2000
POST_TIMEOUT_SECONDS = 5.0
RECEIPT_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class CommandResult:
    outcome: str
    code: str
    call_id: str
    record_id: int | None = None
    record_status: str = ""
    created_at: str = ""
    replayed: bool = False

    @property
    def succeeded(self) -> bool:
        return self.outcome == "success"


class CommandClient:
    """POST once; after uncertain delivery, query the exact same call receipt once."""

    def __init__(
        self,
        base_url: str,
        *,
        request: Callable[[str, str, Mapping[str, str], bytes | None, float], tuple[int, bytes]] | None = None,
        post_timeout: float = POST_TIMEOUT_SECONDS,
        receipt_timeout: float = RECEIPT_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        parsed = urlsplit(base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("command base URL must be an absolute HTTP(S) URL")
        self._base_url = base_url.rstrip("/")
        self._request = request or _url_request
        self._post_timeout = min(POST_TIMEOUT_SECONDS, max(0.001, float(post_timeout)))
        self._receipt_timeout = min(RECEIPT_TIMEOUT_SECONDS, max(0.001, float(receipt_timeout)))
        self._clock = clock

    def create_record(self, runtime: Any, content: str) -> CommandResult:
        """Submit exactly the Java-authorized original content using its fixed call slot."""
        run_id = getattr(runtime, "run_id", "")
        grant = getattr(runtime, "command_grant", None)
        authorized_content = getattr(runtime, "authorized_record_content", None)
        allowed = getattr(runtime, "allowed_write_tools", ()) or ()
        call_id = _call_id(run_id)
        if (
            not isinstance(grant, str) or not grant.strip()
            or not isinstance(authorized_content, str)
            or "create_record" not in allowed
        ):
            return CommandResult("failed", "COMMAND_NOT_AUTHORIZED", call_id)
        if not isinstance(content, str) or content != authorized_content:
            return CommandResult("failed", "AUTHORIZED_CONTENT_MISMATCH", call_id)
        if not content.strip():
            return CommandResult("failed", "EMPTY_CONTENT", call_id)
        if _utf16_units(content) > MAX_CONTENT_UTF16_UNITS:
            return CommandResult("failed", "CONTENT_TOO_LONG", call_id)

        remaining = _remaining_seconds(runtime, self._clock)
        if remaining <= 0:
            return CommandResult("failed", "TOTAL_DEADLINE", call_id)
        headers = {
            "Content-Type": "application/json",
            "X-Mirror-Command-Grant": grant,
        }
        payload = json.dumps(
            {"run_id": str(run_id), "call_id": call_id, "content": content},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        endpoint = f"{self._base_url}/api/internal/agent/commands/create-record"
        try:
            status, body = self._request(
                "POST", endpoint, headers, payload, min(self._post_timeout, remaining),
            )
        except (OSError, TimeoutError, socket.timeout, URLError):
            return self._lookup_receipt(run_id, call_id, grant, headers, runtime)
        except Exception:
            return self._lookup_receipt(run_id, call_id, grant, headers, runtime)

        if 200 <= status < 300:
            result = _parse_success(body, call_id)
            if result is not None:
                return result
            return self._lookup_receipt(run_id, call_id, grant, headers, runtime)
        if 400 <= status < 500:
            return CommandResult("failed", _response_code(body, f"HTTP_{status}"), call_id)
        return self._lookup_receipt(run_id, call_id, grant, headers, runtime)

    def _lookup_receipt(self, run_id: str, call_id: str, grant: str,
                        headers: Mapping[str, str], runtime: Any) -> CommandResult:
        remaining = _remaining_seconds(runtime, self._clock)
        if remaining <= 0:
            return CommandResult("unknown", "RECEIPT_LOOKUP_DEADLINE", call_id)
        endpoint = f"{self._base_url}/api/internal/agent/commands/receipts/{call_id}"
        try:
            status, body = self._request(
                "GET", endpoint, headers, None, min(self._receipt_timeout, remaining),
            )
        except (OSError, TimeoutError, socket.timeout, URLError):
            return CommandResult("unknown", "RECEIPT_LOOKUP_UNAVAILABLE", call_id)
        except Exception:
            return CommandResult("unknown", "RECEIPT_LOOKUP_UNAVAILABLE", call_id)
        if 200 <= status < 300:
            result = _parse_success(body, call_id)
            if result is not None:
                return CommandResult(
                    "success", "OK", call_id, result.record_id, result.record_status,
                    result.created_at, True,
                )
        return CommandResult("unknown", "ACTION_RESULT_UNKNOWN", call_id)


def _call_id(run_id: Any) -> str:
    try:
        return str(uuid5(UUID(str(run_id)), CREATE_RECORD_SLOT))
    except (ValueError, TypeError, AttributeError):
        return ""


def _utf16_units(content: str) -> int:
    return len(content.encode("utf-16-le")) // 2


def _remaining_seconds(runtime: Any, clock: Callable[[], float]) -> float:
    deadline = getattr(runtime, "total_deadline", None) or getattr(runtime, "deadline", None)
    if deadline is None:
        return POST_TIMEOUT_SECONDS
    try:
        return float(deadline) - clock()
    except (TypeError, ValueError):
        return 0.0


def _parse_success(body: bytes, expected_call_id: str) -> CommandResult | None:
    try:
        payload = json.loads(body.decode("utf-8"))
        call_id = payload.get("call_id")
        record = payload.get("record")
        if (
            call_id != expected_call_id
            or payload.get("status") != "succeeded"
            or not isinstance(record, dict)
            or isinstance(record.get("id"), bool)
            or not isinstance(record.get("id"), int)
            or record["id"] <= 0
            or record.get("status") != "record_only"
            or not isinstance(record.get("created_at"), str)
            or not record["created_at"]
        ):
            return None
        return CommandResult(
            "success", "OK", expected_call_id, record["id"], record["status"],
            record["created_at"], bool(payload.get("replayed", False)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return None


def _response_code(body: bytes, fallback: str) -> str:
    try:
        code = json.loads(body.decode("utf-8")).get("code")
        if isinstance(code, str) and code and len(code) <= 64:
            return code
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError):
        pass
    return fallback


def _url_request(method: str, url: str, headers: Mapping[str, str],
                 body: bytes | None, timeout: float) -> tuple[int, bytes]:
    request = Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except HTTPError as response:
        return int(response.code), response.read()


__all__ = ["CommandClient", "CommandResult", "CREATE_RECORD_SLOT"]
