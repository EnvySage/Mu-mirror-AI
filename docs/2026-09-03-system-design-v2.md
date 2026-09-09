# AI 日记"镜子"系统 — 总体设计文档 v2.3

> 版本：v2.3（v2.2 基础上同步递归累计镜子 sprint 设计与镜子语义偏差纠正，裁决 32-34）
> 日期：2026-09-06
> 代码基线：v2.1 基线 `08854ba` 之后经历阶段 0-5 全部落地 + 端到端联调（2026-09-04）+ 词典 sprint（2026-09-05）+ 递归累计镜子 sprint（2026-09-06，🔨 三线开发中）
> 状态：设计定稿，随开发持续修订。已实现标 ✅（含与原设计的偏差），开发中标 🔨，规划中标 🔜

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
| 后端 | Spring Boot 3.5 / Java 21 / MyBatis-Plus，端口 **9050**（✅ 实际端口，v2.1 写 9005 有误；上下文路径 `/api`） |
| AI 服务 | Python 3.11+ / grpcio-aio，gRPC 端口 50051（独立仓库 Mu-mirror-AI）✅ |
| 数据库 | PostgreSQL + pgvector ✅ |
| LLM | 用户自配（OpenAI 兼容协议 / Anthropic 协议）✅ |
| Embedding | 本地 BGE-m3（1024 维，默认）/ API 模式可配（**维度同样硬约束 1024**，见 3.4）✅ |
| 前端 | Vue 3 + TypeScript ✅（"晨纸"亮色主题，commit 28577d9） |
| 认证 | JWT（24h 过期）+ BCrypt ✅ |

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
| 记录管理 + Chunk | P0 | ✅ 已实现 | CRUD、软删除、审核、日历；segment 手动调整全链路（增删改/合并/拆分/confirm 补分类/retry） |
| 用户设置 | P0 | ✅ 已实现 | 模型配置 CRUD + AES 加密 + test-ai/test-embedding 真探测（1024 维校验）+ rag_half_life + auto 审核模式 |
| 标签审核 | P0 | ✅ 已实现 | 并入记录模块（片段卡片审核，taskStatus 已落 metadata） |
| 镜子画像 | P0 | ✅ 已实现 + 🔨 递归累计镜子开发中 | 快照 + 漂移检测 + 对话纳入 + 快照历史/对比接口 + GET /mirror/stats 聚合统计；🔨 语义纠正为"截至 N 月的累计画像"（递归承续上月镜子，rolling-mirror-design.md，见 6.5） |
| 对话 | P0 | ✅ 已实现 | 意图路由 + 四路检索 + 多轮 + SSE 流式 + sources 落库 + 会话管理 |
| 每日总结 | P1 | ✅ 已实现 | DailySummaryScheduler 01:00 定时（幂等），GET /api/summaries，系统 Record 方案 |
| 写作灵感 | P1 | ✅ 已实现 | POST /api/inspiration |
| 日历导航 | P1 | ✅ 已实现 | 按月统计每天记录数（source='user' 过滤 + Asia/Shanghai 统一口径） |
| 数据导出 | P2 | ✅ 已实现 | JSON + Markdown（排除向量，只导出不导入） |
| 活动统计 | P2 | ✅ 已实现 | GET /api/mirror/stats 聚合端点 + 前端六图表（纯 SVG 零依赖） |
| 个人词典 | P1 | ✅ 已实现 | user_terms + ExtractTerms RPC + glossary 四注入点 + 双调度器接线 + 前端词典卡/候选分区（2026-09-05，见 6.11） |
| 工具调用 | P1 | 🔨 开发中 | Planner-Executor + 8 工具注册表 + tool_calls 审计（设计稿 toolcalling-vault-design.md） |
| 资产保管 vault | P1 | 🔨 开发中 | vault_items/vault_blobs 建表 ✅，VaultService/REST/消化管道待开发（同上） |
| 前端 | — | ✅ 已实现 | "晨纸"亮色主题 + 词典/统计/快照对比全接线；vault 前端随 sprint 排产 |

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

> ✅ 已补齐（2026-09-04）：`taskStatus` 由 ClassifyItemConverter 写入 `chunks.metadata.taskStatus`（todo/plan 类有效：not_started/in_progress/completed）；待办聚合与镜子"未完成的事"维度、get_stats 工具依赖它（缺省旧数据 COALESCE 归 not_started，裁决 #16 口径）。
> 系统记录（source='system'，如每日总结）的 Chunk metadata 使用 `contentType="daily_summary"` + `summaryDate` 字段标识。

**`user_settings`（不变 + 🔨 递归镜子轮新增 1 列）：**

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
    rag_half_life INT DEFAULT 30,                   -- ✅ RAG 时间衰减半衰期（天，7-365）
    mirror_lookback INT DEFAULT 1,                  -- 🔨 回看深度 0-3（档位语义见 6.5，rolling-mirror-design.md §2）
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

### 3.3 已实现与规划表（3.2 未覆盖的表）

```sql
-- 画像快照（取代旧 mirror_profiles）
CREATE TABLE profile_snapshots (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    snapshot_type VARCHAR(20) NOT NULL,   -- manual（用户触发）/ monthly（每月1号定时）
    period_month CHAR(7),                 -- 🔨 归属月份 'YYYY-MM'（递归镜子轮补列：幂等判断切精确列，替代按 created_at 日期近似；历史 3 行 UPDATE 补值）
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

-- 个人词典（✅ 已实现，2026-09-05，lexicon-design.md v1.0；schema.sql 137-158 行）
CREATE TABLE IF NOT EXISTS user_terms (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL,
    term VARCHAR(100) NOT NULL,
    aliases JSONB DEFAULT '[]'::jsonb,     -- ["毕设","那个设计"]
    description TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending/confirmed/dismissed
    query_hit_count INT DEFAULT 0,          -- 用户提问命中（控制注入优先级）
    content_hit_count INT DEFAULT 0,        -- 入库内容命中（控制过期沉底）
    last_confirmed_at TIMESTAMPTZ,          -- confirmed 卡片显示"最后确认于x日"
    last_seen_at TIMESTAMPTZ,               -- 最近一次语料出现（衰减依据）
    source_chunk_id BIGINT REFERENCES chunks(id) ON DELETE SET NULL,
    source_record_id BIGINT,                -- 佐证 chunk 所在记录（实现新增：前端跳记录详情直接用）
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_user_terms UNIQUE (user_id, term)
);
CREATE INDEX IF NOT EXISTS idx_user_terms_user_status ON user_terms(user_id, status);

-- vault 用户资产保管（🔨 开发中，vault_items/vault_blobs 建表 ✅，dialogue-enhancement-ideas.md C 组）
CREATE TABLE IF NOT EXISTS vault_items (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL,
    original_name VARCHAR(255) NOT NULL,     -- 清洗后原文件名（路径穿越防护）
    storage_key VARCHAR(500) NOT NULL,       -- {userId}/{yyyyMM}/{uuid}.ext（VaultStorage 接口抽象参数）
    size_bytes BIGINT NOT NULL,
    mime VARCHAR(100) NOT NULL,              -- magic bytes 校验后的真实 mime
    sha256 VARCHAR(64),                      -- 同用户同内容去重
    category VARCHAR(20),                    -- 复用 contentType 枚举
    description VARCHAR(500),                -- 用户一句话提示 / LLM 自动命名（三层渐进）
    digest_status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- 五态 pending/extracted/confirmed/skipped/failed（裁决 #36，原四态 done→confirmed 迁移）
    source_chunk_id BIGINT REFERENCES chunks(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    deleted_at TIMESTAMPTZ                   -- 硬删除留审计位（非软删恢复）
);
CREATE INDEX IF NOT EXISTS idx_vault_items_user ON vault_items(user_id, deleted_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_vault_sha ON vault_items(user_id, sha256) WHERE sha256 IS NOT NULL AND deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS vault_blobs (
    vault_item_id BIGINT PRIMARY KEY REFERENCES vault_items(id) ON DELETE CASCADE,
    data BYTEA NOT NULL                      -- PG BYTEA 直存（裁决 #23：否决磁盘+Mongo 方案）
);

-- 工具调用审计（🔨 开发中，建表 ✅；论文三档消融实验数据源）
CREATE TABLE IF NOT EXISTS tool_calls (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL,
    session_id UUID,
    tool VARCHAR(50) NOT NULL,
    args JSONB DEFAULT '{}'::jsonb,
    result_summary VARCHAR(500),
    success BOOLEAN NOT NULL DEFAULT true,
    latency_ms INT,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_user ON tool_calls(user_id, created_at);
```

> **sources 落库的理由**：来源追溯是对话模块的核心卖点（"每个结论都能点回原始记录"），也是论文亮点；只做即时透传的话，用户回看历史会话时引用全部丢失。存储成本可忽略。sources 可引用任何 record（含 source='system' 的每日总结）。

> **user_terms 设计要点（裁决 #22，lexicon-design.md）**：hit_count 拆两字段——query 命中管"活跃度/注入优先级"，content 命中管"还活着没"，衰减逻辑不打架；dismissed 不删行（30 天后可重新浮现），沉底即可。新增的 `source_record_id` 列是实现阶段补的（F 契约需要跳记录详情，直接存 record_id 免前端映射）。

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

**created_at 语义声明（✅ 核查确认，2026-09-06 递归镜子轮）**：`records.created_at` 一律取**写入时刻**；日历、月度快照窗口（`until`）、`/mirror/stats` 等按时间聚合的口径均以该列为时间轴。生成管道（每日总结等系统 Record）本就按**归属日期**写入对应窗口——不存在"生成时间与归属日期错位"的数据回填需求（数据专项 2026-09-06 核查 0 不一致）。镜子累计窗口的归属月份以 `profile_snapshots.period_month` 显式列承载（3.3），不复用 `created_at` 近似推断。

### 4.3 状态机

| 状态 | 含义 | 可执行操作 |
|------|------|-----------|
| processing | AI 处理中 | 无 |
| reviewing | 待审核 | 编辑 Chunk（文本/元数据/增删）、确认、删除 |
| done | 已确认锁定 | 仅查看 |
| failed | 处理失败 | 重新尝试（`POST /records/{id}/retry`，重跑分类管道）、删除 |

