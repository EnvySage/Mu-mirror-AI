# Mu-mirror-AI 开发文档

## 项目概述

Mu-mirror-AI 是一个基于 gRPC 的 AI 服务框架，提供以下功能：

- **RecordProcessor** - 记录分类与处理（支持拆分）
- **EmbeddingService** - 向量嵌入服务
- **MirrorChat** - 对话服务（待实现）
- **MirrorProfile** - 用户画像服务（待实现）

支持多种 LLM 提供商（OpenAI 兼容协议、Anthropic 协议），可通过代理访问。

---

## 项目结构

```
Mu-mirror-AI/
├── server.py                 # gRPC 服务器入口
├── test_client.py            # 测试客户端
├── generate_proto.py         # Proto 编译脚本
├── requirements.txt          # Python 依赖
├── config.example.yml        # 配置示例
├── pyrightconfig.json        # IDE 类型检查配置
│
├── proto/                    # Protocol Buffers 定义
│   ├── common.proto          # 公共枚举和消息
│   ├── record_processor.proto
│   ├── embedding.proto
│   ├── mirror_chat.proto
│   └── mirror_profile.proto
│
├── generated/                # 自动生成的 gRPC 代码（勿手动修改）
│   ├── common_pb2.py
│   ├── common_pb2_grpc.py
│   ├── record_processor_pb2.py
│   └── ...
│
├── services/                 # 业务服务实现
│   ├── record_processor.py   # 记录处理服务
│   ├── embedding_service.py  # 嵌入服务
│   ├── chat_service.py       # 对话服务
│   └── profile_service.py    # 画像服务
│
├── llm/                      # LLM 集成模块
│   ├── base.py               # LLM 基类
│   ├── factory.py            # LLM 工厂函数
│   ├── openai_llm.py         # OpenAI 兼容实现
│   └── anthropic_llm.py      # Anthropic 协议实现
│
├── embedding/                # 向量嵌入模块
│   ├── base.py               # Embedder 基类
│   ├── factory.py            # Embedder 工厂函数
│   ├── local_embedder.py     # 本地模型实现
│   └── api_embedder.py       # API 调用实现
│
└── prompts/                  # 提示词模板
    └── classify.txt          # 分类+拆分 prompt
```

---

## 开发环境配置

### 1. 克隆项目

```bash
git clone https://github.com/EnvySage/Mu-mirror-AI.git
cd Mu-mirror-AI
```

### 2. 创建虚拟环境

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/Mac
source venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 生成 Proto 文件

```bash
python generate_proto.py
```

### 5. 启动服务

```bash
python server.py
```

---

## Proto 定义说明

### common.proto - 公共定义

```protobuf
// 协议类型枚举
enum AiProtocol {
    AI_PROTOCOL_UNKNOWN = 0;
    OPENAI = 1;      // OpenAI 兼容协议
    ANTHROPIC = 2;   // Anthropic 协议
}

// LLM 配置
message LlmConfig {
    string provider = 1;        // 厂商标识（如 "openai", "mimo"）
    AiProtocol protocol = 2;    // 协议类型
    string api_key = 3;         // API 密钥
    string base_url = 4;        // 自定义 API 地址（代理）
    string model = 5;           // 模型名称
}

// Embedding 配置
message EmbeddingConfig {
    string source = 1;          // "local" 或 "api"
    string local_model = 2;     // 本地模型名
    string api_provider = 3;    // API 厂商
    string api_key = 4;         // API 密钥
    string api_model = 5;       // API 模型名
    string base_url = 6;        // 自定义 API 地址
}

// 内容类型枚举
enum ContentType {
    CONTENT_UNKNOWN = 0;
    TODO = 1;
    THOUGHT = 2;
    LEARNING = 3;
    PLAN = 4;
    NOTE = 5;
    WORK = 6;
    SOCIAL = 7;
    HEALTH = 8;
}

// 情绪类型枚举
enum MoodType {
    MOOD_UNKNOWN = 0;
    HAPPY = 1;
    EXCITED = 2;
    SATISFIED = 3;
    GRATEFUL = 4;
    EXPECTING = 5;
    CALM = 6;
    BORED = 7;
    CONFUSED = 8;
    ANXIOUS = 9;
    SAD = 10;
    ANGRY = 11;
    EXHAUSTED = 12;
    STRESSED = 13;
}

// 任务状态枚举
enum TaskStatus {
    STATUS_UNKNOWN = 0;
    NOT_STARTED = 1;
    IN_PROGRESS = 2;
    COMPLETED = 3;
}
```

