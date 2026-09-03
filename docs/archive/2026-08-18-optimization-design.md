# AI 日记"镜子"系统 — 优化设计文档

> 版本：v0.3
> 日期：2026-08-18
> 基于：系统设计文档 v1.3 + AI 服务层设计文档 v0.4 + 落地实现文档 v0.5
> 状态：设计中

**本文档定位：** 在不改动原有设计的基础上，针对画像系统、对话系统、检索系统三个方向的优化设计。原文档中未提及的部分（认证、异常机制、前端流程等）保持不变。

**关联文档：**
- [系统设计文档](2026-07-23-ai-diary-mirror-design.md) — 整体功能设计
- [AI 服务层设计文档](2026-08-04-ai-service-design.md) — Python gRPC 服务详细设计
- [落地实现文档](2026-07-23-mirror-implementation.md) — 技术栈、项目结构

---

## 一、优化总览

### 1.1 问题分析

| 问题 | 现状 | 影响 |
|------|------|------|
| 画像不持久化 | 每次"查看镜子"重新生成，用完就扔 | 用户看不到自己的变化轨迹，镜子没有"记忆" |
| 对话与画像割裂 | 对话历史不参与画像生成 | 画像只看日记数据，不了解用户当前关注什么 |
| 意图过滤粗粒度 | ExtractIntent 只返回过滤条件 | 所有问题走同一条检索路径，部分问题不需要向量检索 |
| RAG 无衰减 | 所有日记同等权重 | 久远日记噪音大，影响检索准确率 |

### 1.2 优化方案总览

| 优化项 | 方案 | 新增复杂度 |
|--------|------|-----------|
| 画像持久化 | 新增 `profile_snapshots` 表，画像生成后存储 | 低：1 张表 + 10 行存储逻辑 |
| 画像向量化 | 画像快照生成时 embedding，存入快照表 | 低：复用现有 Embedding 服务 |
| 画像变化轨迹 | 分层保留策略（近期 + 月度） | 低：定时清理逻辑 |
| 对话纳入画像 | 查看镜子时查最近对话，一起传给 Python | 低：加一段查询 |
| 意图路由 | ExtractIntent 增加 `query_type` 字段 | 中：Proto 改 1 字段 + Java 加路由 |
| RAG 时间衰减 | 向量检索时加时间权重（用户可调） | 中：SQL 加权计算 |

### 1.3 不新增的内容

| 排除项 | 原因 |
|--------|------|
| Function Calling / Tool Use | 已有 ExtractIntent + SQL 查询，不需要 LLM 自主调工具 |
| 实时对话洞察提取 | 对话频率低（脉冲式使用），大部分时候空跑浪费 |
| 新增 RPC 服务 | 只改现有 proto message，不加新服务 |
| 新增定时任务 | 对话洞察合并到画像生成流程，零新任务 |

---

## 二、数据库变更

### 2.1 新增表：画像快照

```sql
-- 画像快照表（唯一新增的表）
CREATE TABLE profile_snapshots (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),

    -- 各维度分析文本（与 GenerateProfileResponse 字段对应）
    mood_analysis TEXT,              -- 情绪分析
    learning_analysis TEXT,          -- 学习分析
    todo_analysis TEXT,              -- 待办分析
    rhythm_analysis TEXT,            -- 生活节奏分析
    user_tags JSONB DEFAULT '[]',    -- 用户特征标签（字符串数组，如 ["技术学习", "夜猫子"]）
    overall_summary TEXT,            -- 整体总结

    -- 向量（用于画像漂移检测）
    embedding vector(1024),

    -- 快照类型
    snapshot_type VARCHAR(20) NOT NULL DEFAULT 'manual',
    -- manual: 用户点"查看镜子"生成
    -- monthly: 每月1号定时生成
    -- 注：年度快照已去掉，用12个月度快照对比即可

    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 索引
CREATE INDEX idx_snapshots_user_type
    ON profile_snapshots(user_id, snapshot_type, created_at DESC);

-- 向量索引已去掉：数据量小（5-6人家庭使用，每人最多15份快照），直接扫描更快
```

### 2.2 对话历史表结构

对话历史需要支持按 session 分组查询，用于画像生成时纳入对话数据。