> 时间口径备注（递归镜子轮）：上表状态流转与 created_at 无耦合；系统 Record（每日总结，source='system'）的 created_at 亦为写入时刻，归属日期由 `metadata.summaryDate` 表达（6.7），两口径不混用。

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

### 6.2 记录 + Chunk（✅ 已实现，含全部扩展项）

- 端点：创建（触发管道）、列表（日期范围，默认今天）、详情（含 Chunk 列表）、confirm、软删除、日历统计。
- **RecordVO 的 `segments` 字段从 Chunks 推导**（废除读 records.segment）。
- RecordController 无 PUT 接口（旧文档的"更新记录"端点已删，修改一律走 Chunk 端点）。
- **扩展项全部落地（2026-09-04）**：`DELETE /chunks/{id}`、`POST /records/{id}/chunks`（自动单段分类）、`POST /records/{id}/retry`、confirm 补分类（5.4，补分类/Embedding 失败不阻断）、review_mode=auto 接线、fail_reason 落库（截 500 字，retry 清空）。
- **统计口径**：记录列表与日历统计一律过滤 `source='user'`；系统记录（每日总结）仅通过总结接口与 RAG 检索可见。

### 6.3 日历导航（✅ 已实现）

`GET /records/calendar?month=YYYY-MM` → 每天有效记录数。只导航不分析，标记只区分有/无。时区 Asia/Shanghai（日历 SQL `AT TIME ZONE 'Asia/Shanghai'` + 列表 ZoneId + VO JsonFormat 三层统一，UTC/北京双口径 bug 已修）。`source='user'` 过滤已补。

### 6.4 用户设置（✅ 已实现）

模型配置 CRUD、部分更新、API Key AES-256-GCM 加密 + 前 3 位脱敏、注册自动创建。**已接线**：`rag_half_life` 滑块（7-365，前端带衰减权重预览）、auto 审核模式（5.5，设置页提示"无手动调整机会"权衡）、**Embedding 1024 维校验**（3.4，test-embedding 携带用户 EmbeddingConfig 实测维度，非 1024 拒绝）。**🔨 递归镜子轮**：`mirror_lookback` 回看深度（0-3，默认 1）——设置页"镜子引擎"组（与 rag_half_life 同组），四档文案照 6.5 档位表，3 档慢提示，SettingsDTO/VO + PUT 透传。

### 6.5 镜子画像（✅ 已实现 + 🔨 递归累计镜子开发中）

> 设计蓝本：`coordination/rolling-mirror-design.md` v1.0（2026-09-06 与用户对齐定稿）。核心原则一句话：**叙事让 LLM 传承，账本让数据库记**。

**⚠️ 语义变更声明（2026-09-06 用户纠正的语义偏差，教训登记）**：镜子从"最近 30 天/当月切片"纠正为"**截至 N 月的累计画像**"——N 月镜子 = 上一份镜子（N-1）+ N 月增量 = **截至 N 月的你**；首月（genesis）无上月镜子，全量生成（7 月已是 genesis）。用户在 2026-09-06 发现 8 月快照应为"截至 8 月的累计"而非"8 月切片"——原实现（monthly 快照 30 天/当月窗口）与设计意图"迄今全部"不符，属实现偏差，本轮纠正。连锁口径：

| 项 | 旧（切片） | 新（累计） |
|---|---|---|
| N 月镜子 | 只看 N 月记录 | 上一份镜子（N-1）+ N 月增量 = **截至 N 月的你** |
| 首月（genesis） | — | 无上月镜子，全量生成（7 月已是 genesis） |
| 对比模式 Δ | 两段切片对比 | 相邻月相减 = "这一个月你变了什么"（漂移逻辑不动，仍按 embedding 余弦距离） |
| 幽灵卡/时间线/按月接口 | — | 全部保留，节点语义变为累计 |

**触发**：用户点"查看镜子"（manual 快照，`POST /api/mirror/generate`）；每月 1 号 02:00 定时（monthly 快照，MirrorServiceImpl.monthlySnapshot，@Scheduled cron `0 0 2 1 * ?` Asia/Shanghai）。

**生成流程（🔨 递归镜子轮改造，原已实现部分照旧）**：
1. Java SQL 统计五维数据（未完成待办[metadata.taskStatus 已落库]、最近学习、情绪分布、关键词、活跃时段，全部参数化 SQL + AT TIME ZONE 'Asia/Shanghai'）+ 取最近会话的对话。
2. gRPC `GenerateProfile`（四块输入，见下）→ 六维分析文本 + user_tags + overall_summary。
3. Java 存 `profile_snapshots`（🔨 写入 `period_month` 归属月份，幂等判断切精确列）；五维文本按固定顺序拼接（`joinedAnalysisText()`）→ `Embed` → 向量存快照（失败不阻断，向量缺失仅影响漂移检测）。

**🔨 生成时 LLM 输入四块（prompt 结构，rolling-mirror-design.md §1）**：

| 块 | 内容 | 说明 |
|----|------|------|
| ① prev_mirror | 上期镜子全文（N-1 月快照 overall_summary + 五维分析拼接）；递归链超 12 个月的更早镜子只带一行压缩摘要 | genesis 月无①③；**叙事传承的载体** |
| ② records | 本月原始记录（按回看深度档位带原文，见下方滑块） | 增量事实来源 |
| ③ correction_index | 上期镜子涉及的记录 title+日期清单 | **仅当回看深度=0 时带上**——低档用户防误差累积的补偿 |
| ④ stats_facts | 待办/统计实况直查：数据库实时值（未完成/已完成待办、本月记录数、情绪分布计数），与 get_stats 同源 SQL | 每次生成都查；**LLM 只叙事不记账** |

> **真源原则（裁决 #33）**：待办/统计状态不靠 LLM 转述传承——即使①上月镜子写了"某事未完成"，④实况显示已完成时，本月镜子必须写"本月完成"。叙事可以累积，账本必须实时。

**🔨 回看深度滑块 `mirror_lookback`（0-3，默认 1；设置页"镜子引擎"组，与 rag_half_life 同组）**：

| 档 | 原文带入 | ③ 校正索引 |
|---|---|---|
| 0 | 无（纯继承） | 带（唯一防误差手段） |
| 1 | 上月原文（默认） | 不带 |
| 2 | 近三月原文 | 不带 |
| 3 | 全部历史原文 | 不带 |

- 设置文案：「生成镜子时带多少原文回看：0=只继承上月镜子 / 1=上月原文 / 2=近三月原文 / 3=全部原文（慢，消耗大）」；3 档带慢提示。
- 落点：`user_settings.mirror_lookback INT DEFAULT 1`（🔨），SettingsDTO/VO + PUT 设置接口透传。

**🔨 四道防洪闸（全配置化；触发时打日志 + 前端 toast"历史记录较多，已按回看深度截取最近部分"）**：

```yaml
mirror:
  lookback_max_chunks: 600        # 条数闸，超限取最近的
  lookback_max_chars: 150000      # 总字符闸（与条数闸先触发者生效），旧→新裁剪直到塞下
  per_chunk_max_chars: 2000       # 单条日记渲染截断
  mirror_summary_after_months: 12 # 递归链超 12 个月，更早镜子只带一行摘要
```

**变化轨迹与漂移检测（✅ 已实现 + 扩展）**：
- 分层保留：manual 保最近 2 份，monthly 保 12 份；每月定时生成后执行清理。画像用于对比，不堆数量。
- 漂移检测：本月 vs 上月快照 `embedding <=> embedding` 余弦距离（`selectDriftDistance`），driftDistance 仅 monthly 快照计算（manual 无对比基线为 null）；前端展示"变化幅度" + 两份快照对比。累计语义下漂移逻辑不变——相邻月快照相减本就回答"这一个月你变了什么"。
- **扩展（2026-09-05 上线）**：快照历史两接口 `GET /api/mirror/snapshots`（manual+monthly 合并倒序，LIMIT 14，overallSummary 截 50 字）与 `GET /api/mirror/snapshots/{id}`（完整快照，他人/不存在一律 4041 不暴露存在性），前端时间线/查看切换/对比模式已接线。
- **聚合统计（2026-09-05 上线）**：`GET /api/mirror/stats?days=30`（clamp 7-90）返回 moodDaily/hourDist/weekdayDist（周一=0，SQL DOW 转换 `(dow+6)%7`）/keywordTop/todo 状态计数/recordDaily，全窗口补零；前端六图表（纯 SVG 零依赖）。

**画像与对话的关系**：对话历史参与画像生成（LLM 综合日记+对话）；画像不进日常 RAG 检索，但 PROFILE 类问题直接查快照作答（见 6.6）。

**🔨 本轮分工与验收（rolling-mirror-design.md §4-5，B/AI/F 三线并行）**：
- **B 侧**：user_settings 加 mirror_lookback 列 + GenerateProfileRequest 组装（①②③④，四闸限流，④复用 stats mapper）+ proto 三字段 + profile_snapshots 加 period_month（历史 3 行 UPDATE 补值）+ 单测与真库实测。
- **AI 侧**：GenerateProfile prompt 重写（第 6 套 profile.txt 改造）——语义声明改"撰写截至 N 月的累计画像"，四块占位符 {prev_mirror}/{records}/{correction_index}/{stats_facts}（空块不留孤儿节头，glossary 同模式）；明确"待办状态以④为准（不继承①旧说法）"。
- **F 侧**：设置页"镜子引擎"组回看深度下拉（0-3 四档文案）+ 3 档慢提示；幽灵卡/时间线不动；B 部署后 USE_MOCK_MONTHLY 切 false 联调。
- **验收口径**：生成 8 月镜子应同时提及 7 月实训（继承）与 8 月追番工具（增量），待办状态与设置页实况一致；切回看深度 0 再生成仍累计但细节更少；tool_calls 无异常，92+ 测试回归全绿。

### 6.6 对话（✅ 已实现 + 🔨 工具增强开发中）

