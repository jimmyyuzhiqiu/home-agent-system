# Windows Deployment

## Prerequisites

- Windows 10/11
- Docker Desktop
- Git

## Deploy

```bash
git clone https://github.com/jimmyyuzhiqiu/home-agent-system.git
cd home-agent-system
docker compose up -d --build
```

访问：

- `http://localhost:8088/setup`

然后在浏览器里完成：

1. 创建管理员
2. 配置 OpenAI API Key
3. 完成首启

## Recommended `.env`

如果还没有 `.env`，先复制：

```bash
copy .env.example .env
```

最少建议改这两项：

```env
SECRET_KEY=请改成高强度随机值
HOME_AGENT_PUBLIC_BASE_URL=http://localhost:8088
```

如果你后面接域名，再把 `HOME_AGENT_PUBLIC_BASE_URL` 改成公网 HTTPS 地址。

## Common Commands

查看状态：

```bash
docker compose ps
```

查看日志：

```bash
docker compose logs -f web
docker compose logs -f runtime
docker compose logs -f nginx
```

重建：

```bash
docker compose up -d --build
```

停止：

```bash
docker compose down
```

## Notes

- 本地 `localhost` 场景优先使用 API Key
- OAuth 更适合有 HTTPS 域名时使用
- BlueBubbles 在纯 Docker 主线里默认不启用
