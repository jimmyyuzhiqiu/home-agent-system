#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

python3 scripts/sync_openclaw_gateway.py
python3 scripts/check_gateway.py

docker compose up -d --build
docker compose exec -T app python init_admin.py

echo "服务已启动: http://localhost:8088"
