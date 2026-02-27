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
  cat > .env <<EOF
SECRET_KEY=${SECRET}
DATABASE_URL=sqlite:////data/app.db
OPENCLAW_BASE_URL=http://host.docker.internal:3333
OPENCLAW_GATEWAY_TOKEN=
ADMIN_USERNAME=Jimmy
ADMIN_PASSWORD=Jimmy11a@123
SESSION_EXPIRE_MINUTES=120
MAX_UPLOAD_MB=20
EOF
  echo "已生成 .env（含随机 SECRET_KEY）"
fi

python3 scripts/sync_openclaw_gateway.py || echo "[warn] 未同步到 OpenClaw 网关配置，继续启动（CLI Bridge 仍可用）"
[ -f .env.runtime ] || printf "OPENCLAW_BASE_URL=http://host.docker.internal:3333\nOPENCLAW_GATEWAY_TOKEN=\n" > .env.runtime
python3 scripts/check_gateway.py || echo "[warn] 网关连通性检查失败，继续启动（CLI Bridge 兜底）"

docker compose up -d --build

docker compose exec -T app python init_admin.py
docker compose exec -T app python init_user_isolation.py

echo "初始化完成（含用户隔离目录/命名空间初始化）。访问: http://localhost:8088"
