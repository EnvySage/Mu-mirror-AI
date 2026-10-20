from datetime import date, datetime, timedelta, timezone
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from uuid import UUID

import pytest

from agent_core.retrieval import retrieve
from agent_core.types import IntentResult, TimeExpression, TimeWindow
from agent_data.records import RecordChunk, RecordRepository
from agent_data.scope import ReadScope


SCOPE = ReadScope(UUID(int=1), UUID(int=2), 10)
NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


class Embedder:
    def __init__(self, vector=None, error=None):
        self.vector = vector if vector is not None else [1.0] + [0.0] * 1023
        self.error = error
        self.calls = []

    def embed(self, text, *, timeout=None, max_retries=None):
        self.calls.append((text, timeout, max_retries))
        if self.error:
            raise self.error
        return self.vector


class Records:
    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.structured_calls = []
        self.vector_calls = []

    def search(self, scope, filters=None, *, limit=20):
        self.structured_calls.append((scope, filters, limit))
        return self.chunks[:limit]

    def search_vector(self, scope, vector, filters=None, **kwargs):
        self.vector_calls.append((scope, vector, filters, kwargs))
        return self.chunks[:kwargs["limit"]]


def chunk(chunk_id, record_id, *, distance=None, ranking_score=None, created_at=None):
    return RecordChunk(
        chunk_id=chunk_id,
        record_id=record_id,
        user_id=SCOPE.user_id,
        segment=f"segment {chunk_id}",
        metadata={"contentType": "note", "mood": ["calm"]},
        embedding=None,
        created_at=created_at or NOW,
        distance=distance,
        ranking_score=ranking_score,
    )


def intent(route="hybrid", *, content_type=None, moods=(), time_window=None):
    return IntentResult(
        action_kind="read",
        route=route,
        standalone_question="回顾最近状态",
        content_type=content_type,
        moods=tuple(moods),
        time_expression=TimeExpression(kind="none"),
        file_query_mode="none",
        time_window=time_window,
    )


def test_semantic_route_uses_vector_distance_and_keeps_ranking_separate():
    records = Records([chunk(1, 11, distance=0.12, ranking_score=0.48)])
    embedder = Embedder()

    result = retrieve(
        intent("semantic"), SCOPE, SimpleNamespace(records=records), embedder,
        half_life_days=30, limit=5, reference_time=NOW,
    )

    assert len(records.vector_calls) == 1
    assert records.structured_calls == []
    assert result.evidence[0].distance == pytest.approx(0.12)
    assert result.evidence[0].ranking_score == pytest.approx(0.48)
    assert result.evidence[0].origin == "semantic"
    assert embedder.calls == [("回顾最近状态", None, 0)]


def test_structured_route_uses_filters_without_embedding_and_empty_filters_fall_back():
    window = TimeWindow(NOW - timedelta(days=1), NOW)
    records = Records([chunk(1, 11)])

    result = retrieve(
        intent("structured", content_type="note", moods=("calm",), time_window=window),
        SCOPE,
        SimpleNamespace(records=records),
        None,
    )

    filters = records.structured_calls[0][1]
    assert (filters.content_type, filters.moods, filters.start_at, filters.end_at) == (
        "note", ("calm",), window.start, window.end,
    )
    assert result.route == "structured"
    assert result.evidence[0].distance is None
    assert result.evidence[0].ranking_score is None
    assert result.evidence[0].origin == "structured"

    empty = Records([chunk(2, 12, distance=0.1, ranking_score=0.1)])
    embedder = Embedder()
    fallback = retrieve(
        intent("structured"), SCOPE, SimpleNamespace(records=empty), embedder, reference_time=NOW,
    )
    assert fallback.route == "hybrid"
    assert empty.structured_calls == []
    assert len(empty.vector_calls) == 1
    assert fallback.evidence[0].origin == "hybrid"


