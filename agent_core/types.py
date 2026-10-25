"""Public, typed contracts shared by graph_v2 nodes and services."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable
from uuid import UUID

from agent_data.scope import ReadScope


@dataclass(frozen=True, slots=True)
class TimeExpression:
    kind: str
    date: str | None = None
    month: str | None = None
    offset: int | None = None
    days: int | None = None


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """A half-open local-time window; two missing bounds means no time filter."""

    start: datetime | None
    end: datetime | None

    def __post_init__(self) -> None:
        for bound in (self.start, self.end):
            if bound is not None and (bound.tzinfo is None or bound.utcoffset() is None):
                raise ValueError("time window bounds must be timezone-aware")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("time window start must be before end")


@dataclass(frozen=True, slots=True)
class Clarification:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    kind: str
    record_id: int | None = None
    chunk_id: int | None = None
    vault_item_id: int | None = None
    text: str = ""
    date: str | None = None
    distance: float | None = None
    origin: str | None = None
    ranking_score: float | None = None


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolObservation:
    call_id: str
    tool: str
    success: bool
    code: str
    summary: str
    data: Any = None
    evidence: tuple[Evidence, ...] = ()
    has_material: bool = False
    latency_ms: int | None = None


@dataclass(frozen=True, slots=True)
class CreatedRecord:
    record_id: int
    status: str
    created_at: str


@dataclass(frozen=True, slots=True)
class AgentEvent:
    kind: str
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IntentResult:
    action_kind: str
    route: str
    standalone_question: str
    content_type: str | None
    moods: tuple[str, ...]
    time_expression: TimeExpression
    file_query_mode: str
    validation_issues: tuple[str, ...] = ()
    time_window: TimeWindow | None = None
    prepare_tool: str | None = None
    prepare_args: dict[str, Any] | None = None
    clarification_question: str = ""


@dataclass(frozen=True, slots=True)
class PreparedContext:
    glossary_top30: tuple[Any, ...]
    glossary_matched: tuple[Any, ...]
    glossary_for_intent: tuple[Any, ...]
    matched_ids: tuple[int, ...]
    history_plan: tuple[Any, ...]
    history_answer: tuple[Any, ...]


@dataclass(slots=True)
class AgentState:
    question: str
    standalone_question: str = ""
    action_kind: str = "conversation"
    route: str = "hybrid"
    intent: IntentResult | None = None
    history_plan: list[Any] = field(default_factory=list)
    history_answer: list[Any] = field(default_factory=list)
    glossary: list[Any] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    observations: list[ToolObservation] = field(default_factory=list)
    pending_calls: list[ToolCall] = field(default_factory=list)
    plan_rounds: int = 0
    tool_call_count: int = 0
    fingerprints: list[str] = field(default_factory=list)
    final_batch: bool = False
    stop_reason: str = ""
    answer: str = ""
    sources: list[Any] = field(default_factory=list)
    vault_refs: list[Any] = field(default_factory=list)
    created_records: list[CreatedRecord] = field(default_factory=list)
    fallback: bool = False
    outcome: str = ""
    prepared_context: PreparedContext | None = None
    retrieval_summary: str = ""
    plan_done: bool = False
    retry_plan: bool = False
    pending_auto_backstop: list[bool] = field(default_factory=list)
    backstop_attempted: bool = False
    error_code: str = ""
    degraded_reasons: list[str] = field(default_factory=list)
    planning_protocol: str = "text_json"
    plan_finish_reason: str = ""
    answer_finish_reason: str = ""
    answer_repair_count: int = 0
    material_status: str = ""
    material_note: str = ""
    error_message: str = ""
    prompt_hashes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Trusted per-run dependencies; never copy this object into graph state or traces."""

    user_id: UUID
    session_id: UUID
    run_id: str
    user_message_id: int
    reference_date: date
    timezone: str
    read_scope: ReadScope
    llm: Any = None
    llm_config: Any = None
    embedding_client: Any = None
    read_pool: Any = None
    command_client: Any = None
    command_grant: str | None = None
    authorized_record_content: str | None = None
    allowed_write_tools: tuple[str, ...] = ()
    deadline: float | None = None
    cancelled: Callable[[], bool] | Any | None = None
    event_writer: Any = None
    repositories: Any = None
    loop_deadline: float | None = None
    answer_deadline: float | None = None
    max_plan_rounds: int = 4
    half_life_days: float = 30.0
    shadow: bool = False
    answer_budget_ms: int = 180_000
    clock: Callable[[], float] | None = None
    total_deadline: float | None = None
    assistant_context: Any = None
    prepare_tools: tuple[dict[str, str], ...] = ()
    planning_protocol: str = "auto"
