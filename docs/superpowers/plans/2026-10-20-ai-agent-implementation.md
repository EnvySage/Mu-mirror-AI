# Mirror AI Agent v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留旧对话链路的前提下，构建Python直接查询受控AI数据、以LangGraph编排对话、通过Java具名接口创建记录的新引擎。

**Architecture:** Java保留鉴权、配置解密、会话/消息持久化与SSE入口。Python加载词典/历史、执行检索和读工具；Java只承接具名业务写操作。旧RPC与新Agent分端口/执行池；每轮引擎冻结，不中途切换重跑。

**Tech Stack:** 现有Python协议客户端、LangGraph StateGraph/custom streaming、Psycopg3只读池、gRPC、现有Spring Boot/MyBatis/PostgreSQL+pgvector、Vue。

**Spec:** [完整设计](../../2026-10-20-ai-agent-architecture-v2.md)。本清单用于未来实施，本次不执行代码改造或部署。

## Global Constraints

- Python根目录：`C:/Users/cheng/Desktop/claude/own/Mu-mirror-AI`（下文AI）；Java根目录：`C:/Users/cheng/Desktop/claude/own/Mu-mirror-B`（B）；前端根目录：`C:/Users/cheng/Desktop/claude/own/Mu-mirror-F`（F）。文件清单均相对指定仓库。
- 当前旧RPC为10003、workers=4；新Agent使用10004、workers=2、maximum_concurrent_rpcs=2。
- 前端入口仍是POST `/api/mirror/chat`；engine为legacy/graph_v2/shadow，默认legacy；旧chat-loop-enabled只控制legacy内部行为。
- Python只读账号仅访问设计第6节的8个视图；每个查询强制ReadScope；不直接读取user_settings、records整条原文或vault_blobs。
- 新版单路检索、规划最多4轮、每批最多2工具、循环180000ms、整轮480000ms；日记上下文最多5段、画像最多2份；向量1024维；首版顺序执行。
- 首版创建只保存当前明确提交的原文，≤2000字、requestAiProcessing=false；只开放create_record，不开放修改、删除、自动授权。
- api_key、DSN、token、客户端和连接不进入State/事件日志/前端；不启用checkpointer；不引入LangChain模型封装、ORM、Redis、MQ。
- 改AI运行代码必须同步upload镜像；不手改生成stub；不改旧prompt行为来掩盖新旧差异。
- 开工先保存可审阅baseline；保留用户已有改动；按用户偏好保持代码未提交，只有用户明确要求才commit。下文每任务以diff审阅为收尾，不自动创建提交。
- 新增依赖候选langgraph==1.2.16、psycopg[binary]==3.3.6、psycopg-pool==3.3.3，必须先在独立Python3.12环境验证。当前本地Python3.14.6不是生产兼容性证明。

## Review Focus

1. 一条记录部分片段已授权、部分未授权：Python只能看到已授权segment，不能通过整条content或统计泄漏其余内容（Task 2）。
2. 创建事务提交但网络结果丢失：同call只创建一次；只能报告回执已确认成功或unknown，不能让模型换ID重试（Task 7）。
3. 新会话重复requestId、两个浏览器同时发同一会话：用户消息/会话不重复，第二个活跃run返回409，不混淆上下文（Task 6）。
4. 已生成部分正文后断流、用户取消或Java进程重启：不换引擎重跑、不清空正文、不自动再次写入（Tasks 6、8）。
5. 只读池不可用、Embedding配置错误或维度不符：保留硬过滤与明确降级原因，绝不静默换模型或把未命中说成未记录（Tasks 3、4、5）。

---

## Task 1（M0）：基线、共享函数和部署镜像边界