### record_processor.proto - 记录处理服务

```protobuf
service RecordProcessor {
    rpc Classify(ClassifyRequest) returns (ClassifyResponse);
}

message ClassifyRequest {
    string content = 1;
    LlmConfig llm_config = 2;
}

message ClassifyResponse {
    bool skip = 1;                      // 是否跳过
    string skip_reason = 2;             // 跳过原因
    repeated ClassifyItem items = 3;    // 分类结果列表
}

message ClassifyItem {
    string title = 1;                   // 标题（≤10字）
    string summary = 2;                 // 摘要（≤30字）
    ContentType content_type = 3;       // 内容类型
    repeated MoodType moods = 4;        // 情绪标签
    TaskStatus status = 5;              // 任务状态
    repeated string keywords = 6;       // 关键词
}
```

---

## Classify 接口详解

### 核心逻辑

Classify 接口**一次调用同时完成拆分和分类**：

```
输入内容
    │
    ▼
┌─────────────────┐
│ 内容 < 3字？    │ ── Yes ──→ skip=True
└─────────────────┘
    │ No
    ▼
┌─────────────────┐
│ 调用 LLM        │
│ (拆分+分类)     │
└─────────────────┘
    │
    ▼
┌─────────────────┐
│ skip=True？     │ ── Yes ──→ skip=True, items=[]
└─────────────────┘
    │ No
    ▼
┌─────────────────┐
│ items 为空？    │ ── Yes ──→ skip=True
└─────────────────┘
    │ No
    ▼
返回 items 列表
```

### Java 端调用示例

```java
ClassifyRequest request = ClassifyRequest.newBuilder()
    .setContent("上午学了 Spring，下午健身")
    .setLlmConfig(llmConfig)
    .build();

ClassifyResponse response = stub.classify(request);

if (response.getSkip()) {
    // 跳过无意义内容
    System.out.println("跳过: " + response.getSkipReason());
} else {
    // 处理分类结果
    for (ClassifyItem item : response.getItemsList()) {
        saveRecord(
            item.getTitle(),
            item.getSummary(),
            item.getContentType(),
            item.getMoodsList(),
            item.getKeywordsList()
        );
    }
}
```

### 测试场景

| 输入 | 期望结果 |
|------|----------|
| "今天学习了 Spring Boot" | skip=False, items 长度=1 |
| "上午学了 Spring，下午健身" | skip=False, items 长度=2 |
| "啊啊啊" | skip=True, items=[] |
| "升职了" | skip=False, items 长度=1, moods=[HAPPY] |
| "中午吃饭，下午干活" | skip=False, items 长度=2, moods=[] |

### 关键约束

| 项目 | 说明 |
|------|------|
| items 不能为空 | skip=False 时，items 必须至少有 1 条 |
| skip=True 时 items 可以为空 | 无意义内容时返回空 items |
| content_type 用枚举 | 用 ContentType.LEARNING，不是字符串 |
| moods 用枚举 | 用 MoodType.HAPPY，不是字符串 |
| title ≤ 10 字 | 超出会被截断 |
| summary ≤ 30 字 | 超出会被截断 |

---

## 情绪标签规则

### 适度推测原则

| 场景 | 处理 | 示例 |
|------|------|------|
| 明确情绪词 | ✅ 打标签 | "很开心" → HAPPY |
| 情绪表达 | ✅ 打标签 | "哈哈"、"唉" |
| 明显正面事件 | ✅ 可推测 | "升职了" → HAPPY/SATISFIED |
| 明显负面事件 | ✅ 可推测 | "被骂了" → SAD/ANGRY |
| 中性事件 | ❌ 不打 | "吃饭"、"干活"、"学习" |
| 模棱两可 | ❌ 不打 | 不确定就不打 |

### 可选情绪

- HAPPY（开心）、EXCITED（兴奋）、SATISFIED（满足）、GRATEFUL（感恩）、EXPECTING（期待）
- CALM（平静）、BORED（无聊）、CONFUSED（困惑）
- ANXIOUS（焦虑）、SAD（难过）、ANGRY（愤怒）、EXHAUSTED（疲惫）、STRESSED（压力）

