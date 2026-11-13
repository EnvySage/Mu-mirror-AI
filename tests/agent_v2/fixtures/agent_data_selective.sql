-- Included only by the disposable agent_data.sql fixture.
DO $$ BEGIN
    IF current_database() <> 'mirror_ai_agent_v2_test' THEN
        RAISE EXCEPTION 'agent_data_selective.sql requires mirror_ai_agent_v2_test';
    END IF;
END $$;

-- Version 1 ordinary/mixed/paged records. Bodies are synthetic; raw and unapproved
-- segments must stay unavailable even though approved text no longer needs a vector.
INSERT INTO records (id, user_id, content, source, status, ai_processing_authorized,
                     user_reviewed, index_policy_version, memory_state, created_at, deleted_at)
SELECT id, '00000000-0000-0000-0000-000000000001', 'RAW_V1_PRIVATE_SECRET', 'user',
       CASE WHEN id = 117 THEN 'reviewing' ELSE 'done' END,
       id <> 114, id <> 113, 1, CASE WHEN id = 115 THEN 'NONE' ELSE 'READY' END,
       ('2026-11-11T10:00:00+08:00'::timestamptz + (id - 110) * interval '1 day'),
       CASE WHEN id = 116 THEN '2026-11-17T12:00:00+08:00'::timestamptz ELSE NULL END
FROM generate_series(110, 117) AS id;

INSERT INTO chunks (id, user_id, record_id, content, segment, metadata, ai_memory_approved,
                    embedding_enabled, embedding_status, embedding, created_at) VALUES
    (5001, '00000000-0000-0000-0000-000000000001', 110, 'RAW_V1_PRIVATE_SECRET',
     repeat('普通日常下午干活。', 20) || '结尾事实：下午喝咖啡晚上睡不着',
     '{"contentType":"health","mood":["exhausted"]}', TRUE, FALSE, 'not_requested', NULL, '2026-11-11T10:00:00+08:00'),
    (5002, '00000000-0000-0000-0000-000000000001', 111, 'RAW_V1_PRIVATE_SECRET',
     '陈记牛肉面太咸，以后不去了', '{"contentType":"note","mood":[]}', TRUE, TRUE, 'ready',
     (ARRAY[0::real,0::real,0::real,1::real] || array_fill(0::real, ARRAY[1020]))::vector, '2026-11-12T10:00:00+08:00'),
    (5003, '00000000-0000-0000-0000-000000000001', 111, 'RAW_V1_PRIVATE_SECRET',
     '普通日常但残留向量', '{"contentType":"note","mood":[]}', TRUE, FALSE, 'ready',
     (ARRAY[0::real,0::real,0::real,1::real] || array_fill(0::real, ARRAY[1020]))::vector, '2026-11-12T10:01:00+08:00'),
    (5004, '00000000-0000-0000-0000-000000000001', 111, 'RAW_V1_PRIVATE_SECRET',
     '连续两周下午疲惫但索引失败', '{"contentType":"health","mood":[]}', TRUE, TRUE, 'failed',
     (ARRAY[0::real,0::real,0::real,1::real] || array_fill(0::real, ARRAY[1020]))::vector, '2026-11-12T10:02:00+08:00'),
    (5005, '00000000-0000-0000-0000-000000000001', 111, 'RAW_V1_PRIVATE_SECRET',
     '新项目决定等待向量', '{"contentType":"work","mood":[]}', TRUE, TRUE, 'pending', NULL, '2026-11-12T10:03:00+08:00'),
    (5006, '00000000-0000-0000-0000-000000000001', 111, 'RAW_V1_PRIVATE_SECRET',
     'UNAPPROVED_V1_SECRET', '{"contentType":"note","mood":[]}', FALSE, FALSE, 'not_requested', NULL, '2026-11-12T10:04:00+08:00'),
    (5007, '00000000-0000-0000-0000-000000000002', 111, 'OTHER_USER_PRIVATE',
     'CROSS_USER_V1_SECRET', '{"contentType":"note","mood":[]}', TRUE, FALSE, 'not_requested', NULL, '2026-11-12T10:04:00+08:00');

INSERT INTO chunks (id, user_id, record_id, content, segment, metadata, ai_memory_approved,
                    embedding_enabled, embedding_status, created_at)
SELECT 5100 + n, '00000000-0000-0000-0000-000000000001', 112, 'RAW_V1_PRIVATE_SECRET',
       '分页日常第' || n || '段' || CASE WHEN n = 20 THEN '，末尾事实：周末也没缓过来' ELSE '' END,
       '{"contentType":"note","mood":[]}', TRUE, FALSE, 'not_requested', '2026-11-13T10:00:00+08:00'
FROM generate_series(0, 20) AS n;
INSERT INTO chunks (id, user_id, record_id, content, segment, metadata, ai_memory_approved,
                    embedding_enabled, embedding_status, created_at)
SELECT 5200 + id - 113, '00000000-0000-0000-0000-000000000001', id, 'RAW_V1_PRIVATE_SECRET',
       'HIDDEN_V1_GATE_SECRET', '{"contentType":"note","mood":[]}', TRUE, FALSE, 'not_requested', now()
FROM generate_series(113, 117) AS id;
