# AI 日记"镜子"系统 — 总体设计文档 v2.1

> 版本：v2.1（v2.0 补全协作章节与裁决 17-21）
> 日期：2026-09-03
> 代码基线：commit `08854ba`（Record 瘦身 + Chunk 业务单元重构）
> 状态：设计定稿，随开发持续修订

**本文档是系统唯一的权威设计文档**，基于"代码现状 + 设计意图"重新对账编写，取代以下旧文档（已移至 `docs/archive/`，仅作历史参考，不再维护）：

| 旧文档（现位于 docs/archive/） | 版本 | 处置 |
|--------|------|------|
| `2026-07-23-ai-diary-mirror-design.md` | v1.4 | 被本文档取代 |
| `2026-07-23-mirror-implementation.md` | v0.6 | 被本文档取代 |
| `2026-08-04-ai-service-design.md` | v0.5 | 被本文档取代（第七、九章覆盖其内容） |
| `2026-08-18-optimization-design.md` | v0.3 | 增量设计已并入第六章，被本文档取代 |

与旧文档的所有冲突，以本文档第十五章《裁决清单》为准。

**分工导航**：
- 前端团队 → 重点读 第二、三、五、六、八、十章（架构/数据模型/审核交互/模块/前端契约/API）
- Python AI 服务团队 → 重点读 第七、九、十四章（proto 契约/协作清单/路线图）
- Java 后端 → 全文

---

## 一、项目概述

### 1.1 定位

AI 驱动的个人记录与自我认知平台（毕业设计项目）。用户随手记录日常，AI 自动拆分、分类、打标签，最终生成"用户画像"（称为"镜子"），帮助用户认识自己。

### 1.2 核心理念

1. **记录零负担**：想记就记，不强制格式；标签体系定死、由 AI 打，用户只做审核修正。
2. **AI 做整理，用户做决策**：AI 拆分/分类/总结，用户在审核窗口纠正（包括 segment 手动调整）；确认后才入库 RAG。
3. **隐私优先**：用户自配 LLM 与 Embedding 模型，API Key 加密存储；数据在自己的数据库里。
4. **完整 AI 应用模式**（论文卖点）：RAG、pgvector 向量检索、时间衰减、意图路由、多轮对话、流式输出、画像快照与漂移检测、定时任务。

### 1.3 系统规模假设与非目标

**规模假设（明确裁决）：** 单实例小规模系统。目标用户为个人 / 家庭（5-6 人以内），单用户年数据量约 1000-2000 条记录。所有性能与存储决策按此尺度取舍：

- 常规索引全部保留（正确性优先，索引成本可忽略）；
- chunks 向量列建 HNSW 索引（维度固定 1024，提前建好免去数据增长后的迁移）；
- 画像快照因每用户仅 ~14 份，不建向量索引（顺序扫描更快），保留常规联合索引；
- 导出、统计等接口不做分页/批处理优化。

**非目标：** 多租户隔离与高并发、水平扩展、移动端原生应用、实时协同、数据实时同步。

### 1.4 技术栈

| 组件 | 选型 |
|------|------|
| 后端 | Spring Boot 3.5 / Java 21 / MyBatis-Plus，端口 9005，上下文路径 `/api` |
| AI 服务 | Python 3.11+ / grpcio-aio，gRPC 端口 50051（独立仓库 Mu-mirror-AI） |
| 数据库 | PostgreSQL + pgvector |
| LLM | 用户自配（OpenAI 兼容协议 / Anthropic 协议） |
| Embedding | 本地 BGE-m3（1024 维，默认）/ API 模式可配（**维度同样硬约束 1024**，见 3.4） |
| 前端 | Vue 3 + TypeScript（未开始） |
| 认证 | JWT（24h 过期）+ BCrypt |

**分工原则：Java 管数据与业务，Python 管纯推理。** 向量检索在 Java 端用 pgvector SQL 完成；Python 完全无状态，配置随每次 gRPC 请求携带。

---

## 二、总体架构

```
┌─────────────────────────────────────────────────┐
│                 Vue 3 前端                       │
│   记录/审核 │ 镜子 │ 对话 │ 日历 │ 设置           │
└──────────────────────┬──────────────────────────┘
                       │ HTTP（/api，JWT）
┌──────────────────────▼──────────────────────────┐
│              Spring Boot 后端                    │
│  Controller 层 → Service 层                      │
│  ├─ 数据管道：Clean → Classify（事件驱动异步）    │
│  ├─ 审核编排：补分类 + Embedding（confirmReview） │
│  ├─ pgvector 检索（相似度 + 元数据过滤 + 衰减）   │
│  └─ 定时任务：每日总结 / 月度画像快照             │
└──────────────────────┬───────────┬──────────────┘
                       │ SQL       │ gRPC
              ┌────────▼────────┐ ┌▼───────────────────────┐
              │ PostgreSQL      │ │ Python AI 服务（无状态）│
              │ + pgvector      │ │ Classify / Embed /     │
              │ 用户自配地址     │ │ Chat / Profile         │
              └─────────────────┘ └────────────────────────┘
```

### 模块清单与实现状态

| 模块 | 优先级 | 状态 | 说明 |
|------|--------|------|------|
| 用户认证 | P0 | ✅ 已实现 | 注册/登录/JWT//auth/me |
| 记录管理 + Chunk | P0 | ✅ 已实现（有扩展项） | CRUD、软删除、审核、日历；segment 手动调整待扩展 |
| 用户设置 | P0 | ✅ 已实现 | 模型配置 CRUD + AES 加密 + 连接测试（桩） |
| 标签审核 | P0 | ✅ 已实现 | 并入记录模块（Chunk 编辑） |
| 镜子画像 | P0 | 🔜 规划 | 快照 + 漂移检测 + 对话纳入 |
| 对话 | P0 | 🔜 规划 | 意图路由 + RAG + 多轮 + 流式 + 来源追溯 |
| 每日总结 | P1 | 🔜 规划 | 定时生成，进 RAG（系统 Record 方案，见 6.7） |
| 写作灵感 | P1 | 🔜 规划 | 停顿触发，临时不存 |
| 日历导航 | P1 | ✅ 已实现 | 按月统计每天记录数 |
| 数据导出 | P2 | 🔜 规划 | JSON + Markdown（只导出不导入，见裁决 #19） |
| 活动统计 | P2 | 🔜 规划 | 可视化，依赖已实现数据 |
| 前端 | — | 🔜 未开始 | 契约见第八章 |

> 旧文档中的"🗄️ 数据库配置（P0）"从功能清单移除：数据库连接属于部署配置（见第十二章），不是用户功能。

---

## 三、核心数据模型

### 3.1 设计原则：Chunk 是唯一业务单元

