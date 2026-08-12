# Mirror AI Service

基于 gRPC 的 Python AI 服务，为 Mirror 应用提供智能功能。

## 项目结构

```
.
├── proto/                          # Proto 定义
│   ├── common.proto               # 公共消息（枚举、配置）
│   ├── record_processor.proto     # 记录处理服务
│   ├── embedding.proto            # Embedding 服务
│   ├── mirror_chat.proto          # 对话服务
│   └── mirror_profile.proto       # 画像服务
├── generated/                      # 生成的 gRPC 代码（勿手动修改）
│   ├── common_pb2.py
│   ├── record_processor_pb2*.py
│   ├── embedding_pb2*.py
│   ├── mirror_chat_pb2*.py
│   └── mirror_profile_pb2*.py
├── services/                       # 服务实现
│   ├── record_processor.py        # 记录分类（Classify, Split）
│   ├── embedding_service.py       # Embedding（Embed, EmbedBatch, GetModelInfo）
│   ├── chat_service.py            # 对话（ExtractIntent, Chat）
│   └── profile_service.py         # 画像（GenerateProfile）
├── llm/                           # LLM 模块
│   ├── base.py                    # 统一接口
│   ├── openai_llm.py              # OpenAI 实现
│   ├── qwen_llm.py                # 通义千问实现
│   ├── zhipu_llm.py               # 智谱实现
│   └── factory.py                 # 工厂函数
├── embedding/                     # Embedding 模块
│   ├── base.py                    # 统一接口
│   ├── local_embedder.py          # 本地 BGE-m3
│   ├── api_embedder.py            # API Embedding
│   └── factory.py                 # 工厂函数
├── prompts/                       # Prompt 模板
│   ├── classify.txt               # 分类
│   ├── intent.txt                 # 意图提取
│   └── profile.txt                # 画像生成
├── server.py                      # 服务入口（端口 50051）
├── test_client.py                 # 测试客户端
├── generate_proto.py              # 重新生成 gRPC 代码
├── config.example.yml             # 配置示例
├── Dockerfile                     # Docker 构建
├── requirements.txt
└── README.md
```

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 启动服务
python server.py

# 另一个终端测试连接
python test_client.py
```

## 重新生成 Proto

修改 `proto/*.proto` 后运行：

```bash
python generate_proto.py
```

## 服务列表

| 服务 | 方法 | 说明 |
|------|------|------|
| RecordProcessor | Classify | 记录分类（标题/摘要/标签/情绪） |
| RecordProcessor | Split | 多事件拆分 |
| EmbeddingService | Embed | 文本转向量 |
| EmbeddingService | EmbedBatch | 批量转向量 |
| EmbeddingService | GetModelInfo | 查询模型信息 |
| MirrorChat | ExtractIntent | 意图提取（过滤条件） |
| MirrorChat | Chat | 对话生成（流式） |
| MirrorProfile | GenerateProfile | 画像生成 |

## 当前状态

测试阶段 — 所有服务返回硬编码数据，用于验证 gRPC 链路。