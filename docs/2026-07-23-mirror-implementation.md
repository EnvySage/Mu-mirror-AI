# AI 日记"镜子"系统 — 落地与实现文档

> 版本：v0.4
> 日期：2026-07-23（初版） / 2026-08-04（更新） / 2026-08-07（用户隔离更新）
> 状态：讨论中

**关联文档：**
- [系统设计文档](2026-07-23-ai-diary-mirror-design.md) — 整体功能设计、数据库、API、异常机制
- [AI 服务层设计文档](2026-08-04-ai-service-design.md) — Python gRPC 服务、Proto 定义、Embedding 切换

---

## 一、技术栈选择

### 1.1 后端

| 技术 | 选择 | 说明 |
|------|------|------|
| 框架 | Spring Boot | 已学过，熟悉 |
| 语言 | Java 17+ | Spring Boot 推荐版本 |
| 构建工具 | Maven | 统一依赖管理 |

### 1.2 AI 服务

| 技术 | 选择 | 说明 |
|------|------|------|
| 语言 | Python 3.11+ | AI/ML 生态最成熟 |
| RPC 框架 | gRPC（grpcio-aio） | 高性能、强类型、支持流式、异步 |
| Embedding 模型 | BGE-m3（本地）/ API（可配置） | 本地优先，API 作为备选 |
| LLM | 用户自配 | 支持 OpenAI/通义千问/智谱等 |
| Embedding 库 | sentence-transformers | 本地加载 BGE-m3 |
| LLM SDK | openai / dashscope / zhipuai | 多厂商适配 |

> 详细设计见 [AI 服务层设计文档](2026-08-04-ai-service-design.md)。

### 1.3 数据库

| 技术 | 选择 | 说明 |
|------|------|------|
| 关系型数据库 | PostgreSQL | 支持 JSONB，生态好 |
| 向量扩展 | pgvector | PostgreSQL 原生扩展 |
| ORM | MyBatis-Plus | 简化数据库操作 |

### 1.4 前端

| 技术 | 选择 | 说明 |
|------|------|------|
| 框架 | Vue 3 | 已有经验 |
| UI 组件库 | Element Plus | 成熟稳定 |
| 构建工具 | Vite | 快速开发 |

### 1.5 部署

| 技术 | 选择 | 说明 |
|------|------|------|
| 容器化 | Docker | 一键部署 |
| 编排 | Docker Compose | 三服务编排：Java + Python + PostgreSQL |

---

## 二、开发环境

### 2.1 本地开发环境

| 工具 | 版本要求 | 说明 |
|------|---------|------|
| JDK | 17+ | Spring Boot 3.x 要求 |
| Python | 3.11+ | AI 服务要求 |
| Node.js | 18+ | Vue 3 要求 |
| Maven | 3.8+ | Java 构建工具 |
| pip / poetry | 最新 | Python 依赖管理 |
| PostgreSQL | 15+ | 支持 pgvector |
| Git | 2.x | 版本控制 |

### 2.2 IDE 推荐

| 语言 | IDE |
|------|-----|
| Java | IntelliJ IDEA |
| 前端 | VS Code |

---

## 三、项目结构

### 3.1 整体结构

```
mirror/
├── mirror-backend/              # Java 后端
├── mirror-ai/                   # Python AI 服务
├── mirror-frontend/             # Vue 3 前端
├── proto/                       # gProto 定义（共享）
│   ├── common.proto
│   ├── record_processor.proto
│   ├── embedding.proto
│   ├── mirror_chat.proto
│   └── mirror_profile.proto
├── sql/                         # 数据库初始化脚本
├── docker-compose.yml
└── .env.example
```

### 3.2 后端结构（mirror-backend）

