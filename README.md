# Mu-mirror-AI

Mirror 应用的 Python AI 服务。基于 gRPC，为「AI 日记镜子」系统提供记录处理、意图抽取、向量化、用户画像，以及 graph_v2 完整对话流程。

**无会话状态**：本服务不保存会话状态，也不使用 checkpoint。旧 gRPC 接口只做原有 AI 处理；graph_v2 每轮直接连接 PostgreSQL 的 `mirror_ai` 受控只读视图，Python 按可信 user/session/message 范围加载词典、历史、检索资料并执行只读工具。模型配置随每次 gRPC 请求到达，用完即弃。

## 在系统中的位置

```
Vue 3 前端 ──HTTP(/api, JWT)──> Spring Boot 后端 ──SQL/业务写入──> PostgreSQL + pgvector
                                      ├──gRPC :10003──> 旧版 AI RPC
                                      └──gRPC :10004──> graph_v2 对话 Agent ──只读视图──> PostgreSQL
```

**分工原则：Java 管鉴权、模型配置解密、聊天入口、会话与消息持久化、SSE 转发和业务写入；Python 管完整对话流程与受控只读数据访问。** 真正的写操作只通过 Java 具名业务接口执行；普通记录创建默认只保存用户原文，不自动授权 AI 整理或长期记忆。旧引擎和 graph_v2 并存，每轮开始时固定引擎，失败不会自动换引擎重跑。

## 技术栈

| 项 | 值 |
|---|---|
| 语言 / 运行时 | Python 3.11+ |
| 通信 | gRPC（**同步 server，非 aio**） |
| LLM 客户端 | `openai`（OpenAI 兼容协议）/ `anthropic`（Anthropic 协议） |
| Embedding | API 模式（默认）/ 本地 BGE-m3（需 torch） |
| 配置 | YAML（`config.yml`），仅服务级参数，**不含任何用户密钥** |

## 目录结构

```
server.py                gRPC server 装配（注册 4 个 service）
services/                服务实现
  record_processor.py      Classify（拆分 / single 单段）/ ExtractTerms（词典候选抽取）
  chat_service.py          Chat（流式）/ ExtractIntent（意图路由）
  plan_service.py          PlanTools / PlanNextStep（工具规划，流式循环决策）
  profile_service.py       GenerateProfile（递归累计镜子）
  embedding_service.py     Embed / EmbedBatch / GetModelInfo
llm/                     LLM 客户端封装：openai_llm / anthropic_llm + factory（用户配置驱动）
embedding/               api_embedder（阿里云 MaaS 等）/ local_embedder（BGE-m3）+ factory
prompts/                 9 套 prompt 模板（见下）
mirror_core/             业务辅助层
  prompts_loader.py      占位符渲染 + 别名通道（旧调用名不破坏）
  time_substitution.py   相对时间消解（"今天"→"9月22日"，见下）
  glossary_render.py     个人词典注入渲染
  todo_render.py         待办清单注入渲染
  recent_context_render.py 近期记录摘要注入渲染
  errors.py              异常 → gRPC 状态码统一映射
  llm_json.py            LLM 输出 JSON 解析（容错代码块包裹等）
config.py / config.yml   服务级配置加载
proto/                   Proto 定义（**契约源头，改动需与 Java 仓对齐**）
generated/               生成的 gRPC 代码（勿手改）
tests/                   单测与 E2E
deploy/                  发布脚本（Jenkins / 手工）
upload/                  部署镜像副本（与根目录逐字节同步，见「关于 upload/」）
```

## gRPC 服务

| 服务 | 方法 | 说明 |
|---|---|---|
| `RecordProcessor` | `Classify` | 记录分类：拆分 + 打标签，`single=true` 时为单段模式（禁止拆分，恰好 1 条） |
| `RecordProcessor` | `ExtractTerms` | 个人词典候选抽取 |
| `EmbeddingService` | `Embed` / `EmbedBatch` | 文本转向量（1024 维硬约束） |
| `EmbeddingService` | `GetModelInfo` | 模型信息，双语义：无配置=健康检查；带配置=按用户配置实测维度 |
| `MirrorChat` | `ExtractIntent` | 意图抽取（`query_type` 路由：PROFILE / STRUCTURED / SEMANTIC / HYBRID） |
| `MirrorChat` | `Chat` | 对话生成（流式 `ChatChunk`） |
| `MirrorChat` | `PlanTools` | 工具规划：LLM 输出工具调用计划 JSON（≤2 步） |
| `MirrorChat` | `PlanNextStep` | 对话循环的单步规划（流式，含思考过程透传） |
| `MirrorProfile` | `GenerateProfile` | 用户画像（"镜子"）生成 |