**已实现流程（SSE 事件流）**：
```
用户提问 POST /api/mirror/chat
 → GlossaryService.matchQueryTerms（term/alias 字符串匹配，命中 query_hit_count++，
   只把命中的词条传 Python；未命中传 top 高频词 grounding）
 → gRPC ExtractIntent（+query_type 四选一 + content_type/moods/time_range + rewritten_query）
 → Java 按路由检索：
     PROFILE    → 查 profile_snapshots（最新 2 份），不查 chunks；查不到 fallback HYBRID
     STRUCTURED → SQL 过滤 chunks.metadata，不走向量
     SEMANTIC   → pgvector 相似度检索 + 时间衰减（可关）
     HYBRID     → 元数据预过滤 + pgvector + 时间衰减
 → 上下文截断（Java 端）：快照 ≤2 份、日记 ≤5 条、对话历史最近 20 轮（v2.1 设计 3 轮，实现调至 20）
 → SSE 事件流：meta（sessionId/route）→ delta*（流式内容）→ sources（[{record_id,quote,date}]）→ done
 → 保存 user/assistant 消息（assistant 带 sources）+ 触碰 session.updated_at
```

**时间衰减（SEMANTIC/HYBRID，Java SQL 实现，✅ rag_half_life 已接线）**：

```
final_score = (embedding <=> query) × 1 / (1 + 天数差 / half_life)
```

half_life 默认 30 天（用户可调 7-365）；ExtractIntent 明确返回 time_range 时自动关闭衰减（用户点名了历史时间，不应被降权）。检索一律排除 `embedding IS NULL` 与软删除记录的 chunks。

**来源追溯（✅ 已实现）**：回答附带 sources（record_id/quote/date），前端引用芯片可点击查看原文；落库见 3.3。来源可包含每日总结（系统记录）。

**兜底（✅ 已实现）**：AI 失败 → "暂时无法回答"（无 sources 兜底落库）；检索为空 → "没有找到相关记录"；ExtractIntent 超时回退响应补 rewritten_query=原文，修 HYBRID 兜底链 embed(null) NPE。

**🔨 工具增强（开发中，toolcalling-vault-design.md）**：PlanTools Planner-Executor 框架（≤2 步工具计划）→ Java 执行 8 工具注册表 → 结果塞 ChatRequest.tool_results → 正常流式；meta SSE 事件追加 `tools_used`（工具轨迹芯片）、新增 `vault_refs` 事件（对话内文件卡三档）。失败/超时(3s)/空计划 → 跳过工具走现有 RAG，零回归。创新场景首批：B1 事实核查 verify_claim / B2 认知边界 coverage_check / B4 快照对谈 snapshot_persona。顺延：B3 对话教词、B5 漂移澄清（等词典二期）、A5/D1（二期）、E3 原生循环（架构预留）。

### 6.7 每日总结（✅ 已实现，系统 Record 方案）

**裁决 #17**：废除 `daily_summaries` 表。每日总结作为一条**系统生成的 Record** 进入系统，复用全部既有链路（RAG 检索、对话来源引用、导出）——与"Chunk 是唯一业务单元"哲学完全一致。

**已实现生成流程**（每天 01:00 Asia/Shanghai，DailySummaryScheduler，幂等）：
1. SQL 统计昨日数据（记录数、类型分布、情绪分布、活跃时段）。
2. gRPC 复用 `MirrorChat.Chat` 生成日报文本（不新增 Proto/RPC）。
3. 创建系统记录：
   - `Record(source='system', status='done', user_reviewed=true, content=日报文本)`——**跳过管道、跳过审核**；
   - `Chunk(segment=日报文本, metadata={contentType:'daily_summary', summaryDate:'2026-09-02', recordCount:N, ...})`；
   - 立即 `Embed` → 向量入库。
4. 前端查询：`GET /api/summaries?date=YYYY-MM-DD`（按 source='system' + summaryDate 查系统记录），日报在界面上有独立入口（SummarySheet），不混入记录流。
5. **顺路任务（词典 sprint 接线）**：生成日报后逐用户调 `glossaryService.extractScheduled(userId)` 抽取个人词典候选（见 6.11），内部全量 try-catch，主流程零影响。

**为何不做 `chunks.record_id` 可空 / 总结独立检索路径**：破坏外键完整性或造成"两条检索代码路径"，均违背简洁性。方案 A 的全部代价只是统计口径排除 system 记录（一个 WHERE 条件）。

### 6.8 写作灵感（✅ 已实现）

输入停顿 >30s 触发：当前输入 → `Embed` → pgvector 检索相关历史 → 复用 `Chat` 生成 2-3 条提示。临时内容不存储。后端端点 `POST /api/inspiration` 已上线。

### 6.9 数据导出（✅ 已实现，P2）

`GET /api/export/json`（结构化备份）与 `GET /api/export/markdown`（人可读）。**自动排除所有 embedding 向量字段**。全量同步导出不分页（年数据量 1000-2000 条）。**只导出不导入**（裁决 #19：毕设论文表述"数据可携带"即可，导入功能砍掉）。

### 6.10 资产保管 vault（✅ 已实现 + 审查修复批完成，2026-09-06）

> 设计蓝本：`coordination/toolcalling-vault-design.md` v1.0 + `dialogue-enhancement-ideas.md` C 组。一句话：网盘只是仓库，**这是"记得你为什么存它"的仓库**——文件过管道：AI 分类（contentType 枚举）+ embedding（之后能检索内容）+ 词典自动有实体指向。日记是快记忆，文件是重记忆，统一进检索+词典体系。

**确认门禁（裁决 #35，2026-09-06 用户定稿——embed 的准入条件）**：
- 上传 → 自动提取（元数据榨取 + LLM key/description 尝试）→ 停在 `extracted` 态，**不自动 embed**；回执卡展示 key/description 可编辑 + [确认，让它可被检索] / [仅保管，不检索]（skipped）两键
- 提取失败 → key/description 留空待用户填写，填完确认才进 embedding。**未确认：可下载预览（保管完整），检索不到**——"用户背书过的才进记忆"，与记录审核/词典确认同构（裁决 #22 一脉）
- 确认端点 `POST /vault/{id}/confirm`（body key/description/category）→ ①更新元数据 ②生成 key chunk（embed 文本=key+description+类型拼合，contentType='note'，挂 vault_item_id，即 key-embed 进通用检索，裁决 #36=Q3）③全文消化 chunks 此时才 embed ④状态→confirmed（异步 202 语义）
- digest_status 五态：pending（排队）→ extracted（待确认·检索不到）→ confirmed（已确认·已 embed）/ skipped（仅保管）/ failed（读取失败）。旧 done 数据迁移：非图片→confirmed、图片→extracted

**三层防误删（Q2 裁决=硬删保留）**：① 对话内删除内联确认文案带文件名 ② 资产页删除输入文件名后四位（后端 `confirm_name` 校验，不符 400）③ 删除 toast 5 秒撤销窗（前端暂存，真删推迟 5 秒）。deleted_at 留审计位，非软删恢复。

**审查修复批（2026-09-06，31600cb）两高危修复**：
- update() 全列覆盖竞态：PUT /vault/{id} 改 LambdaUpdateWrapper 定向 SET（只 description/category），不再整行覆盖——修复"上传后 1s 内 PUT 改名把 digest_status 覆盖回 pending"竞态；DigestService 5 处回写点同步改定向 SET
- @Async 消化任务静默丢失：新建 AsyncConfig `vaultDigestExecutor`（core=2/max=4/queue=64/digest- 前缀命名/CallerRunsPolicy），修复默认 executor 零存活线程导致任务被拒且无感知
- 已立案未修：上传事务未提交可见性竞态（digest 线程 READ_COMMITTED 读不到未提交行，偶发 pending 卡住，建议 afterCommit 回调，agent-B.md 有记录）

**存储与安全（✅）**：
- **Postgres BYTEA 直存**（裁决 #23，否决磁盘路径 + MongoDB 方案）：vault_items 元数据 + vault_blobs 本体分表（列表永不拉 blob）；数据主权 100% 收敛。
- 全配置化：单文件 **20MB**、每用户总配额 **500MB**、**SHA-256 去重**（同用户同内容拒绝+提示）。
- 类型白名单：pdf/docx/txt/md/csv/jpg/png/webp/gif/mp3/wav/m4a；**exe/zip/svg/视频一律拒**（svg 防 XSS）。
- **magic bytes 校验**（不信扩展名）；original_name 清洗（路径符号/控制字符/255 截断）；非图片类下载强制 `Content-Disposition: attachment`。
- **用户隔离**：全接口 JWT + ownership 校验，非本人资源 404（不暴露存在性）；硬删除 + 确认交互；删除时级联清关联 chunks（向量库无孤儿）；deleted_at 留审计位。

**Key 三层渐进生成**：① 元数据榨取（上传瞬间，零 LLM：PDF Title+首页文本 / docx 属性+首段 / mp3 ID3 / 图片 EXIF / txt 首行）→ ② LLM 自动命名（消化管道顺路：description + category + 词典候选词条——vault×词典咬合点）→ ③ 用户补正（上传卡可空描述框 + 消化回执三键 + 资产页常改常删）。兜底：精确层同时匹配 original_name+description+消化关键词；零 key 检索按时间+类型（"昨天传的图片"）。

**三档消化**：文本/PDF/docx → 全消化（抽文本→现有 chunk 管道挂 vault_item_id，超 5 万 token 只索引前 N 章并告知）；图片 → 半消化（用户描述必填引导 + EXIF 进 embedding；视觉模型 future work）；音视频 → 零消化（元数据卡片，digest_status=skipped）。管道隔离：消化失败只影响该文件状态。

**检索三层漏斗（find_item）**：① 精确（词典 term/别名 + 文件名 ILIKE）→ ② 语义（description+摘要 embedding）→ ③ 全文（消化 chunks 向量）。

**对话工具三件套**：save_item（上传后关联描述）/ find_item（三层检索，引用强度分档）/ recall_item（文件详情+引用摘录）。写操作红线：上传=显式动作免确认；对话内删除/覆盖必须确认卡（二期 create_todo 复用此模式）。

### 6.11 个人词典 User Lexicon（✅ 已实现，2026-09-05 词典 sprint）

