# Mu-mirror-AI 历史摘要

本文件汇总已归档文档的结论，避免为了查历史反复读旧稿。具体原文在 `archive/`，代码实现与当前边界以仓库代码、`README.md`、`2026-10-25-langgraph-chat-implementation.md` 和 `agent-v2-rollout.md` 为准。

## 时间线

- 2026-07/08：早期 AI 日记、Python gRPC 服务与优化设计（`archive/2026-07-23-*`、`archive/2026-08-*`）。已被后续实现取代。
- 2026-10-20：Agent v2 架构稿与 M0 基线。确定 Python 编排、Java 业务写入、8 个只读视图、独立 10004 端口。原始架构稿在 `archive/2026-10-20-ai-agent-architecture-v2.md`。
- 2026-10-21：三端 Agent v2 合并补救与操作卡工具修复。记录在 `archive/integration/agent-v2-recovery-report.md` 与 `archive/2026-10-21-operation-card-tool-fix.md`。
- 2026-10-25：LangGraph 链路诊断与可靠性实施。诊断稿在 `archive/2026-10-25-langgraph-chat-audit.md`，当前实现见 `2026-10-25-langgraph-chat-implementation.md`。

## 仍然有效的结论

1. Python 不连业务库、不保存会话状态；用户模型配置随每次 gRPC 请求传入。
2. 业务写入只走 Java 具名接口；Python 只用 `mirror_ai` 只读视图。
3. graph_v2 默认关闭，legacy 仍是默认引擎；灰度门槛见 `agent-v2-rollout.md`。
4. 工具、材料、输出的验收规则已经固化在注册表和 `agent_core`；旧诊断中的缺口不再代表当前实现。
5. 早期 `ENVIRONMENT_SETUP.md`、`QUICKSTART.md`、`development.md` 已删除：启动以 `README.md` 为准，且必须使用 `.venv` 的 Python。

## 已归档

- `archive/2026-07-23-ai-diary-mirror-design.md`
- `archive/2026-07-23-mirror-implementation.md`
- `archive/2026-08-04-ai-service-design.md`
- `archive/2026-08-18-optimization-design.md`
- `archive/2026-10-20-ai-agent-architecture-v2.md`
- `archive/2026-10-20-chat-agent-langgraph-migration.md`
- `archive/2026-10-21-operation-card-tool-fix.md`
- `archive/2026-10-25-langgraph-chat-audit.md`
- `archive/agent-v2-baseline.md`
- `archive/integration/agent-v2-recovery-report.md`
- `archive/HANDOVER-2026-09-06.md`
- `archive/plans/`
