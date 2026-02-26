#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

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

python3 scripts/sync_openclaw_gateway.py
python3 scripts/check_gateway.py

docker compose up -d --build

docker compose exec -T app python init_admin.py

echo "初始化完成。访问: http://localhost:8088"