> 设计蓝本：`coordination/lexicon-design.md` v1.0。一句话：从用户日记里学"论文=毕设RAG检索"这类个人指代，确认后注入 LLM 全链路，提升分类/检索/对话准确率。

**核心哲学（不可违背）**：
1. **机器猜的不直接用**：pending 只展示不注入，confirmed 才生效——与"审核过的才进记忆"同构（裁决 #22）
2. **词表是加权不是替换**：原文 query 照常走向量检索，词表错了退化为普通检索，不是灾难
3. **Python 无状态铁律**：词表随 gRPC 请求携带，用完即弃，Python 不查库
4. **不动存量向量**：1024 维老 chunk 向量零重建（裁决 #18），词表只走 query 侧改写 + prompt grounding
5. **不新增用户等待路径**：抽取挂定时任务顺路，confirmReview 不碰

**数据模型**：`user_terms` 表（✅ 已建，DDL 见 3.3），三分态 pending/confirmed/dismissed，query_hit_count/content_hit_count 双计数，dismissed 不删行（30 天复活窗）。

**抽取（AI Agent 实现 RPC，B Agent 调度）**：
- **每日 01:00**：DailySummaryScheduler 顺路。语料=近 14 天 confirmed chunks（user_edited=true 权重优先），调 `ExtractTerms` RPC。
- **去重**：近 30 天已处理（confirmed/dismissed/pending 已存在）的 term 跳过；dismissed 30 天后可重新浮现。
- **分级**：new（新词）/ evidence（已有 pending 证据+1）/ update（confirmed 词解释过时，打回 pending 等确认）。
- **每月 1 号 02:00**：MirrorServiceImpl.monthlySnapshot 顺路 `glossaryService.monthlyMaintenance(userId)` 两项：① 词条合并（重复/矛盾词条 aliases 合并建议 → pending 复核）；② 漂移审计（confirmed description vs 近 30 天含该词语料，LLM 判一致性，不一致打回 pending + 新解释建议）。**漂移最多活一个月且被系统主动递到用户面前**。
- 抽取失败只打日志绝不影响每日总结/月度画像主流程。

**注入（B Agent，GlossaryService ✅ 已实现）**：
- 查 confirmed 词条按 query_hit_count 排序 top **30** 截断，进程内缓存 60s（confirm/dismiss/update 后 writeThrough 主动失效）。
- ExtractIntent query 侧匹配：term/alias 字符串包含，命中词条 query_hit_count++ 且**只把命中的**传 Python（省 prompt）；未命中传 top 高频词（grounding 用）。
- Classify/GenerateProfile/Chat 传 top 30 全量。
- Python 端 `glossary_render.py` 渲染固定软约束话术："以下用户个人词汇表**仅供参考，解释可能过时**；与近期记录矛盾时，以近期记录为准。" + 每词条附 confirmed_at（"N月确认"）；空词条不留孤儿话术（B 未传时 prompt 与现状完全一致，零回归）。

**gRPC 四注入点（✅ 已实现，字段号 shared-protocol.md 登记）**：ClassifyRequest.4 / ExtractIntentRequest.3 / ChatRequest.5 / GenerateProfileRequest.10，类型均 `repeated GlossaryTerm`。

**前端（F Agent ✅ 第十轮完成）**：
- 每日总结 sheet 词条候选分区（用户主提议——塞进已有浏览动线）：pending 候选展示 AI 理解 + 证据跳原文 + ⚠️ 固定文案警示（非 LLM 生成）+ [确认][改一改][不要] 三键；update 候选注明"建议更新解释"。
- 设置页"个人词典"卡（全量管理入口）：待确认（badge）/已生效（"最后确认于x日 · 近30天相关记录n条"）/已忽略（折叠）三组；确认/编辑/删除/手动新增（"教镜子一个词"）/重新抽取；侧栏+移动端齿轮角标（有 pending 才显示）。
- `src/stores/glossary.js` + `src/api/glossary.js` + TermCard 组件；桌面 27/27 + 移动 18/18 走查 0 error。

---

## 七、AI 服务（Python gRPC）

### 7.1 服务与 RPC

**GenerateProfileRequest 递归镜子轮扩展（🔨 B 提案，AI 对账；rolling-mirror-design.md §4-B3）**：

> 字段号说明：设计稿提案 prev_mirror=10 / correction_index=11 / mirror_lookback=12，但 glossary 已占 10（词典轮登记），**实际字段号以 B/AI 在 shared-protocol.md 对账登记为准**（预计顺延为 11/12/13），本文按提案号记录。

```protobuf
// mirror_profile.proto —— GenerateProfileRequest 在既有字段（recent_chats=9 / glossary=10）之上追加：
message GenerateProfileRequest {
  // ... 既有字段 1-10 ...
  string prev_mirror = 11;        // ① 上一份 monthly 快照全文（overall_summary + 五维拼接），可空（genesis 月）
  string correction_index = 12;   // ③ 校正索引（回看深度=0 时由 B 填充，其余档位空串）
  int32 mirror_lookback = 13;     // 回看深度档位（0-3；②的内容筛选在 Java 端完成，Python 只透传渲染）
}
// ④ 待办/统计实况不走 proto：复用既有五维统计字段，B 端组装时以数据库实时值填充（真源直查，裁决 #33）
```

| Proto | 服务 | RPC | 状态 |
|-------|------|-----|------|
| common.proto | — | 枚举（MoodType 13 / ContentType 8 / TaskStatus 3 / AiProtocol 2）、LlmConfig、EmbeddingConfig；**GlossaryTerm{term=1,description=2,aliases=3,confirmed_at=4}、ChunkDTO（扁平 12 字段，含 task_status/user_edited）**（词典轮新增） | ✅ |
| record_processor.proto | RecordProcessor | `Classify(ClassifyRequest) → ClassifyResponse{skip, skip_reason, repeated ClassifyItem}`；ClassifyItem{title, summary, content, content_type, moods, status, keywords}；`single` 标志（4.4）；`glossary` 注入（=4）；**`ExtractTerms(ExtractTermsRequest) → ExtractTermsReply`（词典候选抽取，并入本服务）** | ✅ |
| embedding.proto | EmbeddingService | `Embed`（vector/dimension/model_name）、`GetModelInfo`（ModelInfoRequest 含 embedding_config=1，api 模式实测用户配置模型维度） | ✅ |
| mirror_chat.proto | MirrorChat | `ExtractIntent`（query_type + glossary=3）、`Chat`（服务端流式 + glossary=5）；🔨 规划追加 `PlanTools(PlanToolsRequest) returns (PlanToolsReply)`（并入本服务，toolcalling 轮） | ✅ / PlanTools 🔨 |
| mirror_profile.proto | MirrorProfile | `GenerateProfile`（recent_chats/ChatRecord + glossary=10）；🔨 递归镜子轮追加 `prev_mirror`/`correction_index`/`mirror_lookback`（提案号 11/12/13，对账后登记） | ✅ / 递归扩展 🔨 |

**EmbeddingConfig 携带 base_url**（API 模式自定义地址，Python 端 ApiEmbedder 已接线 ✅）；本地 BGE-m3 单例懒加载，仅 local 模式加载。GetModelInfo 双语义：无配置 → 健康检查（Docker healthcheck 用）；携带配置 → 按用户配置构造 embedder 实测维度（1024 硬约束在 Java 侧校验拒绝，裁决 #18）。

**ExtractTerms 契约（词典轮 ✅）**：

```protobuf
message ExtractTermsRequest {
  repeated ChunkDTO chunks = 1;             // 近 14 天 confirmed chunks（user_edited 优先；月度审计换 30 天窗口）
  repeated GlossaryTerm existing_terms = 2; // 现有词条（去重 + aliases 合并 + 漂移判断依据）
  LlmConfig llm_config = 3;
}
message ExtractTermsReply {
  message TermCandidate {
    string term = 1;
    repeated string aliases = 2;
    string description = 3;       // 含来源依据，用户 10 秒可判断
    string kind = 4;              // new / evidence / update
    string evidence = 5;          // 佐证摘要（"近14天出现3次"）
    int64 source_chunk_id = 6;
  }
  repeated TermCandidate candidates = 1;
}
```

**PlanTools 契约（🔨 规划，toolcalling-vault-design.md 第 6 节）**：

```protobuf
message ToolSpec { string name = 1; string description = 2; string args_schema = 3; }
message ToolResult { string tool = 1; string summary = 2; string payload_json = 3; bool success = 4; }
message PlanToolsRequest {
  string question = 1;
  repeated GlossaryTerm glossary = 2;
  repeated ToolSpec tools = 3;      // Java 侧注册表快照（Python 不硬编码工具清单）
  LlmConfig llm_config = 4;
}
message PlanToolsReply { repeated PlannedCall calls = 1; }
message PlannedCall { string tool = 1; string args_json = 2; }
// ChatRequest 追加：repeated ToolResult tool_results（Python 渲染成上下文块）
```

### 7.2 配置传递与无状态

配置唯一源 = Java `user_settings` 表。每次 gRPC 请求由 AiGrpcClient 读取、解密 API Key、组装 LlmConfig/EmbeddingConfig 随请求携带；Python 用完即弃。天然支持多用户各用各的模型。曾评估过的 ConfigService 推送方案已废弃。

### 7.3 Prompt 要点（Python prompts/，✅ 七套已实现，第 8 套规划）

