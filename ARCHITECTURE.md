# Architecture

## Overview

当前默认架构已经切换为纯 Docker：

- `web`: Flask + Jinja 的 Home Agent 应用
- `runtime`: 容器内 OpenClaw Runtime
- `nginx`: 对外入口

默认访问路径：

- 本地：`http://localhost:8088`
- 首启：`http://localhost:8088/setup`

如果挂了 HTTPS 域名或 Cloudflare Tunnel：

- `https://你的域名/setup`

## Runtime Model

系统默认不再依赖宿主机 `start.sh`、宿主机 `openclaw` CLI、或宿主机 `~/.openclaw` 登录态。

当前主路径为：

1. 浏览器进入 `/setup`
2. 创建管理员
3. 通过浏览器配置 Provider
4. `runtime` 容器内负责：
   - OpenClaw
   - Provider 探测
   - 搜索 / 抓取 / 浏览器运行时
   - 文件处理
   - planner / worker / verify 执行

## Provider Flow

Provider 由管理员统一配置，全站共用。

支持两种方式：

- API Key
- OAuth

规则：

- API Key 是本地与远端部署都稳定可用的主路径
- OAuth 仅在 HTTPS 固定域名下作为增强路径显示
- `localhost` / `127.0.0.1` 不作为 OAuth 主路径

## User Isolation

每个家庭用户都有独立：

- 会话
- 记忆
- planner / worker binding
- workspace / namespace

管理员统一拥有：

- 全站审计权限
- Runtime 与 Provider 管理权限
- 用户管理权限

## Data

默认持久化卷：

- `home_agent_app_data`
- `home_agent_app_uploads`
- `home_agent_openclaw_state`
- `home_agent_runtime_workspaces`

因此重启容器后，以下内容默认保留：

- 管理员账号
- Provider 状态
- Runtime 状态
- 会话
- 记忆
- 上传文件

## Public Entry

公网或内网统一只需要暴露 `nginx`：

- `8088 -> nginx -> web`

如果配 Tunnel 或反代：

- 外部域名 -> `localhost:8088`

## Legacy Compatibility

仓库里仍保留以下遗留兼容代码：

- `host_bridge/`
- `scripts/start_host_bridge.sh`
- `scripts/check_bridge.py`
- `scripts/sync_openclaw_gateway.py`

它们不是当前默认主线，只用于迁移旧部署或排障。