**Files:**
- AI Modify: `tests/test_round9.py`、`services/chat_service.py`、`services/plan_service.py`、`server.py`、`requirements.txt`、`requirements-minimal.txt`、`Dockerfile`。
- AI Create: `services/intent_service.py`、`services/answer_service.py`、`scripts/sync_upload.py`、`tests/test_service_extraction.py`、`tests/test_upload_sync.py`。
- AI Create: `docs/agent-v2-baseline.md`，记录旧链路配置、包版本、文件hash、测试结果与已知限制，不存密钥。
- AI Mirror: 对应upload运行文件；文档、tests不复制。

**Interfaces:**
- Produces `extract_intent(query, glossary, llm_config, *, llm_factory=create_llm) -> IntentResult`，保持旧字段白名单与失败映射。
- Produces `build_answer_messages(question, history, chunks, glossary, tool_results, *, prompt_name="chat") -> list[dict]`、`stream_answer(messages, chunks, tool_results, llm, *, cancel=None) -> Iterator[AnswerEvent]`；旧路径默认行为不变。
- Produces `plan_next_step(request, llm, *, prompt_name="plan_tools", cancel=None) -> Iterator[PlanEvent]`；旧servicer负责protobuf转换与gRPC错误。
- Produces `sync_upload(repo_root: Path, *, check: bool=False) -> list[Path]`，确定性runtime文件集合，忽略真实配置覆盖；check模式只报告漂移。

- [ ] Step 1：分别记录三个仓库git status与diff；对原建议文档计算SHA256。使用临时目录保存需保留的preimage，不覆盖旧建议、不自动commit。
- [ ] Step 2：在现有venv运行 `venv/Scripts/python.exe -B -m pytest tests/ -q -p no:cacheprovider`。预期先复核本次已知358通过/7个httpx2导入失败；若不同，按实况记录，不能套用旧数字。
- [ ] Step 3：检查真实SDK依赖；修复错误测试夹具对不存在httpx2的依赖，使用与锁定SDK兼容的HTTP request/response对象。断言401/403/404/429/5xx映射仍相同，不用skip掩盖失败。
- [ ] Step 4：先增加共享函数输出与旧servicer一致的测试，覆盖thinking/content顺序、final/done、假引用跨块、sources跳号、词典为空。运行新测试确认尚缺共享函数导致FAIL。
- [ ] Step 5：抽取上述业务函数，让旧servicer委托调用；默认prompt、历史重复等旧行为暂时不在此任务改变；单独调用这些函数不应依赖gRPC context。
- [ ] Step 6：新增upload同步测试并实现脚本；配置只同步无密钥的模板/约定服务配置，禁止将环境DSN/token写入镜像。运行全套单测及 `scripts/sync_upload.py --check`，预期全部通过、无漂移。
- [ ] Step 7：用现有可用Python3.12运行时或隔离容器验证原minimal依赖，再加入三个候选依赖；做pip check、proto导入、StateGraph/custom stream与psycopg pool导入冒烟。此步骤可以联网下载依赖，但不修改现有venv；失败则先处理兼容性并更新设计的锁定值，不继续M1。对齐Dockerfile使用minimal依赖；实际升级SDK作为单独变更记录。
- [ ] Step 8：审阅diff，确认旧模板、原建议文档hash未变，记录实际通过数量和未执行E2E。此任务完成后才启动后续改造。

## Task 2（M1）：数据库只读边界与作用域

**Files:**
- B Create: `src/main/resources/db/migration-v4-agent-v2.sql`、`deploy/agent-reader-role.sql`。
- AI Create: `agent_data/__init__.py`、`agent_data/scope.py`、`agent_data/pool.py`、`agent_data/records.py`、`agent_data/glossary.py`、`agent_data/history.py`、`agent_data/profiles.py`、`agent_data/vault.py`、`agent_data/todos.py`。
- AI Create: `tests/agent_v2/fixtures/agent_data.sql`、`tests/agent_v2/conftest.py`、`tests/agent_v2/test_read_scope.py`、`tests/agent_v2/test_data_access_integration.py`。