| Prompt | 要点 | 状态 |
|--------|------|------|
| classify | 个人记录助手；判无意义（skip）；标题≤10字、摘要≤30字；8 类型 + 13 情绪多选 + taskStatus + 3-5 关键词；一次调用完成拆分+分类；{glossary} 占位符 | ✅ |
| classify（single 变体） | 禁止拆分；"这是用户确认过边界的完整片段"；返回恰好 1 条 | ✅ |
| profile | 自我认知助手；五维统计 + 最近对话；**只事实陈述不主观判断**（"标记了 3 次焦虑"而非"状态不好"）；{glossary} 占位符 | ✅ / 🔨 递归镜子轮重写为第 6 套改造版：累计画像语义声明 + {prev_mirror}/{records}/{correction_index}/{stats_facts} 四占位符（空块不留孤儿节头） + "待办状态以④为准，不继承①旧说法"指令 |
| intent | query_type 四选一 + 过滤条件 + 改写 query，JSON 英文小写；{glossary} 占位符 | ✅ |
| chat | 人生教练"镜子"；只陈述事实、**每个结论引用来源**、检索不足诚实说明、3-5 句；{glossary} 占位符 | ✅ |
| inspiration | 基于历史给 2-3 个写作方向，不直接复制 | ✅ |
| extract_terms | 个人词典抽取助手；个人指代/黑话算、通用词/明确专有名词不算；语料按时间排序、user_edited 前置标注"[用户手动修改过]"（权重更高）；description 要求"用户语境解释+括号内来源依据"；已有词条传入用于 new/evidence/update 分级与 aliases 合并；输出 JSON {candidates:[...]}，宁缺毋滥上限 10 条 | ✅ |
| glossary 渲染 | 模块级 glossary_render.py（非 LLM prompt）：固定软约束话术"以下用户个人词汇表**仅供参考，解释可能过时**；与近期记录矛盾时，以近期记录为准。" + 每词条"（N月确认）"标注；空词条不留孤儿节头；四 prompt 模板 {glossary} 占位符接入 | ✅ |
| plan_tools | 工具规划助手；工具注册表 + few-shot 2 例；JSON 约定 `{"tools":[{"tool":"search_records","args":{...}}]}` ≤2 步；verify_claim 约束"宁可少核查不可错核查"；coverage_check 声明知识边界 | 🔨 |

### 7.4 超时

| RPC | 超时 |
|-----|------|
| Classify | 180s（含拆分+分类） |
| Embed | 10s |
| Chat | 60s（流式） |
| ExtractIntent | 15s |
| GenerateProfile | 60s |
| ExtractTerms | 180s（14 天语料 + LLM 归纳，取 classify 同级） |
| PlanTools | 3s（规划超时即跳过工具走现有 RAG，零回归） |

**同步关系**：Java 改 proto 后必须在 Python 端重跑 `generate_proto.py`，否则字段错位（踩坑记录：api_key/base_url/model 曾串位）。词典轮由 AI 侧落地 proto（B 侧尚未对齐时），以 shared-protocol.md 登记为准，B 对账字段号（glossary 4/3/5/10、TermCandidate 六字段）。

---

## 八、前端协作契约

> 本章供前端团队直接引用：响应包装、状态语义、审核交互到端点的映射、标签显示对照。

### 8.1 基础约定

| 项 | 约定 |
|----|------|
| Base URL | `http://<host>:9050/api`（✅ 实际端口；v2.1 写 9005 有误） |
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

### 8.5 个人词典契约（✅ 已实现）

**API（GlossaryController，7 端点全上线）**：见 10 章词典行；分组响应 `GET /api/glossary` 返回三组分好（pending/confirmed/dismissed）。

**字段口径（snake_case，与拦截器输出一致）**：

```typescript
interface UserTermVO {
  id: number;
  term: string;
  aliases: string[];
  description: string;        // AI 理解（含来源依据）
  status: 'pending' | 'confirmed' | 'dismissed';
  kind?: 'new' | 'evidence' | 'update';   // 候选分区标记
  evidence?: string;          // "近14天出现3次"
  query_hit_count: number;
  content_hit_count: number;
  last_confirmed_at?: string; // 卡片"最后确认于x日"
  last_seen_at?: string;
  source_chunk_id?: number;   // 依据跳原文（B 侧同步给 source_record_id 跳记录详情）
  source_record_id?: number;  // 前端直接用 ui.selectedRecordId 打开记录
}
```

**交互约定**：
- 每日总结 sheet 候选分区：固定警示文案（非 LLM 生成）"⚠️ 确认后对话会按此理解检索你的记录；理解过时会导致偏差，可随时在设置页修改"；改一改 = 卡片原地展开编辑；evidence/update 同分区，update 注明"建议更新解释"。
- dismissed 组的"恢复"= 重新 confirm；"删除"= 真删走 DELETE（pending/confirmed 的忽略 = 沉底不删行，符合"dismissed 不删行"设计）。
- 侧栏/移动端齿轮角标：有 pending 才显示，>9 显 9+；MainLayout onMounted 全局拉一次（失败静默不打断导航）。
- `POST /api/glossary/extract` 响应按 `data.candidates[]` 解析（ExtractTermsReply 同构），空/异常回退全量重拉。

### 8.6 工具与 vault 前端契约（🔨 开发中，mock 先行）

**SSE 新事件（对话流）**：

| 事件 | payload | 说明 |
|------|---------|------|
| meta.tools_used | `["search_records:12条"]` | 工具轨迹芯片（气泡上方"查了 9 月记录 · 12 条"，风格同 sources）🔨 |
| vault_refs | `[{n, vault_item_id, display_name, file_type, size, digest_status, quote}]` | AI 输出 `[n]` 引用标记解析后的文件卡数据 🔨 |

**对话内文件卡三档**：强引用（回答基于文件内容）→ 完整卡（类型 SVG 图标+显示名+大小/日期/消化状态+AI 引用摘录+[预览][下载]）；弱引用 → 行内芯片 `[PDF] 开题报告` 可点开；模糊提及（"昨天传的图片"）→ 芯片展示，用户点开确认。边界：文件已删 → 芯片置灰"文件已删除"不可点；消化中 → "索引中…"可下载不可问答；同文件多引用 → 同气泡单卡。预览：图片/PDF 内嵌（/preview 流）、音视频播放器、docx 降级"下载查看"。

**vault 接口（🔨 规划，B Agent 任务 2）**：上传（POST multipart，硬删+确认交互）/ 列表（时间倒序，永不拉 blob）/ 详情 / 更新（改名/描述）/ 删除（级联清 chunks）/ 下载（attachment）/ 预览（内嵌流）/ 配额查询。配额可视化条（如 128MB/500MB）+ 低信息文件置顶区（"未能识别，请描述一下"——B2 认知边界透明在 vault 的应用）。

### 8.7 主要 VO 结构（TypeScript 参考）

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

- `server.py` gRPC 入口；`services/record_processor.py`（Classify）、`embedding_service.py`（Embed/GetModelInfo）、`chat_service.py`（ExtractIntent/Chat 流式）、`profile_service.py`（GenerateProfile）、`lexicon_service.py`（RecordProcessorServicer 继承版：Classify 复用 + ExtractTerms，并入同一服务复用 B 侧 channel）
- `llm/`：BaseLlm + openai_llm（兼容 qwen/zhipu）+ anthropic_llm + factory（按 protocol 路由）
- `embedding/`：local_embedder（BGE-m3，单例懒加载，仅 local 模式加载）+ api_embedder（已接 base_url）+ factory
- `glossary_render.py`：词表渲染（固定软约束话术 + "N月确认"标注，空列表不留孤儿节头）
- `errors.py`：异常 → gRPC 状态码统一映射（abort_with_mapped）；`llm_json.py`：LLM JSON 输出解析公共件
- `generate_proto.py`：proto 编译脚本；`tests/`：单测 + e2e_round9/10（桩 LLM 全链路）

### 9.2 待办清单（按路线图阶段）

| # | 事项 | 说明 | 状态 |
|---|------|------|------|
| 1 | proto 重新编译 | Java 端 proto 变更后在 Python 端跑 `generate_proto.py`，否则字段错位（历史踩坑） | ✅ 持续执行 |
| 2 | `ClassifyRequest.single` 单段模式 | single=true 时禁止拆分、恰好返回 1 条 ClassifyItem；prompt 加 single 变体 | ✅ 完成 |
| 3 | taskStatus 确保填充 | todo/plan 类必须返回 not_started/in_progress/completed | ✅ 完成（B 端已落 metadata） |
| 4 | ApiEmbedder 接 `base_url` | `EmbeddingConfig.base_url` 已接线 | ✅ 完成 |
| 5 | mirror_chat.proto 实现 | ExtractIntent（query_type）+ Chat（流式） | ✅ 完成 |
| 6 | mirror_profile.proto 实现 | GenerateProfile：五维统计 + recent_chats + 输出六维 + user_tags + overall_summary | ✅ 完成（🔨 递归镜子轮 prompt 重写 + 三新字段见 7.1） |
| 7 | prompts 模板 | classify / classify-single / intent / profile / chat / inspiration / extract_terms 七套 | ✅ 完成（plan_tools 为第 8 套） |
| 8 | 异常 → gRPC 状态码映射 | errors.py 统一 abort_with_mapped | ✅ 完成（第九轮） |
| 9 | 健康检查 | GetModelInfo 双语义（无配置=健康检查；携带 embedding_config=按用户配置实测维度） | ✅ 完成 |
| 10 | ExtractTerms RPC + glossary 渲染 | 第 7 套 prompt + 四注入点渲染 + 超量截断防护（max_chunks=200/max_chunk_chars=200/max_candidates=10） | ✅ 完成（第十轮） |
| 11 | PlanTools RPC + 第 8 套 prompt | plan_tools.txt + tool_results 渲染 + verify_claim/coverage prompt 约束；proto 契约见 7.1 | 🔨 下一轮（toolcalling sprint 任务 1） |

### 9.3 契约要点（不可破坏）