---

## Embedding 接口

### 接口列表

| 接口 | 说明 | 超时 |
|------|------|------|
| Embed | 单条文本嵌入 | 10秒 |
| EmbedBatch | 批量文本嵌入 | 30秒 |
| GetModelInfo | 获取模型信息 | 5秒 |

### 调用示例

```java
// 本地模型
EmbeddingConfig config = EmbeddingConfig.newBuilder()
    .setSource("local")
    .setLocalModel("BAAI/bge-m3")
    .build();

// API 调用
EmbeddingConfig config = EmbeddingConfig.newBuilder()
    .setSource("api")
    .setApiProvider("openai")
    .setApiKey("sk-xxx")
    .setApiModel("text-embedding-3-small")
    .setBaseUrl("https://proxy.example.com/v1")
    .build();

EmbedRequest request = EmbedRequest.newBuilder()
    .setText("要嵌入的文本")
    .setEmbeddingConfig(config)
    .build();

EmbedResponse response = stub.embed(request);
List<Float> vector = response.getVectorList();
int dimension = response.getDimension();
```

### 错误处理

```python
try:
    embedder = create_embedder(...)
    vector = embedder.embed(text)
except Exception as e:
    context.set_code(grpc.StatusCode.INTERNAL)
    context.set_details(f"Embedding 失败: {str(e)}")
```

---

## LLM 集成

### 支持的协议

| 协议 | 枚举值 | 实现类 |
|------|--------|--------|
| OpenAI 兼容 | AiProtocol.OPENAI (1) | OpenAiLlm |
| Anthropic | AiProtocol.ANTHROPIC (2) | AnthropicLlm |

### LlmConfig 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| provider | string | 厂商标识 |
| protocol | AiProtocol | 协议类型（枚举） |
| api_key | string | API 密钥 |
| base_url | string | 代理地址 |
| model | string | 模型名 |

### 添加新协议

1. 更新 `proto/common.proto` 的 AiProtocol 枚举
2. 创建 `llm/xxx_llm.py` 实现 BaseLlm 接口
3. 更新 `llm/factory.py` 的 create_llm 函数
4. 运行 `python generate_proto.py` 重新编译

---

## 错误处理规范

### gRPC 错误码

```python
# 记录错误并返回错误状态
context.set_code(grpc.StatusCode.INTERNAL)
context.set_details(f"操作失败: {str(e)}")

# 或者返回带错误信息的响应
return ClassifyResponse(
    skip=True,
    skip_reason=str(e),
    items=[]
)
```

### 常见错误

| 错误码 | 说明 | 处理方式 |
|--------|------|----------|
| INTERNAL | 服务内部错误 | 检查日志，返回 skip=True |
| INVALID_ARGUMENT | 参数错误 | 检查请求参数 |
| DEADLINE_EXCEEDED | 超时 | 增加超时时间或优化性能 |
| UNAVAILABLE | 服务不可用 | 检查服务状态 |

---

## 提交规范

### Commit Message 格式

```
<type>(<scope>): <subject>

<body>

<footer>
```

### Type 类型

- `feat` - 新功能
- `fix` - Bug 修复
- `docs` - 文档更新
- `style` - 代码格式（不影响功能）
- `refactor` - 重构
- `test` - 测试相关
- `chore` - 构建/工具相关

### 示例

```
feat(classify): 实现拆分+分类合并接口

- Classify 现在一次调用完成拆分和分类
- ClassifyResponse 改为 items 列表
- 新增情绪标签规则：适度推测，中性事件不打标签

Closes #123
```

---

## 快速验证清单

### Classify 接口

- [ ] 重新编译 proto（`python generate_proto.py`）
- [ ] Classify 返回 `ClassifyResponse.items` 而不是单个字段
- [ ] `skip=False` 时 items 至少有 1 条
- [ ] content_type 用枚举，不是字符串
- [ ] moods 用枚举，不是字符串
- [ ] 错误时设置 `context.set_code()` 和 `context.set_details()`
- [ ] title ≤ 10字，summary ≤ 30字

### Embedding 接口

- [ ] Embed 返回向量和维度信息
- [ ] 支持本地模型和 API 调用
- [ ] 支持自定义 base_url（第三方代理）
- [ ] 错误时返回合适的错误码

---

## 联系方式

如有问题，请提交 Issue 或联系项目维护者。
