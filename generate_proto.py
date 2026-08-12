"""重新生成 gRPC 代码

用法: python generate_proto.py
"""

import subprocess
import sys

cmd = [
    sys.executable, "-m", "grpc_tools.protoc",
    "-I./proto",
    "--python_out=./generated",
    "--grpc_python_out=./generated",
    "./proto/common.proto",
    "./proto/record_processor.proto",
    "./proto/embedding.proto",
    "./proto/mirror_chat.proto",
    "./proto/mirror_profile.proto",
]

print("生成 gRPC 代码...")
result = subprocess.run(cmd, capture_output=True, text=True)

if result.returncode != 0:
    print(f"失败:\n{result.stderr}")
    sys.exit(1)

# 修复相对导入
fixes = {
    "generated/record_processor_pb2.py": [
        ("import common_pb2 as common__pb2", "from generated import common_pb2 as common__pb2"),
    ],
    "generated/embedding_pb2.py": [
        ("import common_pb2 as common__pb2", "from generated import common_pb2 as common__pb2"),
    ],
    "generated/embedding_pb2_grpc.py": [
        ("import embedding_pb2 as embedding__pb2", "from generated import embedding_pb2 as embedding__pb2"),
    ],
    "generated/record_processor_pb2_grpc.py": [
        ("import record_processor_pb2 as record__processor__pb2", "from generated import record_processor_pb2 as record__processor__pb2"),
    ],
    "generated/mirror_chat_pb2.py": [
        ("import common_pb2 as common__pb2", "from generated import common_pb2 as common__pb2"),
    ],
    "generated/mirror_chat_pb2_grpc.py": [
        ("import mirror_chat_pb2 as mirror__chat__pb2", "from generated import mirror_chat_pb2 as mirror__chat__pb2"),
    ],
    "generated/mirror_profile_pb2.py": [
        ("import common_pb2 as common__pb2", "from generated import common_pb2 as common__pb2"),
    ],
    "generated/mirror_profile_pb2_grpc.py": [
        ("import mirror_profile_pb2 as mirror__profile__pb2", "from generated import mirror_profile_pb2 as mirror__profile__pb2"),
    ],
}

for filepath, replacements in fixes.items():
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    for old, new in replacements:
        content = content.replace(old, new)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

print("完成")