- 不访问数据库、不读文件系统用户数据——所有输入来自请求参数。
- `config.yml` 仅服务级配置（端口 50051、workers、prompts 路径、extract_terms 段），禁止存放用户模型配置。
- 枚举值一律英文小写（除 proto 枚举大写名），与 Java 端 `ContentType`/`MoodType` 枚举一一对应。
- 每日总结复用 `Chat` RPC（不新增服务）；写作灵感复用 `Embed` + `Chat`（不新增 RPC）；ExtractTerms 并入 RecordProcessor 服务（不建独立 gRPC service，B 复用同一 channel）。
- LLM 失败绝不影响其他服务：单次调用失败走状态码映射返回，同 servicer 后续调用正常（失败隔离已 E2E 实测）。

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
| GET | `/api/records/calendar` | ✅ | 日历标记（source='user' 过滤 + Asia/Shanghai 统一口径已上线） |
| POST | `/api/records/{id}/retry` | ✅ | FAILED 重试（重跑管道） |
| PUT | `/api/chunks/{id}` | ✅ | 编辑片段（文本/元数据） |
| DELETE | `/api/chunks/{id}` | ✅ | 删除片段 |
| POST | `/api/records/{id}/chunks` | ✅ | 新增片段（自动单段分类） |
| GET/PUT | `/api/settings` | ✅ | 配置读取/更新（Key 脱敏；rag_half_life、1024 维校验已上线；🔨 mirror_lookback 递归镜子轮透传） |
| POST | `/api/settings/test-ai` / `test-embedding` | ✅ | 连接真探测（test-embedding 携带 EmbeddingConfig 实测维度） |
| GET | `/api/mirror` | ✅ | 最新画像快照（manual 优先，无则最新 monthly；🔨 节点语义改为"截至 N 月累计"） |
| POST | `/api/mirror/generate` | ✅ | 生成 manual 快照（🔨 组装四块输入 + 四道防洪闸限流，见 6.5） |
| GET | `/api/mirror/snapshots` | ✅ | 快照历史列表（manual+monthly 合并倒序，LIMIT 14） |
| GET | `/api/mirror/snapshots/{id}` | ✅ | 单份完整快照（他人/不存在一律 4041） |
| GET | `/api/mirror/stats?days=30` | ✅ | 聚合统计（moodDaily/hourDist/weekdayDist/keywordTop/todo/recordDaily，窗口补零） |
| POST | `/api/mirror/chat` | ✅ | 对话（SSE：meta/delta/sources/done/error） |
| GET/DELETE | `/api/mirror/sessions[/{id}]` | ✅ | 会话列表 / 历史 / 删除 |
| GET | `/api/summaries?date=` | ✅ | 每日总结查询（查 source='system' 记录） |
| POST | `/api/inspiration` | ✅ | 写作灵感 |
| GET | `/api/export/json` · `/api/export/markdown` | ✅ | 数据导出（无导入，排除向量） |
| GET | `/api/glossary` | ✅ | 词典三分组（pending/confirmed/dismissed） |
| POST | `/api/glossary` | ✅ | 手动新增词条（"教镜子一个词"，直接 confirmed） |
| PUT | `/api/glossary/{id}` | ✅ | 编辑词条/更新解释 |
| DELETE | `/api/glossary/{id}` | ✅ | 真删除（dismissed 沉底不删行，此端点删行） |
| POST | `/api/glossary/{id}/confirm` | ✅ | 确认词条（pending → confirmed） |
| POST | `/api/glossary/{id}/dismiss` | ✅ | 忽略词条（沉底，30 天复活窗） |
| POST | `/api/glossary/extract` | ✅ | 手动触发抽取（懒人立即出候选） |
| POST | `/api/vault` 等 vault 七端点 | 🔞 → 🔜 | 上传/列表/详情/更新/删除/下载/预览/配额（🔨 开发中，toolcalling-vault-design.md 4.x；PG BYTEA 直存 + SHA-256 去重 + magic bytes 校验 + 404 不暴露存在性） |

---

## 十一、安全

| 项 | 现状 | 待办 |
|----|------|------|
| API Key 存储 | AES-256-GCM（随机 IV），读取脱敏；**密钥已外置**（CryptoUtils 环境变量注入） | — |
| 认证 | JWT 24h + BCrypt；SSE 通道 ERROR/FORWARD dispatch permitAll 已修安全噪音 | 生产密钥配置化收尾；刷新策略可选 |
| SQL 注入 | MyBatis-Plus 参数化；手写 SQL 一律占位符 + `<script>` 包裹（`<if>` 不包 script 会把标签当 SQL 发出的踩坑已两次复现） | 保持约定 |
| 传输 | gRPC plaintext（本机/内网部署） | 可选 TLS，非毕设必做项 |
| 数据隔离 | 所有查询强制 user_id 过滤；检索排除 `embedding IS NULL` 与软删除记录的 chunks；快照详情他人/不存在一律 4041 | 保持约定 |
| 时区一致性 | 日历 SQL AT TIME ZONE + 列表 ZoneId + VO JsonFormat 全统一 Asia/Shanghai（UTC/北京双口径 bug 已修） | 保持约定 |
| vault（🔨） | magic bytes 校验、original_name 清洗防路径穿越、非图片下载强制 attachment、跨用户 404 | 随 sprint 实现落地安全用例 |

---

## 十二、部署

Docker Compose 三服务：`backend`（9050）+ `ai`（50051）+ `postgres`（pgvector 镜像）。环境变量注入 JWT 密钥与 AES 密钥；数据库初始化执行 `db/schema.sql` + `db/migration-v2.sql`（v2.1 全量基准 + 幂等迁移，空库从零建库已实测）。健康检查：`/api/auth/status`、gRPC `GetModelInfo`（无配置双语义）。时区统一 Asia/Shanghai。

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
- **词典开关消融（🔨 阶段 6）**：关词表 vs 开词表 Hit@5/MRR 对比（lexicon-design.md 第 7 节验收口径；otaku_it 语料验证链路：抽取 → pending → 确认 → 对话 rewritten_query 含毕设/RAG → 命中相关 chunk）。
- **工具三档消融（🔨 阶段 6）**：无工具 vs Planner-Executor vs 原生循环（E3 架构预留）三档，对比检索命中率/回答质量/延迟（dialogue-enhancement-ideas.md E4）。

### 13.5 数据点统计（论文素材）

```sql
-- AI 拆分被人工修正率
SELECT COUNT(*) FILTER (WHERE user_edited) :: float / COUNT(*) FROM chunks WHERE user_id = ?;
```

- **tool_calls 审计表（🔨 建表 ✅）**：每次工具执行落 tool/args/result_summary/success/latency_ms——论文图表素材：工具使用频率/成功率/延迟分布；三档消融的量化数据源。
- **user_terms 双计数**：query_hit_count（注入优先级）/ content_hit_count（存活度）支撑"词表加权是渐进增强"论证；月度漂移审计打回 pending 的记录是"in-situ model editing / 用户模型随时间演化"的实证素材。
- **E2E 联调数据（2026-09-04）**：10/10 验收链路全过、8 集成 bug 修复记录（MyBatis typeHandler 全局污染 / druid wall 不识别 LATERAL / jsonb ? 占位符冲突 / PGobject 向量解析等）可作工程实践章节素材。

---

## 十四、开发路线图

