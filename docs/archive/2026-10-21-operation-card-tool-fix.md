# 操作卡工具调用异常分析与修复

日期：2026-10-21。涉及 Mu-mirror-AI、Mu-mirror-B；沿用当前分支与已有未提交改动。

## 现象与证据边界

用户请求“帮我写一个日记，我今天改bug改到头晕”，回答生成了日记草稿，并把 `<tool_call><function=operation_card>...` 当正文输出。系统注册的日记准备工具为 `prepare_record`，不存在 `operation_card` 工具或这套 XML 调用协议。前端通过 SSE `action_proposal` 渲染操作卡，`delta` 只追加正文。

本次没有取得该线上请求的 runId、规划原始响应、路由或日志，所以不能断言当时走了哪个引擎、规划器是漏调用还是失败。以下是源码中确认的相关缺口与本地回归验证，不代表已完成线上验收。

## 确认的缺口及修复

| 缺口 | 修复 | 主要位置 |
| --- | --- | --- |
| legacy 在准备卡片后仍调用回答模型，模型可以编造调用、重复准备或误报状态 | 操作工具返回后使用实际结果生成服务端回复，保留真实 `action_proposal` 事件，跳过最终回答模型；失败和目标选择也走对应回复 | B `ChatServiceImpl`、`PreparedActionReply` |
| graph_v2 对复用的已取消卡仍返回固定的“确认后保存”文案 | 两种引擎共用状态回复逻辑，识别 PENDING/CANCELLED/SUCCEEDED/EXECUTING/STALE/FAILED；缺失有效卡片不报告成功 | B `AgentEventAdapter`、`PreparedActionReply` |
| PREPARE_WRITE 已返回结果，但模型忘记 done=true 时仍继续规划，有重复生成相似卡片的风险 | 完成本批操作准备后停止后续规划；仍允许执行同一批中已规划的其他工具 | B `ToolOrchestrator` |
| 新意图 JSON 包含卡片参数和日记正文，仍共用短路由的 256 token 上限；Anthropic 流式规划也只有默认 4096 上限 | LLM 接口支持按调用指定输出预算；含 PREPARE_WRITE 的 graph 意图、单次规划与循环规划使用 8192 上限；辅助函数与实际 gRPC 入口均覆盖，普通调用保留默认 | AI `llm/*`、`intent_service`、`plan_service` |
| prepare 工具参数不是对象时，被静默改成空对象；NaN 可被输出成非法标准 JSON | prepare 非对象参数剔除；拒绝非有限数值；保留旧 READ 工具的缺省参数兼容行为 | AI `plan_service.sanitize_calls` |
| 最终正文可直接携带伪工具协议 | 两种回答流加入增量过滤，拦截代码块外的 tool_call/tool_calls/function_calls/function= 协议文本，支持跨 chunk、未闭合、多个块；不执行、不解析为业务动作；保留普通文本和围栏代码示例 | AI `tool_output_guard`、`answer_service` |
| “帮我写日记+已给内容”没有明确的卡片生成示例，回答阶段缺少工具能力边界 | 明确选择 prepare_record，卡片本身充当可编辑草稿和确认入口；缺内容才澄清；单纯分享不自动制卡；最终回答阶段不得模拟工具调用 | AI intent/plan/chat prompts |

8192 是输出上限，不是每次实际消耗；仍关闭 JSON 任务思考，不新增模型重试。卡片确认、身份来源、业务执行权限和提交接口保持原有边界。伪协议过滤是输出兜底，不会自动把模型正文转换为可执行操作，也不能保证外部模型永远正确分类。

## 验证

新增回归先在修复前运行，确认截图伪调用泄漏、预算未透传、无卡被当成功、已取消状态误报、准备后继续规划等测试按预期失败，再验证修复后通过。

