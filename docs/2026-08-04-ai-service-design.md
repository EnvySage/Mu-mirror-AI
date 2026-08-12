# AI 服务层设计文档 — Python gRPC 服务

> 版本：v0.3
> 日期：2026-08-04（初版） / 2026-08-07（配置机制重构）
> 状态：设计中

**关联文档：**
- [系统设计文档](2026-07-23-ai-diary-mirror-design.md) — 整体功能设计、数据库、API、异常机制
- [落地实现文档](2026-07-23-mirror-implementation.md) — Java 端实现思路、项目结构、开发计划

---

## 一、架构总览

### 1.1 整体架构

```
Vue 3 前端
    ↓ HTTP
Spring Boot（Java）
  ├── 业务逻辑、CRUD、认证、定时任务
  ├── 数据库操作（PostgreSQL + pgvector 查询）
  └── gRPC Client
        ↓ gRPC
Python AI 服务（gRPC Server）
  ├── 记录分类（标题/摘要/标签/情绪）
  ├── Embedding（本地 BGE-m3 / API）
  ├── 对话生成（多轮 RAG）
  └── 画像生成
        ↓
  ├── 本地 Embedding 模型（BGE-m3）
  └── LLM API（用户自配：OpenAI/通义千问/智谱）
```

### 1.2 职责划分

| 层 | 职责 | 不做什么 |
|---|---|---|
| **Java（Spring Boot）** | 业务逻辑、CRUD、认证、定时任务、数据库读写（含 pgvector 向量查询）、gRPC 调用 | 不做 AI 推理、不调 LLM API |
| **Python（AI 服务）** | 纯 AI 推理：分类、Embedding、对话、画像生成 | 不碰数据库、不管业务逻辑 |

**核心原则：Python 是无状态的推理引擎，所有数据持久化由 Java 负责。**

### 1.3 数据流

**随手记处理流程：**
```
用户输入 → Java 接收
  → gRPC: Classify（携带用户 LLM 配置，Python 返回标题/摘要/标签）
  → Java 保存 records 表（status=pending_review）
  → 前端展示审核界面，等待用户操作
  → 用户审核通过（可先修改标签）
  → gRPC: Embed（携带用户 Embedding 配置，Python 返回向量）
  → Java 保存 chunks 表（pgvector）
  → status=done，记录锁定
```

**向镜子提问流程：**
```
用户提问 → Java 接收
  → gRPC: ExtractIntent（Python 返回过滤条件）
  → Java 执行 pgvector 向量检索 + 元数据过滤
  → gRPC: Chat（Python 基于检索结果生成回答）
  → Java 保存对话历史
  → 返回前端
```

**画像生成流程：**
```
用户点击"查看镜子" → Java 接收
  → Java 从数据库查询各维度数据（待办/学习/情绪/关键词/活跃时段）
  → gRPC: GenerateProfile（Python 基于数据生成画像）
  → Java 保存画像
  → 返回前端
```

---

## 二、gRPC Proto 设计

### 2.1 文件结构

```
proto/
├── common.proto           # 公共消息定义（含配置消息）
├── record_processor.proto # 记录处理服务
├── embedding.proto        # Embedding 服务
├── mirror_chat.proto      # 对话服务
└── mirror_profile.proto   # 画像服务
```

> **设计说明**：配置通过每次请求传递（Java 从数据库读取后放入请求），Python 端完全无状态，不存储任何用户配置。

### 2.2 common.proto