**Interfaces:**
- Produces `ReadScope(user_id: UUID, session_id: UUID, user_message_id: int)`，缺任一字段拒绝构造。
- Produces `create_read_pool(dsn: str) -> ConnectionPool`、`read_transaction(pool, scope)`上下文管理器；连接参数与超时按设计第6/13节。
- Produces `RecordRepository.search(scope, filters, *, limit=20) -> list[RecordChunk]`、`stats(scope, window) -> RecordStats`、`coverage(scope, keyword) -> Coverage`。
- Produces `GlossaryRepository.top_confirmed(scope, limit=30)`、`HistoryRepository.recent(scope, limit)`、`ProfileRepository.recent(scope, limit=2)`、`compare(scope, first_id, second_id)`。
- Produces `VaultRepository.find(scope, query, filters, embedder, limit=10)`、`recall(scope, item_id, query, embedder)`；`TodoRepository.stats(scope)`。
- Consumes PostgreSQL现有schema+migration-v3；测试连接仅指向显式设置的隔离测试库。

- [ ] Step 1：构造A/B用户，同名文件、相同正文、混合授权片段、processing/reviewing、已删除、失效画像、未确认文件、todo仅独立授权等fixture。fixture只能在隔离测试库运行。
- [ ] Step 2：编写失败集成断言：A看不到B；混合授权记录不返回未授权segment/整条content；invalidated画像不可见；pending词典不可见；未确认文件只能见元数据；SQL注入式query仅当字符串处理；limit<1拒绝、超限截断。
- [ ] Step 3：实现设计的8个视图与只读角色脚本；role脚本由管理员运行，仅收账号参数，不写密码。视图投影字段固定，不用SELECT *；新增views不修改旧业务SQL。
- [ ] Step 4：实现repositories，所有读取都接收ReadScope，模型参数不能覆盖scope。计数DISTINCT record_id；coverage去重日期；关键词过滤在LIMIT前。读取后立即归还连接，模型调用不得持有连接。
- [ ] Step 5：使用 `MIRROR_TEST_READ_DSN` 运行 `venv/Scripts/python.exe -m pytest tests/agent_v2/test_data_access_integration.py -q`。必须用真实只读角色证明UPDATE/INSERT/SELECT user_settings/SELECT vault_blobs被拒绝；缺测试库时可skip开发检查，但M1验收不能标PASS。
- [ ] Step 6：连接池并发借还测试：请求超过pool上限有界失败，不永久等待；取消/异常后连接可复用。审阅迁移为新增结构，不含历史授权回填或删除。

## Task 3（M1）：日期、上下文和新版意图

**Files:**
- AI Create: `agent_core/__init__.py`、`agent_core/types.py`、`agent_core/time_ranges.py`、`agent_core/context_loader.py`、`prompts/agent_intent.txt`。
- AI Modify: `services/intent_service.py`、`prompts_loader.py`、`config.py`、`config.example.yml`。
- AI Create: `tests/agent_v2/test_time_ranges.py`、`tests/agent_v2/test_context_loader.py`、`tests/agent_v2/test_understand.py`。

**Interfaces:**
- Produces `normalize_time(expr: TimeExpression, reference_date: date, timezone: str) -> TimeWindow | Clarification`。
- Produces `load_context(scope, question, repositories) -> PreparedContext`，含top30、matched、matched_ids、plan_history、answer_history。
- Produces `understand(question, prepared, runtime) -> IntentResult`；正文/命令grant来自可信runtime时走确定性create_record分支，其余调用一次轻量模型。
- Task 3在types定义Evidence/ToolObservation/ToolCall/AgentEvent/AgentState/RuntimeContext公共名称；后续任务使用同名，不另建重复dict协议。