- AI：`venv/Scripts/python.exe -m pytest tests -q` → **549 passed, 11 skipped**。
- B：`mvn -q -Dtest=ChatRetrievalTest,ChatLoopTest,AgentEventAdapterTest,PreparedActionReplyTest,ToolOrchestratorTest,PrepareTodoToolTest,PrepareWriteToolRegistryTest test` → **90 tests，0 failures，0 errors，0 skipped**。
- B 全量 `mvn -q test` 尝试：**476 tests，0 failures，1 error，45 skipped**。唯一错误为 `MuMirrorBApplicationTests.contextLoads`：缺少 JDBC URL（`Failed to determine suitable jdbc url`），未对该启动测试做跳过或删除断言。之后增加的单次规划与已取消卡回归包含在上述 90 项中。
- AI 根目录直接 `pytest -q` 的全量尝试还会收集手动 RPC 客户端 `test_client.py`，其 6 个函数缺少 `stub` fixture：test_classify、test_embed、test_get_model_info、test_extract_intent、test_chat、test_generate_profile。没有把手动客户端改成假通过；正式 `tests/` 结果如上。
- AI/B `git diff --check` 通过。涉及的 12 个 AI 运行文件均已同步到 `upload/` 并逐文件比较哈希；原 HEAD 中这些镜像文件一致。
- 未使用真实模型计费调用、未进行线上卡片确认或数据库写入验收。没有提交、推送或部署。

## 后续部署验收场景

更新并重启 AI 与 B 后，在实际浏览器检查：

1. “帮我写个日记，我今天改bug改到头晕” → 出现待确认的记录卡，正文可以编辑，确认前不生成正式日记。
2. “帮我写个日记” → 简短询问要记录的内容，不编造日记。
3. “今天改bug改到头晕” → 普通回应，不自动制卡。
4. 取消卡片后重试同一准备请求 → 复用取消状态，不再说等待确认。
5. 准备工具失败 → 明确提示未能生成卡片，不伪报保存成功。
6. legacy 循环开启/关闭及 graph_v2 分别验证；检查 action_proposal 与消息归属，避免只验证纯文本。

开始修改前的 AI/B Git diff 已保存在本机临时目录 `mirror-tool-fix-20261021-baseline`，用于区分本次修复与已有改动；这是补丁快照，不是提交。

## 第二轮：重启后仍不能创建记录卡的真实执行故障

用户重测时间约为 21:26。本地运行日志确认模型已返回合法的 `prepare_record` 计划，参数包含 `kind=record.create`、`rawContent`、`mode=ONLY_SAVE`。Java 失败原因为 **记录候选不允许字段: kind**，并非该轮模型没有调用工具。

### 根因与补充修复

1. `PrepareRecordTool` 将整个 args 作为 `ActionPrepareDTO.payload`，导致外层已提供的 kind 又出现在记录 payload 中；真实 `RecordActionHandler` 拒绝该额外字段。修复为将 kind 只放在操作外层，从记录 payload 删除。保留处理器的字段白名单校验。
2. `ToolOrchestrator` 对准备工具抛出的异常只记录审计，没有将失败结果加入返回列表。上层因此看到零条工具结果，绕过操作失败回复，继续调用回答模型。单次及循环规划现在均会保留 PREPARE_WRITE 参数解析/执行失败的结构化结果；循环里的准备尝试结束后不再继续规划。
3. 初轮测试对操作服务的模拟未覆盖该参数接缝。本次增加真实 `PrepareRecordTool → ActionProposalServiceImpl → RecordActionHandler → RecordDraftServiceImpl` 组合回归，只模拟数据库和外部 I/O；先复现同一 kind 报错，再验证生成待确认候选、未进行正式业务写入。另补准备异常必须返回失败结果的测试。

### 第二轮验证结果

- Java 相关 8 组测试合计 **119 项，0 failures，0 errors**。
- 完整 `mvn -q test`：480 项，0 failures，1 error，45 skipped。唯一错误仍为 `MuMirrorBApplicationTests.contextLoads` 缺少 JDBC URL。
- Java 已重新编译并重启到端口 9050；Python 原修复进程保持运行。
- 约 21:37，使用本地接口创建独立临时测试账号，仅复制用户已有的加密模型配置，串行发送一次真实聊天请求：“今天修mirror项目修了好久，帮我记下这个”。收到真实 SSE `action_proposal`、`action_status`、`delta`、`done`；只有一张 `record.create` 卡，数据库状态 `PENDING / READY`，payload 不再包含 kind；done 为 completed/action_prepared。
- 服务端回复：“我把内容放进记录卡了，你看一眼；确认后我再保存。”，正文没有伪工具调用。
- 确认前，测试账号正式 records 数量为 **0**。未点击确认；验证后已清理该次创建的临时账号及其测试数据，未修改用户原有记录和卡片。
- 这是实际模型、HTTP/SSE 与数据库候选卡链路验证；浏览器展示与点击确认后的保存仍由用户重测。
- 没有提交、推送或远程部署。

