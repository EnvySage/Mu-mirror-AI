"""Authenticated gRPC adapter for the independent, read-only AgentRunner."""

from __future__ import annotations

import hmac
import os
import threading
from types import SimpleNamespace
from typing import Any, Callable, Iterator

import grpc

from agent_core.runner import AgentDependencies, run_agent
from agent_core.types import AgentEvent
from generated import agent_pb2 as pb2
from generated import agent_pb2_grpc as agent_grpc


class AgentRunnerServicer(agent_grpc.AgentRunnerServicer):
    """Verify Java's internal token before loading repositories or creating SDK clients."""

    def __init__(
        self,
        *,
        internal_token: str | None = None,
        dependencies: AgentDependencies | None = None,
        dependencies_factory: Callable[[], AgentDependencies] | None = None,
    ):
        self._internal_token = internal_token if internal_token is not None else os.getenv("MIRROR_AGENT_INTERNAL_TOKEN", "")
        self._dependencies = dependencies
        self._dependencies_factory = dependencies_factory or _create_default_dependencies
        self._dependency_lock = threading.Lock()
        self._closed = False

    def RunAgent(self, request: pb2.AgentRunRequest, context) -> Iterator[pb2.AgentEvent]:
        if not self._authorized(context):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or invalid internal token")
        dependencies = self._get_dependencies()
        cancellation = lambda: not context.is_active()
        sequence = 0
        terminal_seen = False
        vault_metadata: dict[int, dict[str, Any]] = {}
        for event in run_agent(request, dependencies, cancellation):
            if not context.is_active():
                return
            message = self._to_proto_event(request.run_id, event, sequence + 1, vault_metadata)
            if message is None:
                continue
            if message.WhichOneof("event") in {"completed", "failed"}:
                if terminal_seen:
                    continue
                terminal_seen = True
            if message.WhichOneof("event") == "tool_finished":
                # Metadata is carried separately by internal tool observations on the completion event.
                pass
            sequence += 1
            yield message
        if not terminal_seen and context.is_active():
            yield pb2.AgentEvent(
                run_id=request.run_id,
                seq=sequence + 1,
                failed=pb2.AgentFailure(
                    code="MISSING_TERMINAL",
                    message="agent stream ended without completion",
                    has_partial_answer=False,
                    stop_reason="planner_error",
                ),
            )

    def _authorized(self, context) -> bool:
        expected = self._internal_token
        if not expected:
            return False
        supplied = ""
        try:
            for item in context.invocation_metadata():
                if item.key.lower() == "x-internal-token":
                    supplied = item.value
                    break
        except Exception:
            return False
        if not supplied:
            return False
        try:
            return hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))
        except (AttributeError, UnicodeError):
            return False

    def _get_dependencies(self) -> AgentDependencies:
        if self._dependencies is not None:
            return self._dependencies
        with self._dependency_lock:
            if self._dependencies is None:
                self._dependencies = self._dependencies_factory()
            return self._dependencies

    def _to_proto_event(self, run_id: str, event: AgentEvent, sequence: int, vault_metadata):
        output = pb2.AgentEvent(run_id=run_id, seq=sequence)
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        if event.kind == "meta":
            output.meta.route = str(metadata.get("route", ""))
            output.meta.tools_used.extend(str(value) for value in metadata.get("tools_used", ()) or ())
        elif event.kind in {"thinking", "delta"}:
            target = output.thinking if event.kind == "thinking" else output.delta
            target.content = event.content
        elif event.kind == "tool_finished":
            output.tool_finished.call_id = str(metadata.get("call_id", ""))
            output.tool_finished.tool = str(metadata.get("tool", ""))
            output.tool_finished.args_json = str(metadata.get("args_json", "{}"))
            output.tool_finished.summary = str(metadata.get("summary", ""))
            output.tool_finished.success = bool(metadata.get("success", False))
            output.tool_finished.code = str(metadata.get("code", ""))
            output.tool_finished.latency_ms = max(0, int(metadata.get("latency_ms", 0) or 0))
            output.tool_finished.audited_by_java = bool(metadata.get("audited_by_java", False))
            output.tool_finished.auto_backstop = bool(metadata.get("auto_backstop", False))
            for item in metadata.get("vault_items", ()) or ():
                if not isinstance(item, dict) or item.get("vault_item_id") is None:
                    continue
                vault_metadata[int(item["vault_item_id"])] = item
        elif event.kind == "prepare_action":
            output.prepare_action.call_id = str(metadata.get("call_id", ""))
            output.prepare_action.tool = str(metadata.get("tool", ""))
            output.prepare_action.args_json = str(metadata.get("args_json", "{}"))
        elif event.kind == "completed":
            completed = output.completed
            completed.answer_text = str(metadata.get("answer_text", ""))
            for source in metadata.get("sources", ()) or ():
                completed.sources.add(
                    record_id=int(getattr(source, "record_id", 0) or 0),
                    quote=str(getattr(source, "quote", "")),
                    date=str(getattr(source, "date", "") or ""),
                    n=int(getattr(source, "n", 0) or 0),
                )
            for reference in metadata.get("vault_refs", ()) or ():
                item_id = int(getattr(reference, "vault_item_id", 0) or 0)
                item = vault_metadata.get(item_id, {})
                completed.vault_refs.add(
                    n=int(getattr(reference, "n", 0) or 0),
                    vault_item_id=item_id,
                    display_name=str(item.get("original_name", "")),
                    file_type=str(item.get("category", "")),
                    size_bytes=int(item.get("size_bytes", 0) or 0),
                    digest_status=str(item.get("digest_status", "")),
                    quote=str(getattr(reference, "quote", "")),
                )
            for created in metadata.get("created_records", ()) or ():
                completed.created_records.add(
                    record_id=int(getattr(created, "record_id", 0) or 0),
                    status=str(getattr(created, "status", "")),
                    created_at=str(getattr(created, "created_at", "") or ""),
                )
            completed.stop_reason = str(metadata.get("stop_reason", "done"))
            completed.outcome = str(metadata.get("outcome", "answer"))
            completed.fallback = bool(metadata.get("fallback", False))
            completed.glossary_hit_ids.extend(int(value) for value in metadata.get("glossary_hit_ids", ()) or ())
            completed.plan_rounds = max(0, int(metadata.get("plan_rounds", 0) or 0))
            completed.tool_call_count = max(0, int(metadata.get("tool_call_count", 0) or 0))
            metrics = metadata.get("metrics", {})
            if isinstance(metrics, dict):
                for key, value in (metrics.get("phase_latency_ms", {}) or {}).items():
                    completed.metrics.phase_latency_ms[str(key)] = max(0, int(value))
                completed.metrics.degraded_reasons.extend(str(value) for value in metrics.get("degraded_reasons", ()) or ())
                for key, value in (metrics.get("prompt_hashes", {}) or {}).items():
                    completed.metrics.prompt_hashes[str(key)] = str(value)
        elif event.kind == "failed":
            output.failed.code = str(metadata.get("code", "AGENT_ERROR"))
            output.failed.message = str(metadata.get("message", "本轮处理失败"))
            output.failed.has_partial_answer = bool(metadata.get("has_partial_answer", False))
            output.failed.stop_reason = str(metadata.get("stop_reason", "read_error"))
            metrics = metadata.get("metrics", {})
            if isinstance(metrics, dict):
                output.failed.metrics.degraded_reasons.extend(
                    str(value) for value in metrics.get("degraded_reasons", ()) or ()
                )
        else:
            return None
        return output

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        pool = getattr(getattr(self._dependencies, "repositories", None), "pool", None)
        close = getattr(pool, "close", None)
        if callable(close):
            close()


def _create_default_dependencies() -> AgentDependencies:
    from agent_data.glossary import GlossaryRepository
    from agent_data.history import HistoryRepository
    from agent_data.pool import create_read_pool
    from agent_data.profiles import ProfileRepository
    from agent_data.records import RecordRepository
    from agent_data.todos import TodoRepository
    from agent_data.vault import VaultRepository
    from agent_tools.command_client import CommandClient

    dsn = os.getenv("MIRROR_AI_READ_DSN", "")
    pool = create_read_pool(dsn)
    repositories = SimpleNamespace(
        pool=pool,
        records=RecordRepository(pool),
        todos=TodoRepository(pool),
        profiles=ProfileRepository(pool),
        glossary=GlossaryRepository(pool),
        history=HistoryRepository(pool),
        vault=VaultRepository(pool),
    )
    command_base_url = os.getenv("MIRROR_AGENT_COMMAND_BASE_URL", "").strip()
    command_client = CommandClient(command_base_url) if command_base_url else None
    return AgentDependencies(repositories=repositories, command_client=command_client)
