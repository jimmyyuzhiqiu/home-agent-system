#!/usr/bin/env python3
import json
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=45,
    )


def compose_json(*args: str):
    proc = run(*args)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or "docker compose 命令失败")
    raw = (proc.stdout or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        return json.loads(raw)
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def main() -> int:
    try:
        services = compose_json("ps", "--format", "json")
    except Exception as exc:
        print("[error] 无法读取 Docker Compose 服务状态")
        print(f"        详情: {exc}")
        return 2

    service_map = {item.get("Service"): item for item in services}
    missing = [name for name in ("web", "runtime", "nginx") if name not in service_map]
    if missing:
        print("[error] 纯 Docker 主拓扑未完全启动")
        print(f"        缺少服务: {', '.join(missing)}")
        print("可执行修复：./scripts/start.sh")
        return 2

    unhealthy = [name for name in ("web", "runtime") if service_map[name].get("Health") not in {"healthy", ""}]
    if unhealthy:
        print("[error] 关键服务未健康")
        for name in unhealthy:
            print(f"        {name}: {service_map[name].get('State')} / {service_map[name].get('Health')}")
        return 2

    runtime_probe = run(
        "exec",
        "-T",
        "runtime",
        "python3",
        "-c",
        (
            "import json, urllib.request; "
            "resp = urllib.request.urlopen('http://127.0.0.1:18888/health', timeout=5); "
            "print(resp.read().decode('utf-8'))"
        ),
    )
    if runtime_probe.returncode != 0:
        print("[error] Runtime Provider 状态检查失败")
        print(f"        详情: {(runtime_probe.stderr or runtime_probe.stdout).strip()}")
        return 2

    try:
        payload = json.loads(runtime_probe.stdout or "{}")
    except Exception as exc:
        print("[error] Runtime 返回了无效的 JSON")
        print(f"        详情: {exc}")
        return 2

    provider_ready = bool(payload.get("provider_ready"))
    status = "ready" if payload.get("chat_ready") else "pending"
    default_model = payload.get("default_model") or "-"
    print(f"[ok] Runtime 在线: status={status} model={default_model}")
    if provider_ready:
        print("[ok] Provider 已就绪，聊天入口可用")
        return 0
    print("[warn] Provider 尚未配置完成，浏览器内发送会被禁用")
    print("       请访问 http://localhost:8088/setup 完成 API Key 或 OAuth 配置")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