```sql
-- 聊天会话表
CREATE TABLE chat_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    title VARCHAR(255),           -- 会话标题，可选
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_sessions_user_time 
    ON chat_sessions(user_id, created_at DESC);

-- 对话历史表
CREATE TABLE conversation_history (
    id BIGSERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id),
    role VARCHAR(20) NOT NULL,    -- 'user' 或 'assistant'
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_history_session 
    ON conversation_history(session_id, created_at ASC);

CREATE INDEX idx_history_user_time 
    ON conversation_history(user_id, created_at DESC);
```

### 2.3 现有表不变

`records`、`chunks`、`user_settings` 等表结构不变。

---

## 三、画像系统优化

### 3.1 画像生成流程（增强版）

```
用户点"查看镜子"
    ↓
Java 查数据库：
    ├── 情绪统计（mood 分布）
    ├── 学习记录（content_type=learning）
    ├── 待办列表（content_type=todo）
    ├── 关键词汇总
    ├── 活跃时段
    └── ★ 最近对话记录（最近7天，不足5个session则补到5个session）
    ↓
打包传给 gRPC GenerateProfile
    ↓
Python 用 LLM 生成画像（综合考虑所有数据 + 对话记录）
    ↓
Java 收到画像结果：
    ├── 1. 返回前端展示
    ├── 2. ★ 存入 profile_snapshots 表
    └── 3. ★ 调用 Embedding 生成画像向量，存入快照的 embedding 字段
    ↓
完成
```

### 3.2 画像向量化

**目的：** 量化用户画像的变化幅度（"你变了多少"）。

**实现：**
- 画像生成后，将五个维度的分析文本按固定顺序拼接（mood_analysis + learning_analysis + todo_analysis + rhythm_analysis + overall_summary）
- 调用现有 Embedding 服务（复用 `EmbeddingService.Embed`）
- 向量存入 `profile_snapshots.embedding`

**用途：**
```sql
-- 画像漂移检测：本月 vs 上月的向量距离
SELECT
    s1.id AS current_id,
    s2.id AS previous_id,
    (s1.embedding <=> s2.embedding) AS drift_distance
FROM profile_snapshots s1, profile_snapshots s2
WHERE s1.user_id = :userId
  AND s2.user_id = :userId
  AND s1.snapshot_type = 'monthly'
  AND s2.snapshot_type = 'monthly'
  AND s1.created_at > s2.created_at
ORDER BY s1.created_at DESC
LIMIT 1;
```

- `drift_distance` 接近 0 → 画像没怎么变
- `drift_distance` 较大 → 画像有显著变化

**前端展示（简化版）：**
```
🪞 我的变化
本月 vs 上月：变化幅度 ████████░░ 较大

[查看本月画像] [查看上月画像]
```

> 注：先只展示原文对比，不自动生成"主要变化"文字。后续如需添加，只需增加一次 LLM 调用对比两份画像即可。

### 3.3 画像分层保留策略

**原则：** 画像用于对比，不是堆数量。保留有价值的快照，清理过期的。

| 粒度 | 保留规则 | 用途 |
|------|----------|------|
| 手动快照（用户点"查看镜子"） | 只保留最近 2 份 | "跟上次比变了什么" |
| 月度快照（每月1号定时） | 保留 12 个月 | "这一年趋势" |

**清理逻辑（每月1号定时任务执行，时区 UTC+8）：**

```
1. 生成本月月度快照
2. 清理手动快照：保留最近 2 份，删除其余
3. 清理月度快照：保留最近 12 份，删除其余
```

**数据量恒定：** 每个用户最多 2（手动）+ 12（月度）= 约 14 份快照。不会无限增长。

### 3.4 画像查询（向镜子提问时）

当 ExtractIntent 返回 `query_type=PROFILE` 时，Java 端直接查 `profile_snapshots`：

```sql
-- 获取最新画像 + 上一份画像（用于对比）
SELECT * FROM profile_snapshots
WHERE user_id = :userId
ORDER BY created_at DESC
LIMIT 2;
```

不走向量检索，不查 chunks。

**Fallback 逻辑：** 如果 PROFILE 查不到数据（用户从未点过"查看镜子"），自动 fallback 到 HYBRID 路径，用向量检索兜底。

---

## 四、对话系统优化

### 4.1 对话纳入画像生成

**原理：** 对话记录是画像的补充输入源。用户跟镜子聊的内容反映了他当前关注什么。

**实现：** 在画像生成时，Java 额外查询最近对话记录：

```sql
-- 先找最近 5 个 session（按时间倒序）
SELECT id FROM chat_sessions 
WHERE user_id = :userId 
ORDER BY created_at DESC 
LIMIT 5;

-- 再查这些 session 的对话
SELECT role, content, created_at 
FROM conversation_history 
WHERE session_id IN (:sessionIds)
ORDER BY created_at ASC;
```

