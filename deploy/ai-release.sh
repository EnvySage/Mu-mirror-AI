#!/usr/bin/env bash
# AI 服务发布（在 Jenkins workspace 里执行）
#
#   rsync 覆盖代码（不碰 venv）→ 依赖变了才 pip install → 重启 → 分别探活 10003/10004
#
# 2C2G 前提：REQ 用 requirements-minimal.txt（不含 torch / sentence-transformers），
# 并在设置页把 embedding 切成 api 模式。装了 torch 这台机扛不住。
set -euo pipefail

MIRROR_HOME="${MIRROR_HOME:-/opt/mirror}"
APP_DIR="${AI_APP_DIR:-$MIRROR_HOME/ai/app}"
VENV="${AI_VENV:-$MIRROR_HOME/ai/venv}"
SERVICE="${AI_SERVICE:-mirror-ai}"
LEGACY_PORT="${AI_PORT:-10003}"
AGENT_PORT="${AI_AGENT_PORT:-10004}"
SRC="${SRC:-.}"
REQ="${REQ:-requirements-minimal.txt}"
HASH_FILE="$MIRROR_HOME/ai/.requirements.hash"

die() { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

command -v rsync >/dev/null || die "缺 rsync（apt install rsync）"
[[ -x "$VENV/bin/python" ]] || die "venv 不存在：$VENV（先按 deploy/init-server.sh 输出准备）"
[[ -f "$SRC/server.py" ]] || die "$SRC 下没有 server.py，SRC 指向不对？"

mkdir -p "$APP_DIR"

# 代码同步：--delete 保证删掉的旧模块不残留，但排除 venv / 缓存 / 测试
rsync -a --delete \
  --exclude 'venv/' --exclude '.venv/' \
  --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude '.git/' --exclude '.pytest_cache/' \
  --exclude 'tests/' \
  "$SRC"/ "$APP_DIR"/
chown -R mirror:mirror "$APP_DIR" 2>/dev/null || true
echo "✓ 代码已同步到 $APP_DIR"

# 依赖只在清单变化时安装（torch 之类装一次要几分钟，绝不能每次构建重装）
if [[ -f "$APP_DIR/$REQ" ]]; then
  NEW_HASH="$(md5sum "$APP_DIR/$REQ" | cut -d' ' -f1)"
  OLD_HASH="$(cat "$HASH_FILE" 2>/dev/null || true)"
  if [[ "$NEW_HASH" != "$OLD_HASH" ]]; then
    echo "依赖清单有变化，安装 $REQ …"
    "$VENV/bin/pip" install --no-cache-dir -r "$APP_DIR/$REQ" \
      -i https://pypi.tuna.tsinghua.edu.cn/simple \
      || die "pip install 失败（权限或网络问题）"
    echo "$NEW_HASH" > "$HASH_FILE"
    echo "✓ 依赖已更新"
  else
    echo "依赖未变，跳过 pip install"
  fi
fi

sudo systemctl restart "$SERVICE"

wait_for_tcp() {
  local port="$1" label="$2"
  for _ in $(seq 1 20); do
    if timeout 2 bash -c "</dev/tcp/127.0.0.1/$port" 2>/dev/null; then
      echo "✓ $label gRPC 端口就绪（:$port）"
      return 0
    fi
    sleep 2
  done
  echo "✗ $label gRPC 探活失败（:$port）：tail -100 $MIRROR_HOME/shared/logs/ai.err.log" >&2
  return 1
}

wait_for_tcp "$LEGACY_PORT" "legacy"

# Read the same enabled flag from the deployment environment or config.yml.
# Secrets are never printed; runtime service and release environment must match.
AGENT_ENABLED="$(PYTHONPATH="$APP_DIR" "$VENV/bin/python" - "$APP_DIR/config.yml" <<'PY'
import os
import sys
import yaml

def truthy(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}

with open(sys.argv[1], encoding="utf-8") as stream:
    config = yaml.safe_load(stream) or {}
enabled = truthy(os.environ.get("MIRROR_AI_AGENT_ENABLED", "")) or truthy(
    (config.get("agent") or {}).get("enabled", False)
)
print("true" if enabled else "false")
PY
)"

if [[ "$AGENT_ENABLED" == "true" ]]; then
  wait_for_tcp "$AGENT_PORT" "Agent"

  [[ -n "${MIRROR_AI_READ_DSN:-}" ]] || die "Agent 已启用，但发布健康检查缺 MIRROR_AI_READ_DSN"
  MIRROR_AI_READ_DSN="$MIRROR_AI_READ_DSN" PYTHONPATH="$APP_DIR" "$VENV/bin/python" - <<'PY'
import os
import psycopg

connection = psycopg.connect(os.environ["MIRROR_AI_READ_DSN"], connect_timeout=5)
try:
    with connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute("SELECT 1 FROM mirror_ai.glossary_terms LIMIT 0")
    connection.rollback()
finally:
    connection.close()
PY
  echo "✓ 只读视图 SELECT 探测通过（mirror_ai.glossary_terms；无行读取）"

  # No token and no body: this must be rejected before command parsing or any write.
  [[ -n "${MIRROR_AGENT_COMMAND_HEALTH_URL:-}" ]] || die "Agent 已启用，但缺 MIRROR_AGENT_COMMAND_HEALTH_URL 鉴权探测地址"
  COMMAND_STATUS="$(curl -sS -m 5 -o /dev/null -w '%{http_code}' -X POST \
    "$MIRROR_AGENT_COMMAND_HEALTH_URL" 2>/dev/null || true)"
  [[ "$COMMAND_STATUS" == "401" ]] || die "Java 命令无凭据探测应返回 401，实际 HTTP $COMMAND_STATUS"
  echo "✓ Java 具名命令接口无凭据探测返回 401"

  PYTHONPATH="$APP_DIR" "$VENV/bin/python" - "$AGENT_PORT" <<'PY'
import sys
import grpc
from generated import agent_pb2, agent_pb2_grpc

channel = grpc.insecure_channel(f"127.0.0.1:{sys.argv[1]}")
try:
    stub = agent_pb2_grpc.AgentRunnerStub(channel)
    try:
        list(stub.RunAgent(agent_pb2.AgentRunRequest(), timeout=5))
    except grpc.RpcError as error:
        if error.code() != grpc.StatusCode.UNAUTHENTICATED:
            raise SystemExit(f"Agent gRPC no-token probe returned {error.code()}")
    else:
        raise SystemExit("Agent gRPC accepted a request without its internal token")
finally:
    channel.close()
PY
  echo "✓ Agent gRPC 内部 token 鉴权拒绝无凭据请求"
else
  echo "ℹ Agent 未启用；只检查 legacy :$LEGACY_PORT，不要求 DSN 或 10004。"
fi
