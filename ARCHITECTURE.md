# Architecture & Tech Stack (MVP)

## 1) System Architecture

- **Frontend + API**: Flask monolith (`app/app.py`)
- **DB**: SQLite (`data/app.db` via `DATABASE_URL`)
- **Reverse Proxy**: Nginx (`nginx/default.conf`, optional in Docker mode)
- **Agent Provider Path**:
  1. OpenClaw Gateway HTTP (preferred when available)
  2. OpenClaw CLI Bridge fallback (`openclaw agent --session-id ... --json`)

## 2) Core Modules

- `User/Auth`: Flask-Login + password hash (Werkzeug)
- `Conversation/Message`: multi-user conversation persistence
- `UserAgentBinding`: per-user planner/worker dual-agent binding
- `TaskRunAudit`: planner -> worker -> planner execution audit trail
- `Security`: CSRF, login rate limit, forced default-password change, upload whitelist/size limit

## 3) Tech Selection

- **Python + Flask**: fastest MVP delivery, low ops complexity
- **SQLite**: zero-dependency local persistence for single-host deployment
- **Nginx + Docker Compose**: simple external access and service orchestration
- **OpenClaw CLI fallback**: avoids hard dependency on gateway API compatibility, improves availability

## 4) Minimal Project Skeleton

- `app/` application code + templates/static/uploads
- `data/` runtime sqlite and uploaded files
- `nginx/` reverse proxy config
- `scripts/` init/start/self-check scripts
- `docker-compose.yml` local deployment
- `README.md` runbook and operations guide

## 5) Startup Command (MVP)

```bash
./.venv/bin/python -c 'from app.app import app; app.run(host="127.0.0.1", port=8010)'
```

Health check:

```bash
curl http://127.0.0.1:8010/healthz
# => {"ok":true,"service":"home-agent-app"}
```