**查询策略：** 优先最近 7 天的对话，如果 7 天内 session 不足 5 个，则往前补到 5 个 session。

**传给 Python 的数据：**
```
GenerateProfileRequest {
    ...现有字段不变...
    recent_chats: [
        {role: "user", content: "我最近压力大吗？", created_at: "..."},
        {role: "assistant", content: "你周三学 Spring Security 时比较焦虑...", created_at: "..."},
        ...
    ]
}
```

**Python 端处理：**
- 在画像生成 prompt 中加入对话记录作为参考
- LLM 综合考虑日记数据 + 对话内容生成画像
- 画像自然包含用户当前关注的方向

**效果对比：**

| 没有对话输入 | 有对话输入 |
|-------------|-----------|
| "你标记了 3 次焦虑" | "你最近在关注学习压力，周三学 Spring Security 时焦虑较多，但你问过如何缓解，说明你在积极调整" |

### 4.2 不做实时对话洞察提取

**原因：** 用户使用模式是"脉冲式"的——平时不用，偶尔突然问很多。实时提取在大部分时候空跑。

**替代方案：** 对话记录作为画像生成的输入源（见 4.1），不需要单独的洞察提取机制。

**未来的扩展路径：** 如果用户量增大、对话变频繁，可以将洞察提取独立为 session 结束时的轻量 LLM 调用。当前阶段不需要。

---

## 五、提问系统优化

### 5.1 ExtractIntent 增强

**现状：** ExtractIntent 返回过滤条件（content_type、moods、time_range），所有问题走同一条检索路径。

**优化：** 增加 `query_type` 字段，让 Java 端根据查询类型走不同路径。

**Proto 变更：**
```protobuf
message ExtractIntentResponse {
    string query_type = 1;           // ★ 新增
    optional string content_type = 2;
    repeated string moods = 3;
    string time_range = 4;
    string rewritten_query = 5;
}
```

**四种查询类型：**

| query_type | 含义 | Java 端行为 | 示例问题 |
|------------|------|------------|---------|
| `PROFILE` | 画像类问题 | 查 `profile_snapshots`，不查 chunks | "我最近状态怎么样？" "我是什么样的人？" |
| `STRUCTURED` | 结构化查询 | SQL 过滤查 `records`，不需要向量检索 | "我有什么待办？" "最近的 learning 记录" |
| `SEMANTIC` | 语义查询 | pgvector 检索 `chunks` | "我上周为什么心情不好？" |
| `HYBRID` | 混合查询 | SQL 过滤 + pgvector 检索组合 | "最近有什么开心的事？" |

**Java 端路由逻辑：**
```java
public List<Object> search(UUID userId, ExtractIntentResponse intent) {
    switch (intent.getQueryType()) {
        case "PROFILE":
            // 查画像快照
            List<Object> profiles = profileSnapshotMapper.findLatest(userId, 2);
            // Fallback: 如果查不到，走 HYBRID 路径
            if (profiles.isEmpty()) {
                return ragService.hybridSearch(userId, intent);
            }
            return profiles;

        case "STRUCTURED":
            // 纯 SQL 过滤，不走向量
            return recordMapper.findByFilters(userId, intent);

        case "SEMANTIC":
            // 纯向量检索
            return ragService.semanticSearch(userId, intent.getRewrittenQuery());

        case "HYBRID":
        default:
            // SQL 过滤 + 向量检索组合
            return ragService.hybridSearch(userId, intent);
    }
}
```

### 5.2 Python 端意图识别 Prompt（更新）

```
分析以下用户查询，提取过滤条件和查询类型。

用户查询：{query}

查询类型（必须选择一个）：
- PROFILE: 关于用户整体状态、画像、特征的问题（如"我最近怎么样""我是什么样的人"）
- STRUCTURED: 有明确结构化条件的查询（如"我有什么待办""最近学了什么"）
- SEMANTIC: 需要理解语义的模糊查询（如"我上周为什么心情不好""有没有类似的经历"）
- HYBRID: 同时需要结构化过滤和语义检索（如"最近有什么开心的事""学习方面遇到了什么困难"）

可选的内容类型：todo, thought, learning, plan, note, work, social, health
可选的情绪：happy, excited, satisfied, grateful, expecting, calm, bored, confused, anxious, sad, angry, exhausted, stressed

请返回 JSON 格式（使用英文小写值）：
{
  "query_type": "PROFILE|STRUCTURED|SEMANTIC|HYBRID",
  "content_type": "learning|null",
  "moods": ["happy"]|null,
  "time_range": "最近7天|null",
  "rewritten_query": "改写后的检索query"
}
```

