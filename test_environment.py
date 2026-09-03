#!/usr/bin/env python3
"""
环境测试脚本 - 验证 Mu-mirror-AI 项目依赖是否正确安装
"""

import sys
import os

# 添加 lib 目录到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'lib'))

def test_imports():
    """测试所有必要的依赖是否可以导入"""
    print("测试依赖导入...")

    try:
        import grpc
        print(f"[OK] gRPC: {grpc.__version__}")
    except ImportError as e:
        print(f"[FAIL] gRPC 导入失败: {e}")
        return False

    try:
        import openai
        print(f"[OK] OpenAI: {openai.__version__}")
    except ImportError as e:
        print(f"[FAIL] OpenAI 导入失败: {e}")
        return False

    try:
        import anthropic
        print(f"[OK] Anthropic: {anthropic.__version__}")
    except ImportError as e:
        print(f"[FAIL] Anthropic 导入失败: {e}")
        return False

    try:
        import yaml
        print(f"[OK] PyYAML: {yaml.__version__}")
    except ImportError as e:
        print(f"[FAIL] PyYAML 导入失败: {e}")
        return False

    try:
        import loguru
        print(f"[OK] Loguru: {loguru.__version__}")
    except ImportError as e:
        print(f"[FAIL] Loguru 导入失败: {e}")
        return False

    return True

def test_project_modules():
    """测试项目模块是否可以导入"""
    print("\n测试项目模块...")

    try:
        import generated.common_pb2
        print("[OK] common_pb2 模块")
    except ImportError as e:
        print(f"[FAIL] common_pb2 模块导入失败: {e}")
        return False

    try:
        import generated.embedding_pb2
        print("[OK] embedding_pb2 模块")
    except ImportError as e:
        print(f"[FAIL] embedding_pb2 模块导入失败: {e}")
        return False

    try:
        import generated.mirror_chat_pb2
        print("[OK] mirror_chat_pb2 模块")
    except ImportError as e:
        print(f"[FAIL] mirror_chat_pb2 模块导入失败: {e}")
        return False

    try:
        import generated.mirror_profile_pb2
        print("[OK] mirror_profile_pb2 模块")
    except ImportError as e:
        print(f"[FAIL] mirror_profile_pb2 模块导入失败: {e}")
        return False

    try:
        import generated.record_processor_pb2
        print("[OK] record_processor_pb2 模块")
    except ImportError as e:
        print(f"[FAIL] record_processor_pb2 模块导入失败: {e}")
        return False

    return True

def main():
    """主测试函数"""
    print("=" * 50)
    print("Mu-mirror-AI 环境测试")
    print("=" * 50)
    print(f"Python 版本: {sys.version}")
    print(f"Python 路径: {sys.executable}")
    print(f"工作目录: {os.getcwd()}")
    print()

    # 测试依赖导入
    if not test_imports():
        print("\n[FAIL] 依赖导入测试失败")
        return 1

    # 测试项目模块
    if not test_project_modules():
        print("\n[FAIL] 项目模块测试失败")
        return 1

    print("\n" + "=" * 50)
    print("[SUCCESS] 所有测试通过! 环境配置正确。")
    print("=" * 50)
    return 0

if __name__ == "__main__":
    sys.exit(main())