v2.0 最重要的模型决策：**一条用户输入 = 一条 Record（只存原始内容与状态）；AI 拆分出的每个语义片段 = 一个 Chunk，承载全部业务数据（segment 文本 + AI 元数据 + 向量）。**

- Record 是"原始输入的凭证"，瘦身为只存 `content` + 状态；任何 AI 生成的东西都不落在 Record 上。
- Chunk 是"业务单元"：审核时编辑它、确认时向量化它、检索时命中它、画像/统计时聚合它。
- **segment 的唯一真源是 `chunks.segment`**。旧模型中 `records.segment`（JSONB 数组）与 chunks 重复存储，用户编辑 chunk 后 record 数组不同步，产生"列表所见 ≠ 入库所得"的不一致——v2.0 废除 `records.segment` 列。

**废除项（含理由）：**

| 废除项 | 理由 |
|--------|------|
| `records.segment` | 与 chunks.segment 双份存储，已出现一致性隐患；由 RecordVO 从 chunks 推导 |
| `records.title/summary/content_type/mood` | 已随 08854ba 重构物理 DROP，元数据归 chunks.metadata |
| `records.original_record_id` | 不再拆多条 Record，拆分组关系由"同一 record_id 的多个 chunk"天然表达 |
| `tags` 表 + Tag 实体 + TagMapper | 死代码：管道从未写入，关键词存 `chunks.metadata.keywords` |
| `mirror_profiles` 表 | 被 `profile_snapshots` 取代（见 6.5） |
| `daily_summaries` 表 | 被"系统 Record 方案"取代（见 6.7 与裁决 #17） |
| 内容块模型（blocks: text/code/link） | 只停留在旧文档的"扩展性设计"，从未落库；Record 已扁平化，彻底删除该设计 |
| 周报/月报总结 | 月度趋势由"月度画像快照"承担（6.5），总结只保留每日粒度 |
| `chat_sessions.last_message_at` | 统一用 `updated_at`（每次新消息触碰会话行），少一个冗余列 |

### 3.2 已实现表（现状 + v2.0/v2.1 变更）

```sql
-- users（不变）
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- records（v2.0/v2.1 目标形态：DROP segment 列；新增 source 列）
CREATE TABLE records (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    content TEXT NOT NULL,              -- 原始输入，不可修改
    source VARCHAR(20) DEFAULT 'user',  -- user=用户输入 / system=系统生成（如每日总结）【v2.1 新增】
    status VARCHAR(20) DEFAULT 'processing',  -- processing/reviewing/done/failed
    user_reviewed BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    deleted_at TIMESTAMPTZ              -- 软删除
);
CREATE INDEX idx_records_user_id ON records(user_id);
CREATE INDEX idx_records_created_at ON records(created_at);
CREATE INDEX idx_records_deleted_at ON records(deleted_at);
-- 迁移：ALTER TABLE records DROP COLUMN IF EXISTS segment;
-- 迁移：ALTER TABLE records ADD COLUMN IF NOT EXISTS source VARCHAR(20) DEFAULT 'user';
-- 迁移：DROP TABLE IF EXISTS tags;

-- chunks（v2.0 核心业务单元，新增 2 列 + HNSW 索引）
CREATE TABLE chunks (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    record_id BIGINT NOT NULL REFERENCES records(id),
    content TEXT NOT NULL,              -- 整条记录原文（冗余存储，检索展示用）
    segment TEXT,                       -- 语义片段（embedding 的输入文本；真源）
    metadata JSONB,                     -- AI 元数据（见下）
    classified_segment TEXT,            -- 生成当前 metadata 时所用的 segment 文本
                                        -- NULL = 尚未分类/文本已被用户改动，confirm 时需补分类
    user_edited BOOLEAN DEFAULT FALSE,  -- 用户是否编辑过（文本或元数据），统计用
    embedding vector(1024),
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_chunks_user_id ON chunks(user_id);
CREATE INDEX idx_chunks_record_id ON chunks(record_id);
CREATE INDEX idx_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops);
```

**`chunks.metadata` 结构（JSONB）：**

```json
{
  "title": "学 Spring Boot",          // ≤10 字
  "summary": "学习了核心概念",         // ≤30 字
  "contentType": "learning",          // 8 选 1，英文小写
  "mood": ["satisfied", "calm"],      // 13 选多选，英文小写
  "keywords": ["Spring Boot", "学习"] // 3-5 个
}
```

> ⚠️ 待补齐：AI 返回的 `taskStatus`（仅 todo/plan 类有效：not_started/in_progress/completed）目前被 ClassifyProcessor 丢弃，应写入 `metadata.taskStatus`（待办聚合与镜子"未完成的事"维度依赖它）。
> 系统记录（source='system'，如每日总结）的 Chunk metadata 使用 `contentType="daily_summary"` + `summaryDate` 字段标识。

**`user_settings`（不变 + 规划新增 1 列）：**

```sql
CREATE TABLE user_settings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID UNIQUE NOT NULL REFERENCES users(id),
    ai_provider VARCHAR(50),
    ai_protocol VARCHAR(20) DEFAULT 'anthropic',   -- openai / anthropic
    ai_api_key TEXT,                                -- AES-256-GCM 加密
    ai_base_url TEXT,
    ai_model VARCHAR(100),
    embedding_source VARCHAR(20) DEFAULT 'local',   -- local / api
    embedding_base_url TEXT,
    embedding_api_key TEXT,                         -- 加密
    embedding_model VARCHAR(100),
    review_mode VARCHAR(20) DEFAULT 'manual',       -- manual / auto（接线方案见 5.5）
    rag_half_life INT DEFAULT 30,                   -- 规划：RAG 时间衰减半衰期（天，7-365）
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

### 3.3 规划表

```sql
-- 画像快照（取代旧 mirror_profiles）
CREATE TABLE profile_snapshots (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    snapshot_type VARCHAR(20) NOT NULL,   -- manual（用户触发）/ monthly（每月1号定时）
    mood_analysis TEXT,
    learning_analysis TEXT,
    todo_analysis TEXT,
    rhythm_analysis TEXT,
    user_tags JSONB,                      -- ["技术学习", "夜猫子"]
    overall_summary TEXT,
    embedding vector(1024),               -- 漂移检测用（五维文本按固定顺序拼接后向量化）
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_snapshots_user ON profile_snapshots(user_id, snapshot_type, created_at DESC);
-- 不建向量索引：每用户仅 ~14 份快照（手动保 2 + 月度保 12），顺序扫描更快

-- 会话
CREATE TABLE chat_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    title VARCHAR(200),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()   -- 每次新消息触碰；会话列表按它倒序
);
CREATE INDEX idx_sessions_user ON chat_sessions(user_id, updated_at DESC);