---

## 六、RAG 检索优化

### 6.1 时间衰减

**原理：** 最近的日记权重高，久远的日记权重低。不是删除旧数据，是降权。

**实现（Java 端 SQL）：**

```sql
-- 带时间衰减的向量检索
SELECT
    c.id,
    c.content,
    c.metadata,
    (c.embedding <=> :queryVector) AS raw_distance,
    -- 时间权重：半衰期参数可调，默认30天
    (1.0 / (1.0 + EXTRACT(DAY FROM (NOW() - c.created_at)) / :halfLife)) AS time_weight,
    -- 最终分数：相似度 × 时间权重
    (c.embedding <=> :queryVector) * (1.0 / (1.0 + EXTRACT(DAY FROM (NOW() - c.created_at)) / :halfLife)) AS final_score
FROM chunks c
WHERE c.user_id = :userId
ORDER BY final_score ASC
LIMIT :topK;
```

**半衰期参数：**
- 默认值：30 天
- 用户可在设置中调整（范围：7-365 天）
- 调整时前端展示预览表格，告知用户不同时间点的权重变化

**衰减效果（默认30天半衰期）：**

| 日记时间 | 距今天数 | 时间权重 | 效果 |
|----------|----------|----------|------|
| 今天 | 0 | 1.0 | 满权重 |
| 7天前 | 7 | 0.81 | 略微降权 |
| 30天前 | 30 | 0.50 | 降一半 |
| 90天前 | 90 | 0.25 | 大幅降权 |
| 180天前 | 180 | 0.14 | 接近忽略 |

**自动关闭衰减：** 当用户明确指定了时间范围（如"去年12月的记录"），即 ExtractIntent 返回的 `time_range` 不为空时，自动关闭时间衰减，按原始相似度排序。

### 6.2 上下文限制

**原则：** 不管检索到多少条，喂给 LLM 的总量要卡死，控制 token 成本。

| 内容类型 | 上限 | 说明 |
|----------|------|------|
| 画像快照 | 最多 2 份 | 当前 + 上次对比 |
| 相关日记 | 最多 5 条 | 按 final_score 排序取 top-5 |
| 对话历史 | 最近 3 轮 | 6 条消息（user + assistant 各 3） |

**实现：** 在 Java 端传给 Python 之前截断，不在 Python 端截断。Python 端拿到的就是最终数据。

### 6.3 意图过滤 + 时间衰减的组合

```
用户："最近有什么开心的事？"
  ↓
ExtractIntent 返回：
  query_type: HYBRID
  moods: ["happy"]
  time_range: "最近30天"
  rewritten_query: "开心的事情"
  ↓
Java 端执行：
  1. SQL 预过滤：WHERE moods @> '["happy"]' AND created_at >= 30天前
  2. pgvector 检索：embedding <-> query_vector
  3. 时间衰减加权
  4. 取 top-5
  ↓
传给 Python Chat 生成回答
```

结构化过滤大幅缩小检索范围，向量检索在小范围内找最相关的，时间衰减保证最近的优先。

---

## 七、Proto 变更汇总

### 7.1 ExtractIntentResponse（修改）

```protobuf
// mirror_chat.proto
message ExtractIntentResponse {
    string query_type = 1;           // ★ 新增：PROFILE/STRUCTURED/SEMANTIC/HYBRID
    optional string content_type = 2;
    repeated string moods = 3;
    string time_range = 4;
    string rewritten_query = 5;
}
```

### 7.2 GenerateProfileRequest（修改）

```protobuf
// mirror_profile.proto
message GenerateProfileRequest {
    // 现有字段不变
    repeated TodoItem todos = 1;
    repeated LearningItem learnings = 2;
    repeated MoodStat mood_stats = 3;
    repeated KeywordStat keywords = 4;
    ActiveTimeStats active_time = 5;
    int32 total_records = 6;
    string time_range = 7;
    LlmConfig llm_config = 8;

    // ★ 新增：最近对话记录
    repeated ChatRecord recent_chats = 9;
}

// ★ 新增消息类型
message ChatRecord {
    string role = 1;        // "user" 或 "assistant"
    string content = 2;
    string created_at = 3;
}
```

### 7.3 GenerateProfileResponse（不变）