| 阶段 | 时间 | 内容 | 涉及端 | 状态 |
|------|------|------|--------|------|
| 0. 技术债清理 | 9 月上旬（~1 天） | 第十六章清单：删死代码、密钥外置、schema 重写为基准 DDL、检索排除软删除、ApiEmbedder 接 base_url | Java + Python | ✅ 完成（2026-09-03） |
| 1. segment 手动调整全链路 | 9 月上中旬（~3 天） | 新端点 + classified_segment/user_edited/source 列 + confirm 补分类 + proto single 模式 + Python classify 变体 + taskStatus 落 metadata | Java + Python | ✅ 完成（2026-09-04） |
| 2. 前端骨架 | 9 月中旬起（与后端并行） | 记录/审核页先行（消化片段卡片交互）、登录、设置页；对话/镜子页后置 | 前端 | ✅ 完成（2026-09-04，v2 原型重做 + 9-05"晨纸"亮色主题） |
| 3. 镜子画像 | 9 月底~10 月 | profile_snapshots + GenerateProfile + monthly 定时任务 + 漂移检测 | Java + Python | ✅ 完成（2026-09-04，含快照历史/对比/stats 扩展） |
| 4. 对话 | 10 月~11 月 | ExtractIntent(query_type) + 四路检索 + 流式 Chat + sources 落库 | Java + Python + 前端 | ✅ 完成（2026-09-04） |
| 5. 总结/灵感/导出/auto 审核 | 11 月 | 每日总结（系统 Record 方案）、写作灵感、数据导出、review_mode 接线 | Java + Python | ✅ 完成（2026-09-04） |
| **端到端联调** | 2026-09-04 | 三服务串全链路，10/10 验收清单通过，8 集成 bug 修复 | 全部 | ✅ 完成 |
| **词典 sprint** | 2026-09-05 | user_terms + ExtractTerms + 四注入点 + 双调度器 + 前端词典卡/候选分区（lexicon-design.md） | DB/AI/B/F | ✅ 完成（前端 mock 态，B 接口 ✅ 待切 USE_MOCK） |
| **toolcalling + vault sprint** | 2026-09-06 起 | Planner-Executor + 8 工具 + tool_calls 审计 + vault 全套 + 对话文件卡三档（toolcalling-vault-design.md） | AI/B/F | 🔨 B vault 全链路+确认门禁+修复批 ✅（31600cb，132/132）；F 确认门禁前端+防误删 ✅（ae2bc0c，mock 切真）；AI PlanTools RPC ✅（dac15e8）。待端到端复验"问论文→key chunk→文件卡" |
| **递归累计镜子 sprint** | 2026-09-06 起 | 镜子语义纠正为"截至 N 月的累计画像"：prev_mirror 递归承续 + 回看深度 0-3 + 校正索引 + 四道防洪闸 + period_month 列（rolling-mirror-design.md v1.0） | B/AI/F | ✅ 三线完成（B 1065c25 / AI a3396f6 / F c31016b），真库 8 月累计镜子已生成 |
| 6. 测试 + 论文素材 | 12 月~1 月 | 第十三章测试设计与检索对比实验、userEdited 修正率统计、词典开关消融、工具三档消融 | 全部 | 🔜 |

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
| 16 | ClassifyItem.taskStatus 无落库 | 写入 chunks.metadata.taskStatus | 待办聚合与镜子"未完成的事"依赖它（✅ 已补齐，ClassifyItemConverter 落库） |
| 17 | daily_summaries 独立表 | **废除**；总结=系统 Record（source='system'）+ Chunk，复用全链路 | 与"Chunk 唯一业务单元"一致；对话可引用总结；代价仅统计口径一个 WHERE |
| 18 | Embedding 维度按模型可变（1024/1536） | **硬约束 1024 维**，设置页校验拒绝其他维度 | 避免 HNSW 迁移与向量重建机制；动态维度写入论文 future work |
| 19 | 导出+导入（JSON 可回导） | 只导出不导入 | "数据可携带"论文表述即可，导入砍掉省工 |
| 20 | （无）records 来源概念 | 新增 `records.source`（user/system） | 支撑每日总结系统 Record 方案的统计口径分离 |
| 21 | FAILED 只能删除 | 实现 `POST /records/{id}/retry` 重跑管道 | 兑现旧文档"重新尝试"承诺 |
| 22 | 词典候选直接注入生效 | **pending 只展示不注入，confirmed 才生效**（lexicon-design.md #0.1） | 机器猜的不直接用——与"审核过的才进记忆"同构（human-in-the-loop）；LLM 抽取有噪音，靠 pending 门禁 + 月度审计兜底 |
| 23 | vault 文件落本地磁盘 + 元数据入库 / 或 MongoDB | **Postgres BYTEA 直存**，vault_items/vault_blobs 分表（列表永不拉 blob） | 数据主权 100% 收敛，无第二存储依赖；单实例小规模下 20MB 文件 × 500MB 配额完全可承受 |
| 24 | Python 端实现工具调用循环 | **工具执行权在 Java**（查库），LLM 决策在 Python（PlanTools 输出计划） | Python 无状态铁律不连库；任意 OpenAI 兼容 provider 都能吃（不依赖 tools API）；桩 LLM 可测全链路 |
| 25 | 工具调用失败阻断对话 | **失败/超时(3s)/解析不出/空计划 → 跳过工具走现有 RAG，零回归** | 词表/工具是加权不是替换，错了退化不是灾难 |
| 26 | toolcalling 用原生 function calling 循环（方案 B）先行 | **方案 A Planner-Executor 先行，方案 B 预留**（执行层复用 A 的注册表，仅换决策来源） | 一次额外 LLM 往返几十 token；流式不受影响；桩 LLM 测不了 B；三档消融需要 B 但可后置 |
| 27 | 词典抽取实时触发（提交记录即抽） | **每日 01:00 定时顺路抽取 + 每月 1 号漂移审计**，不新增用户等待路径 | confirmReview 不碰；LLM 抽取有噪音是已知风险，定时批量 + pending 门禁足够 |
| 28 | 词表全量注入每次请求 | **query 侧匹配命中才传（省 prompt），未命中传 top 高频词 grounding；注入上限 30 条 + 60s 进程内缓存** | prompt 膨胀防线；Classify/Profile/Chat 传 top 30 全量，ExtractIntent 只传命中 |
| 29 | 词典漂移靠人工发现 | **月度审计主动打回 pending**（confirmed 解释 vs 近 30 天语料 LLM 判一致性） | 漂移最多活一个月且被系统主动递到用户面前（"游戏：8月起多指FGO，3-7月指明日方舟"） |
| 30 | 对话写操作直接执行 | **写操作红线：显式动作（上传）免确认；对话内删除/覆盖必须确认卡** | 有存有取才像"有记忆的实体"；确认交互与词典 confirm/消化回执三键同心智 |
| 31 | 对话记忆窗口 3 轮 | 实现调至 **20 轮**（2026-09-05 会话链路修复轮） | 小规模上下文成本可忽略；多轮追问画像/记录需要更长记忆 |
| 32 | 镜子 = 当月切片（monthly 快照只统计当月窗口） | **镜子 = 截至 N 月的累计画像**（递归承续：上月镜子全文 + 本月增量，genesis 首月全量；rolling-mirror-design.md §0） | 2026-09-06 用户发现 8 月快照应为"截至 8 月的累计"而非"8 月切片"——设计意图是"迄今全部的你"，切片实现为语义偏差已纠正；累计语义下漂移对比逻辑不变（相邻月相减=这个月变了什么） |
| 33 | 待办/统计状态靠 LLM 转述传承（上月镜子说什么本月就继承什么） | **真源直查：生成时库内实值注入**（stats_facts 块与 get_stats 同源 SQL）；明确指令"待办状态以实况为准，不继承上月镜子旧说法" | 叙事让 LLM 传承，账本让数据库记；LLM 转述会累积误差且过期（"没做"→"做完了"），账本永远实时 |
| 34 | 回看深度用抽象档位/固定窗口 | **0-3 档语义滑块**（0=纯继承+校正索引 / 1=上月原文 / 2=近三月原文 / 3=全部原文）+ **四道防洪闸全配置化**（600 chunks / 15 万字符 / 单条 2000 / 12 月摘要化） | 用户可理解语义优先于抽象档位；3 档"慢，消耗大"明示代价；闸门触发日志+toast 透明告知"已截取最近部分" |
| 35 | 文件消化完自动 embed（done 态直接可检索） | **确认门禁：消化停在 extracted 态，用户确认后才 embed**（toolcalling-vault-design.md §3.3b） | 用户背书过的才进记忆——机器猜的 key/description 不直接用，与记录审核/词典确认同构（#22 一脉）；未确认可保管可下载，检索不到 |
| 36 | 文件全文 embed 后靠全文命中 | **key-embed 进通用检索**：embed 文本=key+description+类型拼合（contentType='note' 挂 vault_item_id），命中出文件卡不喂全文（§3.3c） | 用户原设想：单 key 可被"论文咋样了"这类对话命中；不喂全文防上下文爆炸，内容问答走 recall_item 显式拉 |
| 37 | 待办状态确认入库后永久锁定（taskStatus 停在日记写下那一刻，侧栏未完成项无完成路径） | **待办登记表 todo_registry**（todo-registry-design.md）：独立字典不混 user_terms；confirmReview 自动登记；新日记 Classify 注入未完成清单（≤20 条带原文）→ LLM 判 refers_to_todo → pending 建议 → 用户裁决（确认可改状态/忽略静默，新证据可再提示）；侧栏直调三态（auto/manual 通用）；chunk.metadata.taskStatus 保持唯一真源（registry 是物化索引，事务内双写） | 跨日记状态跟踪是"日记系统的自我记忆闭环"（论文素材：跨时间实体状态传播 + human-in-the-loop 门禁）；机器猜的不直接用（#22 一脉）；关联是用户背书的产物 |

> 词典轮裁决 #22/#27/#28/#29 源自 `coordination/lexicon-design.md`；工具/vault 轮裁决 #23/#24/#25/#26/#30/#35/#36 源自 `coordination/toolcalling-vault-design.md` 与 `dialogue-enhancement-ideas.md`；递归镜子轮裁决 #32/#33/#34 源自 `coordination/rolling-mirror-design.md`。已砍场景留档：D2 提醒/定时任务（常驻进程+通知系统架构外）、D3 联网查询（"只照你自己"定位）、D4 多人共享问答（单人系统）。

---

## 十六、技术债与清理项

> 2026-09-03 清单中的大部分项已在阶段 0-5 落地（Tag 删除/AES 密钥外置/@EnableScheduling/schema 重写/migration-v2/软删除过滤/统计口径/rag_half_life 接线/taskStatus 落库/连接真探测/confirm 补分类/retry/auto 审核）。剩余与新产生：

| 项 | 说明 | 状态 |
|----|------|------|
| F 词典 mock 切换 | `src/stores/glossary.js` 的 `USE_MOCK = true` → false（B 七端点已上线 9050） | 🔜 联调时一个开关 |
| B proto 对账收尾 | AI 侧已落地 GlossaryTerm/ChunkDTO/ExtractTerms（shared-protocol.md 2026-09-05 行），B 侧 src/main/proto 已对齐字段号（glossary 4/3/5/10） | ✅ 基本完成，联调复核 |
| 枚举名统一 | AI 侧 `AI_PROTOCOL_UNKNOWN=0` vs Java 侧 `PROTOCOL_UNKNOWN=0`（数值相同 wire 兼容，仅 reflect/名字引用差异） | 🔜 低优先 |
| BGE-m3 本地 embedding 实测 | 本机无 torch，链路等价已验（api 模式 1024 校验通过）；Docker 部署后实测 | 🔜 部署时 |
| 每日总结手动触发端点 | 定时任务无法即时验证，真实生成链路未实测 | 🔜 可加调试端点 |
| 列表 N+1 | RecordVO.toVO 逐条查 chunks；小规模可接受，后续可批量 | 🔜 |
| 前端 check-imports.js | 新增的 import 缺失扫描脚本（白屏+页面锁死连环修复后防复发），需保持在新组件接入时运行 | ✅ 已建立 |
| LATERAL / jsonb 占位符坑 | druid wall 不识别 LATERAL、jsonb `?` 占位符冲突改函数形式、MyBatis `<if>` 必须包 `<script>`——手写 SQL 时持续注意 | 保持约定 |
| 8 工具注册表实现 | ToolRegistry/Executor/AuditService + 8 工具 + vault REST + SSE vault_refs（toolcalling-vault-design.md 分工表任务 2） | 🔨 开发中 |
| PlanTools Python 实现 | RPC + 第 8 套 prompt + tool_results 渲染 + verify_claim/coverage 约束（分工表任务 1） | 🔨 排产 |
| F vault 前端 | 对话文件卡三档 + 上传卡 + 消化回执 + 资产页 + 工具轨迹芯片 + 附件上传入口（分工表任务 3，mock 先行可并行） | 🔨 排产 |
| B 递归镜子轮 | mirror_lookback 列 + GenerateProfileRequest 四块组装（四闸限流）+ proto prev_mirror/correction_index/mirror_lookback（字段号 shared-protocol 对账）+ period_month 补列（历史 3 行 UPDATE） | 🔨 开发中 |
| AI 递归镜子轮 | GenerateProfile prompt 第 6 套改造（累计画像语义 + 四占位符 + 待办以实况为准指令）+ E2E（累计断言/genesis/闸门截断/待办矛盾/空块） | 🔨 排产 |
| F 递归镜子轮 | 设置页"镜子引擎"组回看深度下拉（0-3 + 3 档慢提示）+ USE_MOCK_MONTHLY 切 false 联调 | 🔨 排产 |

---

## 十七、论文可写点

