# Agent v2 rollout and rollback

This runbook describes a future test/staged rollout. It does not authorize a production migration or release. The current default remains `legacy`; Task10 has not enabled online shadow sampling, graph_v2 traffic, or removed any legacy RPC.

The Java release and database procedure live in [Mu-mirror-B `docs/agent-v2-rollout.md`](../../Mu-mirror-B/docs/agent-v2-rollout.md). This page owns the Python runtime and paired evaluation procedure.

## 1. Preconditions and data boundary

1. Use a disposable or approved test database and verify the PostgreSQL major version matches the intended deployment. The migration uses pgvector operators/types and `security_barrier` views. The available local Task9 business E2E used PostgreSQL 16.15, but it did not validate these read views; a separate read-only migration/integration check is still required before rollout.
2. Back up the database and record the backup path and checksum before applying Java migrations. Apply `migration-v3-record-ai-authorization.sql`, then `migration-v4-agent-v2.sql`, from the Java repository. Provision a dedicated login role through the deployment secret manager; then run `deploy/agent-reader-role.sql` with only `reader_role` as a psql variable. Never run the destructive synthetic fixture SQL against an application database.
3. Connect with the dedicated reader DSN and verify `SELECT 1 FROM mirror_ai.glossary_terms LIMIT 0` succeeds in a read-only transaction. Verify the account cannot select public business tables, access `vault_blobs`, or perform DML. Do not grant it membership in another role.
4. The Python server starts the old 10003 service without `MIRROR_AI_READ_DSN` when Agent is disabled. Enabling Agent requires both `MIRROR_AGENT_INTERNAL_TOKEN` and `MIRROR_AI_READ_DSN`; inject them through the service manager/Jenkins credentials, never `config.yml`.

## 2. Paired offline evaluation

`eval/agent_v2/cases.jsonl` contains 52 synthetic cases. Each engine receives the same fixture id, history, reference date, model configuration reference, and prompt profile. The cases include permission boundaries, files, dates, empty/zero data, model/database failures, write authorization, idempotency, and lost-response scenarios. They contain no real model credentials or production records.

Run the report harness without claiming either engine executed:

```powershell
python eval/agent_v2/replay.py --simulated --output eval/agent_v2/reports/task10-harness-simulated.json
```

This validates only JSONL loading, pair construction, and report labels. It is not an engine replay, a quality score, or a business-write test. A real offline replay must provide a reviewed adapter with `module:function` via `--adapter`; it must read the same sanitized fixture for both engines and return observed text, timings, citations, tool counts, and token usage (or `null` when unknown). Do not put API keys in case files or the report.

Score paired answers manually with the same rubric. Report date parsing, observation handling, and citation changes in their semantic groups. Permission failures and write/idempotency cases are hard gates. With fewer than 30 measured requests for an engine, label p95 as an observation; do not claim statistical significance or a latency pass. The harness does not invent default scores, citations, or token counts.

### Offline paired run (2026-10-20)

The real-model run is recorded in [`eval/agent_v2/reports/task10-real-model-paired-20261020.json`](eval/agent_v2/reports/task10-real-model-paired-20261020.json). Its baseline contains 52 complete pairs (104 rows) using synthetic fixtures and `cn:deepseek-v4.1-flash` through a local OpenAI-compatible proxy. Focused fixture-correction reruns are stored separately under `follow_up_evaluations`; they do not change the baseline p95. No provider key, user token, or database DSN is in the report.

