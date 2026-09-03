# Mu-mirror-AI 快速开始指南

## 环境配置

✅ **已完成配置的环境**

- Python 3.12.10
- 精简版依赖（无 PyTorch，无 sentence-transformers）
- 所有必需的 gRPC 和 API 客户端依赖

## 快速启动

### 1. 激活环境

```bash
source activate.sh
```

### 2. 验证环境

```bash
python test_environment.py
```

### 3. 运行项目

```bash
# 启动 gRPC 服务器
python server.py

# 或者运行测试客户端
python test_client.py
```

## 依赖说明

### 精简版依赖 (已安装)

- **gRPC** - 高性能 RPC 框架
- **OpenAI** - OpenAI API 客户端
- **Anthropic** - Anthropic API 客户端
- **PyYAML** - 配置文件解析
- **Loguru** - 日志记录

### 完整版依赖 (可选)

如果需要本地运行 embedding 模型，可以安装完整版依赖：

```bash
pip install -r requirements.txt
```

完整版额外包含：
- **PyTorch** - 深度学习框架
- **Sentence Transformers** - 句子嵌入模型

## 使用 API 模式

项目支持使用云端 API 进行 embedding，无需本地模型：

```python
from embedding.factory import create_embedder

# 使用 OpenAI API
embedder = create_embedder(
    source="api",
    api_provider="openai",
    api_key="your-api-key"
)

# 使用智谱 API
embedder = create_embedder(
    source="api",
    api_provider="zhipu",
    api_key="your-api-key"
)

# 使用通义千问 API
embedder = create_embedder(
    source="api",
    api_provider="qwen",
    api_key="your-api-key"
)
```

## 常见问题

### Q: 为什么选择精简版依赖？

A: 精简版依赖具有以下优势：
- **更快的安装速度** - 不需要下载 PyTorch (~120MB)
- **更小的磁盘占用** - 减少约 500MB 的依赖大小
- **更少的依赖冲突** - 避免 PyTorch 与其他库的版本冲突
- **满足主要需求** - 项目主要通过 API 调用云端模型，不需要本地运行

### Q: 如何切换到完整版依赖？

A: 运行以下命令：

```bash
pip install -r requirements.txt
```

### Q: 如何添加新的依赖？

A: 使用以下命令：

```bash
pip install --target ./lib package_name
```

然后更新 `requirements.txt` 或 `requirements-minimal.txt`。

## 项目结构

```
Mu-mirror-AI/
├── lib/                    # 依赖库目录 (精简版)
├── generated/              # 生成的 protobuf 文件
├── services/               # 服务实现
├── embedding/              # Embedding 实现
│   ├── base.py            # 基类
│   ├── api_embedder.py    # API 实现 (推荐)
│   ├── local_embedder.py  # 本地实现 (需要完整版依赖)
│   └── factory.py         # 工厂函数
├── proto/                  # protobuf 定义
├── prompts/                # 提示词
├── docs/                   # 文档
├── activate.sh             # 环境激活脚本
├── test_environment.py     # 环境测试脚本
├── requirements.txt        # 完整版依赖
├── requirements-minimal.txt # 精简版依赖
└── server.py               # 服务器入口
```

## 下一步

1. 阅读 `docs/development.md` 了解开发规范
2. 查看 `README.md` 了解项目概况
3. 配置 API 密钥（参考 `config.example.yml`）
4. 开始开发你的功能！

## 技术支持

如遇问题，请检查：

1. Python 版本是否为 3.12+
2. 是否已激活环境 (`source activate.sh`)
3. 依赖是否正确安装 (`python test_environment.py`)
4. 查看 `ENVIRONMENT_SETUP.md` 获取详细说明
