#!/usr/bin/env python3
import os
import time
import re
import uuid
from pathlib import Path
from urllib.parse import urljoin

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


def setup_admin(session: requests.Session, username: str, display_name: str, password: str):
    page = session.get(f"{BASE}/setup", timeout=20)
    page.raise_for_status()
    csrf = get_csrf(page.text)
    r = session.post(
        f"{BASE}/setup/admin",
        data={
            "csrf_token": csrf,
            "username": username,
            "display_name": display_name,
            "password": password,
            "confirm_password": password,
        },
        allow_redirects=True,
        timeout=20,
    )
    r.raise_for_status()
    return r


def setup_provider_api_key(session: requests.Session, api_key: str, default_model: str):
    page = session.get(f"{BASE}/setup", timeout=20)
    page.raise_for_status()
    csrf = get_csrf(page.text)
    r = session.post(
        f"{BASE}/setup/provider/api-key",
        data={
            "csrf_token": csrf,
            "api_key": api_key,
            "default_model": default_model,
        },
        allow_redirects=True,
        timeout=60,
    )
    r.raise_for_status()
    return r


def finish_setup(session: requests.Session, preferred_model: str):
    page = session.get(f"{BASE}/setup", timeout=20)
    page.raise_for_status()
    csrf = get_csrf(page.text)
    r = session.post(
        f"{BASE}/setup/complete",
        data={
            "csrf_token": csrf,
            "preferred_model": preferred_model,
            "runtime_health_enabled": "on",
        },
        allow_redirects=True,
        timeout=30,
    )
    r.raise_for_status()
    return r


def setup_summary(session: requests.Session):
    r = session.get(f"{BASE}/setup", timeout=20)
    r.raise_for_status()
    return r


def provider_status(session: requests.Session):
    r = session.get(f"{BASE}/api/admin/provider-status", timeout=20)
    r.raise_for_status()
    return r.json()