```protobuf
// 不需要修改，Java 端自己在收到响应后做存储和 embedding
message GenerateProfileResponse {
    string todo_analysis = 1;
    string learning_analysis = 2;
    string mood_analysis = 3;
    repeated string user_tags = 4;
    string rhythm_analysis = 5;
    string overall_summary = 6;
}
```

### 7.4 其他 Proto（不变）

`record_processor.proto`、`embedding.proto`、`common.proto` 不变。

---

## 八、数据流汇总（完整版）

### 流 1：随手记（不变）

```
用户输入 → Java → gRPC Classify → Java 存 records（reviewing）
  → 用户审核 → gRPC Embed → Java 存 chunks → status=done
```

### 流 2：向镜子提问（增强）

```
用户提问
  → gRPC ExtractIntent（返回 query_type + 过滤条件）
  → Java 根据 query_type 路由：
      ├── PROFILE  → 查 profile_snapshots（查不到则 fallback 到 HYBRID）
      ├── STRUCTURED → SQL 查 records
      ├── SEMANTIC → pgvector 检索 chunks（带时间衰减）
      └── HYBRID   → SQL 过滤 + pgvector 检索（带时间衰减）
  → 截断到上下文限制（画像2份 + 日记5条 + 对话3轮）
  → gRPC Chat → 流式返回前端
```

### 流 3：查看镜子（增强）

```
用户点"查看镜子"
  → Java 查：情绪统计 + 学习记录 + 待办 + 关键词 + 活跃时段 + ★最近对话
  → gRPC GenerateProfile（含对话记录）
  → Java 收到结果：
      ├── 返回前端展示
      ├── ★ 存 profile_snapshots（snapshot_type=manual）
      └── ★ Embedding → 存 profile_snapshots.embedding
```

### 流 4：每月定时任务（增强）

```
每月1号凌晨2点（UTC+8）：
  → Java 查本月所有数据 + 本月对话记录
  → gRPC GenerateProfile
  → Java 存 profile_snapshots（snapshot_type=monthly）+ Embedding
  → ★ 清理过期快照：
      ├── 手动快照：保留最近2份
      └── 月度快照：保留最近12份
```

---

## 九、数据导出功能

### 9.1 功能定位

用户可以导出自己的全部数据，体现"隐私优先"原则。用户拥有自己的数据，可以随时带走。

**优先级：** P2（加分项，有时间再做）

### 9.2 两种导出格式

**JSON 导出（备份/迁移）：**

结构化格式，完整数据，可以导入回来或迁移到其他系统。导出时自动排除所有 embedding 向量字段。

```json
{
  "exported_at": "2026-08-18T12:00:00+08:00",
  "user": { "username": "xiansheng" },
  "records": [
    {
      "content": "今天学了 Spring Security...",
      "title": "学习 Spring Security",
      "summary": "学习了认证流程，觉得有难度但已理解",
      "content_type": "learning",
      "mood": ["satisfied", "calm"],
      "keywords": ["Spring Security", "认证"],
      "status": "done",
      "created_at": "2026-08-15T14:30:00"
    }
  ],
  "profile_snapshots": [
    {
      "mood_analysis": "...",
      "learning_analysis": "...",
      "todo_analysis": "...",
      "rhythm_analysis": "...",
      "user_tags": ["技术学习", "夜猫子"],
      "overall_summary": "...",
      "snapshot_type": "manual",
      "created_at": "2026-08-18T12:00:00"
    }
  ],
  "conversations": [
    {
      "session_id": "uuid",
      "created_at": "2026-08-18T10:00:00",
      "messages": [
        { "role": "user", "content": "我最近压力大吗？", "created_at": "..." },
        { "role": "assistant", "content": "你周三学 Spring Security 时...", "created_at": "..." }
      ]
    }
  ],
  "daily_summaries": [
    {
      "summary_date": "2026-08-15",
      "content": "📋 2026-08-15 日报...",
      "record_count": 4
    }
  ]
}
```

**Markdown 导出（阅读/分享）：**

人可读格式，适合自己回顾。不含 embedding 向量等技术数据。

