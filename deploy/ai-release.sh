#!/usr/bin/env bash
# AI 服务发布（在 Jenkins workspace 里执行）
#
#   rsync 覆盖代码（不碰 venv）→ 依赖变了才 pip install → 重启 → 探活 10003
#
# 2C2G 前提：REQ 用 requirements-minimal.txt（不含 torch / sentence-transformers），
# 并在设置页把 embedding 切成 api 模式。装了 torch 这台机扛不住。
set -euo pipefail

MIRROR_HOME="${MIRROR_HOME:-/opt/mirror}"
APP_DIR="${AI_APP_DIR:-$MIRROR_HOME/ai/app}"
VENV="${AI_VENV:-$MIRROR_HOME/ai/venv}"
SERVICE="${AI_SERVICE:-mirror-ai}"
PORT="${AI_PORT:-10003}"
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

# 探活：gRPC 端口可连即视为就绪（B 侧对 AI 掉线是降级不炸，重启窗口只影响 AI 功能）
for _ in $(seq 1 20); do
  if timeout 2 bash -c "</dev/tcp/127.0.0.1/$PORT" 2>/dev/null; then
    echo "✓ AI 服务就绪（:$PORT）"
    exit 0
  fi
  sleep 2
done

echo "✗ AI 探活失败：tail -100 $MIRROR_HOME/shared/logs/ai.err.log" >&2
exit 1
