# Mu-mirror-AI

Python 服务负责 gRPC 推理。业务数据写入及权限规则由 Java 后端负责，推理和 Agent 工具按既有授权范围访问数据。

- 跨端状态见同级后端仓库的 [项目进度](../Mu-mirror-B/docs/PROGRESS.md)。启动、协议和部署说明见 [README](README.md)。
- 写新函数或规则前先找现有实现。改 [proto 源定义](proto/) 时保留兼容字段号，更新生成产物并同步 Java 调用方。
- pre-commit 对超过约 20KB 的文本文件给出提醒；按职责评估是否拆分。高风险改动仍须同步维护文档。
- 新克隆后运行 `git config core.hooksPath .githooks` 启用钩子。
- `upload/` 是部署用副本。运行时代码改动后，用 [同步脚本](scripts/sync_upload.py) 更新，并以 `--check` 检查差异。
- 修改 Agent 对话前，先对照 [现有实施记录](docs/2026-10-25-langgraph-chat-implementation.md) 和 [发布边界](docs/agent-v2-rollout.md)。

## 文档维护契约

- **单一来源**：跨端状态写 B 仓 `docs/PROGRESS.md`；本仓启动/协议/发布写 `README.md`、`docs/agent-v2-rollout.md`；一次性记录完成即移入 `docs/archive/`。
- **同一提交更新**：改 proto、`server.py`、`config.py`、`upload/`、推理服务或 prompt 行为时，必须在同一提交更新对应文档。
- **禁止文档泛滥**：不新建平行的“进度/现状/总结”文档；同一事实只写一处。
- **提交门禁**：pre-commit 会拦截“高风险改动但没有文档改动”的提交；紧急情况用 `SKIP_DOC_CHECK=1`，并在提交说明里写明原因。
