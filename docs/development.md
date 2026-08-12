# Mu-mirror-AI 开发文档

## 项目概述

Mu-mirror-AI 是一个基于 gRPC 的 AI 服务框架，提供以下功能：

- **RecordProcessor** - 记录分类与处理
- **EmbeddingService** - 向量嵌入服务
- **MirrorChat** - 对话服务
- **MirrorProfile** - 用户画像服务

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
    ├── classify.txt
    ├── intent.txt
    └── profile.txt
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
    // ... 共 13 种情绪
}

// 任务状态枚举
enum TaskStatus {
    STATUS_UNKNOWN = 0;
    NOT_STARTED = 1;
    IN_PROGRESS = 2;
    COMPLETED = 3;
}
```

### 字段顺序（重要！）

**LlmConfig 字段顺序必须与 Java 端保持一致：**

| 字段号 | 字段名 | 类型 | 说明 |
|--------|--------|------|------|
| 1 | provider | string | 厂商名 |
| 2 | protocol | AiProtocol | 协议类型（枚举） |
| 3 | api_key | string | API 密钥 |
| 4 | base_url | string | 代理地址 |
| 5 | model | string | 模型名 |

> ⚠️ **注意**：修改 proto 后必须两端同时更新并重新编译，否则会导致字段错位！

---

## 如何添加新的 LLM 提供商

### 方式一：使用现有协议

如果新提供商兼容 OpenAI 或 Anthropic 协议，只需在客户端传入相应配置：

```python
# OpenAI 兼容协议
LlmConfig(
    provider="new_provider",
    protocol=AiProtocol.OPENAI,
    api_key="sk-xxx",
    base_url="https://api.new-provider.com/v1",
    model="new-model"
)

# Anthropic 协议
LlmConfig(
    provider="new_provider",
    protocol=AiProtocol.ANTHROPIC,
    api_key="sk-xxx",
    base_url="https://api.new-provider.com/anthropic",
    model="new-model"
)
```

### 方式二：添加新协议

1. **更新 proto**

```protobuf
// common.proto
enum AiProtocol {
    AI_PROTOCOL_UNKNOWN = 0;
    OPENAI = 1;
    ANTHROPIC = 2;
    NEW_PROTOCOL = 3;  // 新增
}
```

2. **创建 LLM 实现**

```python
# llm/new_protocol_llm.py
from llm.base import BaseLlm

class NewProtocolLlm(BaseLlm):
    def __init__(self, api_key: str, base_url: str = "", model: str = ""):
        # 初始化客户端
        pass

    def chat(self, messages: list[dict], temperature: float = 0.7) -> str:
        # 实现对话
        pass

    def chat_stream(self, messages: list[dict], temperature: float = 0.7):
        # 实现流式对话
        pass
```

3. **更新工厂函数**

```python
# llm/factory.py
from llm.new_protocol_llm import NewProtocolLlm

def create_llm(provider: str, api_key: str, base_url: str = "", model: str = "", protocol: int = 0):
    if protocol == common.AiProtocol.NEW_PROTOCOL:
        return NewProtocolLlm(api_key=api_key, base_url=base_url, model=model)
    # ...
```

4. **重新编译 proto**

```bash
python generate_proto.py
```

---

## 如何添加新的 Service

### 1. 定义 Proto

```proto
# proto/new_service.proto
syntax = "proto3";
package mirror;

service NewService {
    rpc NewMethod (NewRequest) returns (NewResponse);
}

message NewRequest {
    string input = 1;
}

message NewResponse {
    string output = 1;
}
```

### 2. 重新编译

```bash
python generate_proto.py
```

### 3. 实现 Servicer

```python
# services/new_service.py
from generated import new_service_pb2 as pb2
from generated import new_service_pb2_grpc as pb2_grpc

class NewServiceServicer(pb2_grpc.NewServiceServicer):
    def NewMethod(self, request, context):
        # 实现业务逻辑
        return pb2.NewResponse(output="result")