```protobuf
syntax = "proto3";
package mirror;

// 情绪标签（枚举）
enum MoodType {
  MOOD_UNKNOWN = 0;
  // 正面
  HAPPY = 1;      // 开心
  EXCITED = 2;    // 兴奋
  SATISFIED = 3;  // 满足
  GRATEFUL = 4;   // 感恩
  EXPECTING = 5;  // 期待
  // 中性
  CALM = 6;       // 平静
  BORED = 7;      // 无聊
  CONFUSED = 8;   // 困惑
  // 负面
  ANXIOUS = 9;    // 焦虑
  SAD = 10;       // 难过
  ANGRY = 11;     // 愤怒
  EXHAUSTED = 12; // 疲惫
  STRESSED = 13;  // 压力
}

// 内容类型
enum ContentType {
  CONTENT_UNKNOWN = 0;
  TODO = 1;       // 待办
  THOUGHT = 2;    // 感想
  LEARNING = 3;   // 学习
  PLAN = 4;       // 计划
  NOTE = 5;       // 随记
  WORK = 6;       // 工作
  SOCIAL = 7;     // 社交
  HEALTH = 8;     // 健康
}

// 状态
enum TaskStatus {
  STATUS_UNKNOWN = 0;
  NOT_STARTED = 1;
  IN_PROGRESS = 2;
  COMPLETED = 3;
}

// LLM 配置（每次请求携带）
message LlmConfig {
  string provider = 1;    // openai / zhipu / qwen
  string api_key = 2;
  string base_url = 3;    // 可选，留空用默认
  string model = 4;       // 模型名称
}

// Embedding 配置（每次请求携带）
message EmbeddingConfig {
  string source = 1;      // local / api
  // 本地模式
  string local_model = 2; // 如 "BAAI/bge-m3"
  // API 模式
  // 注意：api_provider 当前复用 LLM 的 provider（openai/zhipu/qwen），
  // 即 Embedding API 和 LLM API 使用同一提供商。
  // 如需支持不同提供商，需在 user_settings 表新增 embedding_api_provider 字段。
  string api_provider = 3;
  string api_key = 4;
  string api_model = 5;
}
```

### 2.3 record_processor.proto — 记录处理

```protobuf
syntax = "proto3";
package mirror;

import "common.proto";

service RecordProcessor {
  // 分类：输入文本，返回标题/摘要/标签
  rpc Classify(ClassifyRequest) returns (ClassifyResponse);

  // 多事件拆分：判断是否需要拆分，返回拆分结果
  rpc Split(SplitRequest) returns (SplitResponse);
}

// --- 分类 ---

message ClassifyRequest {
  string content = 1;       // 用户输入的原始文本
  LlmConfig llm_config = 2; // LLM 配置（每次请求携带）
}

message ClassifyResponse {
  bool skip = 1;                // 是否跳过（无意义内容）
  string skip_reason = 2;       // 跳过原因
  string title = 3;             // 标题（10字以内）
  string summary = 4;           // 摘要（30字以内）
  ContentType content_type = 5; // 内容类型
  repeated MoodType moods = 6;  // 情绪（多选）
  TaskStatus status = 7;        // 状态（仅待办/计划类）
  repeated string keywords = 8; // 关键词 3-5 个
}

// --- 拆分 ---

message SplitRequest {
  string content = 1;
  LlmConfig llm_config = 2; // LLM 配置（每次请求携带）
}

message SplitResponse {
  bool need_split = 1;              // 是否需要拆分
  repeated SplitSegment segments = 2; // 拆分后的段落
}

message SplitSegment {
  string content = 1;  // 段落内容
  int32 order = 2;     // 顺序
}
```

### 2.4 embedding.proto — Embedding 服务

```protobuf
syntax = "proto3";
package mirror;

service EmbeddingService {
  // 文本转向量
  rpc Embed(EmbedRequest) returns (EmbedResponse);

  // 批量转向量
  rpc EmbedBatch(EmbedBatchRequest) returns (EmbedBatchResponse);

  // 查询当前使用的 embedding 模型信息
  rpc GetModelInfo(ModelInfoRequest) returns (ModelInfoResponse);
}

message EmbedRequest {
  string text = 1;
  EmbeddingConfig embedding_config = 2; // Embedding 配置（每次请求携带）
}

message EmbedResponse {
  repeated float vector = 1;    // 向量
  int32 dimension = 2;          // 维度
  string model_name = 3;        // 使用的模型名
}

message EmbedBatchRequest {
  repeated string texts = 1;
  EmbeddingConfig embedding_config = 2; // Embedding 配置（每次请求携带）
}

message EmbedBatchResponse {
  repeated EmbedResponse results = 1;
}

message ModelInfoRequest {}

message ModelInfoResponse {
  string model_name = 1;     // 模型名称，如 "bge-m3"
  string source = 2;         // 来源："local" 或 "api"
  int32 dimension = 3;       // 向量维度
  bool available = 4;        // 当前是否可用
}
```

### 2.5 mirror_chat.proto — 对话服务

