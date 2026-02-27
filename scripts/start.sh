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

python3 scripts/sync_openclaw_gateway.py || echo "[warn] 未同步到 OpenClaw 网关配置，继续启动（CLI Bridge 仍可用）"
[ -f .env.runtime ] || printf "OPENCLAW_BASE_URL=http://host.docker.internal:3333\nOPENCLAW_GATEWAY_TOKEN=\n" > .env.runtime
python3 scripts/check_gateway.py || echo "[warn] 网关连通性检查失败，继续启动（CLI Bridge 兜底）"

docker compose up -d --build
docker compose exec -T app python init_admin.py

echo "服务已启动: http://localhost:8088"
