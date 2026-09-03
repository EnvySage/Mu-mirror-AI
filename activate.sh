#!/bin/bash
# 激活脚本 - 设置 Python 环境
# 使用方法: source activate.sh

# 设置 Python 路径
export PYTHONPATH="$(pwd)/lib:$PYTHONPATH"

# 设置 Python 解释器路径
export PYTHON_EXECUTABLE="/e/python/python.exe"

echo "Python 环境已激活"
echo "Python 路径: $PYTHON_EXECUTABLE"
echo "依赖库路径: $(pwd)/lib"
echo ""
echo "使用方法:"
echo "  $PYTHON_EXECUTABLE your_script.py"
echo ""

# 验证安装
echo "验证主要依赖..."
PYTHONPATH="$(pwd)/lib:$PYTHONPATH" $PYTHON_EXECUTABLE -c "
import grpc
import openai
import anthropic
print('所有主要依赖验证成功!')
print(f'  - gRPC: {grpc.__version__}')
print(f'  - OpenAI: {openai.__version__}')
print(f'  - Anthropic: {anthropic.__version__}')
"

echo ""
echo "注意: 本环境使用精简版依赖，不包含 PyTorch 和 sentence-transformers"
echo "如需本地运行 embedding 模型，请安装完整版依赖: pip install -r requirements.txt"