`Classify` 在原有一次模型调用内逐段返回索引建议，split 与 single 均支持：
`ClassifyItem.optional bool embedding_enabled = 11`（支持 presence，明确 false 必须保留）和
`string embedding_reason = 12`（最多 200 字符，仅解释建议，不参与 embedding）。模型 JSON
只接受真实布尔值；缺失、null、字符串或数字等非法建议按 true 兼容，非字符串原因按空串处理。
低信息但有效的日常仍返回原文、分类与时间替换，不整体 skip；空白或无意义语料仍可 skip。
建议不改变 `processing_authorized` 或用户记忆授权，Java 决定最终索引与存储。
真实模型的语义质量和数据库权限链路需单独验收，桩模型测试仅验证协议与映射。

`GenerateProfileRequest.optional string records = 16` 接收 Java 已按授权、回看窗口及预算
筛选的片段正文，直接用于镜子 prompt 的记录材料。字段存在时始终优先（包括显式空字符串），
不会与旧学习摘要拼接或按旧通道的单条 2,000 字上限截断整块材料；只有旧客户端未设置
该字段时才兼容 `learnings` 的既有格式。窗口、逐段和总量预算仍由 Java 负责。

graph_v2 的 `search_records`、`read_record` 继续只读 `mirror_ai.record_chunks` 视图。
B 的 v18 迁移将新策略下已确认授权、未索引的片段纳入正文视图；普通日期/关键词查询不要求
向量，语义查询另检查 `embedding_enabled=true`、`embedding_status=ready` 且向量非空。
日期回顾使用无关键词的日期查询，不调用 embedding；具体主题优先语义检索，向量空结果或
失败仍可通过字面短关键词/日期查询和正文读取核对，不能据此声称用户没有记录。
列表只提供 120 字摘录；`read_record` 保留完整授权片段并按 `next_offset` 续页，`full`
只表示授权范围已读完。图在原有 12 页、64,000 字符和运行时间预算内补齐缺页；摘录或未读完
时展示查询范围与 partial 限制。`search_records` 没有 legacy `mode` 参数，默认引擎未改变。

legacy PlanTools 本期保留原有 `search_records.mode` 路由，并在 `plan_tools.txt` 与单步回滚
提示中说明同一分页契约：搜索 `quote` 是最多 120 字摘录，不代表全文；需要正文时，循环规划器
先用搜索结果中的 `record_id` 调 `read_record`，按 `next_offset` 读取至授权范围完成。单步回滚
规划看不到新搜索结果，只能直接读取已知的 `record_id`；否则答复必须说明依据仍是摘录。以上
仅适配现有 legacy 路径，不更改引擎选择。

隔离 reader 集成测试按 `tests/agent_v2/fixtures/agent_data.sql`（包括相邻 selective fixture）
→ B `migration-v11-agent-v2.sql` → `migration-v18-record-selective-index.sql` →
`deploy/agent-reader-role.sql` 初始化。fixture 仅允许精确库名 `mirror_ai_agent_v2_test`，不得
用于应用数据库；Python 测试账号仅有现有八个视图的 SELECT，不增加业务表权限。

## 快速开始

```bash
# 依赖（最小集；服务器 2C2G 用这份，不含 torch / sentence-transformers）
.venv/Scripts/python.exe -m pip install -r requirements-minimal.txt

# 本机覆盖（首次）：复制示例并按机器改端口；该文件不入库
cp config.local.example.yml config.local.yml

# 启动服务（必须用 venv 的 python：裸 python 启动桩会退出码 49）
.venv/Scripts/python.exe server.py
```