```protobuf
syntax = "proto3";
package mirror;

service MirrorChat {
  // 提取意图（过滤条件）
  rpc ExtractIntent(ExtractIntentRequest) returns (ExtractIntentResponse);

  // 生成回答（支持流式）
  rpc Chat(ChatRequest) returns (stream ChatChunk);
}

// --- 意图提取 ---

message ExtractIntentRequest {
  string query = 1;           // 用户问题
  LlmConfig llm_config = 2;  // LLM 配置（每次请求携带）
}

message ExtractIntentResponse {
  optional string content_type = 2;  // 过滤：内容类型
  repeated string moods = 3;         // 过滤：情绪
  string time_range = 4;             // 过滤：时间范围（如 "最近7天"）
  string rewritten_query = 5;        // 改写后的检索 query
}

// --- 对话 ---

message ChatRequest {
  string question = 1;                // 用户问题
  repeated ChatMessage history = 2;   // 对话历史
  repeated RetrievedChunk chunks = 3; // Java 端检索好的相关记录
  LlmConfig llm_config = 4;          // LLM 配置（每次请求携带）
}

message ChatMessage {
  string role = 1;    // "user" 或 "assistant"
  string content = 2;
}

message RetrievedChunk {
  int64 record_id = 1;
  string content = 2;       // 原文
  string title = 3;
  string created_at = 4;
  string content_type = 5;
  repeated string moods = 6;
  float score = 7;          // 相似度分数
}

// 流式返回
message ChatChunk {
  string content = 1;       // 本次返回的文本片段
  bool done = 2;            // 是否结束
  repeated Source sources = 3; // 来源（最后一次返回）
}

message Source {
  int64 record_id = 1;
  string quote = 2;         // 引用的原文片段
  string date = 3;
}
```

### 2.6 mirror_profile.proto — 画像服务

```protobuf
syntax = "proto3";
package mirror;

service MirrorProfile {
  // 生成画像
  rpc GenerateProfile(GenerateProfileRequest) returns (GenerateProfileResponse);
}

message GenerateProfileRequest {
  // Java 端查询好的各维度数据，直接传给 Python
  repeated TodoItem todos = 1;          // 未完成的待办
  repeated LearningItem learnings = 2;  // 最近学习内容
  repeated MoodStat mood_stats = 3;     // 情绪统计
  repeated KeywordStat keywords = 4;    // 关键词统计
  ActiveTimeStats active_time = 5;      // 活跃时段
  int32 total_records = 6;              // 总记录数
  string time_range = 7;                // 数据时间范围
  LlmConfig llm_config = 8;            // LLM 配置（每次请求携带）
}

message TodoItem {
  int64 record_id = 1;
  string title = 2;
  string summary = 3;
  string created_at = 4;
}

message LearningItem {
  int64 record_id = 1;
  string title = 2;
  string summary = 3;
  repeated string keywords = 4;
  string created_at = 5;
}

message MoodStat {
  string mood = 1;
  int32 count = 2;
  float percentage = 3;
}

message KeywordStat {
  string keyword = 1;
  int32 count = 2;
}

message ActiveTimeStats {
  map<string, int32> hour_distribution = 1;  // {"22": 5, "23": 3, ...}
  map<string, int32> weekday_distribution = 2; // {"周一": 3, ...}
  int32 records_last_7_days = 3;
  string peak_hour = 4;   // 最活跃时段
}

message GenerateProfileResponse {
  // 各维度的分析结果
  string todo_analysis = 1;      // 未完成的事
  string learning_analysis = 2;  // 学习分析
  string mood_analysis = 3;      // 情绪分析
  repeated string user_tags = 4; // 用户特征标签
  string rhythm_analysis = 5;    // 生活节奏分析
  string overall_summary = 6;    // 整体总结
}
```

~~### 2.7 config_service.proto — 配置更新~~（已删除）

> **设计变更**：配置不再通过独立的 ConfigService 推送。改为每次 gRPC 请求携带配置（LlmConfig / EmbeddingConfig），Python 端完全无状态，不存储任何用户配置。这样天然支持多用户使用不同模型。

---

## 三、Python AI 服务设计

### 3.1 模块结构