## 第三轮：仅保存候选误报五维定义变化

用户反馈编辑记录卡时出现“五维定义已变化”，而维度描述一致。只读查询确认：用户五个 growth_axes 的 definition_version 全部为 1，历史表只有这五项初始版本，总版本为 5；最新待确认卡为 ONLY_SAVE，axesVersion 为 0，growthSuggestions 为空。没有证据表明五维定义发生过修改。

根因：RecordActionForm 对所有记录模式都展示评分，并在编辑评分或提交正文时自动补齐五个评分项。仅保存草稿本来不参与成长评分，其默认 axesVersion=0 被错误地当作旧评分版本与当前 5 比较。旧版表单若把占位评分保存进卡片，后端在 ONLY_SAVE 分支前也会错误执行评分版本校验。

修复：

- 前端仅对 AI_ASSISTED 候选展示评分；ONLY_SAVE 编辑正文不制造评分，提交会去掉旧表单产生的评分占位项，卡片也不再对仅保存候选误做版本拦截。
- AI 候选没有评分时，编辑正文不自动补五个零分；首次手工新增评分时绑定当前定义版本。已有 AI 评分版本不一致仍禁止提交，不会自动改版本绕过校验。
- 后端仅在 AI_ASSISTED 且存在评分建议时检查成长版本，ONLY_SAVE 仍走原有的仅保存分支，不写成长账本。
- 没有修改用户五维、待确认卡片、日记或账本数据。

验证：真实 Vue 组件 setup 回归先复现 5 项失败，修复后新增的 7 项全部通过；前端 node --test 总计 24 项通过，npm run build 成功。后端 RecordActionHandlerTest 共 11 项通过，含仅保存旧占位卡可确认、过期 AI 评分仍被拒绝两种分支。完整 Maven 尝试 482 项，0 failures，1 error，45 skipped；唯一 error 仍为 contextLoads 缺少 JDBC URL。

本地 Vite 5175 已核实提供带 supportsGrowthReview 的更新组件，Java 9050 已重新启动加载后端修复。未提交、推送或远程部署。

## 第四轮：纠正助手新增记录的默认模式偏差

用户指出原设计要求助手添加的记录也进入 AI 分析并有五维建议。复核发现本次第一轮修复添加的三处示例错误地固定为 ONLY_SAVE，导致普通“帮我记下来”跳过分析。这是本次修复引入的产品行为偏差，不应把它解释成助手记录本来不参与评分。

已按用户确认的行为纠正：助手新增/整理记录默认 AI_ASSISTED，准备分类、标签与五维建议，确认后保存并授权，记录完整 READY 后再按既有流程入成长账本。仅用户明确要求仅保存、不分析或私密记录时才使用 ONLY_SAVE。

- 修改 agent_intent、plan_tools、plan_tools_single，及各自 upload 镜像。
- PrepareRecordTool 定义声明默认 AI_ASSISTED；模型漏传 mode 时 Java 也补为 AI_ASSISTED。模型明确传入 ONLY_SAVE 的合法分支仍保留，不修改用户其他页面的默认保存偏好。
- 初轮误报修复继续有效：真正的 ONLY_SAVE 卡不做评分；AI_ASSISTED 卡保留五维版本校验。没有把旧的仅保存卡片自动转换为分析模式。
- Java 新增测试先复现默认模式错误，再验证“即使页面默认仅保存，助手工具缺省仍准备 AI 分析候选”；原显式 ONLY_SAVE 测试继续验证不调用分类。
- Python tests/：552 passed、11 skipped。Java PrepareRecordToolIntegrationTest：2 项通过；完整 Maven：483 项、0 failures、1 error、45 skipped，唯一错误仍为 contextLoads 缺少 JDBC URL。
- 两个本地后端已重启。使用独立临时账号串行发送一次真实聊天请求，并轮询真实卡片 API 至分析完成：卡片为 record.create / PENDING / READY，recordDraft.mode=AI_ASSISTED、axesVersion=5、growthSuggestions 共 5 项；确认前正式 records 为 0。临时账号及测试数据已清理。
- 本轮真实验证包含模型规划与后台分类，未确认卡片，未进行正式记分或向量写入验收。未提交、推送或远程部署。