服务默认监听 `config.yml` 的 `server.port`（10003），Agent 监听 `agent.port`（10004）。机器差异只改 `config.local.yml`；端口可用 `MIRROR_AI_PORT` / `MIRROR_AI_AGENT_PORT` 环境变量临时覆盖。完整双机开发约定见 [LOCAL-ENV](../Mu-mirror-B/docs/LOCAL-ENV.md)。

### 冒烟与测试

```bash
# 全量单测（无需外部依赖）
.venv/Scripts/python.exe -m pytest tests/ -q

# 冒烟（18 项，需桩 LLM 在线）
.venv/Scripts/python.exe scripts/smoke_client.py --stub

# 环境自检
.venv/Scripts/python.exe scripts/check_environment.py
```

真实数据库 E2E：`tests/agent_v2/e2e_create_record.py`（需隔离测试库与 Java 命令接口）。

### 桩 LLM

联调用桩替代真实模型，OpenAI 兼容协议，默认端口 18080：

```bash
.venv/Scripts/python.exe scripts/stub_llm.py
```

桩按 prompt 关键词返回不同 JSON / SSE 流式。**改桩后必须重启进程才生效**（曾因旧桩残留导致 E2E 假失败）。

## 配置

`config.yml` 只放服务级参数，**禁止存放用户模型配置**（9.3 契约）：

| 段 | 用途 |
|---|---|
| `server` | 端口、worker 数 |
| `llm` | 超时（非流式总超时 / 流式块间超时）、重试、思考预算 |
| `extract_terms` / `todo_hint` / `recent_context` / `mirror` | 各类 prompt 注入的截断防线 |
| `plan_tools` | 工具规划步数上限、注册表渲染上限、循环规划器思考预算 |
| `prompts` | 模板文件路径 |

`Classify` 的拆分和单段整理使用一次完整的 `llm.timeout_seconds` 请求预算（默认 60 秒），关闭 SDK 内部重试，避免把 60 秒分成两次 30 秒而反复中断较慢的完整 JSON 响应。请求超时仍按 `DEADLINE_EXCEEDED` 返回，由 Java 保留原文并生成待确认的仅保存候选。

## 相对时间消解

原文里的"今天""明天""下周三"这类词，脱离写日记的那一天就读不懂了。本服务把它们消解成绝对日期，让 segment（既展示给用户、又进 embedding）可长期阅读。

**分工**：本服务负责「识别 + 计算」，输出 `original → resolved` 替换表（`ClassifyItem.time_substitutions`）；**Java 侧负责执行字符串替换**。不让 LLM 直接吐改写后的全文——它会顺手润色掉非时间内容。

三道防线（LLM 做日期算术不可靠）：

1. **固定偏移词由代码确定性计算**：今天/明天/后天/大后天/昨天/前天/大前天 + 今日/明日/昨日/今早/今晚/今夜/明早/明晚/昨晚/昨夜。这些是纯算术，`_scan_fixed_terms()` 独立扫描原文补齐，**不依赖 LLM 是否提及**。
2. **其余采用 LLM 结果但严格校验**：必须解析成真实日期、距参照日期不超过一年、`original` 逐字出现在片段中且含日期单位字。
3. **长短词包含保护**：文本里有"大后天"而替换表只有"后天"时不替换后者，否则 Java 的 `String.replace` 会产出"大9月25日"。

> 为什么固定词不交给 LLM：2026-09-22 真机实测，同一句"昨天好累啊"连跑 6 次只有 2 次被替换。抓完整响应发现模型有时把 `time_substitutions` 放到 `items` 外层，有时不放。固定词是纯算术，不该赌模型每次记得说。改用代码兜底后连跑 8 次全部成功。

不消解的：指一段时间的词（"上个月""这个月""下周""最近""这几天"）——它们无法锁定到具体某一天，原样保留。

## Prompt 模板

| 模板 | 用途 |
|---|---|
| `classify.txt` / `classify-single.txt` | 记录拆分分类 / 单段分类 |
| `intent.txt` | 意图抽取 |
| `chat.txt` | 对话生成 |
| `profile.txt` | 画像（镜子）生成 |
| `inspiration.txt` | 写作灵感 |
| `extract_terms.txt` | 个人词典候选抽取 |
| `plan_tools.txt` / `plan_tools_single.txt` | 工具规划 / 回滚路径快照 |

