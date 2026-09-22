# Mu-mirror-AI

Mirror 应用的 Python AI 服务。基于 gRPC，为「AI 日记镜子」系统提供全部推理能力：记录分类拆分、意图抽取、向量化、对话规划与生成、用户画像（镜子）生成。

**无状态**：本服务不连数据库、不存任何会话状态。用户的 LLM / Embedding 配置随**每次 gRPC 请求**到达（`LlmConfig` / `EmbeddingConfig` 消息），用完即弃。数据与业务全在 Java 侧。

## 在系统中的位置

```
Vue 3 前端 ──HTTP(/api, JWT)──> Spring Boot 后端 ──SQL──> PostgreSQL + pgvector
                                      │
                                      └──gRPC──> 本服务（纯推理，无状态）
```

**分工原则：Java 管数据与业务，Python 管纯推理。** 向量检索用 pgvector SQL 在 Java 端完成；本服务只负责"把文本变成结构化结果"。

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
prompts_loader.py        占位符渲染 + 别名通道（旧调用名不破坏）
time_substitution.py     相对时间消解（"今天"→"9月22日"，见下）
glossary_render.py       个人词典注入渲染
todo_render.py           待办清单注入渲染
recent_context_render.py 近期记录摘要注入渲染
errors.py                异常 → gRPC 状态码统一映射
llm_json.py              LLM 输出 JSON 解析（容错代码块包裹等）
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

## 快速开始

```bash
# 依赖（最小集；服务器 2C2G 用这份，不含 torch / sentence-transformers）
.venv/Scripts/python.exe -m pip install -r requirements-minimal.txt

# 启动服务（必须用 venv 的 python：裸 python 启动桩会退出码 49）
.venv/Scripts/python.exe server.py
```

服务默认监听 `config.yml` 中的 `server.port`。**注意根目录 `config.yml` 默认 50051，而 `upload/config.yml` 是 10003** —— Java 侧 dev 环境期望 10003，联调时需对齐（详见「常见坑」）。

### 冒烟与测试

```bash
# 单测（357 项，无需外部依赖）
.venv/Scripts/python.exe -m pytest tests/ -q

# 冒烟（18 项，需桩 LLM 在线）
.venv/Scripts/python.exe test_client.py --stub

# 环境自检
.venv/Scripts/python.exe test_environment.py
```

E2E 脚本（`tests/e2e_round*.py`）需要真实 server + 桩 LLM 同时在线。

### 桩 LLM

联调用桩替代真实模型，OpenAI 兼容协议，默认端口 18080：

```bash
.venv/Scripts/python.exe ../coordination/stub_llm.py
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

模板用字面 `{key}` 替换而非 `str.format`（模板内含 JSON 示例的花括号）。`prompts_loader._ALIASES` 提供别名通道，旧调用名不会因模板改名而失效。

## 重新生成 Proto

改 `proto/*.proto` 后：

```bash
.venv/Scripts/python.exe generate_proto.py
```

脚本会自动同步 `upload/generated/`，避免两份 stub 漂移。**proto 契约源头在本仓**，改动前需与 Java 仓对齐字段号并在 `coordination/shared-protocol.md` 登记。

## 部署

`deploy/ai-release.sh` 为 Jenkins 发布脚本：rsync 覆盖代码（不碰 venv）→ 依赖清单变化时才 `pip install` → 重启 systemd 服务 → 探活 10003 端口。

Windows 一键部署见仓库外层的 `部署AI.bat`。**打包用排除法而非白名单**——白名单漏一个模块就是线上 `ImportError`（2026-09-22 踩过：`time_substitution.py` 新增后未加进白名单）。

## 关于 `upload/`

`upload/` 是部署镜像副本，与根目录**逐字节同步**（`generate_proto.py` 自动同步 stub，其余文件手工同步）。它不是死代码：`upload/config.yml` 的端口（10003）才是与部署一致的那份。

**改动本仓代码时需双写**（根目录 + `upload/`），否则部署出去的是旧版本。

## 常见坑

| 坑 | 现象 | 解决 |
|---|---|---|
| 端口不一致 | 从仓库根起 Python 听 50051，而 Java 打 10003，表现为"对话没有工具轨迹"（规划失败静默降级），**不报错** | 对齐 `config.yml` 的 port 与 Java 侧 `application-dev.yml` |
| 用裸 python 启动 | 退出码 49 | 用 `.venv/Scripts/python.exe` |
| 改了桩 LLM 没重启 | E2E 场景假失败 | 重启 18080 进程 |
| 改了 services/ 或 prompts/ 没重启 | 改动不生效 | 重启 50051 |
| 依赖漂移 | 本地能跑服务器跑不起来 | `requirements-minimal.txt` 全部锁定具体版本，升级前先本地跑完整测试 |

## 相关仓库

| 仓库 | 说明 |
|---|---|
| `Mu-mirror-B` | Java Spring Boot 后端（数据、业务、检索、调度） |
| `Mu-mirror-F` | Vue 3 前端 |
| `coordination/`（仓库外） | 三仓共享的协作文档与协议登记（`shared-protocol.md`） |

## 文档

- `docs/2026-09-03-system-design-v2.md` — **系统唯一权威设计文档**（v2.3）
- `HANDOVER.md` — 交接文档：架构铁律、近两轮大改动、已知问题
- `docs/archive/` — 被取代的历史设计文档，仅作参考
