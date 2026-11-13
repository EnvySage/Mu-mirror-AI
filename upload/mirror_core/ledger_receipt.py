"""Parse a receipt image into a bounded, editable ledger proposal."""

from __future__ import annotations

import base64
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from generated import common_pb2, ledger_pb2
from llm.base import BaseLlm
from mirror_core.errors import ContentInvalidError
from mirror_core.llm_json import parse_json


MAX_IMAGE_BYTES = 3 * 1024 * 1024
_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_FLOW_KINDS = {"EXPENSE", "INCOME", "RECOVERY"}
_CATEGORY_IDS = {"food", "transport", "study", "work", "life", "home", "health", "fun", "salary", "other"}
_AMOUNT = re.compile(r"[0-9]{1,10}(?:\.[0-9]{1,2})?")
_MAX_FLOWS = 20
_PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "ledger_receipt.txt"
_REVIEW_MESSAGE = "部分账目信息无法确定，请核对日期、金额和类型。"


def validate_image(request: ledger_pb2.LedgerImageRequest) -> date:
    if request.mime_type not in _MEDIA_TYPES:
        raise ValueError("请选择 PNG、JPEG、WebP 或 GIF 图片")
    if not 0 < len(request.image) <= MAX_IMAGE_BYTES:
        raise ValueError("请上传不超过 3 MB 的账单图片")
    today = _date(request.today)
    if today is None:
        raise ValueError("缺少有效的记账日期")
    return today


def recognize_receipt(
    request: ledger_pb2.LedgerImageRequest, llm: BaseLlm, today: date, *, timeout: float,
) -> ledger_pb2.LedgerImageResponse:
    encoded = base64.b64encode(request.image).decode("ascii")
    if request.llm_config.protocol == common_pb2.AiProtocol.ANTHROPIC:
        image = {"type": "image", "source": {
            "type": "base64", "media_type": request.mime_type, "data": encoded,
        }}
    else:
        image = {"type": "image_url", "image_url": {"url": f"data:{request.mime_type};base64,{encoded}"}}
    messages = [
        {"role": "system", "content": _PROMPT.read_text(encoding="utf-8")},
        {"role": "user", "content": [
            {"type": "text", "text": f"参考日期 {today.isoformat()}（Asia/Shanghai）。"},
            image,
        ]},
    ]
    raw = llm.json_task(messages, temperature=0, timeout=timeout, max_retries=0, max_tokens=2400)
    try:
        payload = parse_json(raw, ctx="账单识别") if isinstance(raw, str) and len(raw) <= 24_000 else None
    except ContentInvalidError:
        payload = None
    return _draft_response(payload, today)


def _draft_response(payload: object, today: date) -> ledger_pb2.LedgerImageResponse:
    data = payload if isinstance(payload, dict) else {}
    status = data.get("status")
    if status == "NOT_BILL":
        return ledger_pb2.LedgerImageResponse(status="NOT_BILL", message="这张图片看起来不是支付凭证。")

    source_flows = data.get("flows")
    if not isinstance(source_flows, list):
        source_flows = []
    flows = []
    needs_review = status != "BILL" or not source_flows or len(source_flows) > _MAX_FLOWS
    for source in source_flows[:_MAX_FLOWS]:
        if not isinstance(source, dict):
            needs_review = True
            continue
        occurred_on = _date(source.get("occurredOn"))
        kind = _option(source.get("kind"), _FLOW_KINDS)
        amount = _amount(source.get("amount"))
        if occurred_on != today or kind is None or amount is None:
            needs_review = True
        flows.append({
            "kind": kind,
            "amount": amount,
            "categoryId": _option(source.get("categoryId"), _CATEGORY_IDS),
            "occurredOn": occurred_on.isoformat() if occurred_on else None,
            "note": _text(source.get("note"), 200),
        })
    draft = {
        "title": _text(data.get("title"), 80),
        "description": _text(data.get("description"), 300),
        "flows": flows,
        "diaryMode": "NONE",
    }
    return ledger_pb2.LedgerImageResponse(
        status="NEEDS_REVIEW" if needs_review else "BILL",
        draft_json=json.dumps(draft, ensure_ascii=False),
        message=_REVIEW_MESSAGE if needs_review else "已识别到支付凭证，请确认后入账。",
    )


def _amount(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    if not _AMOUNT.fullmatch(text):
        return None
    amount = Decimal(text)
    return f"{amount:.2f}" if amount > 0 else None


def _date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if value == parsed.isoformat() else None


def _option(value: object, allowed: set[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _text(value: object, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""