```markdown
# 我的日记导出
> 导出时间：2026-08-18

## 📝 日记记录

### 2026-08-15
**学习 Spring Security 认证流程**
> 学习了认证流程，觉得有难度但已理解

类型：学习 | 情绪：满足、平静 | 关键词：Spring Security、认证

---

### 2026-08-14
**完成了数据库概念设计**
> ...

## 🪞 画像快照

### 2026-08-18（最新）
**情绪状态：** 平静 60%，开心 20%，焦虑 20%
**最近在学：** Spring Security（认证/授权方向）
**个人标签：** 技术学习、夜猫子、计划型

## 💬 对话记录

### 2026-08-18
> 我最近压力大吗？

你周三学 Spring Security 时比较焦虑...

## 📋 每日总结

### 2026-08-15
今日记录 4 条，情绪分布：calm ×3, happy ×1
```

### 9.3 API 设计

```
GET  /api/export/json       # JSON 全量导出（application/json + Content-Disposition: attachment）
GET  /api/export/markdown   # Markdown 导出（text/markdown + Content-Disposition: attachment）
```

### 9.4 数据量评估

| 数据 | 增长速度 | 一年总量 |
|------|----------|----------|
| 日记记录 | 1-3 条/天 | 365-1095 条 |
| 画像快照 | 最多 14 份（分层保留） | 14 份 |
| 对话记录 | 脉冲式，大部分天数为 0 | 约 100-300 条 |
| 每日总结 | 1 条/天 | 365 条 |
| **合计** | | **约 1000-2000 条** |

数据量很小，一次查询全量导出，不需要分页。

### 9.5 实现要点

- JSON 导出：查表 → Jackson 序列化 → 设置响应头 → 返回文件下载
- Markdown 导出：查表 → 拼接 Markdown 字符串 → 设置响应头 → 返回文件下载
- 导出时自动排除 `embedding` 向量字段（技术数据，对用户无意义）
- 导出内容按时间倒序排列（最新的在前）
- 同步导出，数据量小不需要异步

---

## 十、改动量评估

| 改动项 | 位置 | 工作量 |
|--------|------|--------|
| 新建 `profile_snapshots` 表 | 数据库初始化脚本 | 1 条 SQL |
| 新建 `chat_sessions` + `conversation_history` 表 | 数据库初始化脚本 | 2 条 SQL |
| 画像生成后存快照 + embedding | `RecordService` 或新建 `ProfileService` | ~20 行 Java |
| 查看镜子时查对话记录 | 画像生成的 Service 方法 | ~15 行 Java + 1 段 SQL |
| ExtractIntent 加 `query_type` | Proto + Python `chat_service.py` + Java 路由 | Proto 1 字段 + Python ~20 行 + Java ~30 行 |
| RAG 时间衰减 | Java 端 pgvector 查询 | ~10 行 SQL 修改 |
| 月度快照定时任务 | Spring Scheduler | ~30 行 Java |
| 画像清理逻辑 | 同上定时任务内 | ~20 行 Java |
| Python 画像 prompt 更新 | `prompts/` 目录 | prompt 模板加一段对话输入说明 |
| Python 意图识别 prompt 更新 | `prompts/` 目录 | prompt 模板加 query_type 说明 |
| 半衰期用户设置 | 前端设置页 + 后端配置 | ~30 行前后端代码 |

**总计：** 3 张新表，2 个 proto 字段变更，约 200 行新增代码。无架构变更，无新服务。

---

## 十一、开发计划调整

| 阶段 | 时间 | 变更 |
|------|------|------|
| 设计阶段 | 8月 | 加入本文档的优化设计 |
| 基础开发 | 9月 | 不变 |
| AI 集成 | 10月 | gRPC 联调时同步实现 ExtractIntent query_type 路由 |
| 核心功能 | 11月 | 随手记 + 镜子（含画像持久化）+ 对话（含时间衰减检索） |
| 完善功能 | 12月 | 总结 + 灵感 + 画像漂移检测 + 月度快照定时任务 + ★数据导出 |
| 测试阶段 | 1月 | 不变 |
| 论文撰写 | 2月 | 画像变化轨迹可作为创新点写入论文 |
| 答辩准备 | 3-4月 | 不变 |

---

## 十二、变更记录

| 日期 | 版本 | 变更内容 |
|------|------|----------|
| 2026-08-18 | v0.1 | 初始版本：画像持久化、画像向量化、分层保留、对话纳入画像、意图路由、RAG 时间衰减 |
| 2026-08-18 | v0.2 | 新增数据导出功能（JSON + Markdown），优先级 P2 |
| 2026-08-18 | v0.3 | 设计评审修订：去掉年度快照、去掉向量索引、对话纳入改为7天+5个session兜底、PROFILE fallback到HYBRID、半衰期用户可调、漂移检测简化展示、明确对话历史表结构 |