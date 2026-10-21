# Agent v2 整合补救验证记录

日期：2026-10-21（本地工作区）

## 当前范围与边界

三端均在 `codex/assistant-growth-agent-v2`，以成长重设计 HEAD 为第一父线，合入各自保留的本地 v2 历史。Java 与 AI 合并已完成冲突收尾；Vue 的三处冲突也已整合。所有业务改动仍是本地工作区/暂存区状态，尚未推送或部署。所有 SQL 验证均在本轮创建的 `codex-agent-v2-it-20261021` 临时 PostgreSQL 16 容器内执行；没有连接或改动现有应用数据库。

Java 继续默认 `legacy`，graph 用户白名单为空、shadow rate 为 0、create-record flag 默认为关闭。graph_v2 暂不具备原有 record/todo/memory/growth prepare-confirm 卡片工具；依赖这些功能的用户不得被加入 allowlist。

## 已完成的代码整合

- Java `AgentRunService` 是请求接纳与助手最终保存的唯一边界；适配每日会话，关联唯一 user turn/run，完成时走最新 `ChatMemoryService.persistAssistantReply`，将可信的 assistant-context 来源 ID 写入最终消息。SSE meta 由服务端消息 ID 绑定卡片来源。
- Java 数据库增量改为 `migration-v11-agent-v2.sql`，接在成长线 v10 后；当前 `schema.sql` 与迁移最终结构同步。AI 只读视图按最新 record authorization、READY/完整 approved embedding、已确认 vault、有效画像及被遗忘聊天来源过滤。
- Java 与 AI 两端的 6 个 proto 文件使用同一编译器生成 descriptor 后，规范化比较的 wire/RPC 描述完全一致；AI `upload/` 运行镜像与源文件同步。
- Vue 保留每日聊天、成长与卡片状态，加入 requestId/run 查询、重试、创建回执与历史回执归一化；真实浏览器三端 SSE/历史重载仍未执行。

## 验证证据

| 仓库/范围 | 结果 |
|---|---|
| Java 全量 Maven（排除需单独运行的 `MuMirrorBApplicationTests`） | 460 项通过，0 失败，0 跳过；环境为临时 PostgreSQL，包含数据库、授权、成长、日常会话、命令事务与 run 并发测试。 |
| Java Agent/growth/chat 数据库子集 | 12 项通过，0 跳过。 |
| Java 数据迁移 | 在隔离 DB 从成长基线 v10 `schema.sql` 加载后执行 v11，创建 8 个 `mirror_ai` 视图、`agent_runs` 与 `agent_command_receipts`。 |
| Python reader grants | 执行 `deploy/agent-reader-role.sql` 后 8 个视图有 SELECT；`mirror_ai_reader` 对 `public.records` 无 SELECT。 |
| AI Python 全量 | `pytest tests -q`：522 项通过。 |
| AI 源/upload | `scripts/sync_upload.py --check` 通过；6 份 proto descriptor parity 通过。 |
| Vue | Node 测试 17/17 通过；`npm run build` 通过；此前 `npm run build:preview` 也通过。 |
| 空白/冲突检查 | 三仓库 `git ls-files -u` 均为空；暂存和未暂存 `git diff --check` 通过。 |

Java 应用上下文 smoke 当前用临时数据源和测试 JWT secret 单独复跑中，结果待补。

## 尚未通过的验收门槛

- Vue Gate 5：尚无真实本地 Java→Python→Vue SSE 与历史恢复流程证据；单测、构建和 preview mock 不代替该门槛。
- 五维报错：已查到成长更新接口返回明确 `R(code,message,data)` 业务响应，前端按业务 code 处理；数据库集成测试覆盖首次自定义和已有账本后的 409 锁定语义。用户现场“系统内部错误”缺少具体请求 URL/方法、脱敏请求体、HTTP/业务响应及同时间 Java stacktrace，当前不推定根因或称为已修复。
- graph_v2：旧离线报告中的延迟/质量/故障注入/真实写入门槛仍未满足；保持 legacy 默认，不启动真实模型评测或在线流量。
