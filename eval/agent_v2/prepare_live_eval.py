"""Seed isolated, synthetic database fixtures for the Task 10 offline replay.

This script deliberately refuses databases outside the Task 10 test database and
does not delete or reset existing rows. User credentials and the provider key are
read from files outside the repository; none are copied into the evaluation
report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.request
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import psycopg


EXPECTED_DATABASE = "mirror_ai_agent_v2_task10_eval_20261020"
ENGINES = ("legacy", "graph_v2")
LOCAL_EMBEDDING_URL = "http://127.0.0.1:19060/v1/embeddings"


def _at(day: str, hour: int = 12) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00+08:00")


def _vector_for(texts: list[str], api_key: str) -> dict[str, list[float]]:
    vectors: dict[str, list[float]] = {}
    for offset in range(0, len(texts), 12):
        batch = texts[offset:offset + 12]
        body = json.dumps({"model": "BAAI/bge-m3", "input": batch}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            LOCAL_EMBEDDING_URL,
            data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            payload = json.load(response)
        rows = sorted(payload.get("data", []), key=lambda item: item.get("index", 0))
        if len(rows) != len(batch):
            raise RuntimeError("local embedding proxy returned an unexpected result count")
        for text, row in zip(batch, rows, strict=True):
            vector = row.get("embedding")
            if not isinstance(vector, list) or len(vector) != 1024:
                raise RuntimeError("local embedding proxy did not return 1024 dimensions")
            vectors[text] = [float(value) for value in vector]
    return vectors


def _vector_literal(values: list[float] | None) -> str | None:
    if values is None:
        return None
    return "[" + ",".join(format(value, ".9g") for value in values) + "]"


def _db_count(connection, table: str) -> int:
    return connection.execute(f"SELECT count(*) FROM public.{table}").fetchone()[0]


def _insert_record(connection, user_id: UUID, *, day: str, text: str, content_type: str,
                   moods: list[str] | None = None, status: str = "done",
                   ai_authorized: bool = True, memory_approved: bool = True,
                   todo_approved: bool = False, deleted: bool = False,
                   source: str = "user", key_chunk: bool = False,
                   vault_item_id: int | None = None, vector: list[float] | None = None) -> int:
    created_at = _at(day)
    record = connection.execute(
        "INSERT INTO public.records "
        "(user_id, content, source, status, user_reviewed, ai_processing_authorized, "
        "created_at, updated_at, deleted_at) "
        "VALUES (%s, %s, %s, %s, FALSE, %s, %s, %s, %s) RETURNING id",
        (user_id, text, source, status, ai_authorized, created_at, created_at,
         created_at if deleted else None),
    ).fetchone()
    record_id = record[0]
    metadata = {
        "contentType": content_type,
        "mood": moods or [],
        "title": text[:48],
        "keyChunk": "true" if key_chunk else "false",
    }
    chunk = connection.execute(
        "INSERT INTO public.chunks "
        "(user_id, record_id, content, segment, metadata, classified_segment, user_edited, "
        "ai_memory_approved, todo_approved, vault_item_id, embedding, created_at) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, %s, FALSE, %s, %s, %s, %s::public.vector, %s) "
        "RETURNING id",
        (user_id, record_id, text, text, json.dumps(metadata, ensure_ascii=False), text,
         memory_approved, todo_approved, vault_item_id, _vector_literal(vector), created_at),
    ).fetchone()
    return chunk[0]


def _insert_vault_item(connection, user_id: UUID, *, name: str, day: str, status: str,
                       description: str, body: str | None, key_text: str | None,
                       vectors: dict[str, list[float]]) -> int:
    digest = hashlib.sha256((name + day + status + description).encode("utf-8")).hexdigest()
    item = connection.execute(
        "INSERT INTO public.vault_items "
        "(user_id, original_name, storage_key, size_bytes, mime, sha256, category, description, "
        "digest_status, created_at) VALUES (%s, %s, %s, %s, 'application/pdf', %s, 'document', %s, %s, %s) "
        "RETURNING id",
        (user_id, name, f"task10-eval/{uuid4()}/{name}", len(description.encode("utf-8")),
         digest, description, status, _at(day)),
    ).fetchone()
    item_id = item[0]
    if body:
        _insert_record(
            connection, user_id, day=day, text=body, content_type="note", source="vault",
            ai_authorized=False, memory_approved=False, vault_item_id=item_id,
            vector=vectors.get(body),
        )
    if key_text:
        _insert_record(
            connection, user_id, day=day, text=key_text, content_type="note", source="vault",
            ai_authorized=False, memory_approved=False, key_chunk=True,
            vault_item_id=item_id, vector=vectors.get(key_text),
        )
    return item_id


def _load_users(path: Path) -> dict[str, dict[str, str]]:
    users = json.loads(path.read_text(encoding="utf-8-sig"))
    required = {
        "fixture-user-a-core", "fixture-user-a-write", "fixture-user-b",
        "fixture-user-a-zero", "fixture-user-a-empty",
    }
    if not isinstance(users, dict) or set(users) != required:
        raise ValueError("external user file must contain exactly the five Task 10 synthetic fixtures")
    return users


def _seed_sessions(cursor, cases: list[dict[str, object]], user_ids: dict[str, UUID],
                   evaluation_id: str) -> dict[str, dict[str, str]]:
    sessions: dict[str, dict[str, str]] = {}
    for case in cases:
        fixture_id = str(case["fixture_id"])
        if fixture_id not in user_ids:
            raise RuntimeError(f"unknown fixture id in case: {fixture_id}")
        case_id = str(case["case_id"])
        case_sessions: dict[str, str] = {}
        for engine in ENGINES:
            user_id = user_ids[fixture_id]
            session_id = uuid5(user_id, f"task10:{evaluation_id}:{engine}:{case_id}")
            case_sessions[engine] = str(session_id)
            cursor.execute(
                "INSERT INTO public.chat_sessions (id, user_id, title, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (session_id, user_id, f"task10:{case_id}", _at("2026-10-01"), _at("2026-10-01")),
            )
            history = case.get("history", [])
            if not isinstance(history, list):
                raise RuntimeError(f"invalid history fixture for {case_id}")
            if case_id == "history_other_session":
                other_session = uuid5(user_id, f"task10:{evaluation_id}:{engine}:{case_id}:other")
                cursor.execute(
                    "INSERT INTO public.chat_sessions (id, user_id, title, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (other_session, user_id, "task10:other-session", _at("2026-10-01"), _at("2026-10-01")),
                )
                history_session = other_session
            else:
                history_session = session_id
            for index, message in enumerate(history):
                fallback = case_id == "history_fallback" and message["role"] == "assistant"
                cursor.execute(
                    "INSERT INTO public.conversation_history "
                    "(session_id, user_id, role, content, is_fallback, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (history_session, user_id, message["role"], message["content"], fallback,
                     _at("2026-10-01", 10 + index)),
                )
        sessions[case_id] = case_sessions
    return sessions


def prepare(database_dsn: str, users_file: Path, embedding_key: str,
            sessions_file: Path, cases_file: Path) -> dict[str, object]:
    users = _load_users(users_file)
    user_ids = {name: UUID(item["user_id"]) for name, item in users.items()}
    cases = [json.loads(line) for line in cases_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    evaluation_id = str(uuid4())

    record_specs = [
        ("2026-08-12", "2026年8月我主要忙于毕业设计和 Python 学习，按计划复习哈希表、图算法，并准备面试。", "learning", ["expecting"]),
        ("2025-08-20", "2025年8月我去青岛旅行，参观了海边和博物馆，整理了路线计划。", "social", ["excited"]),
        ("2025-08-07", "2025年8月我记录过青岛旅行准备清单，包含海边和博物馆。", "note", ["calm"]),
        ("2024-08-09", "2024年8月我去杭州旅行，逛了西湖并拍了照片。", "social", ["happy"]),
        ("2024-02-29", "2024年2月29日我开始研究二手书检索项目，并拆分了第一版需求。", "plan", ["expecting"]),
        ("2026-10-19", "昨天在图书馆复习动态规划，完成三道算法题，并整理错题。", "learning", ["satisfied"]),
        ("2026-10-14", "10月14日完成了 Python 异步编程复习。", "learning", ["calm"]),
        ("2026-10-15", "10月15日把毕业设计的搜索接口拆成三个小任务。", "plan", ["expecting"]),
        ("2026-10-16", "10月16日参加项目评审，记录了需要补充的测试用例。", "work", ["confused"]),
        ("2026-10-17", "10月17日练习了图算法，并给学习计划加上复盘时间。", "learning", ["satisfied"]),
        ("2026-10-18", "10月18日和同学讨论毕业设计，确认下一步先完善检索质量。", "work", ["calm"]),
        ("2026-10-20", "10月20日整理面试复习清单，准备继续学习数据库索引。", "todo", ["expecting"]),
        ("2025-12-27", "2025年12月27日完成了年末项目复盘。", "work", ["satisfied"]),
        ("2025-12-31", "2025年12月31日总结了全年学习计划和下一年的目标。", "learning", ["grateful"]),
        ("2026-09-04", "上个月我在新团队负责后端工作，完成了用户资料接口和权限校验。", "work", ["satisfied"]),
        ("2026-09-12", "上个月我和同事讨论毕业设计的检索方案，决定比较关键词与向量召回。", "work", ["expecting"]),
        ("2026-09-21", "毕业方案已经通过评审；我随后整理了检索接口的实现步骤。", "learning", ["relieved"]),
        ("2026-10-02", "我之前整理过数据结构学习计划：先复习哈希表，再做图算法和动态规划练习。", "learning", ["calm"]),
        ("2026-10-05", "两位用户都写了共同短语：蓝色纸船。账户隔离必须阻止跨用户返回。", "note", ["calm"]),
    ]
    denied_specs = [
        ("2026-10-11", "这条尚未获准进行 AI 处理的健康记录不能进入长期记忆。", "health", False, False, "done", False),
        ("2026-10-10", "这条已删除记录不应再被检索。", "note", True, True, "done", True),
        ("2026-10-09", "这条仍在处理中，不能作为完成记录返回。", "note", True, True, "processing", False),
        ("2026-10-08", "这条记录未授权片段进入 AI 记忆。", "note", True, False, "done", False),
        ("2026-10-07", "空白片段不能作为检索证据。", "note", True, True, "done", False),
    ]
    all_texts = [spec[1] for spec in record_specs]
    all_texts.extend(spec[1] for spec in denied_specs)
    other_text = "两位用户都写了共同短语：蓝色纸船。账户隔离必须阻止跨用户返回。"
    all_texts.append(other_text)
    file_texts = [
        "第二节要求：每个 API 请求必须包含幂等键，服务端应验证调用方权限并记录审计摘要。",
        "shared.pdf 的关键摘要：检索服务每个请求最多返回五条结果。该摘要只用于文件识别，不是正文证据。",
    ]
    all_texts.extend(file_texts)
    vectors = _vector_for(list(dict.fromkeys(all_texts)), embedding_key)

    sessions: dict[str, dict[str, str]] = {}
    with psycopg.connect(database_dsn) as connection:
        with connection.cursor() as cursor:
            db_name = cursor.execute("SELECT current_database()").fetchone()[0]
            if db_name != EXPECTED_DATABASE:
                raise RuntimeError(f"refusing to seed unexpected database: {db_name}")
            actual_users = {
                str(row[0]) for row in cursor.execute(
                    "SELECT id FROM public.users WHERE username LIKE 'task10_eval_%'"
                ).fetchall()
            }
            if actual_users != {str(value) for value in user_ids.values()}:
                raise RuntimeError("database synthetic accounts do not match the external fixture map")
            guarded_tables = (
                "records", "chunks", "profile_snapshots", "user_terms", "vault_items",
                "todo_registry", "todo_registry_links", "chat_sessions", "conversation_history",
                "agent_runs", "tool_calls", "agent_command_receipts",
            )
            counts = {table: _db_count(cursor, table) for table in guarded_tables}
            nonempty = {table: count for table, count in counts.items() if count}
            if nonempty:
                raise RuntimeError(f"refusing to append to non-empty evaluation tables: {nonempty}")

            chunk_by_text: dict[str, int] = {}
            core_id = user_ids["fixture-user-a-core"]
            for day, text, content_type, moods in record_specs:
                chunk_by_text[text] = _insert_record(
                    cursor, core_id, day=day, text=text, content_type=content_type,
                    moods=moods, todo_approved=(content_type == "todo"), vector=vectors[text],
                )
            for day, text, content_type, authorized, approved, status, deleted in denied_specs:
                chunk_by_text[text] = _insert_record(
                    cursor, core_id, day=day, text=text, content_type=content_type,
                    ai_authorized=authorized, memory_approved=approved, status=status,
                    deleted=deleted, vector=vectors[text],
                )

            empty_segment_text = "空白片段不能作为检索证据。"
            empty_segment_chunk_id = chunk_by_text[empty_segment_text]
            updated = cursor.execute(
                "UPDATE public.chunks SET segment = '', classified_segment = '', embedding = NULL "
                "WHERE id = %s AND user_id = %s AND content = %s AND segment = %s",
                (empty_segment_chunk_id, core_id, empty_segment_text, empty_segment_text),
            ).rowcount
            if updated != 1:
                raise RuntimeError("empty_chunk fixture must contain exactly one guarded synthetic row")

            profile_rows = (
                ("2026-09-01", "九月初仍在熟悉项目，情绪略焦虑。", "学习数据库基础。", "待办较多。", "常在晚上学习。", ["适应", "学习"], "九月开始适应新团队。"),
                ("2026-10-18", "近期从焦虑转为平静，完成了评审后的拆分。", "持续学习 Python、图算法和数据库索引。", "优先处理检索接口和测试。", "工作日保持短时复盘。", ["Python", "毕业设计", "复盘"], "十月的学习节奏更稳定，毕业设计检索接口已经进入实施阶段。"),
            )
            for day, mood, learning, todo, rhythm, tags, summary in profile_rows:
                cursor.execute(
                    "INSERT INTO public.profile_snapshots "
                    "(user_id, snapshot_type, period_month, mood_analysis, learning_analysis, todo_analysis, "
                    "rhythm_analysis, user_tags, overall_summary, invalidated, created_at) "
                    "VALUES (%s, 'manual', NULL, %s, %s, %s, %s, %s::jsonb, %s, FALSE, %s)",
                    (core_id, mood, learning, todo, rhythm, json.dumps(tags, ensure_ascii=False), summary, _at(day)),
                )

            cursor.execute(
                "INSERT INTO public.user_terms "
                "(user_id, term, aliases, description, status, query_hit_count, last_confirmed_at) "
                "VALUES (%s, '章鱼计划', %s::jsonb, %s, 'confirmed', 0, %s)",
                (core_id, json.dumps(["小章鱼项目", "章鱼项目"], ensure_ascii=False),
                 "毕业设计中的章鱼算法可视化项目；小章鱼是已确认的简称。", _at("2026-10-01")),
            )
            cursor.execute(
                "INSERT INTO public.user_terms (user_id, term, aliases, description, status) "
                "VALUES (%s, 'pending', %s::jsonb, %s, 'pending')",
                (core_id, json.dumps(["pending术语"], ensure_ascii=False), "未确认词条不应作为确定事实。"),
            )

            todo_chunk = chunk_by_text[record_specs[11][1]]
            todo_id = cursor.execute(
                "INSERT INTO public.todo_registry (user_id, title, current_status, source_chunk_id) "
                "VALUES (%s, '完成数据库索引复习', 'pending', %s) RETURNING id",
                (core_id, todo_chunk),
            ).fetchone()[0]
            cursor.execute(
                "INSERT INTO public.todo_registry_links (todo_id, chunk_id, relation) "
                "VALUES (%s, %s, 'source')",
                (todo_id, todo_chunk),
            )
            cursor.execute(
                "INSERT INTO public.todo_registry (user_id, title, current_status, source_chunk_id) "
                "VALUES (%s, '未确认的复习提示', 'pending', %s)",
                (core_id, chunk_by_text[denied_specs[3][1]]),
            )

            _insert_vault_item(
                cursor, core_id, name="shared.pdf", day="2026-10-18", status="confirmed",
                description="共享文件名示例，已确认的项目摘要。", body=None,
                key_text=file_texts[1], vectors=vectors,
            )
            _insert_vault_item(
                cursor, core_id, name="shared.pdf", day="2026-10-19", status="pending",
                description="同名上传文件，尚未确认内容。", body=None,
                key_text=None, vectors=vectors,
            )
            _insert_vault_item(
                cursor, core_id, name="specification.pdf", day="2026-10-16", status="confirmed",
                description="接口规范文档，第二节包含 API 请求要求。", body=file_texts[0],
                key_text=None, vectors=vectors,
            )

            other_id = user_ids["fixture-user-b"]
            _insert_record(
                cursor, other_id, day="2026-10-17", text=other_text, content_type="note",
                moods=["calm"], vector=vectors[other_text],
            )

            sessions = _seed_sessions(cursor, cases, user_ids, evaluation_id)

            connection.commit()

        counts = {table: _db_count(connection, table) for table in guarded_tables}

    sessions_file.write_text(
        json.dumps({"evaluation_id": evaluation_id, "sessions": sessions}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"database": EXPECTED_DATABASE, "evaluation_id": evaluation_id, "counts": counts,
            "session_map": str(sessions_file)}


def prepare_sessions(database_dsn: str, users_file: Path, sessions_file: Path,
                     cases_file: Path) -> dict[str, object]:
    """Create fresh per-engine sessions while reusing an already seeded test DB."""
    users = _load_users(users_file)
    user_ids = {name: UUID(item["user_id"]) for name, item in users.items()}
    cases = [json.loads(line) for line in cases_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    evaluation_id = str(uuid4())
    with psycopg.connect(database_dsn) as connection:
        with connection.cursor() as cursor:
            db_name = cursor.execute("SELECT current_database()").fetchone()[0]
            if db_name != EXPECTED_DATABASE:
                raise RuntimeError(f"refusing to seed unexpected database: {db_name}")
            actual_users = {
                str(row[0]) for row in cursor.execute(
                    "SELECT id FROM public.users WHERE username LIKE 'task10_eval_%'"
                ).fetchall()
            }
            if actual_users != {str(value) for value in user_ids.values()}:
                raise RuntimeError("database synthetic accounts do not match the external fixture map")
            if _db_count(cursor, "records") == 0:
                raise RuntimeError("sessions-only mode requires synthetic record fixtures to exist")
            sessions = _seed_sessions(cursor, cases, user_ids, evaluation_id)
            connection.commit()
    sessions_file.write_text(
        json.dumps({"evaluation_id": evaluation_id, "sessions": sessions}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"database": EXPECTED_DATABASE, "evaluation_id": evaluation_id,
            "case_session_count": len(sessions), "session_map": str(sessions_file)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users-file", type=Path, required=True)
    parser.add_argument("--sessions-file", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("cases.jsonl"))
    parser.add_argument("--sessions-only", action="store_true",
                        help="create fresh per-engine sessions while reusing already seeded synthetic data")
    args = parser.parse_args()
    database_dsn = os.getenv("MIRROR_TASK10_APP_DSN", "")
    embedding_key = os.getenv("MIRROR_TASK10_EMBEDDING_KEY", "")
    if not database_dsn:
        parser.error("MIRROR_TASK10_APP_DSN is required")
    if args.sessions_only:
        result = prepare_sessions(database_dsn, args.users_file, args.sessions_file, args.cases)
    else:
        if not embedding_key:
            parser.error("MIRROR_TASK10_EMBEDDING_KEY is required unless --sessions-only is set")
        result = prepare(database_dsn, args.users_file, embedding_key, args.sessions_file, args.cases)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
