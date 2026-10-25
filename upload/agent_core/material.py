"""Task-specific material requirements; metadata never stands in for body text."""
from dataclasses import dataclass, replace

from agent_core.types import AgentState, Evidence


BODY_CONTEXT_CHARS = 64_000
MAX_BODY_PAGES = 12


@dataclass(frozen=True)
class MaterialDecision:
    status: str
    note: str = ""
    tool: str = ""
    args: dict | None = None


def has_material(state: AgentState) -> bool:
    return any(item.text.strip() for item in state.evidence) or any(
        item.success and (item.has_material or item.data is not None) for item in state.observations
    )


def assess_material(state: AgentState) -> MaterialDecision:
    if not state.intent or state.intent.file_query_mode != "body":
        # Explicit record reads also need contiguous authorized fragments.
        record_pages = [item for item in state.observations if item.tool == "read_record"]
        if record_pages:
            return _body_decision(state, "read_record", "record_id", record_pages)
        if has_material(state):
            return MaterialDecision("sufficient")
        failures = [item for item in state.observations if not item.success]
        if failures:
            return MaterialDecision("missing", "本轮工具查询未能完成，暂时无法核对所需资料；这不表示资料不存在。")
        return MaterialDecision("missing")

    recalls = [item for item in state.observations if item.tool == "recall_item"]
    if recalls:
        return _body_decision(state, "recall_item", "vault_item_id", recalls)
    candidates = []
    for observation in reversed(state.observations):
        if observation.tool == "find_item" and observation.success and isinstance(observation.data, dict):
            candidates = observation.data.get("items", [])
            break
    if len(candidates) > 1:
        names = "、".join(f"“{item.get('original_name', '未命名文件')}”" for item in candidates[:4])
        return MaterialDecision("ambiguous", f"找到了多个候选文件：{names}。你想分析哪一篇？")
    if len(candidates) == 1:
        item = candidates[0]
        if item.get("digest_status") != "confirmed":
            return MaterialDecision("missing", "已经找到文件，但正文尚未确认，暂时不能分析正文。请先确认文件内容。")
        return MaterialDecision("read", tool="recall_item", args={"vault_item_id": item["vault_item_id"]})
    return MaterialDecision("missing", "还没有定位到可读取正文的文章。请提供文章名称，或明确要分析哪一篇。")


def _target_id(observation, key):
    data = observation.data if isinstance(observation.data, dict) else {}
    item = data.get("item", {})
    return data.get(key) or (item.get(key) if isinstance(item, dict) else None) or data.get("requested_args", {}).get(key)


def _body_decision(state, tool, key, observations):
    targets = list(dict.fromkeys(_target_id(item, key) for item in observations if _target_id(item, key)))
    if not targets:
        return MaterialDecision("missing", "正文读取没有返回有效的文章目标，暂时不能分析正文。")
    body_chars = sum(len(item.text) for item in state.evidence if item.origin in {"recall_item", "read_record"})
    for target in targets:
        selected = [item for item in observations if _target_id(item, key) == target]
        pages = {}
        for observation in selected:
            data = observation.data if isinstance(observation.data, dict) else {}
            if observation.success and data.get("content_scope") in {"full", "partial"}:
                pages[data.get("offset", 0)] = observation
        offset = 0
        visited = set()
        while offset in pages and offset not in visited:
            visited.add(offset)
            page = pages[offset]
            if not page.data.get("body_available"):
                return MaterialDecision("missing", "文章已定位，但正文尚未确认或没有可读取内容，暂时不能分析正文。")
            if page.data.get("complete") is True:
                if not any(item.evidence for item in selected):
                    return MaterialDecision("missing", "文章已定位，但没有返回可读取的正文内容。")
                break
            next_offset = page.data.get("next_offset")
            if not isinstance(next_offset, int) or isinstance(next_offset, bool) or next_offset <= offset:
                return MaterialDecision("partial", "正文读取不完整，以下分析仅依据已读取片段，不能代表全文。")
            offset = next_offset
        else:
            failed = [item for item in selected if not item.success]
            if failed:
                if body_chars:
                    return MaterialDecision("partial", "后续正文读取失败，以下分析仅依据已读取片段，不能代表全文。")
                return MaterialDecision("missing", "文章已定位，但正文读取失败，暂时无法进行正文分析；这不表示文章不存在。")
            if body_chars >= BODY_CONTEXT_CHARS or len(observations) >= MAX_BODY_PAGES:
                return MaterialDecision("partial", "文章较长，本轮只读取了部分正文；以下分析仅依据已读取片段，不能代表全文。")
            # A vector excerpt does not establish whole-document coverage.
            return MaterialDecision("read", tool=tool, args={key: target, **({"offset": offset} if offset else {})})
    return MaterialDecision("sufficient")


def answer_evidence(evidence: list[Evidence]) -> tuple[list[Evidence], bool]:
    """Bound the model context, returning an explicit coverage downgrade if clipped."""
    remaining = BODY_CONTEXT_CHARS
    selected = []
    clipped = False
    for item in evidence:
        if len(item.text) > remaining:
            clipped = True
            if remaining:
                selected.append(replace(item, text=item.text[:remaining] + "\n[本轮上下文在此截断]"))
            remaining = 0
        else:
            selected.append(item)
            remaining -= len(item.text)
    return selected, clipped
