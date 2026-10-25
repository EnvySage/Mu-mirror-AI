# LangGraph chat reliability implementation plan

> For agentic workers: implement independent units with proof-first tests; integrate and review the graph centrally.

**Goal:** Make graph_v2 distinguish valid tool execution, sufficient evidence, and accepted final answers.
**Architecture:** Preserve Java authorization/persistence and the existing graph. Add explicit model planning protocols, complete paged content reads, task-aware evidence checks, and bounded output repair.
**Tech stack:** Python, LangGraph 1.2.16, existing OpenAI/Anthropic SDK adapters, PostgreSQL/pgvector.
**Spec:** `docs/2026-10-25-langgraph-chat-audit.md`, approved by the user's request to implement the proposed optimizations.

## Constraints

- Scope is graph_v2; keep legacy public behavior and write authorization working.
- No business database changes: disposable Docker database `mirror_ai_agent_v2_test` only.
- Keep existing branch `ai`; no publication or deployment requested.
- Model keys and database secrets must not appear in logs/artifacts.
- Existing untracked audit report is preserved. No worker changes upload/, shared environments, index, or unrelated files.

## Units and fixed integration contracts

### U1 Model protocol adapters (independent)

Own `llm/base.py`, `llm/openai_llm.py`, `llm/anthropic_llm.py`, new `llm/planning.py`, and dedicated protocol tests.
Add graph-only `plan(messages, tools, *, protocol='auto', timeout=None, max_retries=0)` returning a dictionary with `calls`, `done`, `reason`, `protocol`, `finish_reason`. Support native tool calling; explicit strict JSON-schema mode where supported; text-JSON fallback only on capability errors, never auth/rate-limit/server errors. Preserve legacy chat/json_task contracts. Enforce a shared deadline across fallback attempts. Add `answer_stream` exposing text/thinking and a terminal `('finish', reason)` without changing legacy chat_stream events.
Proof: mocked SDK transport requests/responses drive the real adapters; unsupported capability fallback, real errors, finish reasons and timeout propagation.

### U2 Accepted answer streaming (independent)

Own `services/tool_output_guard.py`, graph-only parts of `services/answer_service.py`, `tests/test_tool_output_guard.py`, and a dedicated graph-answer test file.
Use graph-only strict protocol filtering (tag attributes and fenced fake calls); retain explicit protocol-example support. Keep streaming normal answers. Retry at most once only before visible content; after visible output, report a typed failure instead of appending a replacement. Empty/whitespace-only or length-truncated output cannot count as success. Consume optional `llm.answer_stream`; finish reasons never become user text. Extend `AnswerEvent` with defaulted metadata. Graph answer generator emits `kind='failed'` with metadata `code` and `message`, then stops; accepted generations end with done including sources, refs, and metadata such as finish_reason/repair_count. Optional `deadline` and `clock` arguments keep retry within one budget.
Proof: original XML variants, conflicting retry prefix, valid examples, empty/length output, cancellation, citations, and existing legacy tests.

### U3 Content contracts and scoped pagination (independent)

Own `agent_data/vault.py`, `agent_data/records.py`, `agent_data/todos.py`, `agent_tools/registry.py`, `agent_tools/read_tools.py`, `prompts/agent_plan.txt`, and dedicated tests for these boundaries.
Retain existing calls. Add full untruncated ordered vault pages via recall_item args `offset` (default 0), `limit` (bounded); data contains snippets, `next_offset`, `complete`, `body_available` and `content_scope` ('full' for exhausted ordered body, 'partial' for a page with more, 'excerpt' for vector query). Extend VaultRecall with defaulted fields without breaking positional constructors. Add scoped record fragment paging with `record_id`/`offset` and a read_record tool using authorized views, never raw records. Add list_todos from the existing authorized todo view. Registry is authoritative; validate shapes/types/enums before execution, keep handler cross-field checks. Remove unsupported mode/semantic instructions from agent_plan. No source char slicing or silent truncation in ordered body reads.
Proof: pagination reconstructs body including last segment, cross-user/unconfirmed reads denied, validation does not query DB, record/todo filtering, real PostgreSQL integration.

### U4 Graph integration (depends on U1-U3)

Own `agent_core/types.py`, `nodes.py`, optional `material.py`, `graph.py`, graph fake collaborators/tests, config/runtime graph-only wiring and diagnostics.
Use plan protocol if present; retain simple fake/text clients. Invalid plans/calls become observations and bounded correction opportunities rather than silent skip/no-results. Preserve evidence through optional read failure, while cancellation/total deadline remain terminal. Before synthesis, validate required material; uniquely resolved body candidates trigger deterministic paged recall; ambiguous candidates yield specific clarification. Metadata is insufficient for body tasks. Read body pages within time and context bounds and disclose partial coverage if bounded; no claims of complete reads. Accept output only after done; consume failed events and preserve partial status. Include actual degradation/material/protocol/output metrics. Keep cites bound to current authorized evidence. Give planner updated accurate schemas.
Proof: replace the nine audit reproductions with regression tests of required behavior, end-to-end compiled graph with fake models and real scoped repositories.

### U5 Integration and completion

Synchronize supported source to upload using the existing script. Run project tests (including Docker PostgreSQL integration), focused compatibility/authorization/cancellation suites, upload check and diff checks. Review actual changes with an independent reviewer and resolve findings. Save verification evidence and implementation notes. No commits/push/deployment required by this user request.