```
mirror-backend/
├── src/main/java/com/mirror/
│   ├── config/                  # 配置类
│   ├── controller/              # API 控制器
│   ├── service/                 # 业务逻辑
│   ├── grpc/                    # gRPC 客户端
│   │   ├── GrpcClientConfig.java
│   │   └── AiGrpcClient.java
│   ├── mapper/                  # 数据库操作
│   ├── model/                   # 实体类
│   ├── dto/                     # 数据传输对象
│   └── utils/                   # 工具类
├── src/main/resources/
│   └── application.yml          # 主配置
└── pom.xml
```

### 3.3 AI 服务结构（mirror-ai）

```
mirror-ai/
├── server.py                    # gRPC 服务入口
├── config.py                    # 配置管理
├── services/                    # gRPC 服务实现
│   ├── record_processor.py
│   ├── embedding_service.py
│   ├── chat_service.py
│   └── profile_service.py
├── llm/                         # LLM 多厂商适配
├── embedding/                   # Embedding 本地/API 切换
├── prompts/                     # Prompt 模板
├── config.example.yml
├── requirements.txt
└── Dockerfile
```

### 3.4 前端结构（mirror-frontend）

```
mirror-frontend/
├── src/
│   ├── api/                     # API 调用
│   ├── components/              # 组件
│   ├── views/                   # 页面
│   ├── stores/                  # 状态管理
│   └── utils/                   # 工具函数
├── package.json
└── vite.config.js
```

---

## 四、开发计划

### 4.1 阶段划分

| 阶段 | 时间 | 内容 | 交付物 |
|------|------|------|--------|
| **设计阶段** | 8月 | 系统设计、技术选型、Proto 定义 | 设计文档、Proto 文件 |
| **基础开发** | 9月 | Java 后端骨架 + Python AI 服务骨架 + 前端基础 | 可运行的框架（三服务联调） |
| **AI 集成** | 10月 | gRPC 联调 + 数据管道 + RAG | AI 处理流程跑通 |
| **核心功能** | 11月 | 随手记 + 镜子 + 对话 | MVP |
| **完善功能** | 12月 | 总结 + 灵感 + 优化 + Embedding API 模式 | 完整功能 |
| **测试阶段** | 1月 | 系统测试 + Bug 修复 | 测试报告 |
| **论文撰写** | 2月 | 毕业论文 | 论文初稿 |
| **答辩准备** | 3-4月 | 答辩材料 + 演示 | 答辩通过 |

### 4.2 第一阶段详细计划（8月 — 当前阶段）

| 周 | 任务 |
|----|------|
| 第1周 | 系统设计文档定稿、Proto 文件定义 |
| 第2周 | 三服务骨架搭建（Java + Python + Vue）、数据库建表 |
| 第3周 | gRPC 客户端/服务端联调、Embedding 本地模型跑通 |
| 第4周 | 基础 API 开发、随手记流程端到端跑通 |

---

## 五、核心代码实现思路

> 以下为 Java 端关键代码思路。Python 端实现见 [AI 服务层设计文档](2026-08-04-ai-service-design.md)。

### 5.1 gRPC 客户端封装

