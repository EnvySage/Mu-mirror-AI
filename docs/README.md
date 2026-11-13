# Mu-mirror-AI 文档索引

当前状态以代码和三端进度为准：`../Mu-mirror-B/docs/PROGRESS.md`。
本目录只保留仍需维护的文档；历史材料在 `archive/`，不再作为现状依据。

## 当前文档

| 文档 | 用途 |
|---|---|
| [README](../README.md) | 服务概览、gRPC 接口、启动与部署 |
| [AGENTS](../AGENTS.md) | 仓库级 Agent 约束 |
| [LangGraph 对话实施记录](2026-10-25-langgraph-chat-implementation.md) | graph_v2 当前实现边界、工具契约、材料与输出验收 |
| [Agent v2 rollout](agent-v2-rollout.md) | 只读视图、灰度、回滚与验收门槛 |
| [历史摘要](HISTORY.md) | 已归档设计与一次性记录的一页摘要 |
| [选择性索引真实模型验收归档](archive/integration/2026-11-13-record-selective-index-real-model.md) | 固定 12 例合成输入的历史分类验收与验证边界 |
| [archive/](archive/) | 历史设计、诊断、基线与已完成计划，仅供追溯 |

## 维护规则

1. **代码优先**：文档与代码冲突时，以当前代码为准，先改文档。
2. **一个主题一处**：现状只写在 README / 实施记录 / rollout；历史细节放 `archive/`。
3. **完成后归档**：一次性诊断、基线、修复记录、实施计划完成后移入 `archive/`，不要留在 `docs/` 根目录。
4. **文件大小**：pre-commit 对超过约 20KB 的文本文件给出提醒，不阻止提交；按职责评估是否拆分，文档同步门禁继续生效。
5. **提交前**：同步 `upload/` 运行副本，并运行 `scripts/sync_upload.py --check`。
