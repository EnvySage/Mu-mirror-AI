"""Daily chat summarization and user-confirmable memory suggestions."""

import json
from datetime import date, datetime

from generated import mirror_chat_pb2 as pb2
from llm.factory import create_llm
from mirror_core.llm_json import parse_json
from mirror_core.prompts_loader import loader

_MEMORY_KINDS = {"fact", "preference", "plan", "event"}
_MAX_CANDIDATES = 3
_MAX_CANDIDATE_CONTENT = 500


def _optional_date(value: object) -> str:
    """Keep only explicit ISO dates; never invent or normalize guessed values."""
    if not isinstance(value, str) or not value.strip():
        return ""
    candidate = value.strip()
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        try:
            return datetime.fromisoformat(candidate.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return ""


class ChatMemoryService:
    """Stateless processor; activating suggestions belongs to Java/user action."""

    def __init__(self, llm_factory=create_llm, prompt_loader=loader):
        self._llm_factory = llm_factory
        self._prompt_loader = prompt_loader

    def summarize_day(self, request, context=None):
        source_ids = {int(message.message_id) for message in request.messages if message.message_id > 0}
        if not request.messages:
            return pb2.SummarizeDayResponse(source_hash=request.source_hash)

        message_rows = [
            {"message_id": int(message.message_id), "created_at": message.created_at,
             "content": message.content}
            for message in request.messages
        ]
        prompt = self._prompt_loader.render(
            "chat_day_summary",
            source_date=request.source_date,
            timezone=request.timezone,
            source_hash=request.source_hash,
            messages=json.dumps(message_rows, ensure_ascii=False),
        )
        config = request.llm_config
        llm = self._llm_factory(
            provider=config.provider,
            api_key=config.api_key,
            base_url=config.base_url,
            model=config.model,
            protocol=config.protocol,
        )
        text = llm.json_task([{"role": "user", "content": prompt}])
        result = parse_json(text, ctx="每日聊天总结")

        summary = result.get("summary")
        if not isinstance(summary, str):
            summary = ""
        candidates = []
        raw_candidates = result.get("memory_candidates")
        if isinstance(raw_candidates, list):
            for item in raw_candidates:
                if len(candidates) >= _MAX_CANDIDATES:
                    break
                candidate = self._validated_candidate(item, source_ids)
                if candidate is not None:
                    candidates.append(candidate)

        return pb2.SummarizeDayResponse(
            summary=summary.strip(),
            memory_candidates=candidates,
            # Never let the model replace source identity used for idempotency.
            source_hash=request.source_hash,
        )

    @staticmethod
    def _validated_candidate(item, source_ids):
        if not isinstance(item, dict):
            return None
        content = item.get("content")
        kind = item.get("kind")
        refs = item.get("source_message_ids")
        if not isinstance(content, str) or not content.strip() or not isinstance(kind, str):
            return None
        if kind.strip().lower() not in _MEMORY_KINDS or not isinstance(refs, list) or not refs:
            return None
        if any(not isinstance(ref, int) or isinstance(ref, bool) for ref in refs):
            return None
        parsed_refs = refs
        if any(ref not in source_ids for ref in parsed_refs):
            return None
        parsed_refs = list(dict.fromkeys(parsed_refs))
        fields = {
            "content": content.strip()[:_MAX_CANDIDATE_CONTENT],
            "kind": kind.strip().lower(),
            "source_message_ids": parsed_refs,
        }
        event_date = _optional_date(item.get("event_date"))
        expires_at = _optional_date(item.get("expires_at"))
        if event_date:
            fields["event_date"] = event_date
        if expires_at:
            fields["expires_at"] = expires_at
        return pb2.MemoryCandidate(**fields)