def test_profile_prefers_repository_order_and_falls_back_to_hybrid_when_empty():
    profiles = SimpleNamespace(recent=lambda scope, limit=2, month=None: [
        SimpleNamespace(id=80, snapshot_type="manual", period_month=None, overall_summary="manual",
                        mood_analysis="m", learning_analysis="l", todo_analysis="t", rhythm_analysis="r",
                        user_tags=[], created_at=NOW),
        SimpleNamespace(id=81, snapshot_type="monthly", period_month="2026-09", overall_summary="monthly",
                        mood_analysis="m", learning_analysis="l", todo_analysis="t", rhythm_analysis="r",
                        user_tags=[], created_at=NOW),
    ])
    result = retrieve(intent("profile"), SCOPE, SimpleNamespace(profiles=profiles), None)
    assert [item.evidence_id for item in result.evidence] == ["profile:80", "profile:81"]
    assert all(item.record_id is None for item in result.evidence)
    assert result.evidence[0].origin == "profile"

    records = Records([chunk(3, 13, distance=0.1, ranking_score=0.1)])
    empty_profiles = SimpleNamespace(recent=lambda scope, limit=2, month=None: [])
    fallback = retrieve(
        intent("profile"), SCOPE,
        SimpleNamespace(profiles=empty_profiles, records=records), Embedder(), reference_time=NOW,
    )
    assert fallback.route == "hybrid"
    assert fallback.evidence[0].origin == "hybrid"


def test_time_filter_turns_off_decay_and_routes_to_filtered_hybrid_vector_search():
    window = TimeWindow(datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 2, tzinfo=timezone.utc))
    records = Records([chunk(4, 14, distance=0.2, ranking_score=0.2)])

    result = retrieve(
        intent("semantic", time_window=window), SCOPE, SimpleNamespace(records=records), Embedder(),
        half_life_days=30, reference_time=NOW,
    )

    filters = records.vector_calls[0][2]
    options = records.vector_calls[0][3]
    assert (filters.start_at, filters.end_at) == (window.start, window.end)
    assert options["apply_decay"] is False
    assert result.evidence[0].distance == pytest.approx(0.2)
    assert result.evidence[0].ranking_score == pytest.approx(0.2)
    assert result.route == "hybrid"
    assert result.evidence[0].origin == "hybrid"


def test_semantic_route_with_explicit_metadata_constraint_becomes_hybrid():
    records = Records([chunk(6, 16, distance=0.1, ranking_score=0.1)])
    result = retrieve(
        intent("semantic", content_type="learning"), SCOPE, SimpleNamespace(records=records), Embedder(),
        reference_time=NOW,
    )
    assert result.route == "hybrid"
    assert records.vector_calls[0][2].content_type == "learning"


def test_vector_validation_threshold_decay_and_keyword_filter_contract():
    records = Records([])
    wrong = Embedder([1.0] * 1023)
    degraded = retrieve(
        intent("hybrid", content_type="note"), SCOPE, SimpleNamespace(records=records), wrong,
    )
    assert degraded.degraded is True
    assert degraded.degradation_reason == "embedding_dimension_mismatch"
    assert records.vector_calls == []
    assert records.structured_calls[0][1].content_type == "note"

    records = Records([])
    result = retrieve(intent("hybrid"), SCOPE, SimpleNamespace(records=records), Embedder(), reference_time=NOW)
    kwargs = records.vector_calls[0][3]
    assert kwargs["threshold"] == pytest.approx(0.35)
    assert kwargs["half_life_days"] == 30
    assert kwargs["apply_decay"] is True
    assert result.evidence == ()


def test_embedding_failure_keeps_hard_filters_and_never_changes_models():
    records = Records([chunk(5, 15)])
    embedder = Embedder(error=RuntimeError("provider unavailable"))
    result = retrieve(
        intent("hybrid", content_type="learning", moods=("anxious",)),
        SCOPE, SimpleNamespace(records=records), embedder,
    )
    assert result.degraded is True
    assert "embedding_unavailable" in result.degradation_reason
    assert records.vector_calls == []
    assert (records.structured_calls[0][1].content_type, records.structured_calls[0][1].moods) == (
        "learning", ("anxious",),
    )


