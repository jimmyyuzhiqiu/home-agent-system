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

docker compose up -d --build --remove-orphans

echo
docker compose ps

echo "服务已启动: http://localhost:8088"
echo "默认部署已切换为 web + runtime + nginx，首次部署请直接在浏览器完成 /setup。"