- One reviewer scored 33 cases per engine. The nine failure cases were not fault-injected, and the ten write cases ran with the Java command client disabled, so those 19 cases are not quality-scored and do not pass the failure or write gates. All token usage is unknown; the proxy does not expose cost.
- The original local p95 was 9,973 ms for legacy and 26,061 ms for graph_v2 (52 samples each). It includes model-proxy time and is not a production latency claim. Graph_v2 was about 2.61 times legacy, so that baseline fails the latency gate. These samples predate the targeted follow-up code and fixture changes; there is no post-fix 30-sample p95 measurement.
- The original one-reviewer means were 1.4848 for legacy and 1.6364 for graph_v2 on 33 scored cases. This partial score is not a quality pass. A fixture audit superseded the todo and glossary findings: `pending` was not one of the supported todo statuses (`not_started`, `in_progress`, `completed`), and the glossary case had no matching synthetic diary record. The todo case now asks for a count through the designed `get_stats` tool. With a valid `not_started` fixture, graph_v2 returned one authorized todo. After adding one matching synthetic progress record, both engines answered the glossary case with that record. These focused scores are separate from the original 33-case means.
- The original replay had no successful business writes, 25 source-user records, and 0 command receipts. A later fixture-only replay appended one synthetic source-user record for the glossary case; no Java command endpoint was configured, and follow-up run rows report no business write. This does not validate create-record behavior or idempotency. A final stats/todo replay after that fixture addition did not run: the constrained Java JVM failed native allocation before the SSE adapter started. Final database counters could not be re-read because Docker Desktop's named pipe was unavailable and port 55432 had no listener.
- The 6 accepted focused follow-up runs used 22 model calls. An earlier 2-run glossary attempt against the incomplete fixture consumed another 5 calls and was excluded from scoring. The follow-up meter moved from 377 to 404, matching those 27 calls; the original 18-call discrepancy from the 52-pair baseline remains unexplained. Token usage and cost are unknown.
- The proxy cap was 500. Its counter increased from 33 (discarded earlier attempts) to 377; the aggregate delta was 344, while per-run deltas sum to 326. The 18-request mismatch is unresolved, so per-case call counts must not be used as exact provider usage. The cap was not exhausted.

The offline paired replay and focused corrections are documented, but Task 10 rollout gates are not. Keep `legacy` as the default. The original latency result fails, and there is no post-fix full latency sample, fault-injected evaluation, or write validation through the named Java interface. Reconcile the original metering gap and complete those checks in an approved test environment before considering staged traffic.

## 3. Test rollout order

1. Keep Java `MIRROR_CHAT_ENGINE=legacy`, Python Agent disabled, and frontend unchanged while applying and verifying the test database views and role.
2. Deploy Java and Python versions that understand the new proto while keeping the default engine `legacy`. The Python release health check reports 10003 and 10004 separately. When Agent is enabled, it also requires a successful read-only view probe, verifies that Java's named command endpoint returns HTTP 401 without a grant, and verifies that 10004 rejects a gRPC call without the internal token. It performs no write.
3. Deploy the compatible Vue version through its established release path. Check legacy conversation, graph sources, partial failure, cancellation, create receipt, and history replay in a test account before enabling graph traffic.
4. For offline-only sampling in a test environment, set `MIRROR_CHAT_ENGINE=shadow` and a deliberate sample rate. The user response remains legacy. Shadow is disabled at rate 0 by default, uses one in-process execution slot with no waiting queue, and is skipped for explicit create requests or whenever a foreground graph run is active. A foreground graph request cancels an active shadow call. This does not remove shared CPU/database contention.
5. Before graph_v2 traffic, set shadow sampling back to 0 and choose `MIRROR_CHAT_ENGINE=graph_v2`; put only the approved test user UUID in `MIRROR_GRAPH_ENABLED_USER_IDS`. Each request freezes its engine at acceptance. A failed graph request is not replayed through legacy.

## 4. Health and observation

- 10003 is the legacy RPC server; 10004 is the independent Agent server. A successful TCP check alone is not an authorization or data-boundary check.
- With Agent enabled, configure the release job to bind `MIRROR_AI_READ_DSN` as a secret and set `MIRROR_AGENT_COMMAND_HEALTH_URL` to the Java route ending in `/api/internal/agent/commands/create-record`. The probe sends an empty POST with no credentials and requires 401; it never calls an authenticated write.
- Record engine, outcome, stop reason, phase latency, first thinking/body latency, planning/tool counts, degradation reasons, and actual or unknown token use. Do not export model secrets, SQL, full payloads, or complete traces to the frontend.
- Compare old and new total latency on paired fixtures and user response quality. Online data changes over time, so online observations are not strict pairs.

## 5. Rollback and retirement

Rollback changes only future request selection: set `MIRROR_CHAT_ENGINE=legacy`, set shadow sampling to `0`, and restart Java through its established backend release path. If graph_v2 is disabled, Python Agent can then be disabled. Do not delete created records, revert user data, drop `agent_runs`/receipt tables, or remove controlled views as part of code rollback. Preserve the database backup and migration state.

Do not change the default to graph_v2 until all permission/write idempotency gates pass, no core regression remains unexplained, paired manual quality does not decline, and adequate latency samples meet the agreed threshold. Those rollout measurements were not collected in Task10, so the default must stay `legacy`. Keep old RPCs and prompts for at least one stable release cycle. Search Java call sites before a later, separate removal; keep services and mappers still used by classification, profiles, or ordinary pages.