-- 对话历史（裁决 #8：sources 保留落库）
CREATE TABLE conversation_history (
    id BIGSERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id),
    role VARCHAR(20) NOT NULL,             -- user / assistant
    content TEXT NOT NULL,
    sources JSONB,                         -- [{record_id, quote, date}]，assistant 消息的来源追溯
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_history_session ON conversation_history(session_id, created_at ASC);
CREATE INDEX idx_history_user ON conversation_history(user_id, created_at DESC);
```

> **sources 落库的理由**：来源追溯是对话模块的核心卖点（"每个结论都能点回原始记录"），也是论文亮点；只做即时透传的话，用户回看历史会话时引用全部丢失。存储成本可忽略。sources 可引用任何 record（含 source='system' 的每日总结）。

### 3.4 索引策略与 Embedding 维度约束

- 小规模 ≠ 不要索引：查询路径固定、索引成本可忽略，**常规索引全部建**。
- `chunks.embedding` 建 HNSW（cosine）——维度固定 1024，提前就位。
- `profile_snapshots.embedding` 不建向量索引（每用户 ≤14 行），数据模型预留升级路径。

**Embedding 维度硬约束（裁决 #18）：** 全系统统一 1024 维。

- 理由：BGE-m3 本地默认即 1024 维；固定维度让 HNSW 索引一次建成，无需动态迁移与向量重建机制——这套机制的工作量与毕设收益不成比例。
- 落点：设置页保存 / 测试连接（test-embedding）时调用 `GetModelInfo` 校验维度，非 1024 拒绝保存并提示"当前版本仅支持 1024 维模型"。
- 动态维度与向量重建写入论文 future work。

---

## 四、数据管道

### 4.1 管道总览

管道只负责"提交 → 可审核"这一段；审核与向量化在 Service 层的 `confirmReview` 编排（不在管道里）。

```
用户提交 POST /records
   │  Record(status=processing, source='user') 入库 → 发布 RecordCreatedEvent → 立即返回前端
   ▼  [异步线程, @TransactionalEventListener(AFTER_COMMIT)]
┌──────────────────────────────────────────────┐
│ CleanProcessor（@Order 1，纯 Java，不调 AI）   │
│  trim / 合并空行 / 去控制字符 / 空内容检测      │
├──────────────────────────────────────────────┤
│ ClassifyProcessor（@Order 2，gRPC → Python）  │
│  一次 LLM 调用同时完成：                       │
│   1. 判断是否多件事 → 拆分为 N 个语义片段       │
│   2. 每个片段独立分类：title/summary/          │
│      contentType/mood/taskStatus/keywords     │
│      + content（原文片段）                     │
│  无意义内容 → skip=true → 管道中断             │
│  输出：Record.segmentList + chunkMetadataList │
│       （内存传递，不落 Record 表）              │
└──────────────────────┬───────────────────────┘
                       ▼
  RecordEventListener：
    1. Record → REVIEWING
    2. 每个 segment 创建一个 Chunk
       （content=原文, segment=片段, metadata=AI元数据,
         classified_segment=片段文本, embedding=NULL）
    3. 若 review_mode=auto → 直接执行 confirm 流程（见 5.5）
```

### 4.2 记录生命周期

```
POST /records（processing，前端转圈）
   → 管道（Clean → Classify）
       ├─ 失败 / skip → FAILED（可重试/可删除）
       └─ 成功 → REVIEWING + N 个 Chunk（无向量）
   → 审核窗口（用户可做的事见第五章）
       ├─ 确认 confirmReview → 补分类（按需）→ Embedding → DONE（锁定）
       └─ 删除 → 软删除（reviewing/failed 均可删），不进任何下游
```

### 4.3 状态机

| 状态 | 含义 | 可执行操作 |
|------|------|-----------|
| processing | AI 处理中 | 无 |
| reviewing | 待审核 | 编辑 Chunk（文本/元数据/增删）、确认、删除 |
| done | 已确认锁定 | 仅查看 |
| failed | 处理失败 | 重新尝试（`POST /records/{id}/retry`，重跑分类管道）、删除 |

### 4.4 单段分类模式

手动调整 segment 后补分类需要"禁止拆分"的分类调用。Proto 变更：

```proto
message ClassifyRequest {
  string content = 1;
  common.LlmConfig llm_config = 2;
  bool single = 3;   // true：单段模式，禁止拆分，恰好返回 1 条 ClassifyItem
}
```

Python 端 classify prompt 增加 single 变体（去掉"判断是否多件事"指令，明确"这是用户确认过边界的一个完整片段"）。

---

## 五、审核机制与 segment 手动调整（核心设计）

### 5.1 原则

- **审核通过后才 Embedding 入 RAG**：向量库中永远是用户确认的最终版本。
- **审核窗口是唯一修改窗口**：DONE 后锁定，避免级联同步。
- **AI 建议，用户拍板**：拆分/分类是语义判断，LLM 边界会错；用户必须有能力手动调整，且调整不被 AI 二次推翻。
- **拒绝即删除**：软删除，不污染下游（画像/总结/RAG）。

### 5.2 审核窗口能力

**已实现：** 编辑单个 Chunk 的 `segment` 文本与 metadata（title/summary/contentType/mood/keywords），`PUT /chunks/{id}`。

**v2.0 扩展（规划）：** Chunk 结构级操作，采用**片段卡片模型**（不做原文拖拽标边界——offset 模型对毕设过重，卡片编辑已覆盖全部需求）：

| 操作 | 端点 | 语义 |
|------|------|------|
| 改片段 | `PUT /chunks/{id}`（已有） | 修改 segment 文本和/或元数据 |
| 删片段 | `DELETE /chunks/{id}`（新增） | 删除一个 Chunk |
| 增片段 | `POST /records/{id}/chunks`（新增） | 用户手写一段新内容；后端同步调用单段分类（single=true）回填元数据，失败不阻断（metadata 留空，confirm 时兜底重试） |
| 合并 | = 改 A 为全部文本 + 删 B | 无需独立端点 |
| 拆分 | = 改 A 为前半 + 增 B | 无需独立端点 |

**约束：**
- 所有 Chunk 编辑仅限所属 Record 处于 REVIEWING。
- `confirmReview` 时 Record 至少保留 1 个 Chunk，否则报错。
- 片段之间不要求拼接等于原文（`content` 始终保存完整原文兜底）。

### 5.3 编辑的追踪（userEdited / classified_segment）

- `user_edited`：用户编辑过该 Chunk（文本或元数据）即置 true。不参与逻辑，用于统计"AI 拆分被人工修正的比例"（论文数据点）。
- `classified_segment`：生成当前 metadata 时所用的 segment 文本。维护规则：
  - AI 分类回填 metadata 时 → 写入当时文本；
  - 用户改动 segment 文本时 → 置 NULL（元数据编辑不影响它）；
  - 手动新增 Chunk → 初始为 NULL。
- **confirm 补分类判据：`classified_segment IS NULL`**（含义 = "这段文本从未被分类过，或已被用户改过"）。

### 5.4 确认时的补分类策略（核心裁决）

用户改过的 segment 要不要重新给 LLM 分析？**按"文本是否变过"裁决，不按"是否编辑过"：**

| Chunk 状态 | confirm 时处理 | 理由 |
|-----------|----------------|------|
| 只改了标签，文本未变（classified_segment 非空） | **直接入库**，不调 LLM | 审核的意义就是用户纠正 AI；标签改完还要 LLM 重评等于否定用户 |
| 文本变过 / 新增段（classified_segment 为空） | **单段分类**（single=true，禁止拆分）→ 回填 metadata 与 classified_segment → 再 Embedding | 旧标签是给旧边界文本生成的，直接套用是张冠李戴；且新片段没有 contentType/mood 会在 RAG 过滤、镜子统计、待办聚合中"隐身" |

**只补受影响片段，绝不重跑整条 Record**：把整条原文丢回 LLM 会触发拆分逻辑，可能把用户刚调整好的边界再切乱。

**confirmReview 完整流程：**

```
confirmReview(recordId):
  1. 校验：归属、状态=REVIEWING、至少 1 个 Chunk
  2. 补分类：classified_segment IS NULL 的 Chunk
       → 逐个调 classify(single=true)（携带用户 LLM 配置）
       → 成功：回填 metadata + classified_segment
       → 失败：保留旧 metadata 继续（日志告警），不阻断
  3. Embedding：逐个 Chunk（文本 = segment，回退 content）
       → 成功：写向量
       → 失败：不阻断（记录仍 DONE，向量后续补录）
  4. status=DONE，user_reviewed=true，锁定