- [ ] Step 1：时间表驱动测试固定reference_date=2026-10-20，精确断言昨天、上周、2026年8月、最近7天的起止；闰年/跨年/非法日期/offset非法/未提供年份歧义进入Clarification。
- [ ] Step 2：上下文测试断言仅confirmed top30、稳定排序、alias匹配、无命中回退top30；按当前消息ID排除当前问题及之后消息，过滤fallback；规划12条/回答6条正序且无跨会话数据。
- [ ] Step 3：实现normalize_time和load_context；不使用LLM做日期算术，不给Python写词典命中数的权限。
- [ ] Step 4：新版意图测试断言历史中的“方案”能参与独立问题改写；schema脏值被校验；明确授权正文不调用LLM；模型说create但无grant不能执行写入。
- [ ] Step 5：实现agent_intent模板与understand，新模板只服务graph_v2。运行 `venv/Scripts/python.exe -m pytest tests/agent_v2/test_time_ranges.py tests/agent_v2/test_context_loader.py tests/agent_v2/test_understand.py -q`，预期全部通过，旧意图测试仍通过。

## Task 4（M2）：检索、9个只读工具与证据

**Files:**
- AI Create: `agent_core/retrieval.py`、`agent_core/evidence.py`、`agent_tools/__init__.py`、`agent_tools/registry.py`、`agent_tools/read_tools.py`、`prompts/agent_plan.txt`、`prompts/agent_chat.txt`。
- AI Modify: `llm/base.py`、`llm/factory.py`、`llm/openai_llm.py`、`llm/anthropic_llm.py`、`embedding/api_embedder.py`、`embedding/factory.py`、`services/answer_service.py`。
- AI Create: `tests/agent_v2/test_retrieval.py`、`tests/agent_v2/test_read_tools.py`、`tests/agent_v2/test_evidence.py`、`tests/agent_v2/test_runtime_clients.py`。

**Interfaces:**
- Produces `retrieve(intent, scope, repositories, embedder, *, half_life_days=30, limit=5) -> RetrievalResult`。
- Produces `execute_read_tool(call: ToolCall, runtime: RuntimeContext) -> ToolObservation`，支持设计第8节9个读工具；registry暴露JSON参数定义与固定effect=read。
- Produces `merge_evidence(existing, incoming) -> list[Evidence]`、`assign_citations(evidence) -> CitationIndex`、`render_observations(observations, max_chars=2000) -> str`。
- 客户端新增keyword-only可选timeout/max_retries参数，旧调用默认不变；新版使用remaining deadline且max_retries=0。Embedding直接调用对象方法，不调用gRPC。

- [ ] Step 1：失败测试覆盖四路及回退、1024维校验、纯距离与排序分分离、0.35/0.50阈值、明确时间关闭衰减、keyword先过滤后LIMIT。Embedding不可用保留硬过滤，返回degraded，不换模型。
- [ ] Step 2：工具断言：find返回文件ID/候选层级，recall正文仅confirmed；get_stats的0是有效数据；get_coverage不同日期数正确；save_item无写副作用；get_glossary与top30一致；参数中的user_id被拒绝。
- [ ] Step 3：实现retrieve和9工具；文件查找按设计三层顺序；recall缺ID兼容补接由runner负责，repository不随意选别人的/其他文件。
- [ ] Step 4：证据测试断言find+recall同文件只占一个Fn；同记录不同chunk都有可定位n；重复chunk去重；profile不生成record_id=0可点击来源；跳号sources与正文一致。
- [ ] Step 5：实现证据和新版模板；planner同时看到检索摘要、文件ID、统计分布；回答历史仅作为messages一次。保留FakeCiteFilter逐块运行，不在全文生成后才过滤。
- [ ] Step 6：新增客户端测试：新timeout向下传，重试为0，取消关闭流，旧默认参数和Anthropic/OpenAI协议仍通过。运行本任务四个测试文件与旧LLM/引用测试，预期全部通过。

## Task 5（M2）：完整只读图与独立Agent gRPC