class Cursor:
    def fetchall(self):
        return []


class Connection:
    def __init__(self):
        self.queries = []

    def transaction(self):
        return nullcontext()

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        return Cursor()


class Pool:
    def __init__(self):
        self.borrowed = 0
        self.connection_obj = Connection()

    @contextmanager
    def connection(self, timeout=None):
        self.borrowed += 1
        yield self.connection_obj


def test_record_vector_repository_rejects_wrong_dimension_before_borrowing_connection():
    pool = Pool()
    repository = RecordRepository(pool)
    with pytest.raises(ValueError, match="1024"):
        repository.search_vector(SCOPE, [1.0] * 1023)
    assert pool.borrowed == 0


def test_record_vector_repository_filters_keyword_before_limit_and_orders_by_decay_score():
    pool = Pool()
    repository = RecordRepository(pool)
    start_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    repository.search_vector(
        SCOPE, [1.0] + [0.0] * 1023,
        {"keyword": "probe", "content_type": "note", "moods": ["calm"], "start_at": start_at},
        limit=5, threshold=0.35, half_life_days=30, reference_time=NOW,
    )
    sql, params = pool.connection_obj.queries[-1]
    assert "FROM mirror_ai.record_chunks WHERE" in sql
    assert sql.index("strpos(lower(segment)") < sql.index("LIMIT %s")
    assert "OPERATOR(public.<=>)" in sql
    assert "ranking_score ASC, distance ASC" in sql
    assert "GREATEST(0.0" in sql
    assert params[4] == SCOPE.user_id
    assert params[6] == pytest.approx(0.35)
    assert params[7:10] == ["probe", "note", ["calm"]]
    assert params[-1] == 5


def test_record_vector_repository_keeps_distance_when_exact_window_disables_decay():
    pool = Pool()
    repository = RecordRepository(pool)
    repository.search_vector(
        SCOPE, [1.0] + [0.0] * 1023,
        {"start_at": datetime(2026, 10, 1, tzinfo=timezone.utc)},
        apply_decay=False,
    )
    sql, _ = pool.connection_obj.queries[-1]
    assert "AS distance" in sql and "AS ranking_score" in sql
    assert "GREATEST(0.0" not in sql


def test_record_vector_repository_scopes_real_pgvector_rows_and_separates_distance_from_decay(agent_pool, read_scope):
    repository = RecordRepository(agent_pool)
    vector = [1.0] + [0.0] * 1023
    reference_time = datetime.fromisoformat("2026-10-20T10:00:00+08:00")

    ranked = repository.search_vector(
        read_scope, vector, limit=5, threshold=0.35, half_life_days=30, reference_time=reference_time,
    )
    by_chunk = {item.chunk_id: item for item in ranked}
    assert set(by_chunk) == {1001, 1010}
    assert by_chunk[1001].distance == pytest.approx(1 - 2 ** -0.5, abs=1e-5)
    assert by_chunk[1001].ranking_score == pytest.approx(
        by_chunk[1001].distance * (1 + 19 / 30), abs=1e-5,
    )
    assert by_chunk[1001].ranking_score != pytest.approx(by_chunk[1001].distance)

    filtered = repository.search_vector(
        read_scope, vector,
        {"keyword": "coverage phrase one", "start_at": datetime.fromisoformat("2026-10-01T00:00:00+08:00"),
         "end_at": datetime.fromisoformat("2026-10-02T00:00:00+08:00")},
        limit=1, threshold=0.35, half_life_days=30, reference_time=reference_time, apply_decay=False,
    )
    assert [item.chunk_id for item in filtered] == [1001]
    assert filtered[0].ranking_score == pytest.approx(filtered[0].distance)

    thresholded = repository.search_vector(read_scope, [0.0, 1.0] + [0.0] * 1022, threshold=0.35)
    assert [item.chunk_id for item in thresholded] == [1001]
