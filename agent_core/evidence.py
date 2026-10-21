"""Evidence identity, deduplication, citations, and compact tool observations."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Iterable

from agent_core.types import Evidence, ToolObservation


_CITATION = re.compile(r"(?<!\\)\[(\d+)\](?!\()")
_VAULT_CITATION = re.compile(r"(?<!\\)\[F(\d+)\](?!\()", re.IGNORECASE)
TRUNCATED = "…[truncated]"


@dataclass(frozen=True, slots=True)
class CitationSource:
    record_id: int
    quote: str
    date: str | None
    n: int


@dataclass(frozen=True, slots=True)
class VaultReference:
    vault_item_id: int
    n: int
    quote: str
    date: str | None


@dataclass(frozen=True, slots=True)
class CitationIndex:
    """Frozen citation assignment for one generated response."""

    by_evidence_id: dict[str, str]
    diary: tuple[Evidence, ...]
    vault_items: tuple[Evidence, ...]

    def marker_for(self, evidence_id: str) -> str | None:
        return self.by_evidence_id.get(evidence_id)

    def source_for(self, marker: str) -> CitationSource | None:
        match = re.fullmatch(r"\[(\d+)\]", marker.strip())
        if not match:
            return None
        number = int(match.group(1))
        if not 1 <= number <= len(self.diary):
            return None
        evidence = self.diary[number - 1]
        if evidence.record_id is None:
            return None
        return CitationSource(
            record_id=evidence.record_id,
            quote=evidence.text[:100],
            date=evidence.date,
            n=number,
        )

    def sources_for(self, answer: str) -> list[CitationSource]:
        out: list[CitationSource] = []
        seen: set[int] = set()
        for match in _CITATION.finditer(answer or ""):
            number = int(match.group(1))
            if number in seen:
                continue
            source = self.source_for(f"[{number}]")
            if source is not None:
                out.append(source)
                seen.add(number)
        return out

    def vault_refs_for(self, answer: str) -> list[VaultReference]:
        out: list[VaultReference] = []
        seen: set[int] = set()
        for match in _VAULT_CITATION.finditer(answer or ""):
            number = int(match.group(1))
            if not 1 <= number <= len(self.vault_items) or number in seen:
                continue
            evidence = self.vault_items[number - 1]
            if evidence.vault_item_id is not None:
                out.append(VaultReference(evidence.vault_item_id, number, evidence.text[:100], evidence.date))
                seen.add(number)
        return out


def merge_evidence(existing: Iterable[Evidence], incoming: Iterable[Evidence]) -> list[Evidence]:
    """Merge evidence in first-seen order, deduping diary chunks and vault snippets."""
    merged: list[Evidence] = []
    positions: dict[tuple[str, Any], int] = {}
    for evidence in (*tuple(existing), *tuple(incoming)):
        if not isinstance(evidence, Evidence):
            continue
        key = _identity(evidence)
        position = positions.get(key)
        if position is None:
            positions[key] = len(merged)
            merged.append(evidence)
        elif not merged[position].text and evidence.text:
            merged[position] = evidence
    return merged


def assign_citations(evidence: Iterable[Evidence]) -> CitationIndex:
    """Assign diary [n] and per-file [Fn] markers once, preserving evidence order."""
    unique = merge_evidence((), evidence)
    diary = tuple(item for item in unique if item.kind == "diary" and item.chunk_id is not None)
    vault_items: list[Evidence] = []
    vault_number_by_id: dict[int, int] = {}
    markers: dict[str, str] = {}
    diary_number_by_id = {item.evidence_id: index for index, item in enumerate(diary, start=1)}
    for item in unique:
        if item.kind == "diary" and item.chunk_id is not None:
            markers[item.evidence_id] = f"[{diary_number_by_id[item.evidence_id]}]"
        elif item.kind == "vault" and item.vault_item_id is not None:
            number = vault_number_by_id.get(item.vault_item_id)
            if number is None:
                vault_items.append(item)
                number = len(vault_items)
                vault_number_by_id[item.vault_item_id] = number
            markers[item.evidence_id] = f"[F{number}]"
    return CitationIndex(markers, diary, tuple(vault_items))


def render_observations(observations: Iterable[ToolObservation], max_chars: int = 2000) -> str:
    """Render details planners need; truncate safely and disclose the truncation."""
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
    blocks = []
    for observation in observations:
        if not isinstance(observation, ToolObservation):
            continue
        header = f"{observation.tool} [{observation.code}]"
        if not observation.success:
            block = f"{header}: {observation.summary}"
        else:
            details = _json(observation.data)
            evidence = "\n".join(
                f"{item.evidence_id}: {item.date or ''} {item.text}" for item in observation.evidence
            )
            body = "\n".join(part for part in (observation.summary, details, evidence) if part)
            block = f"{header}: {body}"
        if len(block) > max_chars:
            if max_chars <= len(TRUNCATED):
                block = TRUNCATED[:max_chars]
            else:
                block = block[:max_chars - len(TRUNCATED)] + TRUNCATED
        blocks.append(block)
    return "\n\n".join(blocks)


def render_evidence(evidence: Iterable[Evidence], citation_index: CitationIndex | None = None) -> str:
    evidence = tuple(evidence)
    index = citation_index or assign_citations(evidence)
    rows = []
    for item in evidence:
        marker = index.marker_for(item.evidence_id)
        label = marker or "[背景画像]"
        metadata = []
        if item.date:
            metadata.append(item.date)
        if item.distance is not None:
            metadata.append(f"distance={item.distance:.4f}")
        if item.ranking_score is not None:
            metadata.append(f"ranking={item.ranking_score:.4f}")
        suffix = f" ({', '.join(metadata)})" if metadata else ""
        rows.append(f"{label}{suffix} {item.text}")
    return "\n".join(rows) or "（没有可用的检索证据）"


def _identity(evidence: Evidence) -> tuple[str, Any]:
    if evidence.kind == "diary" and evidence.chunk_id is not None:
        return "diary_chunk", evidence.chunk_id
    if evidence.kind == "vault" and evidence.vault_item_id is not None:
        return "vault_chunk", (evidence.vault_item_id, evidence.chunk_id) if evidence.chunk_id is not None else (evidence.vault_item_id, None)
    return evidence.kind, evidence.evidence_id


def _json(value: Any) -> str:
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, default=_json_default, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return vars(value)
    return str(value)