```

### 5.5 审核模式（review_mode 接线方案）

- **manual（默认）**：如上，审核窗口全部能力可用。
- **auto（确认实现）**：EventListener 建 Chunk 成功后，读取 review_mode；为 auto 则直接执行 confirm 流程（此时 metadata 是新鲜的，无补分类需要）→ Embedding → DONE。用户无审核窗口，**没有手动调整 segment 的机会**，设置页开启时应提示此权衡。接线成本约 20 行。

### 5.6 失败与兜底

| 场景 | 处理 |
|------|------|
| Classify 调用失败 / 超时（180s） | Record → FAILED，`POST /records/{id}/retry` 重跑管道（确认实现） |
| AI 判定无意义（skip=true） | 管道中断 → FAILED，前端提示原因 |
| 补分类失败 | 保留旧 metadata，confirm 继续 |
| Embedding 失败 | 不阻断确认，记录仍 DONE，向量后续补录 |
| 手动增片段时分类失败 | metadata 留空，confirm 时兜底重试 |

> 旧文档 §8.1 的"Embedding + 存 chunks 原子性（全成或全败）"表述废除，以本节"失败不阻断"为准（与代码实现一致）。

---

## 六、模块设计

### 6.1 认证（✅ 已实现）

注册（BCrypt + 自动创建空 user_settings）/ 登录（JWT 24h）/ `GET /auth/me`。JWT 密钥须从配置/环境变量注入（当前 dev 配置硬编码，见第十六章）。

### 6.2 记录 + Chunk（✅ 已实现 + 扩展项）

- 端点：创建（触发管道）、列表（日期范围，默认今天）、详情（含 Chunk 列表）、confirm、软删除、日历统计。
- **RecordVO 的 `segments` 字段改为从 Chunks 推导**（废除读 records.segment）。
- RecordController 无 PUT 接口（旧文档的"更新记录"端点已删，修改一律走 Chunk 端点）。
- 扩展项：`DELETE /chunks/{id}`、`POST /records/{id}/chunks`、`POST /records/{id}/retry`、confirm 补分类（5.4）。
- **统计口径**：记录列表与日历统计一律过滤 `source='user'`；系统记录（每日总结）仅通过总结接口与 RAG 检索可见。

### 6.3 日历导航（✅ 已实现）

`GET /records/calendar?month=YYYY-MM` → 每天有效记录数。只导航不分析，标记只区分有/无。时区 Asia/Shanghai（连接串 `options=-c timezone=Asia/Shanghai`）。需补 `source='user'` 过滤。

### 6.4 用户设置（✅ 已实现 + 规划项）

模型配置 CRUD、部分更新、API Key AES-256-GCM 加密 + 前 3 位脱敏、注册自动创建。规划：`rag_half_life`（时间衰减半衰期，调整时前端给权重预览）、auto 审核模式接线（5.5）、**Embedding 维度校验**（3.4，保存/测试连接时校验 1024 维）。

### 6.5 镜子画像（🔜 规划）

**触发**：用户点"查看镜子"（manual 快照）；每月 1 号 02:00 定时（monthly 快照）。

**生成流程**：
1. Java SQL 统计五维数据（未完成待办[依赖 metadata.taskStatus]、最近学习、情绪分布、关键词、活跃时段）+ 取最近 5 个会话的对话（优先近 7 天，不足则前补）。
2. gRPC `GenerateProfile`（统计数据 + recent_chats + LlmConfig）→ 六维分析文本 + user_tags + overall_summary。
3. Java 存 `profile_snapshots`；五维文本按固定顺序拼接 → `Embed` → 向量存快照。

**变化轨迹与漂移检测**：
- 分层保留：manual 保最近 2 份，monthly 保 12 份；每月定时生成后执行清理。画像用于对比，不堆数量。
- 漂移检测：本月 vs 上月快照 `embedding <=> embedding` 余弦距离；前端展示"变化幅度" + 两份快照对比，不做自动文字解读（后续可加一次 LLM 对比调用）。

**画像与对话的关系**：对话历史参与画像生成（LLM 综合日记+对话）；画像不进日常 RAG 检索，但 PROFILE 类问题直接查快照作答（见 6.6）。

### 6.6 对话（🔜 规划）

**流程**：
```
用户提问
 → gRPC ExtractIntent（+query_type 四选一 + content_type/moods/time_range + rewritten_query）
 → Java 按路由检索：
     PROFILE    → 查 profile_snapshots（最新 2 份），不查 chunks；查不到 fallback HYBRID
     STRUCTURED → SQL 过滤 chunks.metadata，不走向量
     SEMANTIC   → pgvector 相似度检索
     HYBRID     → 元数据预过滤 + pgvector + 时间衰减
 → 上下文截断（Java 端）：快照 ≤2 份、日记 ≤5 条、对话历史最近 3 轮
 → gRPC Chat（服务端流式 stream ChatChunk）→ 前端逐段渲染
 → 保存 user/assistant 消息（assistant 带 sources）+ 触碰 session.updated_at
