-- Synthetic PostgreSQL data for tests/agent_v2. Never apply this to an app DB.
-- The database-name guard makes this disposable fixture refuse other targets.
DO $$
BEGIN
    IF current_database() <> 'mirror_ai_agent_v2_test' THEN
        RAISE EXCEPTION 'agent_data.sql requires mirror_ai_agent_v2_test, got %', current_database();
    END IF;
END $$;

CREATE EXTENSION IF NOT EXISTS vector;

DROP SCHEMA IF EXISTS mirror_ai CASCADE;
DROP TABLE IF EXISTS todo_registry CASCADE;
DROP TABLE IF EXISTS conversation_history CASCADE;
DROP TABLE IF EXISTS chat_sessions CASCADE;
DROP TABLE IF EXISTS user_terms CASCADE;
DROP TABLE IF EXISTS profile_snapshots CASCADE;
DROP TABLE IF EXISTS chunks CASCADE;
DROP TABLE IF EXISTS vault_items CASCADE;
DROP TABLE IF EXISTS records CASCADE;
DROP TABLE IF EXISTS users CASCADE;
DROP TABLE IF EXISTS user_settings CASCADE;
DROP TABLE IF EXISTS vault_blobs CASCADE;
DROP TABLE IF EXISTS tool_calls CASCADE;

CREATE TABLE users (
    id UUID PRIMARY KEY,
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL
);