**Files:**
- AI Create: `agent_core/graph.py`、`agent_core/nodes.py`、`agent_core/runner.py`、`services/agent_service.py`、`proto/agent.proto`。
- AI Modify: `server.py`、`generate_proto.py`、`config.py`、`config.example.yml`；生成 `generated/agent_pb2*.py`并同步upload。
- B Create: `src/main/proto/agent.proto`；Modify: `docs/coordination/shared-protocol.md`。
- AI Create: `tests/agent_v2/test_graph.py`、`tests/agent_v2/test_agent_grpc.py`、`tests/agent_v2/test_budget_cancel.py`。

**Interfaces:**
- Produces `build_graph() -> CompiledStateGraph`（无checkpointer）。
- Produces `run_agent(request: AgentRunRequest, dependencies, cancellation) -> Iterator[AgentEvent]`。
- Produces `AgentRunnerServicer.RunAgent(request, context)`，严格对应设计第11节proto字段号；只有custom事件出边界。
- Event.seq从1递增；一轮至多一个completed/failed；RuntimeContext资源在finally关闭。

- [ ] Step 1：用脚本模型/工具写图测试：done=true+calls执行最后一批；空计划；第4轮终止；重复规范化参数终止；planner缺终帧仍保留已有证据；正常文件补读与异常不补读；两轮invoke之间状态全重置。
- [ ] Step 2：实现顺序图与runner；plan_rounds/tool_call_count分别累计；循环180s与整轮480s独立；recursion_limit=64只作最后保护。
- [ ] Step 3：proto按设计实现，更新generate_proto.py生成列表和相对import修复规则；调用生成脚本。两仓proto逐字节相同；不手改stub。
- [ ] Step 4：gRPC测试在临时端口启动独立server；无内部token在查库/调用模型前被拒绝；run_id与seq正确；保留10003旧RPC；第3个Agent请求立即RESOURCE_EXHAUSTED，不无限排队。
- [ ] Step 5：deadline/cancel测试用可控时钟与阻塞桩：取消后不启动下一工具；流关闭；预算只减不重置；数据库异常触发受控失败；不得靠后台线程遗留继续执行。
- [ ] Step 6：只读全链路从临时Agent server到隔离PG，检查Java查询HTTP/gRPC调用计数为0。运行 `venv/Scripts/python.exe -m pytest tests/agent_v2/test_graph.py tests/agent_v2/test_agent_grpc.py tests/agent_v2/test_budget_cancel.py -q`，预期全部通过；同步检查upload。

## Task 6（M3）：Java运行生命周期、引擎分派与结果落库

**Files:**
- B Create: `agent/AgentRunService.java`、`agent/AgentLifecycleService.java`、`agent/AgentEventAdapter.java`、`agent/AgentRunRepository.java`、`grpc/AgentGrpcClient.java`、`config/AgentProperties.java`。
- B Create: `pojo/DO/AgentRun.java`、`mapper/AgentRunMapper.java`、`controller/AgentRunController.java`。
- B Modify: `service/impl/ChatServiceImpl.java`、`controller/MirrorController.java`、`pojo/DTO/ChatRequestDTO.java`、`pojo/DO/ConversationHistory.java`、`pojo/VO/ChatSessionVO.java`、`tools/AuditService.java`、`pojo/DO/ToolCall.java`、`service/GlossaryService.java`、`service/impl/GlossaryServiceImpl.java`、application.yml和migration-v4。
- B Test: `agent/AgentRunServiceTest.java`、`AgentEventAdapterTest.java`、`AgentLifecycleTest.java`、`AgentRunConcurrencyIT.java`。