```
mirror-ai/
├── server.py                # gRPC 服务启动入口
├── services/
│   ├── record_processor.py  # 记录分类服务
│   ├── embedding_service.py # Embedding 服务（本地/API 切换）
│   ├── chat_service.py      # 对话服务
│   └── profile_service.py   # 画像服务
├── llm/
│   ├── base.py              # LLM 统一接口
│   ├── openai_llm.py        # OpenAI 实现
│   ├── qwen_llm.py          # 通义千问实现
│   └── zhipu_llm.py         # 智谱实现
├── embedding/
│   ├── base.py              # Embedding 统一接口
│   ├── local_embedder.py    # 本地 BGE-m3
│   └── api_embedder.py      # API Embedding
├── prompts/
│   ├── classify.txt         # 分类 prompt 模板
│   ├── chat.txt             # 对话 prompt 模板
│   ├── intent.txt           # 意图提取 prompt 模板
│   └── profile.txt          # 画像生成 prompt 模板
├── requirements.txt
├── Dockerfile
└── config.example.yml       # 配置示例
```

### 3.2 Embedding 可切换设计

**统一接口：**
```python
# embedding/base.py
class BaseEmbedder(ABC):
    @abstractmethod
    def embed(self, text: str) -> list[float]: ...

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...

    @abstractmethod
    def get_model_info(self) -> dict: ...
```

**本地实现（BGE-m3，懒加载）：**
```python
# embedding/local_embedder.py
class LocalEmbedder(BaseEmbedder):
    _instance = None
    _model = None

    def __new__(cls, model_name: str = "BAAI/bge-m3"):
        # 单例模式，确保模型只加载一次
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, model_name: str = "BAAI/bge-m3"):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(model_name)
            self.model_name = model_name

    def embed(self, text: str) -> list[float]:
        return self._model.encode(text).tolist()
```

**API 实现：**
```python
# embedding/api_embedder.py
class ApiEmbedder(BaseEmbedder):
    def __init__(self, provider: str, api_key: str, base_url: str, model: str):
        # provider: "openai" / "zhipu" / "qwen"
        ...
```

**工厂函数（根据请求中的配置动态创建）：**
```python
def create_embedder(embedding_config) -> BaseEmbedder:
    source = embedding_config.source
    if source == "local":
        return LocalEmbedder(model_name=embedding_config.local_model)
    elif source == "api":
        return ApiEmbedder(
            provider=embedding_config.api_provider,
            api_key=embedding_config.api_key,
            base_url="",
            model=embedding_config.api_model
        )
    else:
        raise ValueError(f"Unknown embedding source: {source}")
```

> **懒加载说明**：本地模式下，模型在首次使用时下载并加载（懒加载），非本地模式不会加载模型，不占用内存。使用单例模式确保同一模型只加载一次。

### 3.3 LLM 统一接口

```python
# llm/base.py
class BaseLlm(ABC):
    @abstractmethod
    def chat(self, messages: list[dict], temperature: float = 0.7) -> str: ...

    @abstractmethod
    def chat_stream(self, messages: list[dict], temperature: float = 0.7) -> Generator[str]: ...
```

**工厂函数（根据请求中的配置动态创建）：**
```python
def create_llm(llm_config) -> BaseLlm:
    provider = llm_config.provider
    if provider == "openai":
        return OpenAiLlm(api_key=llm_config.api_key, base_url=llm_config.base_url, model=llm_config.model)
    elif provider == "qwen":
        return QwenLlm(api_key=llm_config.api_key, model=llm_config.model)
    elif provider == "zhipu":
        return ZhipuLlm(api_key=llm_config.api_key, model=llm_config.model)
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")
```

### 3.4 配置管理

**设计原则**：Python AI 服务完全无状态，不存储任何用户配置。所有配置由 Java 端从数据库（user_settings）读取，通过每次 gRPC 请求传递给 Python。

**配置流转**：
```
用户在前端改配置 → Java 存 user_settings 表
  → 下次 gRPC 请求时，Java 从数据库读取配置放入请求
  → Python 从请求中取出配置，用完即弃
```

**多用户支持**：
- 每个用户的配置独立存在 user_settings 表中
- Java 根据当前用户读取对应的配置
- Python 无需关心用户是谁，只管处理请求中的配置
- 不同用户可以使用不同的 LLM 和 Embedding 模型，互不干扰