1. **审核后入库的质量保障机制**：RAG 数据始终是用户确认版本；userEdited 统计"AI 拆分被人工修正率"。
2. **按需补分类**：用户修正与 AI 分析的协作模式（变化片段才补，单段禁拆分）。
3. **画像变化轨迹与漂移检测**：快照分层保留 + embedding 余弦距离量化"自我变化"。
4. **意图路由 + 时间衰减 RAG**：query_type 四路分流省去无效向量检索；半衰期加权解决久远记录噪音；13.4 的对比实验提供量化证据。
5. **memory-grounded self-verification（记忆接地的自我核查对话）**：B1 事实核查镜子——用户"我这周啥也没干摆烂了"→ 镜子检索本周记录拿证据温和纠正；学术锚点：行为激活疗法的事实核查，焦虑/抑郁用户的系统性负性偏差用本人记录纠正（dialogue-enhancement-ideas.md B1，🔨）。
6. **认知边界透明（epistemic humility）**：B2 coverage_check——回答前先查"我知道多少、从哪天开始知道"，声明知识边界；通用 LLM 假装全知，**"洞"（没写的日子=不存在）只有记忆库架构才有**（🔨）。
7. **in-situ model editing（用户模型在对话/审核中被即时编辑）**：个人词典 pending 门禁 + 月度漂移审计 + 消化回执三键，构成"机器猜 → 用户拍板 → 系统自我修正"的用户模型在线更新闭环（词典 ✅ / B3 对话教词 🔜）。
8. **异构资产分级记忆**：日记是快记忆，文件是重记忆——vault 三档消化（全消化/半消化/零消化）+ 三层检索漏斗 + 多模态记忆载体统一进检索与词典体系（🔨）。
9. **词典加权检索的消融证据**：query 侧改写 + prompt grounding 的渐进增强设计（不动存量向量），关/开词表 Hit@5/MRR 对比（🔨 阶段 6）。
10. **工具增强对话的架构权衡**：Planner-Executor vs 原生循环三档消融（命中率/质量/延迟），工具执行权与 LLM 决策权分离（Java 查库 / Python 决策）的工程论证（🔨）。
11. **retrospective rolling self-modeling（递归自画像）**：画像不做独立月度切片，而是"上月画像全文 + 本月增量"的递归承续——自我模型是一条被不断重写的链而非离散快照序列。可写三点：① **误差累积与抑制**——递归链中早期偏差会向下游传播，系统用"低档位（0 档）注入校正索引（上期镜子所引记录清单）"这一廉价补偿对抗漂移，可对比 0/1/3 档生成结果的偏差率；② **叙事与账本的双真源分离**——画像叙事由 LLM 递归传承，待办/统计等可验证事实每次从数据库直查覆盖（裁决 #33），"模型说的"与"库里的"分层定真源；③ **上下文预算工程**——12 月摘要化 + 四道防洪闸（条数/字符/单条/月数）全配置化，是"无限期递归链塞进有限窗口"的工程答案（🔨，rolling-mirror-design.md）。

---

## 十八、变更记录

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-09-03 | v2.0 | 初版：以代码基线 08854ba 对账重写；确立 Chunk 唯一业务单元模型；新增 segment 手动调整与按需补分类设计；并入优化设计（画像快照/对话/检索）；统一 16 项旧文档冲突裁决 |
| 2026-09-03 | v2.1 | 补全协作与落地章节：① 每日总结改为系统 Record 方案（废除 daily_summaries，新增 records.source 列，裁决 #17/#20）；② Embedding 维度硬约束 1024（裁决 #18）；③ 确认 auto 审核接线、FAILED retry 端点、砍掉 JSON 导入（裁决 #19/#21）；④ 新增第八章前端协作契约（轮询/交互映射/标签对照/VO 结构）、第九章 Python 协作清单、第十三章测试设计、第十四章开发路线图；⑤ 旧设计文档移至 docs/archive/，PROGRESS.md 索引更新 |
| 2026-09-04~05 | v2.2（现状对账） | **路线图阶段 0-5 主体全部完成 + 端到端联调**：① 记录审核全链路（增删改片段/confirm 补分类/retry/auto 审核/taskStatus 落 metadata）；② 镜子画像（快照+漂移+历史对比接口+GET /mirror/stats 聚合统计）；③ 对话（SSE 四路检索+sources 落库+会话管理，记忆窗口 3→20 轮）；④ 每日总结/灵感/导出/日历时区统一；⑤ 8 集成 bug 修复（typeHandler/LATERAL/jsonb 占位符/PGobject 向量等）；⑥ 前端 v2 原型 UI 重做 + "晨纸"亮色主题（28577d9）+ 六图表系统 + 快照对比；⑦ B 端口实际 9050（文档勘误） |
| 2026-09-05 | v2.2 | **词典 sprint 全链路完成**（lexicon-design.md v1.0）：① user_terms 表 + source_record_id 补列；② ExtractTerms RPC 并入 RecordProcessor 服务（偏差：不建独立 gRPC service）+ 第 7 套 prompt + glossary_render 固定软约束话术；③ glossary 四注入点（Classify=4/ExtractIntent=3/Chat=5/GenerateProfile=10）；④ B 端 GlossaryService（top30/60s 缓存/query 匹配命中计数）+ 七端点 + 双调度器接线（每日 01:00 抽取、每月 1 号漂移审计）；⑤ F 词典卡三分组 + 总结 sheet 候选分区 + 角标（mock 态）；⑥ 新增裁决 #22/#27/#28/#29 |
| 2026-09-05 | v2.2 | **vault/toolcalling sprint 设计定稿**（toolcalling-vault-design.md v1.0）：① Planner-Executor 框架 + 8 工具注册表 + tool_calls 审计表；② vault 三表（vault_items/vault_blobs/tool_calls）建表完成（af60c45），PG BYTEA 直存（裁决 #23）；③ 三层检索/三档消化/Key 三层渐进/对话文件卡三档/上传卡/消化回执/资产页设计；④ 创新场景 B1 事实核查/B2 认知边界/B4 快照对谈进首批，B3/B5 顺延，E3 原生循环预留（裁决 #24/#25/#26/#30）；⑤ ChatView/AI-B 协作排产见第十四章 |
| 2026-09-05 | v2.2 | **Py 错误处理统一轮**（第九轮）：errors.py 异常 → gRPC 状态码统一映射（abort_with_mapped），失败隔离全链路 E2E 实测；GetModelInfo 双语义（健康检查/用户配置实测维度），ModelInfoRequest 加 embedding_config=1（wire 兼容实测）；回归 37 单测 + 11 E2E + 18 冒烟 |
| 2026-09-05~06 | v2.2 | **前端持续轮**："晨纸"主题（28577d9）→ 对话链路修复（ca2cad7 ExtractIntent 超时回退 + SSE 安全噪音根治 19f01ef）→ 第五轮布局+会话体验（22dae80）→ 桌面 fluid 化（1789856）→ 三页信息密度升级（2188a21 stats 端点 + e78e44f 六图表）→ 白屏连环修复（76518a3/798f67b，新增 check-imports.js 防复发）→ 快照历史与对比上线（482d5c2/30c55be）→ 词典前端第十轮完成（F 27/27+18/18 走查） |
| 2026-09-06 | v2.2 | 本文档 v2.2 对账：模块清单/数据模型/六~七章/八章契约/十章 API/十三~十七章全面同步实现现状与 sprint 设计；裁决 #22-#31 续编；技术债清单刷新 |
| 2026-09-06 | v2.3 | **递归累计镜子 sprint 设计定稿 + 镜子语义偏差纠正**（rolling-mirror-design.md v1.0）：① **语义纠正**——用户发现 8 月快照应为"截至 8 月的累计"而非"8 月切片"（原实现 30 天/当月窗口与设计"迄今全部"不符，教训登记），镜子重定义为"上月镜子全文 + 本月增量"的递归承续，genesis 首月全量，漂移对比逻辑不变；② 生成时 LLM 输入四块（prev_mirror / records / correction_index / stats_facts），真源原则"叙事让 LLM 传承，账本让数据库记"（裁决 #33）；③ 回看深度滑块 mirror_lookback（0-3，默认 1，设置页"镜子引擎"组）+ 四道防洪闸全配置化（600 chunks / 15 万字符 / 单条 2000 / 12 月摘要化，触发日志+toast）（裁决 #34）；④ proto GenerateProfileRequest 加 prev_mirror/correction_index/mirror_lookback（B 提案，AI 对账，shared-protocol 登记）；⑤ profile_snapshots 加 period_month CHAR(7)（幂等精确化，历史 3 行 UPDATE 补值）；⑥ 4.2 补 created_at 语义声明（数据专项核查 0 不一致）；⑦ B/AI/F 三线开工（🔨），F 待 B 部署后切 USE_MOCK_MONTHLY 联调；⑧ 论文可写点新增 #11 retrospective rolling self-modeling。裁决 #32/#33/#34 续编 |
| 2026-09-06 | v2.3 | **审查修复批 + 两高危修复完成**（fix-batch-design.md，B 1065c25→31600cb 132/132 测试，F ae2bc0c）：① Y1-Y6 六处设计相反实现修复（系统记录放行检索/词典语料收口/[F编号] 引用/get_profile 加月份/DigestService 拆分/五态迁移）；② 用户裁决 Q1=机械漂移规则+日常补丁、Q2=硬删+三层防误删（后四位校验+5 秒撤销窗）、Q3=key-embed 进通用检索（裁决 #36）+ **确认门禁**（裁决 #35：digest 停 extracted 待确认才 embed，POST /vault/{id}/confirm）；③ F 全套配套（回执卡确认交互/五态徽标/图片必填/三 mock 切真）；④ **两高危修复**（F 走查发现）：update() 全列覆盖竞态改定向 SET、@Async 消化静默丢失（AsyncConfig vaultDigestExecutor 专用线程池）；⑤ 数据重建：清污染数据保留真实 user_settings，重造近两月口语化二次元大学生日记 + 向量补录（召回 Hit@5=5/5）；⑥ 递归累计镜子三线完成（B 1065c25/AI a3396f6/F c31016b），真库 8 月累计镜子验证含 7 月元素。已立案未修：上传事务未提交可见性竞态（afterCommit 方案）。裁决 #35/#36 续编 |