**Interfaces:**
- Produces `startOrFindRun(userId, ChatRequestDTO) -> StartRunResult`，返回new/running/completed/gone/conflict及稳定run/session/message ID。
- Produces `ChatInvocation(userId, sessionId, runId, userMessageId, question)`供两个引擎使用；legacy从消息已落库后的阶段开始，不能再次建会话/插user消息。
- Produces `GlossaryService.recordQueryHits(UUID userId, UUID runId, List<Long> termIds)`，在服务内失效private缓存，以run的glossary_hits_applied标记原子去重。
- Produces `AgentGrpcClient.run(request, listener) -> CancellableCall`；从Java配置构建10004独立channel，提供两个模型配置，绝不提前调用旧意图/检索。
- Produces `ChatFinalResult`与`onEvent(run, event)`、`completeRun(...)`、`cancelRun(...)`；两个引擎最终落库只经过completeRun。legacy streamAnswer/finishWithFallback返回结果，不再自行插assistant/发done；SSE done在公共持久化之后。Task 7启用写工具后在此核对回执。
- Produces GET `/mirror/runs/{run_id}`；归属严格校验；私密字段不出边界。

- [ ] Step 1：run测试先锁定user_id+requestId唯一、question/session冲突409、首轮null session重放不生成新session/user消息、同会话并发409。设计迁移包含索引与nullable旧字段，无历史回填。
- [ ] Step 2：实现短事务接纳与engine选择；旧逻辑抽成接收ChatInvocation的legacy入口，移除重复的消息接纳但保留后续语义；graph_v2不能执行旧retrieve/loop。run写入后不修改engine。
- [ ] Step 3：实现run锁接口，Java发run input包含可信作用域、user_message_id、reference_date；本任务allowed_write_tools为空、command_grant为空，写授权接入留给Task 7。
- [ ] Step 4：事件测试覆盖run_id错、seq重复/倒序、无terminal、两个terminal、final text不一致、sources跳号、工具审计重复。读审计按run/call唯一，Java已审计写操作不重复插入。
- [ ] Step 5：实现消息唯一落库、created_records、词典命中计数一次且只更新本人的confirmed词条，调用旧GlossaryService缓存失效；shadow禁止更新。
- [ ] Step 6：取消/重启测试：断线触发ClientCall.cancel；续租停止后run abandoned；已创建回执仍可查；已完成后onCompletion不改成cancelled。任务清理30天记录，删除会话后同requestId返回410，不重建。
- [ ] Step 7：运行B本任务单测及现有ChatLoopTest、ChatRetrievalTest、ToolOrchestratorTest、VaultRefsParseTest；PG并发集成单独运行。预期legacy行为仍通过，graph_v2一轮仅一份消息；无任何“失败后自动legacy重跑”。

## Task 7（M4）：Java创建命令与幂等回执

**Files:**
- B Create: `src/main/java/org/xianshen/mumirrorb/agent/AgentWriteGrantService.java`、`AgentCommandService.java`。
- B Create: `controller/AgentCommandController.java`、`common/security/AgentCommandAuthFilter.java`（均位于同一org/xianshen/mumirrorb根包）。
- B Create: `pojo/DO/AgentCommandReceipt.java`、`mapper/AgentCommandReceiptMapper.java`；在migration-v4加入回执表。
- B Modify: `config/SecurityConfig.java`；复用Task 6已实现的run锁/状态访问接口。
- B Test: `src/test/java/org/xianshen/mumirrorb/agent/AgentCommandServiceTest.java`、`AgentCommandAuthTest.java`、`AgentCommandTransactionIT.java`。
- AI Create: `agent_tools/command_client.py`、`tests/agent_v2/test_command_client.py`。

**Interfaces:**
- Produces Java `authorizeCreate(String question, AgentRun run) -> Optional<CreateGrant>`，只识别设计三种锚定前缀，规范化正文/hash；`issue/verify`使用Java专用HMAC密钥，Python只持有opaque token。
- Produces Java `createRecord(VerifiedGrant grant, UUID callId, String content) -> CommandReceipt`、`getReceipt(VerifiedGrant grant, UUID callId) -> Optional<CommandReceipt>`。
- Consumes Task 6 `AgentRunRepository.lockRun(UUID)`；该接口与数据库实现由Task 6提供。
- Produces Python `CommandClient.create_record(runtime, content) -> CommandResult`；固定UUIDv5调用ID，POST不自动重试，超时后只查同一回执。

