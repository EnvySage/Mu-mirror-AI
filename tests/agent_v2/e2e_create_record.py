"""Guarded local E2E for graph_v2 create_record against a disposable Task 9 database.

This test uses the real Java HTTP/SSE endpoint, Python gRPC server, command client,
RecordService, and PostgreSQL. It does not call an LLM: the Java-issued create grant
selects the deterministic original-text path. It refuses to run outside a database
whose name starts with ``mirror_ai_agent_v2_task9_`` and a user named ``task9_*``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import requests
from psycopg.rows import dict_row


QUESTION = "帮我记一篇日记：今天把镜子的聊天功能跑通了"
CONTENT = "今天把镜子的聊天功能跑通了"
DB_PREFIX = "mirror_ai_agent_v2_task9_"
USER_PREFIX = "task9_"


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def _data(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("data")
    return value if isinstance(value, dict) else {}


def _get(value: dict[str, Any], camel: str, snake: str | None = None, default=None):
    if camel in value:
        return value[camel]
    if snake and snake in value:
        return value[snake]
    return default


class CreateRecordE2E:
    def __init__(self, base_url: str, token: str, user_id: str, dsn: str, report_path: Path):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        self.user_id = user_id
        self.dsn = dsn
        self.report_path = report_path
        self.scenarios: list[dict[str, Any]] = []
        self.dbname = ""

    def guard_and_seed(self) -> None:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            self.dbname = connection.info.dbname
            if not self.dbname.startswith(DB_PREFIX):
                raise RuntimeError(
                    f"Refusing to write: database {self.dbname!r} does not start with {DB_PREFIX!r}"
                )
            user = connection.execute(
                "SELECT id, username FROM users WHERE id = %s", (self.user_id,),
            ).fetchone()
            if not user or not user["username"].startswith(USER_PREFIX):
                raise RuntimeError("Refusing to write: user must be a task9_* test account")
            connection.execute(
                """
                INSERT INTO user_terms(user_id, term, aliases, description, status, last_confirmed_at)
                VALUES (%s, 'Mirror 项目', '[\"镜子\"]'::jsonb, 'Task 9 隔离 E2E 词典别名', 'confirmed', now())
                ON CONFLICT (user_id, term) DO UPDATE SET
                    aliases = EXCLUDED.aliases,
                    description = EXCLUDED.description,
                    status = 'confirmed',
                    last_confirmed_at = now()
                """,
                (self.user_id,),
            )
            connection.commit()
            alias = connection.execute(
                """
                SELECT 1 FROM user_terms
                WHERE user_id = %s AND term = 'Mirror 项目' AND status = 'confirmed'
                  AND aliases @> '[\"镜子\"]'::jsonb
                """,
                (self.user_id,),
            ).fetchone()
            if alias is None:
                raise AssertionError("the isolated test user's confirmed glossary alias was not seeded")

    def stream_chat(
        self,
        request_id: str,
        *,
        stop_after: str | None = None,
        http_session: requests.Session | None = None,
    ) -> list[tuple[str, Any]]:
        client = http_session or self.session
        response = client.post(
            f"{self.base_url}/mirror/chat",
            json={"question": QUESTION, "requestId": request_id},
            stream=True,
            timeout=(5, 90),
        )
        if response.status_code != 200:
            body = response.text[:500]
            response.close()
            raise AssertionError(f"chat returned HTTP {response.status_code}: {body}")
        response.encoding = "utf-8"

        events: list[tuple[str, Any]] = []
        current_event = "message"
        data_lines: list[str] = []

        def flush() -> bool:
            nonlocal current_event, data_lines
            if not data_lines:
                current_event = "message"
                return False
            payload = json.loads("\n".join(data_lines))
            events.append((current_event, payload))
            should_stop = current_event == stop_after
            current_event = "message"
            data_lines = []
            return should_stop

        try:
            for line in response.iter_lines(decode_unicode=True):
                if line == "":
                    if flush():
                        break
                    continue
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            else:
                flush()
        finally:
            response.close()
        return events

    def run_status(self, run_id: str) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}/mirror/runs/{run_id}", timeout=(5, 15))
        response.raise_for_status()
        return _data(response.json())

    def wait_terminal(self, run_id: str, timeout_seconds: float = 25) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.run_status(run_id)
            if last.get("status") in {"completed", "failed", "cancelled", "abandoned"}:
                return last
            time.sleep(0.25)
        raise AssertionError(f"run did not reach a terminal status: {last.get('status')}")

    def count_records(self) -> int:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            row = connection.execute(
                "SELECT count(*) AS n FROM records WHERE user_id = %s AND content = %s AND deleted_at IS NULL",
                (self.user_id, CONTENT),
            ).fetchone()
            return int(row["n"])

    def assert_record_only(self, record_id: int) -> None:
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            row = connection.execute(
                """
                SELECT id, content, source, status, ai_processing_authorized
                FROM records WHERE id = %s AND user_id = %s
                """,
                (record_id, self.user_id),
            ).fetchone()
            if row is None:
                raise AssertionError(f"created record {record_id} is absent")
            assert row["content"] == CONTENT, row
            assert row["source"] == "user", row
            assert row["status"] == "record_only", row
            assert row["ai_processing_authorized"] is False, row
            chunk_counts = connection.execute(
                """
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE ai_memory_approved OR todo_approved) AS approved
                FROM chunks WHERE record_id = %s AND user_id = %s
                """,
                (record_id, self.user_id),
            ).fetchone()
            assert chunk_counts["total"] == 0, chunk_counts
            assert chunk_counts["approved"] == 0, chunk_counts
            linked_todos = connection.execute(
                """
                SELECT count(*) AS n FROM todo_registry t
                JOIN chunks c ON c.id = t.source_chunk_id AND c.user_id = t.user_id
                WHERE c.record_id = %s AND t.user_id = %s
                """,
                (record_id, self.user_id),
            ).fetchone()
            assert linked_todos["n"] == 0, linked_todos

    @staticmethod
    def _done(events: list[tuple[str, Any]]) -> dict[str, Any]:
        for event, payload in reversed(events):
            if event == "done":
                return payload
        raise AssertionError(f"SSE stream did not contain done: {[name for name, _ in events]}")

    def _record_ids_from_done(self, done: dict[str, Any]) -> list[int]:
        values = _get(done, "createdRecords", "created_records", []) or []
        return [int(_get(item, "recordId", "record_id")) for item in values]

    def _resolve_success(self, request_id: str, events: list[tuple[str, Any]]) -> dict[str, Any]:
        done = self._done(events)
        if done.get("status") == "running":
            meta = next(payload for name, payload in events if name == "meta")
            run_id = _get(meta, "runId", "run_id")
            self.wait_terminal(run_id)
            done = self._done(self.stream_chat(request_id))
        return done

    def run(self) -> dict[str, Any]:
        self.guard_and_seed()

        # One explicit user submission creates one original-text record.
        request_id = str(uuid4())
        first = self.stream_chat(request_id)
        done = self._done(first)
        assert done.get("status") == "completed", done
        assert done.get("outcome") == "action_success", done
        assert len(self._record_ids_from_done(done)) == 1, done
        record_id = self._record_ids_from_done(done)[0]
        self.assert_record_only(record_id)
        session_id = _get(done, "sessionId", "session_id")
        run_id = _get(done, "runId", "run_id")
        assert session_id and run_id, done
        assert self.count_records() == 1
        self.scenarios.append({"name": "explicit_create_original_record_only", "status": "passed"})

        # Replaying the same request ID returns its receipt and does not create another row.
        replay = self._done(self.stream_chat(request_id))
        assert replay.get("outcome") == "action_success", replay
        assert self.count_records() == 1
        self.scenarios.append({"name": "same_request_id_replay", "status": "passed"})

        # Two concurrent transports sharing one request ID converge on the same Java run/call.
        concurrent_id = str(uuid4())
        before = self.count_records()
        def send_concurrent_replay(_index: int) -> list[tuple[str, Any]]:
            # requests.Session is not thread-safe; each concurrent transport owns its client.
            with requests.Session() as client:
                client.headers.update(self.session.headers)
                return self.stream_chat(concurrent_id, http_session=client)

        with ThreadPoolExecutor(max_workers=2) as pool:
            streams = list(pool.map(send_concurrent_replay, range(2)))
        assert all(self._resolve_success(concurrent_id, stream).get("outcome") == "action_success"
                   for stream in streams)
        assert self.count_records() == before + 1
        self.scenarios.append({"name": "concurrent_same_request_id", "status": "passed"})

        # Close the transport after Java emits the successful delta, then recover by replaying its ID.
        lost_id = str(uuid4())
        before = self.count_records()
        interrupted = self.stream_chat(lost_id, stop_after="delta")
        meta = next(payload for name, payload in interrupted if name == "meta")
        assert any(name == "delta" for name, _ in interrupted), interrupted
        recovered = self._done(self.stream_chat(lost_id))
        assert recovered.get("outcome") == "action_success", recovered
        assert self.count_records() == before + 1
        self.scenarios.append({"name": "response_lost_then_same_id_replay", "status": "passed"})

        # Model a user cancellation by closing the SSE transport immediately after the run ID arrives.
        cancelled_id = str(uuid4())
        before = self.count_records()
        accepted = self.stream_chat(cancelled_id, stop_after="meta")
        cancel_meta = next(payload for name, payload in accepted if name == "meta")
        cancel_run_id = _get(cancel_meta, "runId", "run_id")
        cancel_status = self.wait_terminal(cancel_run_id)
        after_cancel = self.count_records()
        assert after_cancel in {before, before + 1}
        if after_cancel == before + 1:
            created = _get(cancel_status.get("assistant") or {}, "createdRecords", "created_records", []) or []
            assert created and int(_get(created[0], "recordId", "record_id")) > 0
        replay_after_cancel = self._done(self.stream_chat(cancelled_id))
        assert self.count_records() == after_cancel
        assert replay_after_cancel.get("status") in {"completed", "cancelled", "failed"}
        self.scenarios.append({"name": "cancel_disconnect_and_idempotent_replay", "status": "passed"})

        # A deleted conversation is not resurrected by request replay; the diary itself remains.
        deleted_session_id = str(_get(recovered, "sessionId", "session_id") or session_id)
        delete_response = self.session.delete(f"{self.base_url}/mirror/sessions/{deleted_session_id}", timeout=(5, 15))
        delete_response.raise_for_status()
        before_replay = self.count_records()
        gone_response = self.session.post(
            f"{self.base_url}/mirror/chat",
            json={"question": QUESTION, "requestId": lost_id},
            timeout=(5, 15),
        )
        assert gone_response.status_code != 200, gone_response.text[:300]
        assert self.count_records() == before_replay
        self.scenarios.append({"name": "deleted_session_replay_is_gone", "status": "passed"})

        # A deliberate new request ID may save the same text again.
        before = self.count_records()
        next_done = self._done(self.stream_chat(str(uuid4())))
        assert next_done.get("outcome") == "action_success", next_done
        assert self.count_records() == before + 1
        self.assert_record_only(self._record_ids_from_done(next_done)[0])
        self.scenarios.append({"name": "new_request_id_allows_same_original_text", "status": "passed"})

        return {
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "execution": "real Java HTTP/SSE + real Python gRPC + real PostgreSQL command transaction; no LLM call",
            "database": self.dbname,
            "user": "task9 test account (identifier omitted)",
            "model": "not invoked; Java grant selected deterministic create path",
            "initial_record": {
                "content_exact": True,
                "source": "user",
                "status": "record_only",
                "ai_processing_authorized": False,
                "chunks_created": 0,
                "approved_chunks_created": 0,
                "todos_created": 0,
            },
            "scenario_results": self.scenarios,
            "record_count_after_suite": self.count_records(),
            "report_kind": "real business-write E2E; cancellation and transport loss are client disconnect simulations, not browser clicks",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(__file__).parent / "reports" / "task9-create-record-e2e.json",
        help="write a JSON result report (default: tests/agent_v2/reports/task9-create-record-e2e.json)",
    )
    args = parser.parse_args()

    base_url = os.environ.get("MIRROR_TASK9_E2E_BASE_URL", "http://127.0.0.1:19050/api").strip()
    runner = CreateRecordE2E(
        base_url=base_url,
        token=_required_env("MIRROR_TASK9_E2E_TOKEN"),
        user_id=_required_env("MIRROR_TASK9_E2E_USER_ID"),
        dsn=_required_env("MIRROR_TASK9_E2E_DSN"),
        report_path=args.report,
    )
    report = runner.run()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Report written to {args.report}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Task 9 E2E failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