模板用字面 `{key}` 替换而非 `str.format`（模板内含 JSON 示例的花括号）。`mirror_core.prompts_loader._ALIASES` 提供别名通道，旧调用名不会因模板改名而失效。

## 重新生成 Proto

改 `proto/*.proto` 后：

```bash
.venv/Scripts/python.exe generate_proto.py
```

脚本会自动同步 `upload/generated/`，避免两份 stub 漂移。**proto 契约源头在本仓**，改动前需与 Java 仓对齐字段号并在 `coordination/shared-protocol.md` 登记。

## 部署

`deploy/ai-release.sh` 为 Jenkins 发布脚本：rsync 覆盖代码（不碰 venv）→ 依赖清单变化时才 `pip install` → 重启 systemd 服务 → 探活 10003 端口。

Windows 一键部署见仓库外层的 `部署AI.bat`。**打包用排除法而非白名单**——白名单漏一个模块就是线上 `ImportError`（2026-09-22 踩过：`mirror_core/time_substitution.py` 新增后未加进白名单）。

## 关于 `upload/`

`upload/` 是部署镜像副本，与根目录**逐字节同步**（`generate_proto.py` 自动同步 stub，其余文件手工同步）。它不是死代码：两份 `config.yml` 的端口已统一为 10003。

**改动本仓代码时需双写**（根目录 + `upload/`），否则部署出去的是旧版本。

## 常见坑

| 坑 | 现象 | 解决 |
|---|---|---|
| 端口不一致 | 历史上根目录 config.yml 为 50051 而 Java 侧期望 10003，表现为"对话没有工具轨迹"（规划失败静默降级），**不报错** | 对齐 `config.yml` 的 port 与 Java 侧 `application-dev.yml` |
| 用裸 python 启动 | 退出码 49 | 用 `.venv/Scripts/python.exe` |
| 改了桩 LLM 没重启 | E2E 场景假失败 | 重启 18080 进程 |
| 改了 services/ 或 prompts/ 没重启 | 改动不生效 | 重启 10003 对应的服务 |
| 依赖漂移 | 本地能跑服务器跑不起来 | `requirements-minimal.txt` 全部锁定具体版本，升级前先本地跑完整测试 |

## 相关仓库

| 仓库 | 说明 |
|---|---|
| `Mu-mirror-B` | Java Spring Boot 后端（数据、业务、检索、调度） |
| `Mu-mirror-F` | Vue 3 前端 |
| `coordination/`（仓库外） | 三仓共享的协作文档与协议登记（`shared-protocol.md`） |

## 文档

- [文档索引](docs/README.md) — 当前文档、归档与维护规则
- [历史摘要](docs/HISTORY.md) — 已归档设计与一次性记录的一页摘要
- [LangGraph 实施记录](docs/2026-10-25-langgraph-chat-implementation.md) — graph_v2 当前实现边界
- [Agent v2 rollout](docs/agent-v2-rollout.md) — 灰度、回滚与验收门槛
- [选择性索引真实模型验收](docs/archive/integration/2026-11-13-record-selective-index-real-model.md) — 固定 12 例的历史验收；当前跨端结果见 B 仓 `docs/PROGRESS.md`
- `docs/archive/` — 被取代的历史设计文档，仅作追溯

本地测试日志和一次性任务报告不入库：使用 `.verify/`，原 `tests/agent_v2/reports/` 也已忽略。可重复执行的回归测试仍保留在 `tests/`；长期验收摘要归档到 `docs/archive/`。

### 塔罗首版

`TarotReading.Analyze` 在现有 legacy RPC 服务注册，使用调用方模型配置，无工具执行或数据库访问。日牌上下文只供最终回答；规划与画像不包含牌义。`scripts/tarot_stub_model.py` 仅作本地协议联调，目标模型措辞验收样例在 `tests/fixtures/tarot-evaluation.json`。运行时修改后运行 `scripts/sync_upload.py` 并检查 `--check`。
