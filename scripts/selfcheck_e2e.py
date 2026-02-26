#!/usr/bin/env python3
import os
import re
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
BASE = os.getenv("SELFTEST_BASE_URL", "http://localhost:8088")


def load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def get_csrf(html: str):
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    if not m:
        raise RuntimeError("未找到 csrf_token")
    return m.group(1)


def login(session: requests.Session, username: str, password: str):
    r = session.get(f"{BASE}/login", timeout=15)
    r.raise_for_status()
    csrf = get_csrf(r.text)
    r = session.post(f"{BASE}/login", data={"csrf_token": csrf, "username": username, "password": password}, allow_redirects=True, timeout=15)
    r.raise_for_status()
    return r


def complete_onboarding_if_needed(session: requests.Session, html: str):
    if "/onboarding" not in html and "新手引导" not in html:
        return
    page = session.get(f"{BASE}/onboarding", timeout=15)
    csrf = get_csrf(page.text)
    r = session.post(f"{BASE}/onboarding/complete", data={"csrf_token": csrf}, allow_redirects=True, timeout=15)
    r.raise_for_status()


def send_chat(session: requests.Session, message: str):
    page = session.get(f"{BASE}/chat", timeout=20)
    page.raise_for_status()
    csrf = get_csrf(page.text)
    r = session.post(f"{BASE}/chat", data={"csrf_token": csrf, "message": message}, allow_redirects=True, timeout=120)
    r.raise_for_status()
    return r.text


def ensure_admin_ready(session: requests.Session, admin_user: str, admin_pwd: str):
    candidates = [admin_pwd, admin_pwd + "_X", admin_pwd + "_X_X"]
    r = None
    used = None
    for p in candidates:
        rr = login(session, admin_user, p)
        if "/login" not in rr.url:
            r = rr
            used = p
            break
    if r is None:
        raise RuntimeError("管理员登录失败，请检查 ADMIN_PASSWORD")

    if "/security/setup" in r.url:
        csrf = get_csrf(r.text)
        new_pwd = used + "_X"
        session.post(
            f"{BASE}/security/setup",
            data={"csrf_token": csrf, "action": "change_password", "old_password": used, "new_password": new_pwd, "confirm_password": new_pwd},
            allow_redirects=True,
            timeout=15,
        ).raise_for_status()
        return new_pwd
    return used


def create_user(session: requests.Session, username: str, password: str):
    page = session.get(f"{BASE}/admin/users", timeout=15)
    page.raise_for_status()
    csrf = get_csrf(page.text)
    r = session.post(
        f"{BASE}/admin/users",
        data={"csrf_token": csrf, "action": "create", "username": username, "password": password, "role": "member"},
        allow_redirects=True,
        timeout=15,
    )
    if username not in r.text and "创建成功" not in r.text:
        raise RuntimeError("创建测试用户失败")


def main():
    load_env(ROOT / ".env")
    admin_user = os.getenv("ADMIN_USERNAME", "Jimmy")
    admin_pwd = os.getenv("ADMIN_PASSWORD", "Jimmy11a@123")
    evidence = []

    test_user = f"ut_{uuid.uuid4().hex[:6]}"
    pwd = "User12345!"

    with requests.Session() as s_admin:
      admin_pwd = ensure_admin_ready(s_admin, admin_user, admin_pwd)
      create_user(s_admin, test_user, pwd)
      evidence.append(f"新建测试用户: {test_user}")

    with requests.Session() as s_user:
      r = login(s_user, test_user, pwd)
      complete_onboarding_if_needed(s_user, r.text)
      html = send_chat(s_user, "我是产品经理，我喜欢极简风格。请帮我做今天任务计划")

      required_texts = ["助手状态透明面板", "Known（已知信息）", "Plan（计划任务）", "Doing（执行状态）", "Memory（最近记忆变更）"]
      for t in required_texts:
          if t not in html:
              raise RuntimeError(f"聊天页缺少区块: {t}")
      if "status-done" not in html and "done" not in html:
          raise RuntimeError("未命中执行状态内容")
      evidence.append("聊天页命中 Known/Plan/Doing/Memory 区块")

      if "Auto" not in html or "Light" not in html or "Dark" not in html:
          raise RuntimeError("主题切换选项缺失")
      if "themeMode" not in html or "data-theme" not in html:
          raise RuntimeError("自动主题脚本缺失")
      evidence.append("自动主题: Auto/Light/Dark 与本地存储脚本命中")

    with requests.Session() as s_admin2:
      login(s_admin2, admin_user, admin_pwd)
      agents = s_admin2.get(f"{BASE}/admin/agents", timeout=20)
      agents.raise_for_status()
      if test_user not in agents.text or "Plan" not in agents.text or "Doing" not in agents.text or "Result" not in agents.text:
          raise RuntimeError("管理员审计页缺少 Plan/Doing/Result 摘要")
      evidence.append("管理员审计增强: 可见每用户 Plan/Doing/Result 摘要")

    out = ROOT / "selfcheck-evidence.log"
    out.write_text("\n".join(evidence) + "\n", encoding="utf-8")
    print("\n".join(evidence))
    print(f"证据文件: {out}")


if __name__ == "__main__":
    main()