```

**时间衰减（SEMANTIC/HYBRID，Java SQL 实现）**：

```
final_score = (embedding <=> query) × 1 / (1 + 天数差 / half_life)
```

half_life 默认 30 天（用户可调 7-365）；ExtractIntent 明确返回 time_range 时自动关闭衰减（用户点名了历史时间，不应被降权）。

**来源追溯**：回答附带 sources（record_id/quote/date），前端可点击查看原文；落库见 3.3。来源可包含每日总结（系统记录）。

**兜底**：AI 失败 → "暂时无法回答"；检索为空 → "没有找到相关记录"。

### 6.7 每日总结（🔜 规划，系统 Record 方案）

**裁决 #17**：废除 `daily_summaries` 表。每日总结作为一条**系统生成的 Record** 进入系统，复用全部既有链路（RAG 检索、对话来源引用、导出）——与"Chunk 是唯一业务单元"哲学完全一致。

**生成流程**（每天 01:00 Asia/Shanghai，Spring Scheduler）：
1. SQL 统计昨日数据（记录数、类型分布、情绪分布、活跃时段）。
2. gRPC 复用 `MirrorChat.Chat` 生成日报文本（不新增 Proto/RPC）。
3. 创建系统记录：
   - `Record(source='system', status='done', user_reviewed=true, content=日报文本)`——**跳过管道、跳过审核**；
   - `Chunk(segment=日报文本, metadata={contentType:'daily_summary', summaryDate:'2026-09-02', recordCount:N, ...})`；
   - 立即 `Embed` → 向量入库。
4. 前端查询：`GET /api/summaries?date=YYYY-MM-DD`（按 source='system' + summaryDate 查系统记录），日报在界面上有独立入口，不混入记录流。

**为何不做 `chunks.record_id` 可空 / 总结独立检索路径**：破坏外键完整性或造成"两条检索代码路径"，均违背简洁性。方案 A 的全部代价只是统计口径排除 system 记录（一个 WHERE 条件）。

### 6.8 写作灵感（🔜 规划）

输入停顿 >30s 触发：当前输入 → `Embed` → pgvector 检索相关历史 → 复用 `Chat` 生成 2-3 条提示。临时内容不存储。

### 6.9 数据导出（🔜 规划，P2）

`GET /api/export/json`（结构化备份）与 `GET /api/export/markdown`（人可读）。**自动排除所有 embedding 向量字段**。全量同步导出不分页（年数据量 1000-2000 条）。**只导出不导入**（裁决 #19：毕设论文表述"数据可携带"即可，导入功能砍掉）。

---

## 七、AI 服务（Python gRPC）

### 7.1 服务与 RPC

| Proto | 服务 | RPC | 状态 |
|-------|------|-----|------|
| common.proto | — | 枚举（MoodType 13 / ContentType 8 / TaskStatus 3 / AiProtocol 2）、LlmConfig、EmbeddingConfig | ✅ |
| record_processor.proto | RecordProcessor | `Classify(ClassifyRequest) → ClassifyResponse{skip, skip_reason, repeated ClassifyItem}`；ClassifyItem{title, summary, content, content_type, moods, status, keywords}；`single` 标志（4.4） | ✅（single 规划） |
| embedding.proto | EmbeddingService | `Embed`（vector/dimension/model_name）、`GetModelInfo` | ✅ |
| mirror_chat.proto | MirrorChat | `ExtractIntent`（加 query_type）、`Chat`（服务端流式） | 🔜 |
| mirror_profile.proto | MirrorProfile | `GenerateProfile`（加 recent_chats/ChatRecord） | 🔜 |

**EmbeddingConfig 携带 base_url**（API 模式自定义地址，Python 端 ApiEmbedder 需接线，见第九章）；本地 BGE-m3 单例懒加载，仅 local 模式加载。

### 7.2 配置传递与无状态

配置唯一源 = Java `user_settings` 表。每次 gRPC 请求由 AiGrpcClient 读取、解密 API Key、组装 LlmConfig/EmbeddingConfig 随请求携带；Python 用完即弃。天然支持多用户各用各的模型。曾评估过的 ConfigService 推送方案已废弃。

### 7.3 Prompt 要点（Python prompts/）

| Prompt | 要点 |
|--------|------|
| classify | 个人记录助手；判无意义（skip）；标题≤10字、摘要≤30字；8 类型 + 13 情绪多选 + taskStatus + 3-5 关键词；一次调用完成拆分+分类 |
| classify（single 变体） | 禁止拆分；"这是用户确认过边界的完整片段"；返回恰好 1 条 |
| profile | 自我认知助手；五维统计 + 最近对话；**只事实陈述不主观判断**（"标记了 3 次焦虑"而非"状态不好"） |
| intent | query_type 四选一 + 过滤条件 + 改写 query，JSON 英文小写 |
| chat | 人生教练"镜子"；只陈述事实、**每个结论引用来源**、检索不足诚实说明、3-5 句 |
| inspiration | 基于历史给 2-3 个写作方向，不直接复制 |

### 7.4 超时

| RPC | 超时 |
|-----|------|
| Classify | 180s（含拆分+分类） |
| Embed | 10s |
| Chat | 60s（流式） |
| ExtractIntent | 15s |
| GenerateProfile | 60s |

**同步关系**：Java 改 proto 后必须在 Python 端重跑 `generate_proto.py`，否则字段错位（踩坑记录：api_key/base_url/model 曾串位）。

---

## 八、前端协作契约

> 本章供前端团队直接引用：响应包装、状态语义、审核交互到端点的映射、标签显示对照。

### 8.1 基础约定

| 项 | 约定 |
|----|------|
| Base URL | `http://<host>:9005/api` |
| 认证 | 除 `/auth/*` 外全部需要 `Authorization: Bearer <JWT>`；Token 24h 过期 |
| 响应包装 | 统一 `R<T>`：`{ code, message, data, timestamp }`；成功 `code=200`；具体错误码以后端 `ResultCode` 枚举为准 |
| 401 处理 | 响应拦截器捕获 401 → 清除本地 Token → 跳转登录页 |
| 时间 | 后端返回 ISO-8601（含时区偏移），前端按 Asia/Shanghai 渲染 |
| 标签存储 | 数据库存英文小写，**中文显示由前端映射**（对照表见 8.4） |

### 8.2 记录状态与轮询

提交记录是异步管道，无推送通道（SSE 为后续可选优化）：

```
POST /records
  → 返回 RecordVO{status: "processing"}
  → 前端每 2-3s 轮询 GET /records/{id}
  → status 变为 reviewing / failed 时停止，渲染对应 UI
```

