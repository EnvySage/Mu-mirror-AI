import json
from types import SimpleNamespace
from uuid import UUID, uuid5

from agent_tools.command_client import CommandClient, CommandResult


RUN_ID = "b7695748-5311-4ecb-8dab-5d0c244f1a63"
CONTENT = "今天完成了设计。"
CREATED_AT = "2026-10-20T12:00:00+08:00"


def _runtime(content=CONTENT):
    return SimpleNamespace(
        run_id=RUN_ID,
        command_grant="opaque-java-grant",
        authorized_record_content=content,
        allowed_write_tools=("create_record",),
        total_deadline=None,
        deadline=None,
    )


def _success(call_id, *, replayed=False):
    return 200, json.dumps({
        "call_id": call_id,
        "status": "succeeded",
        "replayed": replayed,
        "record": {"id": 47, "status": "record_only", "created_at": CREATED_AT},
    }).encode()


def test_post_timeout_then_receipt_success_uses_one_post_and_same_fixed_call_id():
    requests = []

    def transport(method, url, headers, body, timeout):
        requests.append((method, url, headers, body, timeout))
        if method == "POST":
            raise TimeoutError("POST response lost")
        return _success(expected_call_id, replayed=True)

    expected_call_id = str(uuid5(UUID(RUN_ID), "create_record:0"))
    result = CommandClient("http://mirror-java", request=transport).create_record(_runtime(), CONTENT)

    assert result == CommandResult("success", "OK", expected_call_id, 47, "record_only", CREATED_AT, True)
    assert [item[0] for item in requests] == ["POST", "GET"]
    assert requests[0][1].endswith("/api/internal/agent/commands/create-record")
    assert requests[1][1].endswith(f"/api/internal/agent/commands/receipts/{expected_call_id}")
    assert json.loads(requests[0][3]) == {
        "run_id": RUN_ID, "call_id": expected_call_id, "content": CONTENT,
    }
    assert requests[0][2]["X-Mirror-Command-Grant"] == "opaque-java-grant"
    assert requests[0][4] <= 5
    assert requests[1][4] <= 3


def test_receipt_unavailable_after_uncertain_post_returns_unknown_without_second_post():
    methods = []

    def transport(method, url, headers, body, timeout):
        methods.append(method)
        if method == "POST":
            raise OSError("connection lost")
        raise TimeoutError("receipt endpoint unavailable")

    result = CommandClient("http://mirror-java", request=transport).create_record(_runtime(), CONTENT)

    assert result.outcome == "unknown"
    assert result.code == "RECEIPT_LOOKUP_UNAVAILABLE"
    assert methods == ["POST", "GET"]


def test_missing_receipt_returns_unknown_without_retrying_post():
    methods = []

    def transport(method, url, headers, body, timeout):
        methods.append(method)
        return (503, b'{"code":"WRITE_UNAVAILABLE"}') if method == "POST" else (404, b'{"code":"RECEIPT_NOT_FOUND"}')

    result = CommandClient("http://mirror-java", request=transport).create_record(_runtime(), CONTENT)

    assert result.outcome == "unknown"
    assert result.call_id == str(uuid5(UUID(RUN_ID), "create_record:0"))
    assert methods == ["POST", "GET"]


def test_explicit_business_rejection_returns_failed_without_receipt_lookup():
    methods = []

    def transport(method, url, headers, body, timeout):
        methods.append(method)
        return 422, b'{"code":"CONTENT_TOO_LONG"}'

    result = CommandClient("http://mirror-java", request=transport).create_record(_runtime(), CONTENT)

    assert result.outcome == "failed"
    assert result.code == "CONTENT_TOO_LONG"
    assert methods == ["POST"]


def test_successful_post_does_not_query_receipt():
    methods = []
    expected_call_id = str(uuid5(UUID(RUN_ID), "create_record:0"))

    def transport(method, url, headers, body, timeout):
        methods.append(method)
        return _success(expected_call_id)

    result = CommandClient("http://mirror-java", request=transport).create_record(_runtime(), CONTENT)

    assert result.succeeded
    assert result.record_id == 47
    assert not result.replayed
    assert methods == ["POST"]


def test_mismatched_or_overlong_content_never_calls_java():
    calls = []
    client = CommandClient("http://mirror-java", request=lambda *args: calls.append(args))

    mismatched = client.create_record(_runtime(), "模型改过的内容")
    emoji_too_long = "😀" * 1001
    overlong = client.create_record(_runtime(emoji_too_long), emoji_too_long)

    assert mismatched.outcome == "failed"
    assert mismatched.code == "AUTHORIZED_CONTENT_MISMATCH"
    assert overlong.outcome == "failed"
    assert overlong.code == "CONTENT_TOO_LONG"
    assert calls == []
