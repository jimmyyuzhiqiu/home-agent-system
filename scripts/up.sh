#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "[error] 缺少依赖: $1"
    exit 1
  fi
}

need_cmd docker
need_cmd python3

if ! docker info >/dev/null 2>&1; then
  echo "[error] Docker 未运行，请先启动 Docker Desktop/daemon"
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "[error] 当前 Docker 不支持 'docker compose' 子命令"
  exit 1
fi

if [ ! -f .env ]; then
  echo "[info] 检测到首次启动，执行初始化..."
  ./scripts/init.sh
else
  ./scripts/start.sh
fi
