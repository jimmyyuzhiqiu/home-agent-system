#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null 2>&1; then
  echo "[error] 缺少 docker，请先安装 Docker Desktop"
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "[error] Docker 未运行，请先启动 Docker Desktop/daemon"
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "[error] 当前 Docker 不支持 'docker compose' 子命令"
  exit 1
fi

if [ ! -f .env ]; then
  SECRET=$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(36))
PY
)
  BRIDGE_SECRET=$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(36))
PY
)
  cat > .env <<EOF
SECRET_KEY=${SECRET}
DATABASE_URL=sqlite:////data/app.db
OPENCLAW_BASE_URL=http://runtime:3333
HOME_AGENT_BRIDGE_URL=http://runtime:18888
HOME_AGENT_BRIDGE_PORT=18888
HOME_AGENT_BRIDGE_SHARED_SECRET=${BRIDGE_SECRET}
HOME_AGENT_PUBLIC_BASE_URL=http://127.0.0.1:8088
HOME_AGENT_RUNTIME_WORKROOT=/runtime/workspaces/users
OPENCLAW_RUNTIME_PROFILE=runtime
SESSION_EXPIRE_MINUTES=120
MAX_UPLOAD_MB=20
EOF
  echo "已生成 .env（含随机 SECRET_KEY）"
fi

docker compose up -d --build --remove-orphans
echo "初始化完成。访问: http://localhost:8088 ，首次部署请在浏览器完成 /setup。"