CREATE TABLE tool_calls (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL,
    session_id UUID,
    tool VARCHAR(50) NOT NULL,
    args JSONB DEFAULT '{}'::jsonb,
    result_summary VARCHAR(500),
    success BOOLEAN NOT NULL DEFAULT TRUE,
    latency_ms INT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE records (
    id BIGINT PRIMARY KEY,
    user_id UUID NOT NULL,
    content TEXT NOT NULL,
    source VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL,
    ai_processing_authorized BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL
);

-- In this test fixture vault_items is declared before chunks so its FK exists.
CREATE TABLE vault_items (
    id BIGINT PRIMARY KEY,
    user_id UUID NOT NULL,
    original_name VARCHAR(255) NOT NULL,
    storage_key VARCHAR(500) NOT NULL,
    size_bytes BIGINT NOT NULL,
    mime VARCHAR(100) NOT NULL,
    sha256 VARCHAR(64),
    category VARCHAR(20),
    description VARCHAR(500),
    digest_status VARCHAR(20) NOT NULL,
    source_chunk_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL,
    deleted_at TIMESTAMPTZ
);

CREATE TABLE chunks (
    id BIGINT PRIMARY KEY,
    user_id UUID NOT NULL,
    record_id BIGINT NOT NULL,
    content TEXT NOT NULL,
    segment TEXT,
    metadata JSONB,
    ai_memory_approved BOOLEAN NOT NULL DEFAULT FALSE,
    todo_approved BOOLEAN NOT NULL DEFAULT FALSE,
    vault_item_id BIGINT,
    embedding vector(1024),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE profile_snapshots (
    id BIGINT PRIMARY KEY,
    user_id UUID NOT NULL,
    snapshot_type VARCHAR(20) NOT NULL,
    period_month CHAR(7),
    mood_analysis TEXT,
    learning_analysis TEXT,
    todo_analysis TEXT,
    rhythm_analysis TEXT,
    user_tags JSONB,
    overall_summary TEXT,
    embedding vector(1024),
    invalidated BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE chat_sessions (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL
);

CREATE TABLE conversation_history (
    id BIGINT PRIMARY KEY,
    session_id UUID NOT NULL,
    user_id UUID NOT NULL,
    role VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    is_fallback BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE user_terms (
    id BIGINT PRIMARY KEY,
    user_id UUID NOT NULL,
    term VARCHAR(100) NOT NULL,
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    description TEXT NOT NULL,
    status VARCHAR(20) NOT NULL,
    query_hit_count INT NOT NULL DEFAULT 0,
    last_confirmed_at TIMESTAMPTZ
);

CREATE TABLE todo_registry (
    id BIGINT PRIMARY KEY,
    user_id UUID NOT NULL,
    title VARCHAR(100) NOT NULL,
    current_status VARCHAR(20) NOT NULL,
    source_chunk_id BIGINT,
    deleted_at TIMESTAMPTZ
);

CREATE TABLE user_settings (user_id UUID, ai_api_key TEXT);
CREATE TABLE vault_blobs (vault_item_id BIGINT, data BYTEA);

SELECT 'CREATE ROLE mirror_ai_reader LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mirror_ai_reader')
\gexec

INSERT INTO users (id, username, password_hash) VALUES
    ('00000000-0000-0000-0000-000000000001', 'fixture-a', 'not-a-real-password-hash'),
    ('00000000-0000-0000-0000-000000000002', 'fixture-b', 'not-a-real-password-hash');

INSERT INTO records (id, user_id, content, source, status, ai_processing_authorized, deleted_at, created_at) VALUES
    (101, '00000000-0000-0000-0000-000000000001', 'FULL RECORD RAW TEXT MUST NEVER APPEAR IN record_chunks', 'user', 'done', TRUE, NULL, '2026-10-01T10:00:00+08:00'),
    (102, '00000000-0000-0000-0000-000000000001', 'processing source', 'user', 'processing', TRUE, NULL, '2026-10-02T10:00:00+08:00'),
    (103, '00000000-0000-0000-0000-000000000001', 'reviewing source', 'user', 'reviewing', TRUE, NULL, '2026-10-03T10:00:00+08:00'),
    (104, '00000000-0000-0000-0000-000000000001', 'deleted source', 'user', 'done', TRUE, '2026-10-04T10:00:00+08:00', '2026-10-04T09:00:00+08:00'),
    (105, '00000000-0000-0000-0000-000000000001', 'not authorized source', 'user', 'done', FALSE, NULL, '2026-10-05T10:00:00+08:00'),
    (106, '00000000-0000-0000-0000-000000000001', 'todo-only source', 'user', 'done', FALSE, NULL, '2026-10-06T10:00:00+08:00'),
    (201, '00000000-0000-0000-0000-000000000002', 'same shared diary body', 'user', 'done', TRUE, NULL, '2026-10-01T10:00:00+08:00'),
    (401, '00000000-0000-0000-0000-000000000001', 'vault extracted file body', 'vault', 'done', FALSE, NULL, '2026-10-08T10:00:00+08:00'),
    (402, '00000000-0000-0000-0000-000000000002', 'same uploaded file body', 'vault', 'done', FALSE, NULL, '2026-10-08T10:00:00+08:00');

INSERT INTO records (id, user_id, content, source, status, ai_processing_authorized, deleted_at, created_at)
SELECT 1100 + n,
       '00000000-0000-0000-0000-000000000001',
       'synthetic cap fixture ' || n,
       'user', 'done', TRUE, NULL,
       ('2026-10-10T10:00:00+08:00'::timestamptz + CASE WHEN n IN (0, 1) THEN 0 ELSE n END * interval '1 day')
FROM generate_series(0, 20) AS n;

INSERT INTO chunks (id, user_id, record_id, content, segment, metadata, ai_memory_approved, todo_approved, vault_item_id, embedding, created_at) VALUES
    (1001, '00000000-0000-0000-0000-000000000001', 101, 'FULL RECORD RAW TEXT MUST NEVER APPEAR IN record_chunks', 'approved coverage phrase one', '{"contentType":"learning","mood":["calm"]}', TRUE, FALSE, NULL, (ARRAY[1::real, 1::real] || array_fill(0::real, ARRAY[1022]))::vector, '2026-10-01T10:00:00+08:00'),
    (1002, '00000000-0000-0000-0000-000000000001', 101, 'FULL RECORD RAW TEXT MUST NEVER APPEAR IN record_chunks', 'secret unauthorized segment', '{"contentType":"learning","mood":["anxious"]}', FALSE, FALSE, NULL, NULL, '2026-10-01T10:01:00+08:00'),
    (1010, '00000000-0000-0000-0000-000000000001', 101, 'FULL RECORD RAW TEXT MUST NEVER APPEAR IN record_chunks', 'approved coverage phrase two', '{"contentType":"learning","mood":["calm"]}', TRUE, FALSE, NULL, (ARRAY[1::real] || array_fill(0::real, ARRAY[1023]))::vector, '2026-10-01T10:02:00+08:00'),
    (1003, '00000000-0000-0000-0000-000000000001', 102, 'processing source', 'processing segment', '{"contentType":"note","mood":["sad"]}', TRUE, FALSE, NULL, NULL, '2026-10-02T10:00:00+08:00'),
    (1004, '00000000-0000-0000-0000-000000000001', 103, 'reviewing source', 'reviewing segment', '{"contentType":"note","mood":["sad"]}', TRUE, FALSE, NULL, NULL, '2026-10-03T10:00:00+08:00'),
    (1005, '00000000-0000-0000-0000-000000000001', 104, 'deleted source', 'deleted segment', '{"contentType":"note","mood":["sad"]}', TRUE, FALSE, NULL, NULL, '2026-10-04T10:00:00+08:00'),
    (1006, '00000000-0000-0000-0000-000000000001', 105, 'not authorized source', 'record authorization is false', '{"contentType":"note","mood":["sad"]}', TRUE, FALSE, NULL, NULL, '2026-10-05T10:00:00+08:00'),
    (1007, '00000000-0000-0000-0000-000000000001', 106, 'todo-only source', 'todo independent approval', '{"contentType":"todo","mood":[]}', FALSE, TRUE, NULL, NULL, '2026-10-06T10:00:00+08:00'),
    (2001, '00000000-0000-0000-0000-000000000002', 201, 'same shared diary body', 'same shared diary body', '{"contentType":"learning","mood":["calm"]}', TRUE, TRUE, NULL, NULL, '2026-10-01T10:00:00+08:00');

INSERT INTO chunks (id, user_id, record_id, content, segment, metadata, ai_memory_approved, todo_approved, vault_item_id, embedding, created_at)
SELECT 3000 + n,
       '00000000-0000-0000-0000-000000000001',
       1100 + n,
       'synthetic cap fixture ' || n,
       CASE WHEN n IN (0, 1) THEN 'coverage phrase synthetic ' || n ELSE 'limit cap fixture ' || n END,
       '{"contentType":"thought","mood":["happy"]}',
       TRUE, FALSE, NULL, NULL,
       ('2026-10-10T10:00:00+08:00'::timestamptz + CASE WHEN n IN (0, 1) THEN 0 ELSE n END * interval '1 day')
FROM generate_series(0, 20) AS n;

INSERT INTO vault_items (id, user_id, original_name, storage_key, size_bytes, mime, sha256, category, description, digest_status, source_chunk_id, created_at, deleted_at) VALUES
    (301, '00000000-0000-0000-0000-000000000001', 'shared.pdf', 'fixture/a/private/storage-key', 123, 'application/pdf', 'fixture-private-hash-a', 'note', 'shared file metadata', 'pending', NULL, '2026-10-07T10:00:00+08:00', NULL),
    (302, '00000000-0000-0000-0000-000000000002', 'shared.pdf', 'fixture/b/private/storage-key', 123, 'application/pdf', 'fixture-private-hash-b', 'note', 'same shared file metadata', 'confirmed', NULL, '2026-10-07T10:00:00+08:00', NULL),
    (303, '00000000-0000-0000-0000-000000000001', 'specification.pdf', 'fixture/a/private/spec-storage-key', 456, 'application/pdf', 'fixture-private-hash-spec', 'learning', 'authorized AI body text', 'confirmed', NULL, '2026-10-08T10:00:00+08:00', NULL),
    (304, '00000000-0000-0000-0000-000000000001', 'deleted.pdf', 'fixture/a/private/deleted-storage-key', 789, 'application/pdf', 'fixture-private-hash-deleted', 'note', 'deleted file', 'confirmed', NULL, '2026-10-08T10:00:00+08:00', '2026-10-09T10:00:00+08:00');

INSERT INTO chunks (id, user_id, record_id, content, segment, metadata, ai_memory_approved, todo_approved, vault_item_id, embedding, created_at) VALUES
    (2101, '00000000-0000-0000-0000-000000000002', 402, 'other user file', 'shared.pdf key text', '{"keyChunk":"true","contentType":"note"}', FALSE, FALSE, 302, (ARRAY[1::real] || array_fill(0::real, ARRAY[1023]))::vector, '2026-10-08T10:00:00+08:00'),
    (2102, '00000000-0000-0000-0000-000000000002', 402, 'other user file', 'other user confirmed body', '{"keyChunk":"false"}', FALSE, FALSE, 302, (ARRAY[1::real] || array_fill(0::real, ARRAY[1023]))::vector, '2026-10-08T10:01:00+08:00'),
    (4001, '00000000-0000-0000-0000-000000000001', 401, 'entire file source must not be used as a diary chunk', 'specification.pdf authorized AI file key', '{"keyChunk":"true","contentType":"note"}', FALSE, FALSE, 303, (ARRAY[1::real] || array_fill(0::real, ARRAY[1023]))::vector, '2026-10-08T10:00:00+08:00'),
    (4002, '00000000-0000-0000-0000-000000000001', 401, 'entire file source must not be used as a diary chunk', 'approved vault body segment alpha', '{"keyChunk":"false"}', FALSE, FALSE, 303, (ARRAY[1::real] || array_fill(0::real, ARRAY[1023]))::vector, '2026-10-08T10:01:00+08:00'),
    (4003, '00000000-0000-0000-0000-000000000001', 401, 'entire file source must not be used as a diary chunk', 'approved vault body segment beta', '{"keyChunk":"false"}', FALSE, FALSE, 303, (ARRAY[1::real] || array_fill(0::real, ARRAY[1023]))::vector, '2026-10-08T10:02:00+08:00');

INSERT INTO profile_snapshots (id, user_id, snapshot_type, period_month, mood_analysis, learning_analysis, todo_analysis, rhythm_analysis, user_tags, overall_summary, embedding, invalidated, created_at) VALUES
    (801, '00000000-0000-0000-0000-000000000001', 'manual', NULL, 'A mood', 'A learning', 'A todo', 'A rhythm', '["fixture-a"]', 'valid A profile', NULL, FALSE, '2026-10-08T10:00:00+08:00'),
    (802, '00000000-0000-0000-0000-000000000001', 'monthly', '2026-09', 'invalid mood', 'invalid learning', 'invalid todo', 'invalid rhythm', '[]', 'invalidated profile', NULL, TRUE, '2026-10-07T10:00:00+08:00'),
    (803, '00000000-0000-0000-0000-000000000002', 'manual', NULL, 'B mood', 'B learning', 'B todo', 'B rhythm', '["fixture-b"]', 'valid B profile', NULL, FALSE, '2026-10-08T10:00:00+08:00');

INSERT INTO chat_sessions (id, user_id) VALUES
    ('10000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001'),
    ('10000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000002');

INSERT INTO conversation_history (id, session_id, user_id, role, content, is_fallback, created_at) VALUES
    (901, '10000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001', 'user', 'old question', FALSE, '2026-10-08T10:00:00+08:00'),
    (902, '10000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001', 'assistant', 'old answer', FALSE, '2026-10-08T10:01:00+08:00'),
    (903, '10000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001', 'assistant', 'fallback answer', TRUE, '2026-10-08T10:02:00+08:00'),
    (904, '10000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001', 'user', 'current user question', FALSE, '2026-10-08T10:03:00+08:00'),
    (905, '10000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000002', 'assistant', 'B private answer', FALSE, '2026-10-08T10:04:00+08:00');

INSERT INTO user_terms (id, user_id, term, aliases, description, status, query_hit_count, last_confirmed_at) VALUES
    (701, '00000000-0000-0000-0000-000000000001', 'confirmed-A', '["alias-A"]', 'confirmed A description', 'confirmed', 5, '2026-10-08T10:00:00+08:00'),
    (702, '00000000-0000-0000-0000-000000000001', 'pending-A', '[]', 'must remain hidden', 'pending', 99, NULL),
    (703, '00000000-0000-0000-0000-000000000002', 'confirmed-B', '["alias-B"]', 'private B description', 'confirmed', 9, '2026-10-08T10:00:00+08:00');

INSERT INTO todo_registry (id, user_id, title, current_status, source_chunk_id, deleted_at) VALUES
    (601, '00000000-0000-0000-0000-000000000001', 'independently approved todo', 'in_progress', 1007, NULL),
    (602, '00000000-0000-0000-0000-000000000001', 'not independently approved todo', 'completed', 1001, NULL),
    (603, '00000000-0000-0000-0000-000000000002', 'B private todo', 'not_started', 2001, NULL);
