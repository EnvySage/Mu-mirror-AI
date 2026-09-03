# Mu-mirror-AI 环境配置说明

## 环境信息

- **Python 版本**: 3.12.10
- **Python 路径**: `E:\python\python.exe`
- **依赖库路径**: `./lib`

## 快速开始

### 方法 1: 使用激活脚本 (推荐)

```bash
# 在 Git Bash 中运行
source activate.sh
```

激活后，你可以直接使用 Python：

```bash
python your_script.py
# 或者
python test_environment.py
```

### 方法 2: 手动设置环境变量

在运行 Python 脚本前，设置 PYTHONPATH：

```bash
# 在 Git Bash 中
export PYTHONPATH="$(pwd)/lib:$PYTHONPATH"
python your_script.py

# 或者一行命令
PYTHONPATH=./lib python your_script.py
```

### 方法 3: 在代码中添加路径

在你的 Python 脚本开头添加：

```python
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'lib'))
```

## 已安装的依赖

### 核心依赖
- **gRPC**: 1.83.0 - 高性能 RPC 框架
- **OpenAI**: 3.1.0 - OpenAI API 客户端
- **Anthropic**: 0.122.0 - Anthropic API 客户端

### 辅助依赖
- **PyYAML**: 6.0.3 - YAML 解析库
- **Loguru**: 0.7.3 - 日志库
- **NumPy**: 2.5.2 - 数值计算库
- **Requests**: 2.34.2 - HTTP 客户端库

### 注意
- 本环境使用 **精简版依赖** (requirements-minimal.txt)
- 不包含 PyTorch 和 sentence-transformers（本地模型相关依赖）
- 如需本地运行 embedding 模型，请安装完整版依赖：`pip install -r requirements.txt`

## 测试环境

运行环境测试脚本验证配置：

```bash
PYTHONPATH=./lib python test_environment.py
```

## 项目结构

```
Mu-mirror-AI/
├── lib/                    # 依赖库目录
├── generated/              # 生成的 protobuf 文件
├── services/               # 服务实现
├── proto/                  # protobuf 定义文件
├── prompts/                # 提示词文件
├── docs/                   # 文档
├── activate.sh             # 环境激活脚本
├── test_environment.py     # 环境测试脚本
├── requirements.txt        # 依赖列表
└── server.py               # 服务器入口
```

## 常见问题

### Q: 为什么使用 `./lib` 目录而不是虚拟环境？

A: 由于系统中已有 Python 进程占用虚拟环境文件，我们使用 `--target` 选项将依赖安装到 `./lib` 目录。这样可以避免文件锁定问题，同时保持依赖隔离。

### Q: 如何添加新的依赖？

A: 使用以下命令安装新依赖到 `./lib` 目录：

```bash
/e/python/python.exe -m pip install --target ./lib package_name
```

然后更新 `requirements.txt` 文件。

### Q: PyTorch 是 CPU 版本，如何使用 GPU？

A: 当前安装的是 CPU 版本的 PyTorch。如果需要 GPU 支持，请访问 [PyTorch 官网](https://pytorch.org/) 获取适合你 CUDA 版本的安装命令。

### Q: 在 Windows 命令提示符 (cmd) 中如何使用？

A: 在 cmd 中设置环境变量：

```cmd
set PYTHONPATH=%cd%\lib;%PYTHONPATH%
python your_script.py
```

或者使用 PowerShell：

```powershell
$env:PYTHONPATH = "$(Get-Location)\lib;$env:PYTHONPATH"
python your_script.py
```

## 开发建议

1. **使用 IDE**: 推荐使用 PyCharm 或 VSCode，它们可以自动识别 `./lib` 目录中的依赖
2. **版本控制**: `./lib` 目录已在 `.gitignore` 中排除，不会提交到 Git
3. **依赖管理**: 更新依赖时记得同步更新 `requirements.txt`

## 下一步

1. 阅读 `docs/development.md` 了解开发规范
2. 查看 `README.md` 了解项目概况
3. 运行 `test_environment.py` 确认环境正常
4. 开始开发你的功能！