- [ ] Step 1：授权测试覆盖空正文/2001字/历史或文件里的指令/缺前缀/含糊“确认”；均不能获得grant。明确原文获得唯一槽位；模型更改一字hash不符。
- [ ] Step 2：服务测试断言传RecordService的DTO没有title/date字段且requestAiProcessing=false；同call同内容返回同record；不同内容409；不同用户不能查回执；shadow、过期、失去租约、取消run不允许新写入。
- [ ] Step 3：实现grant、内部认证filter与controller，并在Task 6的RunAgent输入组装处接入按需write grant；header使用X-Mirror-Command-Grant，现有JWT filter不误识别；路径没有permitAll旁路。关闭filter的Servlet重复注册。
- [ ] Step 4：实现事务内run行锁+记录创建+回执+写工具审计；传播加入同一业务事务。用真实隔离PG并发两个同call请求，断言只有一条记录、一条回执、一条审计；故障注入在record insert后抛异常，三者全部回滚。
- [ ] Step 5：Python client测试：POST返回丢失但GET回执存在→success；GET也不可用/无结果→unknown；断言无第二次POST、无新call_id。明确业务拒绝→failed。
- [ ] Step 6：运行B `mvn -Dtest=AgentCommandServiceTest,AgentCommandAuthTest test`；集成 `AgentCommandTransactionIT`显式连接测试PG后运行。缺PG不能宣称原子幂等通过；AI command_client测试通过。写工具开关仍默认false。

## Task 8（M3/M4）：前端请求ID、部分失败与创建回执

**Files:**
- F Modify: `src/stores/chat.js`、`src/api/chat.js`、`src/views/ChatView.vue`。
- F Create: `src/components/molecules/CreatedRecordReceipt.vue`、`tests/chat-protocol.test.mjs`。
- F Modify: package.json仅增加 `test:chat-protocol`脚本（Node自带node:test，不为此安装整套测试框架）。如需纯函数测试，将事件归一化抽至 `src/utils/chatProtocol.js`。

**Interfaces:**
- `getRun(runId)`调用Java既有JWT客户端；外部请求字段为camelCase requestId/sessionId，与proto的snake_case隔离。
- Store每次主动send生成UUID，网络重试保留该UUID；meta保存runId/requestId；done将created_records归一为 `{recordId,status,createdAt}`。
- 回执组件通过现有UI store的selectedRecordId/记录抽屉打开记录；不新造不存在的记录ID路由。

- [ ] Step 1：Node测试断言同一次发送重连沿用ID；新发送生成新ID；partial error保留正文；未知新增字段不破坏旧meta/done；created_records/history回放一致。
- [ ] Step 2：实现store/API/纯协议适配；done.status=failed仍停止typing并显示错误状态；创建unknown提示“结果待确认”，不能显示明确失败并引导盲目重建。
- [ ] Step 3：实现轻量创建回执组件，展示仅记录状态与查看入口；普通sources、vault_refs保留原组件。
- [ ] Step 4：运行 `npm run test:chat-protocol`、`npm run build`。浏览器手工验证旧版对话、新版有来源回答、部分流失败、取消、创建成功、刷新历史恢复卡片。demo preview不发送真实请求，保持现有mock分支。

## Task 9（M4）：接通创建分支并做业务E2E

**Files:**
- AI Modify: `agent_core/graph.py`、`agent_core/nodes.py`、`agent_tools/registry.py`。
- AI Create: `tests/agent_v2/test_create_flow.py`、`tests/agent_v2/e2e_create_record.py`。
- B/F：使用Tasks 6–8接口，不新增并行命令协议。

**Interfaces:**
- create_record分支只使用runtime授权正文与grant；返回created_records与确定性action_reply，不依赖LLM生成成功确认。
- action_unknown/failed/success分别对应outcome与stop_reason；created_record不能作为已授权AI记忆sources。