```

### 4. 注册到服务器

```python
# server.py
from services.new_service import NewServiceServicer
from generated import new_service_pb2_grpc as new_grpc

def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    new_grpc.add_NewServiceServicer_to_server(NewServiceServicer(), server)
    # ...
```

---

## LLM 调用流程

```
┌─────────────────────────────────────────────────────────────────┐
│                      Java Client (gRPC)                          │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                         server.py                                │
│                   注册并启动 gRPC 服务                            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                  services/record_processor.py                     │
│                                                                   │
│   1. 提取 request.content 和 request.llm_config                  │
│   2. 调用 create_llm() 创建 LLM 实例                             │
│   3. 调用 llm.chat() 获取响应                                    │
│   4. 解析 JSON 并映射到 Proto 枚举                                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    llm/factory.py                                 │
│                                                                   │
│   create_llm(protocol) {                                          │
│       if ANTHROPIC → AnthropicLlm(base_url, api_key, model)      │
│       else         → OpenAiLlm(base_url, api_key, model)         │
│   }                                                               │
└─────────────────────────────────────────────────────────────────┘
                              │
                ┌─────────────┴─────────────┐
                ▼                           ▼
┌──────────────────────────┐ ┌──────────────────────────┐
│   llm/anthropic_llm.py   │ │    llm/openai_llm.py     │
│                          │ │                          │
│  anthropic.Anthropic(    │ │  openai.OpenAI(          │
│    api_key=api_key,      │ │    api_key=api_key,      │
│    base_url=base_url     │ │    base_url=base_url     │
│  )                       │ │  )                       │
│                          │ │                          │
│  client.messages.create()│ │  client.chat.completions │
└──────────────────────────┘ └──────────────────────────┘
                │                           │
                ▼                           ▼
        Anthropic API / 代理         OpenAI API / 代理
```

---

## 测试方法

### 使用测试客户端

```bash
# 启动服务器
python server.py

# 另一个终端运行测试
python test_client.py
```

### 手动 gRPC 测试

可以使用 grpcurl 或 BloomRPC 等工具测试：

```bash
grpcurl -plaintext -d '{
    "content": "今天学习了spring",
    "llm_config": {
        "provider": "mimo",
        "protocol": 2,
        "api_key": "your-api-key",
        "base_url": "https://token-plan-cn.xiaomimimo.com/anthropic",
        "model": "mimo-v2.5-pro"
    }
}' localhost:50051 mirror.RecordProcessor/Classify
```

---

## 常见问题

### 1. Proto 字段错位

**症状**：Java 端发送的数据在 Python 端读取错误

**原因**：两端 proto 定义不一致

**解决**：
1. 确认 Java 端 proto 是最新版本
2. 在 Python 端更新 `proto/common.proto`
3. 运行 `python generate_proto.py` 重新编译
4. 重启 Python 服务

### 2. 401 认证错误

**症状**：`Error code: 401 - invalid x-api-key`

**原因**：
- API 密钥错误或过期
- `base_url` 未正确传递

**解决**：
1. 检查 API 密钥是否正确
2. 确认 `protocol` 枚举值正确（OPENAI=1, ANTHROPIC=2）
3. 确认 `base_url` 已正确配置

### 3. 模块导入错误

**症状**：`ModuleNotFoundError: No module named 'xxx'`

**解决**：
```bash
# 确保在虚拟环境中
venv\Scripts\activate

# 重新安装依赖
pip install -r requirements.txt

# 重新生成 proto
python generate_proto.py
```

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
feat(llm): 添加 Anthropic 协议支持

- 修改 AnthropicLlm 类支持自定义 base_url
- 更新 factory.py 传递 base_url 参数
- 适配 mimo 等第三方代理服务

Closes #123
```

---

## 联系方式

如有问题，请提交 Issue 或联系项目维护者。
