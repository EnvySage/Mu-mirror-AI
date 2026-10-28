"""Bounded single-card readings. Materials are data, never tool instructions."""
import json
import re
from pathlib import Path
from generated import tarot_pb2 as pb
from mirror_core.llm_json import parse_json

_PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "tarot.txt"


def build_tarot_messages(request):
    if len(request.question) > 500 or not request.card.id or not request.card.name:
        raise ValueError("无效的问题或牌面")
    if len(request.materials) > 8 or sum(len(m.excerpt) for m in request.materials) > 3900:
        raise ValueError("参考资料过多")
    refs = [m.ref for m in request.materials]
    if len(set(refs)) != len(refs) or any(not re.fullmatch(r"[DTM][1-9]\d*", ref) for ref in refs):
        raise ValueError("参考引用编号不正确")
    data = {"question": request.question,
            "card": {"name": request.card.name, "meaning": request.card.meaning, "action": request.card.action},
            "reference_date": request.reference_date, "timezone": request.timezone,
            "materials": [{"ref": m.ref, "kind": m.kind, "date": m.date, "title": m.title, "text": m.excerpt}
                          for m in request.materials]}
    return [{"role": "system", "content": _PROMPT.read_text(encoding="utf-8")},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)}]


def validate_tarot_answer(text, used_refs, allowed_refs):
    if not isinstance(text, str) or not text.strip() or len(text) > 600:
        raise ValueError("解读为空或过长")
    if not isinstance(used_refs, list) or any(not isinstance(r, str) for r in used_refs):
        raise ValueError("引用格式不正确")
    actual = set(re.findall(r"\[([DTM]\d+)\]", text))
    if set(used_refs) != actual or not actual.issubset(allowed_refs):
        raise ValueError("解读引用了未提供的资料")
    return pb.TarotAnalyzeResponse(text=text.strip(), used_refs=list(dict.fromkeys(used_refs)))


def analyze_reading(request, llm, *, timeout):
    messages = build_tarot_messages(request)
    raw = llm.json_task(messages, temperature=0.6, timeout=timeout, max_retries=0, max_tokens=1400)
    payload = parse_json(raw)
    if not isinstance(payload, dict):
        raise ValueError("解读格式不正确")
    return validate_tarot_answer(payload.get("text"), payload.get("used_refs"), {m.ref for m in request.materials})