- [ ] Step 1：图测试断言明确创建时不做初始RAG、不调用LLM重写正文；无grant时registry不提供create_record且执行层拒绝；shadow使用模拟client。
- [ ] Step 2：接通图和SSE回执；处理写成功后网络/模型失败的Java回执收尾，不能重复delta或同时给出成功和失败的矛盾信息。
- [ ] Step 3：隔离测试环境E2E：发送“帮我记一篇日记：今天把镜子的聊天功能跑通了”，词典含镜子别名；数据库正文保持原文、source=user、record_only、ai_processing_authorized=false，没有自动生成approved chunks/待办。
- [ ] Step 4：相同requestId重发、并发同call、响应丢失、点击取消、创建后删除再重放，分别检查一条记录或明确已删除事实；下一次新requestId主动保存相同正文可新建。
- [ ] Step 5：生成E2E报告，包含真实执行环境与桩/真实模型标记，不能把模拟创建算业务写入成功。只在隔离库执行，不触碰真实用户数据。

## Task 10（M5）：对照、部署与退役条件

**Files:**
- AI Create: `eval/agent_v2/cases.jsonl`、`eval/agent_v2/replay.py`、`eval/agent_v2/report.py`、`tests/agent_v2/test_shadow.py`。
- AI/B Create: `docs/agent-v2-rollout.md`（各自仓库可放对应操作段，主文档互链）。
- AI Modify: `deploy/ai-release.sh`、`Jenkinsfile`、config示例与upload；B按既有发布路径新增配置，不新建未验证的部署体系。

**Interfaces:**
- replay输入包含case_id、fixture_id、question、history、reference_date、model配置引用和预期事实/操作；不保存真实api_key。
- report输出engine、outcome、stop_reason、各阶段耗时、首次正文、调用次数、引用正确性、人工评分、token已知/未知；按语义变化分组。
- shadow run独立ID/parent，不写正式消息、审计、词典计数，不执行Java命令；过载跳过。

- [ ] Step 1：准备至少40题，覆盖设计第14节；同一fixture、模型和时间配对回放。包含权限与失败用例，不能只测顺利问答。
- [ ] Step 2：测试shadow隔离、并发优先级、在线写请求排除、模拟结果标识；运行离线报告，分别标注日期/observation/引用等行为改进。
- [ ] Step 3：部署文档写明先备份/应用新增schema与视图，再部署兼容新proto但legacy默认的Java/Python，再部署前端，再为测试用户启用graph_v2。候选依赖与实际部署PG版本必须在测试环境验证。
- [ ] Step 4：健康检查区分10003和10004、只读SELECT与命令鉴权；Agent关闭时旧服务不因缺DSN而启动失败。回退只切后续请求到legacy，不回滚记录、不删除新增表。
- [ ] Step 5：评测门槛按设计第14节，p95样本不足写观察值，不宣称统计显著。小流量通过后设graph_v2默认；稳定一个发布周期后另开清理变更删除旧对话专用代码。
- [ ] Step 6：最终检查旧RPC/Mapper是否被分类、画像、普通页面引用；有引用继续保留。同步根目录/upload，更新README中“Python只纯推理”的旧说明，明确只读AI访问和业务写工具边界。

## 交付自检

- [ ] 设计第1–17节都有对应任务；原建议不被覆盖。
- [ ] 公共类型、接口签名、proto字段和配置名在任务间一致；新增字段兼容旧客户端。
- [ ] 五项Review Focus各有明确的失败/成功断言与负责的任务。
- [ ] “单测通过”“隔离PG集成通过”“桩E2E”“真实模型E2E”“部署验证”分开报告；未运行不能写PASS。
- [ ] 每阶段可以停止并继续使用legacy；没有把创建成功后自动重跑旧版当作回滚。
- [ ] 所有实现改动保持可审阅，不未经用户要求提交或部署。
