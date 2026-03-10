#!/usr/bin/env python3
import json
import os
import sys
import urllib.error
import urllib.request
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


def fetch(url: str):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=4) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def main():
    load_env_file(PROJECT_ROOT / ".env")
    load_env_file(PROJECT_ROOT / ".env.runtime")
    port = os.getenv("HOME_AGENT_BRIDGE_PORT", "18888").strip() or "18888"
    host = os.getenv("HOME_AGENT_BRIDGE_HOST", "127.0.0.1").strip() or "127.0.0.1"
    url = f"http://{host}:{port}/health"
    try:
        status, body = fetch(url)
        data = json.loads(body)
    except urllib.error.URLError as exc:
        print("[error] 宿主机执行桥不可达")
        print(f"        URL={url}")
        print(f"        详情: {exc}")
        return 2
    except Exception as exc:
        print("[error] 宿主机执行桥返回异常")
        print(f"        URL={url}")
        print(f"        详情: {exc}")
        return 2

    if status == 200 and data.get("ok") and data.get("openclaw_available"):
        print(f"[ok] 执行桥可用: {url}")
        return 0

    print("[warn] 执行桥在线，但 OpenClaw 尚未就绪")
    print(f"       URL={url}")
    print(f"       detail={data.get('detail')}")
    print(f"       reason={data.get('reason')}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
