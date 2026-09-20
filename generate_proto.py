"""重新生成 gRPC 代码

用法: python generate_proto.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

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
    "generated/common_pb2.py": [
        ("import common_pb2 as common__pb2", "from generated import common_pb2 as common__pb2"),
    ],
    "generated/record_processor_pb2.py": [
        ("import common_pb2 as common__pb2", "from generated import common_pb2 as common__pb2"),
    ],
    "generated/embedding_pb2.py": [
        ("import common_pb2 as common__pb2", "from generated import common_pb2 as common__pb2"),
    ],
    "generated/embedding_pb2_grpc.py": [
        ("import embedding_pb2 as embedding__pb2", "from generated import embedding_pb2 as embedding__pb2"),
    ],
    "generated/mirror_profile_pb2.py": [
        ("import common_pb2 as common__pb2", "from generated import common_pb2 as common__pb2"),
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

# 同步部署镜像 upload/generated/（git 跟踪的逐字节镜像）
# 历史坑：上一次改 proto 只重生成了根目录，upload/generated/common_pb2_grpc.py 停在旧
# grpcio-tools 的版本头上，两份 stub 悄悄漂移。靠人记不住，所以放在这一步自动做掉。
# 位置必须在"修复相对导入"之后——否则复制过去的是没修导入的版本，upload 侧 import 即炸。
# 用 copyfile 而非文本读写：逐字节复制（含换行符），避免又一次因换行差异出现假不一致。
# upload/generated/ 不存在（例如 upload/ 被清掉）→ 静默跳过，不报错退出。
_SRC_DIR = Path("generated")
_DST_DIR = Path("upload/generated")
if _DST_DIR.is_dir():
    stubs = sorted(_SRC_DIR.glob("*_pb2.py")) + sorted(_SRC_DIR.glob("*_pb2_grpc.py"))
    for stub in stubs:
        shutil.copyfile(stub, _DST_DIR / stub.name)
    print(f"同步 upload/generated/ ({len(stubs)} 个 stub)...")
else:
    print("跳过 upload/generated/ 同步（目录不存在）")

print("完成")