| status | 前端展示 | 允许的操作 |
|--------|----------|-----------|
| processing | 转圈 + "AI 整理中" | 无 |
| reviewing | 审核界面（片段卡片列表） | 编辑/增/删片段、确认、删除记录 |
| done | 正常展示（只读） | 无 |
| failed | 错误提示（含 skip 原因） | 重试（`POST /records/{id}/retry`）、删除记录 |

### 8.3 审核界面：片段卡片交互 → 端点映射

`GET /records/{id}` 返回的 `chunks[]` 渲染为卡片列表，每张卡片展示 `segment`（可编辑文本）+ metadata（title/summary/contentType 下拉/mood 多选/keywords）。

| 用户操作 | 前端动作 | 端点序列 |
|----------|----------|----------|
| 改片段文字/标签 | 卡片内编辑 | `PUT /chunks/{id}` |
| 删掉一段 | 卡片删除按钮 | `DELETE /chunks/{id}` |
| 补一段 | "新增片段"按钮 | `POST /records/{id}/chunks`（body: `{segment: "文本"}`；响应含 AI 自动回填的 metadata，失败时 metadata 为空照常展示） |
| 合并 A、B | 前端引导 | ① `PUT /chunks/A`（segment=合并文本）→ ② `DELETE /chunks/B` |
| 拆分 A | 前端引导 | ① `PUT /chunks/A`（segment=前半）→ ② `POST /records/{id}/chunks`（segment=后半） |
| 确认 | "确认"按钮 | `PUT /records/{id}/confirm`（阻塞数秒——含补分类+Embedding，需 loading 态） |
| 丢弃 | "删除"按钮 | `DELETE /records/{id}` |

注意：合并/拆分是多步序列，前端口按顺序 await；后端无跨请求事务，但片段间无"必须拼回原文"约束，中途失败不会造成脏状态，重试即可。

### 8.4 标签中文显示对照（存储英文 → 显示中文）

**内容类型（8 选 1）：**

| 存储 | 显示 | 存储 | 显示 |
|------|------|------|------|
| todo | 待办 | note | 随记 |
| thought | 感想 | work | 工作 |
| learning | 学习 | social | 社交 |
| plan | 计划 | health | 健康 |

**情绪（13 选多选）：**

| 存储 | 显示 | 存储 | 显示 |
|------|------|------|------|
| happy | 开心 | bored | 无聊 |
| excited | 兴奋 | confused | 困惑 |
| satisfied | 满足 | anxious | 焦虑 |
| grateful | 感恩 | sad | 难过 |
| expecting | 期待 | angry | 愤怒 |
| calm | 平静 | exhausted | 疲惫 |
| stressed | 压力 | | |

**任务状态（仅 todo/plan 类显示）：** not_started 未开始 / in_progress 进行中 / completed 已完成。

**系统记录：** `source='system'` 的记录（每日总结）不出现在记录流，走 `/api/summaries` 独立入口。

### 8.5 主要 VO 结构（TypeScript 参考）

```typescript
interface RecordVO {
  id: number;
  content: string;            // 原始全文（不可改）
  segments: string[];         // 从 chunks 推导的片段列表
  status: 'processing' | 'reviewing' | 'done' | 'failed';
  userReviewed: boolean;
  chunks: ChunkVO[];
  createdAt: string;
  updatedAt: string;
}

interface ChunkVO {
  id: number;
  recordId: number;
  segment: string;            // 片段文本（可编辑）
  metadata: {                 // AI 元数据（可编辑）
    title: string;
    summary: string;
    contentType: string;      // 8 选 1 英文
    mood: string[];           // 13 选多选英文
    keywords: string[];
    taskStatus?: string;      // 仅 todo/plan
  };
  hasEmbedding: boolean;      // confirm 后为 true
}
```

---

## 九、Python AI 服务协作清单

> 本章供 Python 团队直接引用。仓库：Mu-mirror-AI（Python 3.11+ / grpcio-aio）。核心原则：**完全无状态，不存任何用户配置**，配置随每次请求到达，用完即弃。

### 9.1 现有基础（已完成）

- `server.py` gRPC 入口；`services/record_processor.py`（Classify）、`embedding_service.py`（Embed/GetModelInfo）
- `llm/`：BaseLlm + openai_llm（兼容 qwen/zhipu）+ anthropic_llm + factory（按 protocol 路由）
- `embedding/`：local_embedder（BGE-m3，单例懒加载，仅 local 模式加载）+ api_embedder + factory
- `generate_proto.py`：proto 编译脚本

### 9.2 待办清单（按路线图阶段）

| # | 事项 | 说明 | 关联阶段 |
|---|------|------|----------|
| 1 | proto 重新编译 | Java 端 proto 变更后在 Python 端跑 `generate_proto.py`，否则字段错位（历史踩坑） | 阶段 1 |
| 2 | `ClassifyRequest.single` 单段模式 | single=true 时禁止拆分、恰好返回 1 条 ClassifyItem；prompt 加 single 变体 | 阶段 1 |
| 3 | taskStatus 确保填充 | todo/plan 类必须返回 not_started/in_progress/completed（Java 端依赖它做待办聚合） | 阶段 1 |
| 4 | ApiEmbedder 接 `base_url` | `EmbeddingConfig.base_url` 已在 proto 中但 ApiEmbedder 未使用，接上 | 阶段 0 |
| 5 | mirror_chat.proto 实现 | `ExtractIntent`（query_type 四选一 PROFILE/STRUCTURED/SEMANTIC/HYBRID + 过滤条件 + rewritten_query）、`Chat`（服务端流式 `stream ChatChunk{content, done, sources}`） | 阶段 4 |
| 6 | mirror_profile.proto 实现 | `GenerateProfile`：输入五维统计 + recent_chats（ChatRecord{role, content, created_at}）+ total_records + llm_config；输出六维分析 + user_tags + overall_summary | 阶段 3 |
| 7 | prompts 目录六套模板 | classify / classify-single / intent / profile / chat / inspiration（要点见 7.3） | 随各阶段 |
| 8 | 异常 → gRPC 状态码映射 | LLM 超时→DEADLINE_EXCEEDED；内容不合规→INVALID_ARGUMENT；内部错误→INTERNAL；模型不可用→UNAVAILABLE | 随各阶段 |
| 9 | 健康检查 | `GetModelInfo` 作为健康检查端点（Docker healthcheck 用） | 阶段 0 |

### 9.3 契约要点（不可破坏）

- 不访问数据库、不读文件系统用户数据——所有输入来自请求参数。
- `config.yml` 仅服务级配置（端口 50051、workers、prompts 路径），禁止存放用户模型配置。
- 枚举值一律英文小写（除 proto 枚举大写名），与 Java 端 `ContentType`/`MoodType` 枚举一一对应。
- 每日总结复用 `Chat` RPC（不新增服务）；写作灵感复用 `Embed` + `Chat`（不新增 RPC）。

