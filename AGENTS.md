# Mu-mirror-AI

Python 服务负责 gRPC 推理。业务数据写入及权限规则由 Java 后端负责，推理和 Agent 工具按既有授权范围访问数据。

- 跨端状态见同级后端仓库的 [项目进度](../Mu-mirror-B/docs/PROGRESS.md)。启动、协议和部署说明见 [README](README.md)。
- 写新函数或规则前先找现有实现。改 [proto 源定义](proto/) 时保留兼容字段号，更新生成产物并同步 Java 调用方。
- pre-commit 钩子会拒绝超过约 20KB 的文本文件；提交被拒时按报错拆分，不要绕过钩子。
- 新克隆后运行 `git config core.hooksPath .githooks` 启用钩子。
- `upload/` 是部署用副本。运行时代码改动后，用 [同步脚本](scripts/sync_upload.py) 更新，并以 `--check` 检查差异。
- 修改 Agent 对话前，先对照 [现有实施记录](docs/2026-10-25-langgraph-chat-implementation.md) 和 [发布边界](docs/agent-v2-rollout.md)。