**Python 服务配置（仅服务启动相关，与用户配置无关）：**
```yaml
# config.yml（仅服务级配置，不涉及用户数据）
server:
  port: 50051              # gRPC 端口
  workers: 4               # 并发数

prompts:
  classify: "prompts/classify.txt"
  chat: "prompts/chat.txt"
  intent: "prompts/intent.txt"
  profile: "prompts/profile.txt"
```

---

## 四、Java 端 gRPC 客户端设计

### 4.1 模块结构（新增部分）

```
src/main/java/com/mirror/
├── grpc/
│   ├── GrpcClientConfig.java      # gRPC 连接配置
│   ├── AiGrpcClient.java          # 统一的 AI 调用封装
│   └── interceptors/
│       └── LoggingInterceptor.java # 日志拦截器
```

### 4.2 调用封装

```java
@Service
public class AiGrpcClient {

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
        return recordStub.classify(ClassifyRequest.newBuilder()
            .setContent(content)
            .setLlmConfig(getLlmConfig(userId))
            .build());
    }

    // 生成向量（携带用户 Embedding 配置）
    public EmbedResponse embed(String userId, String text) {
        return embedStub.embed(EmbedRequest.newBuilder()
            .setText(text)
            .setEmbeddingConfig(getEmbeddingConfig(userId))
            .build());
    }

    // 提取意图（携带用户 LLM 配置）
    public ExtractIntentResponse extractIntent(String userId, String query) {
        return chatStub.extractIntent(ExtractIntentRequest.newBuilder()
            .setQuery(query)
            .setLlmConfig(getLlmConfig(userId))
            .build());
    }

    // 对话（流式，携带用户 LLM 配置）
    public Iterator<ChatChunk> chat(String userId, String question,
                                     List<ChatMessage> history, List<RetrievedChunk> chunks) {
        return chatStub.chat(ChatRequest.newBuilder()
            .setQuestion(question)
            .addAllHistory(history)
            .addAllChunks(chunks)
            .setLlmConfig(getLlmConfig(userId))
            .build());
    }

    // 生成画像（携带用户 LLM 配置）
    public GenerateProfileResponse generateProfile(String userId, GenerateProfileRequest request) {
        return profileStub.generateProfile(request.toBuilder()
            .setLlmConfig(getLlmConfig(userId))
            .build());
    }
}
```

### 4.3 配置（application.yml）

```yaml
grpc:
  client:
    ai-service:
      address: localhost:50051
      negotiation-type: plaintext  # 开发环境，生产用 TLS
```

### 4.4 超时与重试

```java
// 关键调用设置超时
public ClassifyResponse classify(String content) {
    return recordProcessorStub
        .withDeadlineAfter(30, TimeUnit.SECONDS)  // 30秒超时
        .classify(ClassifyRequest.newBuilder()
            .setContent(content)
            .build());
}

// Embedding 可重试（幂等操作）
public EmbedResponse embed(String text) {
    return Retryer.of(EmbedResponse.class)
        .maxRetries(2)
        .retryOn(StatusRuntimeException.class)
        .call(() -> embeddingStub
            .withDeadlineAfter(10, TimeUnit.SECONDS)
            .embed(EmbedRequest.newBuilder().setText(text).build()));
}
```

---

## 五、Docker 部署设计

### 5.1 docker-compose.yml

```yaml
version: '3.8'

services:
  # PostgreSQL + pgvector
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: mirror
      POSTGRES_USER: mirror
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./sql/init.sql:/docker-entrypoint-initdb.d/init.sql
    ports:
      - "5432:5432"

  # Java 后端
  mirror-backend:
    build: ./mirror-backend
    ports:
      - "8080:8080"
    environment:
      SPRING_DATASOURCE_URL: jdbc:postgresql://postgres:5432/mirror
      GRPC_CLIENT_AI-SERVICE_ADDRESS: mirror-ai:50051
    depends_on:
      - postgres
      - mirror-ai

  # Python AI 服务（完全无状态，配置通过每次 gRPC 请求携带）
  mirror-ai:
    build: ./mirror-ai
    ports:
      - "50051:50051"
    # 注意：以下环境变量仅用于本地开发默认值和健康检查，不参与请求处理
    # 生产环境中，所有配置由 Java 端从数据库读取并通过 gRPC 请求传递
    environment:
      EMBEDDING_SOURCE: ${EMBEDDING_SOURCE:-local}  # 本地开发默认值
      LLM_API_KEY: ${LLM_API_KEY}                   # 本地开发默认值
      LLM_PROVIDER: ${LLM_PROVIDER:-openai}         # 本地开发默认值
    volumes:
      - ai-models:/app/models  # 持久化模型文件
    deploy:
      resources:
        limits:
          memory: 4G  # 本地 embedding 模型需要内存

volumes:
  pgdata:
  ai-models:
```

