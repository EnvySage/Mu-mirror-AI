import base64
import json
from datetime import date

import grpc
import pytest

from generated import common_pb2, ledger_pb2, ledger_pb2_grpc
from mirror_core.ledger_receipt import recognize_receipt, validate_image
from server import build_legacy_server


def request(protocol=common_pb2.AiProtocol.OPENAI):
    return ledger_pb2.LedgerImageRequest(
        image=b"synthetic-receipt",
        mime_type="image/png",
        today="2026-11-08",
        llm_config=common_pb2.LlmConfig(protocol=protocol),
    )


def result_for(**flow_changes):
    flow = {
        "kind": "EXPENSE", "amount": "38.5", "categoryId": "food",
        "occurredOn": "2026-11-08", "note": "晚餐",
    }
    return {"status": "BILL", "title": "晚餐", "flows": [{**flow, **flow_changes}]}


@pytest.mark.parametrize("protocol", [common_pb2.AiProtocol.OPENAI, common_pb2.AiProtocol.ANTHROPIC])
def test_receipt_rpc_sends_image_in_configured_provider_shape(monkeypatch, protocol):
    req = request(protocol)

    class Model:
        def json_task(self, messages, **kwargs):
            assert kwargs["max_retries"] == 0
            assert "不能把合计和明细一起算" in messages[0]["content"]
            image = messages[1]["content"][1]
            encoded = base64.b64encode(req.image).decode("ascii")
            if protocol == common_pb2.AiProtocol.ANTHROPIC:
                assert image == {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png", "data": encoded,
                }}
            else:
                assert image == {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}
            return json.dumps(result_for(), ensure_ascii=False)

    monkeypatch.setattr("services.ledger_vision_service.create_llm", lambda **config: Model())
    server, port = build_legacy_server(port=0)
    server.start()
    try:
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            response = ledger_pb2_grpc.LedgerVisionStub(channel).Recognize(req, timeout=3)
    finally:
        server.stop(0).wait(3)

    assert response.status == "BILL"
    assert json.loads(response.draft_json)["flows"] == [{
        "kind": "EXPENSE", "amount": "38.50", "categoryId": "food",
        "occurredOn": "2026-11-08", "note": "晚餐",
    }]


@pytest.mark.parametrize("flow,expected_amount,expected_date", [
    ({"occurredOn": "2026-11-07"}, "38.50", "2026-11-07"),
    ({"occurredOn": None}, "38.50", None),
    ({"amount": "38.507"}, None, "2026-11-08"),
    ({"kind": "TRANSFER"}, "38.50", "2026-11-08"),
])
def test_uncertain_or_past_transactions_require_review(flow, expected_amount, expected_date):
    class Model:
        def json_task(self, *args, **kwargs):
            return json.dumps(result_for(**flow))

    response = recognize_receipt(request(), Model(), date(2026, 11, 8), timeout=3)
    assert response.status == "NEEDS_REVIEW"
    parsed = json.loads(response.draft_json)["flows"][0]
    assert parsed["amount"] == expected_amount
    assert parsed["occurredOn"] == expected_date


def test_invalid_image_and_date_fail_before_model_call():
    with pytest.raises(ValueError, match="请选择"):
        validate_image(ledger_pb2.LedgerImageRequest(image=b"file", mime_type="text/plain"))
    invalid = request()
    invalid.today = "20261108"
    with pytest.raises(ValueError, match="记账日期"):
        validate_image(invalid)