```java
/**
 * 统一的 AI 调用封装，内部通过 gRPC 调用 Python AI 服务。
 * Java 端只调这个类，不直接碰 gRPC 细节。
 */
@Service
public class AiGrpcClient {

    // gRPC stub（由 GrpcClientConfig 注入）
    private final RecordProcessorGrpc.RecordProcessorBlockingStub recordStub;
    private final EmbeddingServiceGrpc.EmbeddingServiceBlockingStub embedStub;
    private final MirrorChatGrpc.MirrorChatBlockingStub chatStub;
    private final MirrorProfileGrpc.MirrorProfileBlockingStub profileStub;

    @Autowired
    private UserSettingsMapper userSettingsMapper;

    // 获取用户的 LLM 配置
    private LlmConfig getLlmConfig(String userId) {
        UserSettings settings = userSettingsMapper.selectByUserId(userId);
        return LlmConfig.newBuilder()
            .setProvider(settings.getAiProvider())
            .setApiKey(settings.getAiApiKey())
            .setBaseUrl(settings.getAiBaseUrl() != null ? settings.getAiBaseUrl() : "")
            .setModel(settings.getAiModel())
            .build();
    }

    // 获取用户的 Embedding 配置
    private EmbeddingConfig getEmbeddingConfig(String userId) {
        UserSettings settings = userSettingsMapper.selectByUserId(userId);
        return EmbeddingConfig.newBuilder()
            .setSource(settings.getEmbeddingSource())
            .setLocalModel(settings.getEmbeddingModel())
            .setApiProvider(settings.getAiProvider())
            .setApiKey(settings.getEmbeddingApiKey() != null ? settings.getEmbeddingApiKey() : "")
            .setApiModel(settings.getEmbeddingModel())
            .build();
    }

    // 记录分类（携带用户 LLM 配置）
    public ClassifyResponse classify(String userId, String content) {
        return recordStub
            .withDeadlineAfter(30, TimeUnit.SECONDS)
            .classify(ClassifyRequest.newBuilder()
                .setContent(content)
                .setLlmConfig(getLlmConfig(userId))
                .build());
    }

    // 文本转向量（携带用户 Embedding 配置）
    public EmbedResponse embed(String userId, String text) {
        return embedStub
            .withDeadlineAfter(10, TimeUnit.SECONDS)
            .embed(EmbedRequest.newBuilder()
                .setText(text)
                .setEmbeddingConfig(getEmbeddingConfig(userId))
                .build());
    }

    // 批量转向量
    public EmbedBatchResponse embedBatch(String userId, List<String> texts) { ... }

    // 提取对话意图（携带用户 LLM 配置）
    public ExtractIntentResponse extractIntent(String userId, String query) { ... }

    // 对话（流式返回，携带用户 LLM 配置）
    public Iterator<ChatChunk> chat(String userId, String question,
                                     List<ChatMessage> history,
                                     List<RetrievedChunk> chunks) { ... }

    // 生成画像（携带用户 LLM 配置）
    public GenerateProfileResponse generateProfile(String userId,
                                                    GenerateProfileRequest request) { ... }
}
```

### 5.2 RAG 检索服务（Java 端 pgvector）

```java
/**
 * 向量检索在 Java 端完成，使用 pgvector SQL 查询。
 * 流程：gRPC Embed → pgvector SQL → 返回结果
 */
@Service
public class RagService {

    @Autowired
    private AiGrpcClient aiGrpcClient;

    @Autowired
    private ChunkMapper chunkMapper;

    public List<Chunk> search(String userId, String query, Map<String, Object> filters) {
        // 1. gRPC → Python：将 query 转为向量
        EmbedResponse embedResult = aiGrpcClient.embed(query);
        List<Float> queryVector = embedResult.getVectorList();

        // 2. Java 端：构建 pgvector 查询（带用户隔离 + 元数据过滤）
        // SQL: SELECT *, embedding <-> query_vector AS distance
        //      FROM chunks
        //      WHERE user_id = ? AND metadata @> filters
        //      ORDER BY distance LIMIT 10
        return chunkMapper.searchByVector(userId, queryVector, filters, 10);
    }
}
```

### 5.3 记录处理服务（编排 gRPC + 数据库）

