#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p .runtime

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  [ -f .env.runtime ] && source .env.runtime || true
  set +a
fi

PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
  if [ -x "./.venv/bin/python" ]; then
    PYTHON_BIN="./.venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

BRIDGE_HOST="${HOME_AGENT_BRIDGE_HOST:-127.0.0.1}"
BRIDGE_PORT="${HOME_AGENT_BRIDGE_PORT:-18888}"
BRIDGE_URL="http://${BRIDGE_HOST}:${BRIDGE_PORT}/health"
PID_FILE=".runtime/home-agent-host-bridge.pid"
LOG_FILE=".runtime/home-agent-host-bridge.log"

if [ -f "$PID_FILE" ]; then
  EXISTING_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$EXISTING_PID" ] && ps -p "$EXISTING_PID" >/dev/null 2>&1; then
    if curl -fsS "$BRIDGE_URL" >/dev/null 2>&1; then
      echo "[ok] 宿主机执行桥已在线: $BRIDGE_URL"
      exit 0
    fi
  fi
fi

if curl -fsS "$BRIDGE_URL" >/dev/null 2>&1; then
  echo "[ok] 宿主机执行桥已在线: $BRIDGE_URL"
  exit 0
fi

nohup "$PYTHON_BIN" -m host_bridge.app </dev/null >"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"

if command -v disown >/dev/null 2>&1; then
  disown || true
fi

for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 1
  if curl -fsS "$BRIDGE_URL" >/dev/null 2>&1; then
    echo "[ok] 宿主机执行桥已启动: $BRIDGE_URL"
    exit 0
  fi
done

echo "[warn] 宿主机执行桥未成功启动，请查看 $LOG_FILE"
exit 1