---

## 十、API 一览

| 方法 | 路径 | 状态 | 说明 |
|------|------|------|------|
| GET | `/api/auth/status` | ✅ | 健康检查 |
| POST | `/api/auth/register` / `login` | ✅ | 注册 / 登录 |
| GET | `/api/auth/me` | ✅ | 当前用户 |
| POST | `/api/records` | ✅ | 创建记录（触发管道） |
| GET | `/api/records` | ✅ | 列表（startDate/endDate，默认今天；仅 source='user'） |
| GET | `/api/records/{id}` | ✅ | 详情（含 Chunks） |
| PUT | `/api/records/{id}/confirm` | ✅ | 确认（补分类 → Embedding → DONE） |
| DELETE | `/api/records/{id}` | ✅ | 软删除（REVIEWING/FAILED） |
| GET | `/api/records/calendar` | ✅ | 日历标记（需补 source='user' 过滤） |
| POST | `/api/records/{id}/retry` | 🔜 | FAILED 重试（重跑管道） |
| PUT | `/api/chunks/{id}` | ✅ | 编辑片段（文本/元数据） |
| DELETE | `/api/chunks/{id}` | 🔜 | 删除片段 |
| POST | `/api/records/{id}/chunks` | 🔜 | 新增片段（自动单段分类） |
| GET/PUT | `/api/settings` | ✅ | 配置读取/更新（Key 脱敏；规划加 rag_half_life、维度校验） |
| POST | `/api/settings/test-ai` / `test-db` | ✅（桩） | 连接测试（待真实实现） |
| GET | `/api/mirror` | 🔜 | 最新画像快照 |
| POST | `/api/mirror/generate` | 🔜 | 生成 manual 快照 |
| POST | `/api/mirror/chat` | 🔜 | 对话（流式） |
| GET/DELETE | `/api/mirror/sessions[/{id}]` | 🔜 | 会话列表 / 历史 / 删除 |
| GET | `/api/summaries?date=` | 🔜 | 每日总结查询（查 source='system' 记录） |
| POST | `/api/inspiration` | 🔜 | 写作灵感 |
| GET | `/api/export/json` · `/api/export/markdown` | 🔜 | 数据导出（无导入） |

---

## 十一、安全

| 项 | 现状 | 待办 |
|----|------|------|
| API Key 存储 | AES-256-GCM（随机 IV），读取脱敏 | **密钥外置**（当前硬编码在 CryptoUtils，TODO 未修） |
| 认证 | JWT 24h + BCrypt | 生产密钥配置化；刷新策略可选 |
| SQL 注入 | MyBatis-Plus 参数化 | 保持约定：手写 SQL 一律占位符 |
| 传输 | gRPC plaintext（本机/内网部署） | 可选 TLS，非毕设必做项 |
| 数据隔离 | 所有查询强制 user_id 过滤 | 检索 SQL 需排除软删除记录的 chunks（当前潜在遗漏，见第十六章） |

---

## 十二、部署

Docker Compose 三服务：`backend`（9005）+ `ai`（50051）+ `postgres`（pgvector 镜像）。环境变量注入 JWT 密钥与 AES 密钥；数据库初始化执行 `db/schema.sql` + `db/chunks.sql`；健康检查：`/api/auth/status`、gRPC `GetModelInfo`。时区统一 Asia/Shanghai。

---

## 十三、测试设计

### 13.1 单元测试（Java，JUnit 5 + Mockito）

| 目标 | 用例要点 |
|------|----------|
| CleanProcessor | 空内容/纯空白/纯标点丢弃；多空行合并；控制字符过滤（纯函数，边界穷举） |
| classified_segment 状态机 | AI 回填→有值；改文本→NULL；改元数据→不变；新增段→NULL（confirm 补分类正确性的核心） |
| confirmReview | 补分类失败继续 / Embedding 失败仍 DONE / 无 Chunk 报错（mock mapper + gRPC client） |
| 状态流转 | reviewing 之外编辑 chunk / confirm 报错；failed 才能重试 |
| 软删除过滤 | 列表/日历/检索均排除 deleted_at 非空与 source='system' |

### 13.2 集成测试

- Testcontainers（PostgreSQL + pgvector 镜像）跑 Mapper 层：向量检索 SQL、metadata 过滤、时间衰减公式。
- 双服务联调：本地同时起 Java + Python，走完整提交→审核→confirm 链路。
- Python 未就绪时的替代：gRPC 测试桩（Java 端写一个返回固定 ClassifyResponse 的内存实现）。

### 13.3 API 测试

- Knife4j（`/api/doc.html`）手测全部端点；Apifox 建项目做自动化回归与 Mock（前后端并行开发时前端可先对 Mock 开发）。

### 13.4 检索效果评估（论文实验数据）

- 构造 20-30 个典型 query + 人工标注期望命中的记录集合。
- 指标：Hit@5 / MRR。
- 对比实验：① 有/无意图路由（query_type）② 有/无时间衰减 ③ 有/无元数据预过滤——输出三组对比数据支撑论文第 4 章。

### 13.5 数据点统计（论文素材）

```sql
-- AI 拆分被人工修正率
SELECT COUNT(*) FILTER (WHERE user_edited) :: float / COUNT(*) FROM chunks WHERE user_id = ?;
```

---

## 十四、开发路线图

| 阶段 | 时间 | 内容 | 涉及端 |
|------|------|------|--------|
| 0. 技术债清理 | 9 月上旬（~1 天） | 第十六章清单：删死代码、密钥外置、schema 重写为基准 DDL、检索排除软删除、ApiEmbedder 接 base_url | Java + Python |
| 1. segment 手动调整全链路 | 9 月上中旬（~3 天） | 新端点 + classified_segment/user_edited/source 列 + confirm 补分类 + proto single 模式 + Python classify 变体 + taskStatus 落 metadata | Java + Python |
| 2. 前端骨架 | 9 月中旬起（与后端并行） | 记录/审核页先行（消化片段卡片交互）、登录、设置页；对话/镜子页后置 | 前端 |
| 3. 镜子画像 | 9 月底~10 月 | profile_snapshots + GenerateProfile + monthly 定时任务 + 漂移检测 | Java + Python |
| 4. 对话 | 10 月~11 月 | ExtractIntent(query_type) + 四路检索 + 流式 Chat + sources 落库 | Java + Python + 前端 |
| 5. 总结/灵感/导出/auto 审核 | 11 月 | 每日总结（系统 Record 方案）、写作灵感、数据导出、review_mode 接线 | Java + Python |
| 6. 测试 + 论文素材 | 12 月~1 月 | 第十三章测试设计与检索对比实验、userEdited 修正率统计 | 全部 |