### 5.2 环境变量

```bash
# .env
DB_PASSWORD=your_db_password

# 以下为本地开发默认值，生产环境以 Java 数据库（user_settings）为准
EMBEDDING_SOURCE=local        # local 或 api（本地开发默认值）
LLM_PROVIDER=openai           # openai / zhipu / qwen（本地开发默认值）
LLM_API_KEY=sk-xxx            # 本地开发默认值
LLM_MODEL=gpt-4o-mini         # 本地开发默认值
EMBEDDING_API_KEY=            # 仅 api 模式需要
EMBEDDING_API_MODEL=          # 仅 api 模式需要
```

---

## 六、错误处理设计

### 6.1 gRPC 错误码映射

| gRPC 状态码 | Java 端处理 | 用户看到 |
|---|---|---|
| `OK` | 正常处理 | — |
| `DEADLINE_EXCEEDED` | 标记 failed，可重试 | "处理超时，请重试" |
| `UNAVAILABLE` | 重试1次 | "AI 服务暂时不可用" |
| `INVALID_ARGUMENT` | 记录日志 | "输入内容有误" |
| `INTERNAL` | 标记 failed | "处理失败，请重试" |

### 6.2 Python 端异常处理

```python
# 服务端统一异常处理
class AiServiceException(Exception):
    def __init__(self, message: str, code: grpc.StatusCode):
        self.message = message
        self.code = code

# 每个 RPC 方法包装 try-except
async def Classify(self, request, context):
    try:
        result = self.classifier.classify(request.content)
        return ClassifyResponse(**result)
    except TimeoutError:
        context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "LLM 调用超时")
    except Exception as e:
        logger.exception("Classify failed")
        context.abort(grpc.StatusCode.INTERNAL, f"处理失败: {str(e)}")
```

### 6.3 降级策略

| 场景 | 降级方案 |
|---|---|
| Python 服务不可用 | Java 端捕获异常，记录 status=failed，用户可重试 |
| Embedding 失败 | 不写入向量库，记录保持无向量状态 |
| LLM 分类失败 | 使用默认值（type=note, mood=calm），标记 AI 未处理 |
| 画像生成失败 | 返回上次缓存的画像 |

---

## 七、开发顺序建议

| 阶段 | 内容 | 依赖 |
|---|---|---|
| **Phase 1** | 定义 proto 文件，生成 Java/Python 代码 | 无 |
| **Phase 2** | Python 搭骨架：gRPC server + Embedding（本地） | Phase 1 |
| **Phase 3** | Java 搭骨架：gRPC client + 记录 CRUD | Phase 1 |
| **Phase 4** | 联调：随手记流程（classify → save → embed → store） | Phase 2 + 3 |
| **Phase 5** | 对话功能（intent + chat） | Phase 4 |
| **Phase 6** | 画像生成 | Phase 4 |
| **Phase 7** | Embedding API 模式 | Phase 2 |
| **Phase 8** | 多 LLM 支持 | Phase 2 |

---

## 八、变更记录

| 日期 | 版本 | 变更内容 |
|---|---|---|
| 2026-08-04 | v0.1 | 初始设计：架构、proto、Python 模块、部署方案 |
| 2026-08-04 | v0.2 | 文档审查修正：新增 ConfigService Proto、明确 grpcio-aio、配置以 Java 数据库为准 |
| 2026-08-07 | v0.3 | 配置机制重构：删除 ConfigService，改为每次请求携带 LlmConfig/EmbeddingConfig；Python 完全无状态，天然支持多用户不同模型；common.proto 新增配置消息定义；所有请求消息新增配置字段；更新工厂函数和懒加载设计；Java 客户端根据 userId 从数据库读取配置放入请求 |
| 2026-08-07 | v0.3.1 | 文档对齐修正：数据流图补充审核步骤；SplitRequest 新增 LlmConfig；EmbeddingConfig.api_provider 添加映射说明；Docker 环境变量标注为本地开发默认值 |