```java
/**
 * 记录处理：编排 gRPC 调用和数据库写入。
 * AI 推理走 gRPC，数据持久化在 Java 端。
 */
@Service
public class RecordService {

    @Autowired
    private AiGrpcClient aiGrpcClient;

    public ProcessResult process(String userId, String content) {
        // 1. 长度检测（Java 端）
        if (content.length() > 500) {
            return ProcessResult.tooLong();
        }

        // 2. gRPC → Python：AI 分类（携带用户 LLM 配置）
        ClassifyResponse classifyResult = aiGrpcClient.classify(userId, content);
        if (classifyResult.getSkip()) {
            return ProcessResult.skipped(classifyResult.getSkipReason());
        }

        // 3. Java 端：保存记录到 records 表（关联 userId）
        Record record = saveRecord(userId, content, classifyResult);

        // 4. 不在这里做 Embedding，等用户审核通过后再做
        return ProcessResult.pendingReview(record);
    }

    // 审核通过后调用
    public ProcessResult approve(String userId, Long recordId, ClassifyResponse userModifications) {
        Record record = recordMapper.selectById(recordId);
        // 校验记录归属
        if (!record.getUserId().equals(userId)) {
            throw new AccessDeniedException("无权操作此记录");
        }

        // 1. 应用用户修改（如果有）
        if (userModifications != null) {
            applyModifications(record, userModifications);
        }

        // 2. gRPC → Python：生成向量（携带用户 Embedding 配置）
        EmbedResponse embedResult = aiGrpcClient.embed(userId, record.getContent());

        // 3. Java 端：保存向量到 chunks 表（关联 userId）
        Chunk chunk = saveChunk(userId, record.getId(), record.getContent(), embedResult);

        // 4. 更新状态为 done
        record.setRecordStatus("done");
        recordMapper.updateById(record);

        return ProcessResult.success(record, chunk);
    }
}
```

### 5.4 对话服务（Java 编排，流式透传）

```java
/**
 * 对话流程：意图提取 → pgvector 检索 → LLM 生成（流式）
 */
@Service
public class ChatService {

    @Autowired
    private AiGrpcClient aiGrpcClient;

    @Autowired
    private RagService ragService;

    public void chat(String userId, String sessionId, String question, StreamObserver<ChatChunk> responseObserver) {
        // 1. gRPC → Python：提取意图
        ExtractIntentResponse intent = aiGrpcClient.extractIntent(question);

        // 2. Java 端：pgvector 向量检索 + 用户隔离 + 元数据过滤
        Map<String, Object> filters = buildFilters(intent);
        List<Chunk> chunks = ragService.search(userId, intent.getRewrittenQuery(), filters);

        // 3. Java 端：加载对话历史
        List<ChatMessage> history = chatHistoryMapper.getBySessionId(sessionId);

        // 4. gRPC → Python：流式生成回答（携带用户 LLM 配置）
        Iterator<ChatChunk> stream = aiGrpcClient.chat(userId, question, history, toRetrievedChunks(chunks));

        // 5. 透传给前端（SSE / WebSocket）
        while (stream.hasNext()) {
            responseObserver.onNext(stream.next());
        }
        responseObserver.onCompleted();
    }
}
```

---

## 六、待讨论问题

- [x] 技术栈确认（Java + Python + Vue 3，gRPC 通信）
- [ ] 开发环境搭建步骤
- [ ] 数据库初始化脚本
- [ ] Proto 文件定稿 → 生成 Java/Python 代码
- [ ] Python AI 服务骨架搭建
- [ ] Docker Compose 配置
- [ ] 测试用例设计

---

## 七、变更记录

| 日期 | 版本 | 变更内容 |
|------|------|----------|
| 2026-07-23 | v0.1 | 初始文档，技术栈和项目结构初稿 |
| 2026-08-04 | v0.2 | 架构调整：AI 推理拆分为 Python gRPC 服务，更新技术栈、项目结构、核心代码实现思路 |
| 2026-08-04 | v0.3 | 文档审查修正：统一开发计划、明确 grpcio-aio、补充配置管理说明 |
| 2026-08-07 | v0.4 | 用户隔离：所有 gRPC 调用携带 userId 和用户配置（LlmConfig/EmbeddingConfig），RAG 检索、对话服务、记录处理服务新增 userId 参数；记录处理流程改为审核后才 Embedding；AiGrpcClient 从 user_settings 动态构建配置；三文档对齐修正 |