**关键排期判断：前端不等到后端全部完成才启动**——审核页 + segment 调整是最优先界面，能反向验证交互模型，且毕设演示尽早有可看的东西。

---

## 十五、裁决清单（与旧文档的冲突处理）

| # | 旧文档说法 | v2 裁决 | 理由 |
|---|-----------|----------|------|
| 1 | 拆分返回多条 Record（每条独立审核） | 一条 Record + N 个 Chunk | 08854ba 已落地；Record 保留原始输入凭证，业务数据归 Chunk |
| 2 | `records.segment` 存拆分数组 | **废除该列**，Chunk 是 segment 唯一真源 | 双份存储已出现"编辑不同步"一致性隐患 |
| 3 | 审核只能改标签，切错只能整条删 | 审核窗口可增删改 Chunk（片段卡片模型） | 拆分是语义判断 LLM 会错；手动调整是"零负担"理念的必要补救 |
| 4 | 用户改过的 segment 重新整条分析 / 或直接入库 | **按需补分类**：文本变过才对该片段 single 分类；只改标签直接入库 | 直接入库导致 metadata 缺失→下游统计/RAG 过滤漏数据；整条重跑会被 AI 再拆乱 |
| 5 | Embedding+存 chunks 原子性（全成或全败） | Embedding 失败不阻断确认，向量后续补录 | 与代码一致；数据不丢原则优先 |
| 6 | tags 表存关键词 | 废弃 tags 表，关键词在 chunks.metadata | 代码从未写入，双写无意义 |
| 7 | mirror_profiles 存画像 | profile_snapshots（分层保留+向量+快照类型） | 画像要有记忆与变化轨迹 |
| 8 | 对话历史不存 sources（优化文档） | **sources 落库保留** | 来源追溯是核心卖点与论文亮点 |
| 9 | chat_sessions.last_message_at | 统一用 updated_at | 同义冗余列 |
| 10 | 周报/月报总结 | 删除；月度趋势由 monthly 画像快照承担 | 两套"月度输出"冗余 |
| 11 | 内容块模型（blocks） | 彻底删除 | 从未落库，Record 已扁平化 |
| 12 | "🗄️ 数据库配置"P0 功能 | 移出功能清单，归部署配置 | 属于部署层，非用户功能 |
| 13 | DELETE 仅 REVIEWING 可删 | REVIEWING + FAILED 均可删 | 与代码一致 |
| 14 | 画像快照不建任何索引 | 常规索引全建；chunks 建 HNSW；仅快照向量索引省略 | 小规模≠不要索引；维度固定提前就位 |
| 15 | "多用户就绪"表述 | 明确单实例小规模定位（1.3） | 决定性能取舍的一致前提 |
| 16 | ClassifyItem.taskStatus 无落库 | 写入 chunks.metadata.taskStatus | 待办聚合与镜子"未完成的事"依赖它（当前代码丢弃，待补） |
| 17 | daily_summaries 独立表 | **废除**；总结=系统 Record（source='system'）+ Chunk，复用全链路 | 与"Chunk 唯一业务单元"一致；对话可引用总结；代价仅统计口径一个 WHERE |
| 18 | Embedding 维度按模型可变（1024/1536） | **硬约束 1024 维**，设置页校验拒绝其他维度 | 避免 HNSW 迁移与向量重建机制；动态维度写入论文 future work |
| 19 | 导出+导入（JSON 可回导） | 只导出不导入 | "数据可携带"论文表述即可，导入砍掉省工 |
| 20 | （无）records 来源概念 | 新增 `records.source`（user/system） | 支撑每日总结系统 Record 方案的统计口径分离 |
| 21 | FAILED 只能删除 | 实现 `POST /records/{id}/retry` 重跑管道 | 兑现旧文档"重新尝试"承诺 |

---

## 十六、技术债与清理项

| 项 | 说明 |
|----|------|
| 废弃代码清理 | 删 `Tag.java`/`TagMapper.java`/tags 表；移除 pom 中未使用的 Redis、Hutool、MapStruct 依赖 |
| schema.sql 重写 | 现文件堆叠大量 ALTER/DROP，重写为 v2.0 基准 DDL + 迁移脚本（含 segment 列 DROP、source 列 ADD） |
| AES 密钥外置 | CryptoUtils 硬编码默认密钥，改环境变量注入 |
| 连接测试桩 | test-ai / test-db 返回假结果，接真实 gRPC/SQL 探测；新增 Embedding 维度校验 |
| RecordQueryDTO 清理 | page/size/contentType/mood/status 字段无代码使用，删除或接线 |
| 检索排除软删除 | searchBySimilarity* 需关联 records 过滤 `deleted_at IS NULL`（当前删除记录的 chunks 可能仍被检索命中） |
| 统计口径 | 记录列表/日历/导出补 `source='user'` 过滤 |
| review_mode 接线 | 字段存在但无代码读取；实现 auto 模式（5.5） |
| taskStatus 补齐 | ClassifyProcessor 将 taskStatus 写入 chunk.metadata |
| 列表 N+1 | RecordVO.toVO 逐条查 chunks；小规模可接受，后续可批量 |
| 定时任务基础设施 | 主类补 `@EnableScheduling`（每日总结/月度快照前置） |
| PROGRESS.md | 已更新文档索引指向 v2.1（本次完成） |

---

## 十七、论文可写点

1. **审核后入库的质量保障机制**：RAG 数据始终是用户确认版本；userEdited 统计"AI 拆分被人工修正率"。
2. **按需补分类**：用户修正与 AI 分析的协作模式（变化片段才补，单段禁拆分）。
3. **画像变化轨迹与漂移检测**：快照分层保留 + embedding 余弦距离量化"自我变化"。
4. **意图路由 + 时间衰减 RAG**：query_type 四路分流省去无效向量检索；半衰期加权解决久远记录噪音；13.4 的对比实验提供量化证据。

---

## 十八、变更记录

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-09-03 | v2.0 | 初版：以代码基线 08854ba 对账重写；确立 Chunk 唯一业务单元模型；新增 segment 手动调整与按需补分类设计；并入优化设计（画像快照/对话/检索）；统一 16 项旧文档冲突裁决 |
| 2026-09-03 | v2.1 | 补全协作与落地章节：① 每日总结改为系统 Record 方案（废除 daily_summaries，新增 records.source 列，裁决 #17/#20）；② Embedding 维度硬约束 1024（裁决 #18）；③ 确认 auto 审核接线、FAILED retry 端点、砍掉 JSON 导入（裁决 #19/#21）；④ 新增第八章前端协作契约（轮询/交互映射/标签对照/VO 结构）、第九章 Python 协作清单、第十三章测试设计、第十四章开发路线图；⑤ 旧设计文档移至 docs/archive/，PROGRESS.md 索引更新 |