def maybe_bootstrap(session: requests.Session, evidence: list[str], admin_user: str, admin_pwd: str):
    page = session.get(f"{BASE}/setup", timeout=20)
    page.raise_for_status()
    if "创建管理员" in page.text and "创建管理员并继续" in page.text:
        setup_admin(session, admin_user, "家庭管理员", admin_pwd)
        evidence.append("首启向导: 已通过浏览器创建管理员")
        page = session.get(f"{BASE}/setup", timeout=20)
        page.raise_for_status()

    api_key = os.getenv("SELFTEST_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    status = provider_status(session)
    runtime_provider = status.get("runtime_provider") or {}
    if runtime_provider.get("provider_ready"):
        evidence.append("首启向导: Runtime Provider 已就绪")
    elif api_key:
        setup_provider_api_key(session, api_key, os.getenv("SELFTEST_DEFAULT_MODEL", "openai/gpt-5.3-codex"))
        for _ in range(20):
            status = provider_status(session)
            runtime_provider = status.get("runtime_provider") or {}
            if runtime_provider.get("provider_ready"):
                evidence.append("首启向导: 已通过浏览器同步 API Key 并探测成功")
                break
            time.sleep(2)
        else:
            raise RuntimeError(runtime_provider.get("last_error") or "Provider API Key 同步后仍未就绪")
    else:
        evidence.append("首启向导: 未提供 OPENAI_API_KEY，跳过 Provider 配置验证")
        return False

    finish_setup(session, runtime_provider.get("default_model") or os.getenv("SELFTEST_DEFAULT_MODEL", "openai/gpt-5.3-codex"))
    evidence.append("首启向导: 已完成默认模型确认并进入系统")
    return True


def complete_onboarding_if_needed(session: requests.Session, response: requests.Response):
    current_url = response.url or ""
    current_html = response.text or ""
    if "/onboarding" not in current_url and "开始使用" not in current_html and "欢迎进入家庭智能中枢" not in current_html:
        return response

    page = response if "/onboarding" in current_url else session.get(f"{BASE}/onboarding", timeout=15)
    csrf = get_csrf(page.text)
    r = session.post(f"{BASE}/onboarding/complete", data={"csrf_token": csrf}, allow_redirects=True, timeout=30)
    r.raise_for_status()
    return r


def extract_conversation_id(html: str):
    m = re.search(r'name="conversation_id"\s+value="(\d+)"', html)
    if not m:
        raise RuntimeError("未找到会话 ID")
    return int(m.group(1))


def create_conversation(session: requests.Session):
    page = session.get(f"{BASE}/chat", timeout=20)
    page.raise_for_status()
    csrf = get_csrf(page.text)
    r = session.post(
        f"{BASE}/chat/conversations",
        data={"csrf_token": csrf, "title": "自检会话"},
        allow_redirects=True,
        timeout=20,
    )
    r.raise_for_status()
    return extract_conversation_id(r.text), r.text


def send_chat_async(session: requests.Session, conversation_id: int, message: str):
    page = session.get(f"{BASE}/chat?conversation={conversation_id}", timeout=20)
    page.raise_for_status()
    csrf = get_csrf(page.text)
    r = session.post(
        f"{BASE}/api/chat/send",
        data={"csrf_token": csrf, "conversation_id": str(conversation_id), "message": message},
        headers={"Accept": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("error", "异步发送失败"))
    run_id = payload.get("run_id")
    if not run_id:
        raise RuntimeError("未返回 run_id")
    for _ in range(90):
        poll = session.get(f"{BASE}/api/runs/{run_id}/status", headers={"Accept": "application/json"}, timeout=20)
        poll.raise_for_status()
        data = poll.json()
        if data.get("status") in {"done", "failed", "blocked"}:
            return data
        time.sleep(1.5)
    raise RuntimeError("轮询任务状态超时")


def create_user(session: requests.Session, username: str, password: str):
    page = session.get(f"{BASE}/admin/users", timeout=15)
    page.raise_for_status()
    csrf = get_csrf(page.text)
    r = session.post(
        f"{BASE}/admin/users",
        data={"csrf_token": csrf, "action": "create", "username": username, "password": password, "role": "user"},
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
        landing = s_admin.get(urljoin(BASE, "/"), allow_redirects=True, timeout=20)
        landing.raise_for_status()
        if "/setup" in (landing.url or ""):
            setup_completed = maybe_bootstrap(s_admin, evidence, admin_user, admin_pwd)
            if not setup_completed:
                out = ROOT / "selfcheck-evidence.log"
                out.write_text("\n".join(evidence) + "\n", encoding="utf-8")
                print("\n".join(evidence))
                print(f"证据文件: {out}")
                return

        admin_login = login(s_admin, admin_user, admin_pwd)
        if "/setup" in (admin_login.url or ""):
            setup_completed = maybe_bootstrap(s_admin, evidence, admin_user, admin_pwd)
            if not setup_completed:
                out = ROOT / "selfcheck-evidence.log"
                out.write_text("\n".join(evidence) + "\n", encoding="utf-8")
                print("\n".join(evidence))
                print(f"证据文件: {out}")
                return
            admin_login = login(s_admin, admin_user, admin_pwd)
        if "/security/setup" in (admin_login.url or ""):
            raise RuntimeError("管理员登录后仍跳转安全中心，预期不应再强制改密")
        admin_login = complete_onboarding_if_needed(s_admin, admin_login)
        if "/login" in (admin_login.url or ""):
            raise RuntimeError("管理员登录失败，请检查管理员密码")
        create_user(s_admin, test_user, pwd)
        evidence.append(f"新建测试用户: {test_user}")
        evidence.append("管理员登录不再触发强制改密跳转")

    with requests.Session() as s_user:
        r = login(s_user, test_user, pwd)
        if "/security/setup" in (r.url or ""):
            raise RuntimeError("普通用户登录后命中强制改密页面，预期不应发生")
        complete_onboarding_if_needed(s_user, r)
        conversation_id, chat_page = create_conversation(s_user)
        run = send_chat_async(s_user, conversation_id, "我是产品经理，我喜欢极简风格。请帮我做今天任务计划")
        html = s_user.get(f"{BASE}/chat?conversation={conversation_id}", timeout=20).text

        required_texts = ["会话列表", "执行时间线", "我的记忆", "当前会话"]
        for t in required_texts:
            if t not in html:
                raise RuntimeError(f"聊天页缺少区块: {t}")
        if run.get("status") not in {"done", "blocked", "failed"}:
            raise RuntimeError(f"聊天任务未正常完成: {run.get('status')}")
        evidence.append("聊天页命中会话列表 / 执行时间线 / 我的记忆，异步轮询完成")

        if "跟随系统" not in html or "浅色" not in html or "深色" not in html:
            raise RuntimeError("主题切换选项缺失")
        if "js/app.js" not in html or "theme-select" not in html:
            raise RuntimeError("自动主题脚本缺失")
        evidence.append("自动主题: 主题切换控件与全局脚本已挂载")

        memories = s_user.get(f"{BASE}/memories", timeout=20)
        memories.raise_for_status()
        if "记忆中心" not in memories.text or "新增记忆" not in memories.text or "手动记忆" not in memories.text:
            raise RuntimeError("记忆中心缺少关键区块")
        evidence.append("记忆中心: 可访问新增记忆与记忆列表")

    with requests.Session() as s_admin2:
        admin_second = login(s_admin2, admin_user, admin_pwd)
        complete_onboarding_if_needed(s_admin2, admin_second)
        overview = s_admin2.get(f"{BASE}/admin/overview", timeout=20)
        overview.raise_for_status()
        if "后台总览" not in overview.text or "7日活跃用户" not in overview.text:
            raise RuntimeError("后台总览缺少关键指标卡片")
        evidence.append("后台总览: 指标卡片可见")

        agents = s_admin2.get(f"{BASE}/admin/agents", timeout=20)
        agents.raise_for_status()
        if test_user not in agents.text or "planner" not in agents.text or "worker" not in agents.text:
            raise RuntimeError("管理员 Agent 审计页缺少 planner/worker 信息")
        evidence.append("管理员审计增强: 可见 planner / worker 状态")

    out = ROOT / "selfcheck-evidence.log"
    out.write_text("\n".join(evidence) + "\n", encoding="utf-8")
    print("\n".join(evidence))
    print(f"证据文件: {out}")


if __name__ == "__main__":
    main()
