# 家庭多用户 AI Agent 系统（可直接交付版）

> 目标：无需外部 API key，直接复用本机 OpenClaw 登录态；默认走 **OpenClaw CLI Bridge**，网关 HTTP 为可选优先路径。

## 架构与技术选型

- 架构说明：见 `ARCHITECTURE.md`
- MVP 形态：Flask 单体（前后端一体）+ SQLite + 可选 Nginx
- Provider 路由：Gateway HTTP 优先，CLI Bridge 兜底

## 一、开箱即用步骤（首次）

```bash
cd /Users/jimmy/Desktop/home-agent-system
chmod +x scripts/*.sh
./scripts/init.sh
```

完成后访问：`http://localhost:8088`

### 默认管理员账号
- 用户名：`Jimmy`
- 密码：`Jimmy11a@123`

> 首次登录会进入“安全中心”要求修改默认管理员密码（系统强制）。

---

## 二、日常启动

```bash
cd /Users/jimmy/Desktop/home-agent-system
./scripts/start.sh
```

---

## 三、管理员新增用户

1. 管理员登录后进入 **用户管理**。
2. 填写用户名、初始密码、角色（user/admin）。
3. 可选填写“用户专属 OpenClaw Token”（会覆盖全局 token）。
4. 点击创建。

权限规则：
- **admin**：可访问 用户管理 / 聊天审计 / 记忆审计 / Agent审计 / 会话审计 / 安全中心。
- **user**：只能看到自己的聊天、附件、以及“自己的 Agent 信息”。（兼容旧数据中的 `member`，会自动迁移为 `user`）

---

## 四、Provider 路由（默认 CLI Bridge，网关可选）

后端调用顺序：
1. **优先 Gateway HTTP**（`/api/chat` -> `/v1/chat/completions` -> `/v1/responses`）
2. **自动回退 OpenClaw CLI Bridge（默认可用通道）**
   - 使用每个用户会话的 `session_key`
   - 执行：`openclaw agent --session-id <session_key> --message <text> --json`
   - 解析 JSON 中 `result.payloads[].text` 作为 assistant 回复

因此即便网关某接口（例如 `/v1/responses`）返回 405，系统也能自动切到 CLI 通道继续可用。

`init/start` 会自动执行：
1. 读取宿主机 `~/.openclaw/openclaw.json`
2. 提取 `gateway.port` 与 `gateway.auth.token`
3. 生成项目运行时配置 `.env.runtime`（不入库）
4. 启动前执行连通性检测（仅用于网关优先路径）

聊天页会显示当前网关探测信息；即便显示未连接，CLI Bridge 仍可作为默认兜底通道。

---

## 四点一、双Agent默认能力（planner + worker）

系统为**每个用户默认创建双Agent绑定**（`user_agent_binding`）：
- `planner_agent_id` / `planner_session_key`
- `worker_agent_id` / `worker_session_key`

执行流程固定为一次指令闭环：
1. planner 拆解计划
2. worker 执行计划
3. planner 验证并汇总最终成品

对外仍保持单聊天入口，用户无感知内部往返。

可视化入口：
- 用户聊天页：显示“本次由 planner/worker 协作完成”与最后执行摘要
- 管理员 `聊天审计` 页：可见是否触发双Agent流程及摘要
- 管理员 `Agent审计` 页：可见所有用户 planner/worker 双绑定

---

## 五、安全加固（已内置）

- 登录限流（防爆破基础版）：
  - 同用户名+IP 连续失败达到阈值后临时锁定。
- 强制更换默认管理员密码：
  - 初始密码或被重置后，下次登录必须改密。
- SECRET_KEY 弱值检测：
  - 安全中心提示并引导替换弱 SECRET_KEY。
- 会话过期机制：
  - 默认 120 分钟（`SESSION_EXPIRE_MINUTES` 可配置）。
- CSRF 基础防护：
  - 全站表单启用 CSRF Token（Flask-WTF）。
- 上传限制：
  - 默认 20MB（`MAX_UPLOAD_MB`），并限制扩展名白名单。

---

## 六、常见故障自检

### 1) 网页打不开
```bash
docker compose ps
```
确认 `home-agent-app`、`home-agent-nginx` 均为 `Up`。

### 2) 登录失败/被限流
- 检查用户名密码是否正确。
- 若提示限流，等待锁定时间结束再试。
- 强制重置管理员：
```bash
docker compose exec -T app python init_admin.py
```

### 3) OpenClaw 回复失败
- 在聊天页查看“网关状态”和“健康检查细节”。
- 检查 `.env`：
```env
OPENCLAW_BASE_URL=http://host.docker.internal:3333
OPENCLAW_GATEWAY_TOKEN=你的token
```
- 宿主机确认网关是否在 3333 端口监听。

### 4) 上传失败
- 检查文件类型是否在白名单。
- 检查文件大小是否超过 `MAX_UPLOAD_MB`。
- Nginx 侧限制为 25MB（`nginx/default.conf`）。

---

## 七、启动方式

### A. 推荐（确保 CLI Bridge 可用）：宿主机直跑 app

```bash
cd /Users/jimmy/Desktop/home-agent-system/app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd ..
export $(grep -v '^#' .env | xargs)
python3 app/init_admin.py
python3 app/app.py
```

访问：`http://localhost:8000`

> 该模式下 app 与 `openclaw` CLI 同在宿主机，CLI Bridge 可直接调用。

### B. Docker 模式（网关优先路径）

- 对外端口：`8088 -> nginx:80 -> app:8000`
- `app` 启用健康检查 `/healthz`
- `nginx` 依赖 app 健康后再启动，减少冷启动 502

---

## 八、关键环境变量

`.env` 示例：

```env
SECRET_KEY=请使用高强度随机值
DATABASE_URL=sqlite:////data/app.db
OPENCLAW_BASE_URL=http://host.docker.internal:3333
OPENCLAW_GATEWAY_TOKEN=
ADMIN_USERNAME=Jimmy
ADMIN_PASSWORD=Jimmy11a@123
SESSION_EXPIRE_MINUTES=120
MAX_UPLOAD_MB=20
```
