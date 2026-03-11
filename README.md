# Home Agent System

面向家庭多用户场景的 AI Agent 工作台。

当前主线已经切换为：

- 纯 Docker 部署
- 浏览器内完成首启
- 容器内 Runtime 执行 OpenClaw / 浏览器 / 文件处理
- 支持在浏览器里配置 OpenAI API Key
- 支持在 HTTPS 固定域名下发起 OpenAI OAuth

不再以“复用宿主机 OpenClaw 登录态”作为默认方案。

## 当前架构

默认使用 3 个服务：

- `web`: Flask + Jinja 的 Home Agent 应用
- `runtime`: 容器内 OpenClaw Runtime Bridge
- `nginx`: 对外入口，默认暴露 `8088`

默认入口：

- 本地访问: `http://localhost:8088`
- 首启向导: `http://localhost:8088/setup`

## 一键启动

```bash
docker compose up -d --build
```

启动后访问：

- `http://localhost:8088/setup`

然后在浏览器里完成 3 步：

1. 创建管理员账号
2. 配置 Provider
3. 选择默认模型并完成首启

## Provider 配置

### 1. API Key

这是本地和远端部署都稳定可用的主路径。

在 `/setup` 或后台 `Runtime 与 Provider` 页面中：

- 输入 OpenAI API Key
- Runtime 会在容器内同步配置
- 同步后立即做 probe 检查
- 就绪后才能完成首启并开始聊天

### 2. OAuth

浏览器内 OAuth 已经接好，但有前提：

- 访问地址必须是 HTTPS
- 域名不能是 `localhost` 或 `127.0.0.1`

因此：

- 本地调试：优先用 API Key
- 正式部署 + 域名：可以在浏览器里发起 OpenAI OAuth

## Cloudflare Tunnel

可以直接用。

推荐做法：

1. 先拉起 Docker

```bash
docker compose up -d --build
```

2. 把 Cloudflare Tunnel 指到：

```text
http://localhost:8088
```

3. 用你的域名访问：

```text
https://你的域名/setup
```

4. 在浏览器里完成管理员创建和 Provider 配置

建议在 `.env` 里显式设置公网地址：

```env
HOME_AGENT_PUBLIC_BASE_URL=https://你的域名
```

然后重启：

```bash
docker compose up -d --build
```

虽然系统现在支持根据反向代理头自动识别外部域名，但生产环境仍然建议显式配置。

## Windows 部署

Windows 上推荐直接使用 Docker Desktop。

```bash
git clone https://github.com/jimmyyuzhiqiu/home-agent-system.git
cd home-agent-system
docker compose up -d --build
```

然后访问：

- `http://localhost:8088/setup`

如果你配了 Cloudflare Tunnel，就直接访问你的 HTTPS 域名。

## 当前能力

- 多用户账号
- 用户独立会话
- 用户独立记忆
- 用户独立 planner / worker 绑定
- 运行事件与产物记录
- 用户前台简版状态
- 管理员后台审计
- 容器内文件处理
- 容器内搜索 / 抓取 / 浏览器运行时

## 当前限制

- 纯 Docker v1 默认不启用 BlueBubbles 实发
- 本地 `localhost` 访问时不显示 OAuth 主路径
- 如果 Provider 未就绪，聊天发送会被禁用

## 常用命令

查看服务状态：

```bash
docker compose ps
```

查看日志：

```bash
docker compose logs -f web
docker compose logs -f runtime
docker compose logs -f nginx
```

检查 Runtime 状态：

```bash
python3 scripts/check_gateway.py
```

## 数据卷

默认会保留以下卷：

- `home_agent_app_data`
- `home_agent_app_uploads`
- `home_agent_openclaw_state`
- `home_agent_runtime_workspaces`

因此重启 Docker 后：

- 管理员账号
- Provider 状态
- OpenClaw Runtime 状态
- 会话
- 记忆
- 上传文件

都会保留。

## 旧架构说明

仓库里仍保留了一些旧文件，例如宿主机 `host_bridge` 相关代码，用于迁移兼容。

但当前默认主线不是：

- 宿主机 `start.sh` 共享 OpenClaw
- 复用本机 `~/.openclaw` 登录态
- CLI Bridge 作为默认运行路径

当前默认主线是：

- 纯 Docker
- 容器内 Runtime
- 浏览器内首启
- 浏览器内 Provider 配置

## 安全建议

- 生产环境务必修改 `SECRET_KEY`
- 管理员密码使用强密码
- 公网部署建议配合 Cloudflare Access 或至少限制 `/setup` 暴露窗口
- 首启完成后，普通用户不能再替代管理员完成初始化

## 结论

如果你现在的目标是：

- 只拉起 Docker
- 打开浏览器
- 登录 / 配置 OpenAI
- 让家人直接通过网页使用

那么当前代码主线就是按这个方向做的。

如果你看到旧文案说“复用本机 OpenClaw 登录态”或“默认 CLI Bridge”，那是旧 README，没有跟上代码。
