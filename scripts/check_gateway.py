#!/usr/bin/env python3
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path):
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip()


def fetch(url: str, token: str):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=4) as resp:
        return resp.status


def probe(base_url: str, token: str):
    checks = ["/health", "/api/health", "/v1/models", "/"]
    last_err = ""
    for p in checks:
        try:
            code = fetch(base_url + p, token)
            if code < 500:
                return True, f"{base_url}{p} -> {code}"
            last_err = f"HTTP {code}"
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return True, f"{base_url}{p} -> {e.code}"
            last_err = f"HTTP {e.code}"
        except Exception as e:
            last_err = str(e)
    return False, last_err


def main():
    load_env_file(PROJECT_ROOT / ".env")
    load_env_file(PROJECT_ROOT / ".env.runtime")

    base_url = os.getenv("OPENCLAW_BASE_URL", "http://host.docker.internal:3333").rstrip("/")
    token = os.getenv("OPENCLAW_GATEWAY_TOKEN", "").strip()

    ok, detail = probe(base_url, token)
    if ok:
        print(f"[ok] 网关可达: {detail}")
        return 0

    # 在宿主机执行脚本时，host.docker.internal 可能不可解析；补充本机回环探测
    local_probe = base_url.replace("host.docker.internal", "127.0.0.1")
    if local_probe != base_url:
        ok2, detail2 = probe(local_probe, token)
        if ok2:
            print(f"[ok] 网关本机可达: {detail2}")
            print("[info] host.docker.internal 仅用于容器内访问，已通过本机回环地址确认网关在线")
            return 0

    last_err = detail
    print("[error] 网关连通性检查失败")
    print(f"        BASE_URL={base_url}")
    print(f"        详情: {last_err}")
    print("\n可执行修复：")
    print("1) 检查宿主机网关是否已启动：openclaw gateway status")
    print("2) 若未启动：openclaw gateway start")
    print("3) 重新同步配置并重启项目：./scripts/start.sh")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
