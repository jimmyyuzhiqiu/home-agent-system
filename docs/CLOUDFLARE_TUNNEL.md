# Cloudflare Tunnel

## Goal

把 Home Agent 暴露到公网 HTTPS 域名，同时保持应用继续运行在本机 Docker 中。

## Local Service

先本地启动：

```bash
docker compose up -d --build
```

本地入口：

- `http://localhost:8088`

## Tunnel Target

Cloudflare Tunnel 指向：

```text
http://localhost:8088
```

不要直接指向容器内部端口，只指向宿主机已经暴露的 `8088`。

## Public Base URL

建议在 `.env` 里显式配置：

```env
HOME_AGENT_PUBLIC_BASE_URL=https://你的域名
```

然后重启：

```bash
docker compose up -d --build
```

## Setup Path

首次部署访问：

- `https://你的域名/setup`

完成：

1. 创建管理员
2. 配置 Provider
3. 完成首启

## OAuth

在 Cloudflare Tunnel 场景下，OAuth 更适合启用，因为满足：

- HTTPS
- 固定公网域名

如果你只想最稳妥地完成部署，仍然优先使用 API Key。

## Security

建议：

- 首启完成后不要长期裸露 `/setup`
- 使用强密码和强 `SECRET_KEY`
- 最好给域名再加一层 Cloudflare Access

## Troubleshooting

如果域名能打开但 OAuth 不显示，优先检查：

- `HOME_AGENT_PUBLIC_BASE_URL` 是否为 HTTPS 公网域名
- Tunnel 是否正确传递了外部域名
- 当前访问是否真的是域名，而不是 `localhost`
