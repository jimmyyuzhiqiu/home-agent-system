import os
import re
import uuid
import json
import secrets
import subprocess
from functools import wraps
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, abort, jsonify
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

WEAK_SECRET_KEYS = {"change-me", "replace_with_random_secret", "default_secret", "secret", "123456"}
DEFAULT_ADMIN_USERNAME = "Jimmy"
DEFAULT_ADMIN_PASSWORD = "Jimmy11a@123"
SYSTEM_MEMORY_SEEDS = [
    ("manual", "默认自主执行"),
    ("manual", "仅在权限问题请求用户"),
    ("manual", "结果优先成品交付"),
]


def get_env(key: str, default: str = ""):
    return os.getenv(key, default).strip()


app = Flask(__name__)
app.config["SECRET_KEY"] = get_env("SECRET_KEY", "change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = get_env("DATABASE_URL", "sqlite:////data/app.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = int(get_env("MAX_UPLOAD_MB", "20")) * 1024 * 1024
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=int(get_env("SESSION_EXPIRE_MINUTES", "120")))

ALLOWED_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "webp", "pdf", "txt", "doc", "docx", "xlsx", "csv", "md", "zip"
}
OPENCLAW_DISCOVERY = [
    get_env("OPENCLAW_BASE_URL", "http://host.docker.internal:3333"),
    "http://host.docker.internal:3333",
    "http://gateway:3333",
    "http://localhost:3333",
]


db = SQLAlchemy(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="user", nullable=False)
    openclaw_token = db.Column(db.String(255), nullable=True)
    note = db.Column(db.String(255), nullable=True)
    memory_namespace = db.Column(db.String(120), nullable=False, unique=True)
    force_password_change = db.Column(db.Boolean, default=True, nullable=False)
    onboarding_completed = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def is_authenticated(self):
        return True

    def is_active(self):
        return True

    def is_anonymous(self):
        return False

    def get_id(self):
        return str(self.id)

    @property
    def role_normalized(self):
        return "admin" if (self.role or "").strip().lower() == "admin" else "user"

    @property
    def is_admin(self):
        return self.role_normalized == "admin"


class UserAgentBinding(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True, index=True)
    agent_id = db.Column(db.String(120), nullable=False, unique=True)  # legacy planner agent
    model = db.Column(db.String(120), nullable=False, default="unknown")
    session_key = db.Column(db.String(120), nullable=False, unique=True)  # legacy planner session
    planner_agent_id = db.Column(db.String(120), nullable=True)
    planner_session_key = db.Column(db.String(120), nullable=True)
    worker_agent_id = db.Column(db.String(120), nullable=True)
    worker_session_key = db.Column(db.String(120), nullable=True)
    last_provider = db.Column(db.String(80), nullable=True)
    last_called_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Conversation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    title = db.Column(db.String(120), default="默认会话")
    session_key = db.Column(db.String(120), nullable=False, unique=True)
    agent_id = db.Column(db.String(120), nullable=True)
    model = db.Column(db.String(120), nullable=True)
    last_provider = db.Column(db.String(80), nullable=True)
    last_called_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversation.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    attachment_name = db.Column(db.String(255), nullable=True)
    attachment_path = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class MemoryEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    kind = db.Column(db.String(20), nullable=False, default="fact")  # fact/preference/goal/manual
    content = db.Column(db.Text, nullable=False)
    source = db.Column(db.String(20), nullable=False, default="auto")  # auto/manual
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class TaskRunAudit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversation.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    user_message = db.Column(db.Text, nullable=False)
    planner_plan = db.Column(db.Text, nullable=True)
    worker_output = db.Column(db.Text, nullable=True)
    final_summary = db.Column(db.Text, nullable=True)
    duration_ms = db.Column(db.Integer, nullable=True)
    dual_agent_triggered = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class LoginAttempt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False)
    ip = db.Column(db.String(64), nullable=False)
    fail_count = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def normalize_role(role: str):
    r = (role or "").strip().lower()
    if r == "admin":
        return "admin"
    return "user"


def is_admin_user(user: User | None):
    if not user:
        return False
    return normalize_role(getattr(user, "role", "")) == "admin"


def admin_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not is_admin_user(current_user):
            flash("仅管理员可访问", "danger")
            return redirect(url_for("chat"))
        return view_func(*args, **kwargs)
    return wrapped


def ensure_schema_compat():
    db.create_all()
    conn = db.engine.raw_connection()
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(user)")
    user_cols = {row[1] for row in cur.fetchall()}
    if "force_password_change" not in user_cols:
        cur.execute("ALTER TABLE user ADD COLUMN force_password_change BOOLEAN NOT NULL DEFAULT 1")
    if "note" not in user_cols:
        cur.execute("ALTER TABLE user ADD COLUMN note VARCHAR(255)")
    if "onboarding_completed" not in user_cols:
        cur.execute("ALTER TABLE user ADD COLUMN onboarding_completed BOOLEAN NOT NULL DEFAULT 1")

    # 兼容旧角色命名：member -> user
    cur.execute("UPDATE user SET role='user' WHERE role IS NULL OR TRIM(role)='' OR role='member'")

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='login_attempt'")
    if not cur.fetchone():
        cur.execute(
            "CREATE TABLE login_attempt (id INTEGER PRIMARY KEY AUTOINCREMENT, username VARCHAR(80) NOT NULL, ip VARCHAR(64) NOT NULL, fail_count INTEGER NOT NULL DEFAULT 0, locked_until DATETIME, updated_at DATETIME NOT NULL)"
        )

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_agent_binding'")
    if not cur.fetchone():
        cur.execute(
            "CREATE TABLE user_agent_binding (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL UNIQUE, agent_id VARCHAR(120) NOT NULL UNIQUE, model VARCHAR(120) NOT NULL DEFAULT 'unknown', session_key VARCHAR(120) NOT NULL UNIQUE, planner_agent_id VARCHAR(120), planner_session_key VARCHAR(120), worker_agent_id VARCHAR(120), worker_session_key VARCHAR(120), last_provider VARCHAR(80), last_called_at DATETIME, created_at DATETIME)"
        )

    cur.execute("PRAGMA table_info(user_agent_binding)")
    binding_cols = {row[1] for row in cur.fetchall()}
    if "planner_agent_id" not in binding_cols:
        cur.execute("ALTER TABLE user_agent_binding ADD COLUMN planner_agent_id VARCHAR(120)")
    if "planner_session_key" not in binding_cols:
        cur.execute("ALTER TABLE user_agent_binding ADD COLUMN planner_session_key VARCHAR(120)")
    if "worker_agent_id" not in binding_cols:
        cur.execute("ALTER TABLE user_agent_binding ADD COLUMN worker_agent_id VARCHAR(120)")
    if "worker_session_key" not in binding_cols:
        cur.execute("ALTER TABLE user_agent_binding ADD COLUMN worker_session_key VARCHAR(120)")

    cur.execute("PRAGMA table_info(conversation)")
    conv_cols = {row[1] for row in cur.fetchall()}
    if "agent_id" not in conv_cols:
        cur.execute("ALTER TABLE conversation ADD COLUMN agent_id VARCHAR(120)")
    if "model" not in conv_cols:
        cur.execute("ALTER TABLE conversation ADD COLUMN model VARCHAR(120)")
    if "last_provider" not in conv_cols:
        cur.execute("ALTER TABLE conversation ADD COLUMN last_provider VARCHAR(80)")
    if "last_called_at" not in conv_cols:
        cur.execute("ALTER TABLE conversation ADD COLUMN last_called_at DATETIME")

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_run_audit'")
    if not cur.fetchone():
        cur.execute(
            "CREATE TABLE task_run_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL, user_id INTEGER NOT NULL, user_message TEXT NOT NULL, planner_plan TEXT, worker_output TEXT, final_summary TEXT, duration_ms INTEGER, dual_agent_triggered BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME)"
        )

    cur.execute("PRAGMA table_info(task_run_audit)")
    audit_cols = {row[1] for row in cur.fetchall()}
    if "duration_ms" not in audit_cols:
        cur.execute("ALTER TABLE task_run_audit ADD COLUMN duration_ms INTEGER")

    conn.commit()
    conn.close()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_url(url: str):
    return (url or "").strip().rstrip("/")


def is_weak_secret():
    secret = app.config["SECRET_KEY"]
    return (not secret) or (secret in WEAK_SECRET_KEYS) or len(secret) < 24


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def ensure_user_agent_binding(user_id: int):
    binding = UserAgentBinding.query.filter_by(user_id=user_id).first()
    if binding:
        changed = False
        if not binding.planner_agent_id:
            binding.planner_agent_id = binding.agent_id or f"planner-u{user_id}-{uuid.uuid4().hex[:8]}"
            changed = True
        if not binding.planner_session_key:
            binding.planner_session_key = binding.session_key or f"planner-s-u{user_id}-{uuid.uuid4().hex[:12]}"
            changed = True
        if not binding.worker_agent_id:
            binding.worker_agent_id = f"worker-u{user_id}-{uuid.uuid4().hex[:8]}"
            changed = True
        if not binding.worker_session_key:
            binding.worker_session_key = f"worker-s-u{user_id}-{uuid.uuid4().hex[:12]}"
            changed = True
        if changed:
            binding.agent_id = binding.planner_agent_id
            binding.session_key = binding.planner_session_key
            db.session.commit()
        return binding

    planner_agent_id = f"planner-u{user_id}-{uuid.uuid4().hex[:8]}"
    planner_session_key = f"planner-s-u{user_id}-{uuid.uuid4().hex[:12]}"
    worker_agent_id = f"worker-u{user_id}-{uuid.uuid4().hex[:8]}"
    worker_session_key = f"worker-s-u{user_id}-{uuid.uuid4().hex[:12]}"
    binding = UserAgentBinding(
        user_id=user_id,
        agent_id=planner_agent_id,
        model=get_env("OPENCLAW_MODEL", "openai-codex/gpt-5.3-codex"),
        session_key=planner_session_key,
        planner_agent_id=planner_agent_id,
        planner_session_key=planner_session_key,
        worker_agent_id=worker_agent_id,
        worker_session_key=worker_session_key,
    )
    db.session.add(binding)
    db.session.commit()
    return binding


def ensure_user_conversation(user_id):
    binding = ensure_user_agent_binding(user_id)
    conv = Conversation.query.filter_by(user_id=user_id).order_by(Conversation.id.desc()).first()
    if conv:
        changed = False
        if conv.session_key != binding.session_key:
            conv.session_key = binding.session_key
            changed = True
        if conv.agent_id != binding.agent_id:
            conv.agent_id = binding.agent_id
            changed = True
        if conv.model != binding.model:
            conv.model = binding.model
            changed = True
        if changed:
            db.session.commit()
        return conv

    conv = Conversation(
        user_id=user_id,
        title="默认会话",
        session_key=binding.session_key,
        agent_id=binding.agent_id,
        model=binding.model,
    )
    db.session.add(conv)
    db.session.commit()
    return conv


def get_login_guard(username: str, ip: str):
    record = LoginAttempt.query.filter_by(username=username, ip=ip).first()
    if not record:
        record = LoginAttempt(username=username, ip=ip)
        db.session.add(record)
        db.session.commit()
    return record


def check_login_rate_limit(username: str, ip: str):
    now = utcnow()
    record = get_login_guard(username, ip)
    if record.locked_until and record.locked_until > now:
        remain = int((record.locked_until - now).total_seconds())
        return False, max(remain, 1)

    if (now - record.updated_at) > timedelta(minutes=15):
        record.fail_count = 0
        record.locked_until = None
        record.updated_at = now
        db.session.commit()

    return True, 0


def mark_login_failure(username: str, ip: str):
    now = utcnow()
    record = get_login_guard(username, ip)
    if (now - record.updated_at) > timedelta(minutes=15):
        record.fail_count = 0
    record.fail_count += 1
    record.updated_at = now
    if record.fail_count >= 8:
        record.locked_until = now + timedelta(minutes=15)
    db.session.commit()


def clear_login_failure(username: str, ip: str):
    record = LoginAttempt.query.filter_by(username=username, ip=ip).first()
    if record:
        record.fail_count = 0
        record.locked_until = None
        record.updated_at = utcnow()
        db.session.commit()


def get_user_memories(user_id: int, limit: int = 10):
    return MemoryEntry.query.filter_by(user_id=user_id).order_by(MemoryEntry.created_at.desc()).limit(limit).all()


def inject_memory_context(user_id: int, text: str):
    mems = get_user_memories(user_id, limit=8)
    if not mems:
        return text
    lines = [f"- ({m.kind}) {m.content}" for m in mems]
    prefix = "[长期记忆(仅当前用户)]\n" + "\n".join(lines) + "\n[/长期记忆]\n\n"
    return prefix + text


def auto_extract_memories(user_id: int, user_text: str):
    # 轻量规则提取：事实/偏好/目标
    patterns = [
        ("preference", r"(?:我喜欢|我偏好|我习惯|请优先|以后都)([^。！？\n]{2,80})"),
        ("goal", r"(?:我的目标是|我计划|我要|希望在)([^。！？\n]{2,80})"),
        ("fact", r"(?:我是|我在|我用|我家里有|我的)([^。！？\n]{2,80})"),
    ]
    extracted = []
    for kind, p in patterns:
        for m in re.finditer(p, user_text):
            snippet = m.group(0).strip()
            if len(snippet) >= 4:
                extracted.append((kind, snippet))

    added = 0
    for kind, content in extracted[:5]:
        existed = MemoryEntry.query.filter_by(user_id=user_id, content=content).first()
        if existed:
            continue
        db.session.add(MemoryEntry(user_id=user_id, kind=kind, content=content, source="auto"))
        added += 1
    if added:
        db.session.commit()
    return added


def memory_remember(user_id: int, content: str):
    content = (content or "").strip()
    if not content:
        return False, "内容为空"
    db.session.add(MemoryEntry(user_id=user_id, kind="manual", content=content, source="manual"))
    db.session.commit()
    return True, "已记住"


def memory_forget(user_id: int, content: str):
    content = (content or "").strip()
    if not content:
        return 0
    q = MemoryEntry.query.filter_by(user_id=user_id).filter(MemoryEntry.content.contains(content)).all()
    for item in q:
        db.session.delete(item)
    db.session.commit()
    return len(q)


def ensure_system_memories(user_id: int):
    added = 0
    for kind, content in SYSTEM_MEMORY_SEEDS:
        existed = MemoryEntry.query.filter_by(user_id=user_id, content=content).first()
        if existed:
            continue
        db.session.add(MemoryEntry(user_id=user_id, kind=kind, content=content, source="system"))
        added += 1
    if added:
        db.session.commit()
    return added


def gateway_health_for(base_url: str, token: str | None = None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    checks = ["/health", "/api/health", "/v1/models", "/"]
    for path in checks:
        try:
            r = requests.get(base_url + path, headers=headers, timeout=4)
            if r.status_code < 500:
                return True, f"{path} -> {r.status_code}"
        except Exception as e:
            last = str(e)
            continue
    return False, f"unreachable: {last if 'last' in locals() else 'unknown error'}"


def resolve_gateway(user: User):
    user_token = (user.openclaw_token or "").strip() if user else ""
    global_token = get_env("OPENCLAW_GATEWAY_TOKEN", "")
    token = user_token or global_token

    for raw in OPENCLAW_DISCOVERY:
        base_url = normalize_url(raw)
        if not base_url:
            continue
        ok, detail = gateway_health_for(base_url, token)
        if ok:
            return {"ok": True, "base_url": base_url, "detail": detail, "token_source": "user" if user_token else "global" if global_token else "none"}

    base = normalize_url(get_env("OPENCLAW_BASE_URL", "http://host.docker.internal:3333"))
    ok, detail = gateway_health_for(base, token)
    return {"ok": ok, "base_url": base, "detail": detail, "token_source": "user" if user_token else "global" if global_token else "none"}


def _extract_gateway_text(data):
    if not isinstance(data, dict):
        return None
    if data.get("reply"):
        return data.get("reply")
    if data.get("text"):
        return data.get("text")
    if data.get("output_text"):
        return data.get("output_text")
    choices = data.get("choices") or []
    if choices:
        msg = (choices[0] or {}).get("message") or {}
        if msg.get("content"):
            return msg.get("content")
    return None


def _deep_find(obj, keys: set[str]):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, (str, int, float)):
                return str(v)
            got = _deep_find(v, keys)
            if got:
                return got
    elif isinstance(obj, list):
        for it in obj:
            got = _deep_find(it, keys)
            if got:
                return got
    return None


def call_openclaw_gateway(user: User, binding: UserAgentBinding, message_text: str, attachment_hint: str | None = None):
    gw = resolve_gateway(user)
    base_url = gw["base_url"]
    token = user.openclaw_token or get_env("OPENCLAW_GATEWAY_TOKEN", "")
    headers = {
        "Content-Type": "application/json",
        "X-OpenClaw-Agent-Id": binding.agent_id,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    payload = {
        "sessionKey": binding.session_key,
        "memoryNamespace": user.memory_namespace,
        "user": user.username,
        "message": message_text,
        "agentId": binding.agent_id,
        "model": binding.model,
    }
    if attachment_hint:
        payload["attachmentHint"] = attachment_hint

    endpoints = ["/api/chat", "/v1/chat/completions", "/v1/responses"]
    last_error = None
    for ep in endpoints:
        try:
            r = requests.post(base_url + ep, json=payload, headers=headers, timeout=45)
            if r.ok:
                data = r.json() if "application/json" in r.headers.get("Content-Type", "") else {"text": r.text}
                text = _extract_gateway_text(data) or str(data)
                model = _deep_find(data, {"model", "model_name"}) or binding.model
                sid = _deep_find(data, {"sessionKey", "session_id", "sessionId"}) or binding.session_key
                aid = _deep_find(data, {"agentId", "agent_id"}) or binding.agent_id
                return {"ok": True, "reply": text, "provider": f"gateway:{ep}", "model": model, "session_key": sid, "agent_id": aid}
            last_error = f"{ep} => {r.status_code}: {r.text[:180]}"
        except Exception as e:
            last_error = str(e)

    return {"ok": False, "error": f"网关: {base_url} / 健康: {gw['detail']} / 错误: {last_error}"}


def _run_cli(cmd: list[str], timeout_sec: int = 90):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)


def call_openclaw_cli(binding: UserAgentBinding, message_text: str, attachment_hint: str | None = None):
    final_msg = message_text if not attachment_hint else f"{message_text}\n\n{attachment_hint}"
    cmd = [
        "openclaw", "agent",
        "--session-id", binding.session_key,
        "--message", final_msg,
        "--json",
        "--thinking", "off",
        "--timeout", get_env("OPENCLAW_CLI_TIMEOUT_SEC", "120"),
    ]

    try:
        proc = _run_cli(cmd, timeout_sec=int(get_env("OPENCLAW_CLI_TIMEOUT_SEC", "120")) + 10)
    except FileNotFoundError:
        return {"ok": False, "error": "未找到 openclaw CLI，请在宿主机安装并将 app 运行在可访问 openclaw 的环境中"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "OpenClaw CLI 调用超时，请稍后重试"}
    except Exception as e:
        return {"ok": False, "error": f"OpenClaw CLI 调用异常: {e}"}

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return {"ok": False, "error": f"OpenClaw CLI 执行失败({proc.returncode}): {err[:240]}"}

    raw = (proc.stdout or "").strip()
    if not raw:
        return {"ok": False, "error": "OpenClaw CLI 返回为空"}

    try:
        data = json.loads(raw)
    except Exception:
        return {"ok": False, "error": f"OpenClaw CLI 返回非 JSON: {raw[:240]}"}

    payloads = (((data.get("result") or {}).get("payloads")) or [])
    texts = [((p or {}).get("text") or "").strip() for p in payloads if ((p or {}).get("text") or "").strip()]
    if not texts:
        return {"ok": False, "error": "OpenClaw CLI 未返回 assistant 文本"}

    model = _deep_find(data, {"model", "model_name"}) or binding.model
    sid = _deep_find(data, {"sessionKey", "session_id", "sessionId"}) or binding.session_key
    aid = _deep_find(data, {"agentId", "agent_id"}) or binding.agent_id
    return {
        "ok": True,
        "reply": "\n\n".join(texts),
        "provider": "cli:openclaw-agent",
        "model": model,
        "session_key": sid,
        "agent_id": aid,
    }


def call_openclaw(user: User, binding: UserAgentBinding, message_text: str, attachment_hint: str | None = None):
    gw_result = call_openclaw_gateway(user, binding, message_text, attachment_hint)
    if gw_result.get("ok"):
        return gw_result

    cli_result = call_openclaw_cli(binding, message_text, attachment_hint)
    if cli_result.get("ok"):
        return cli_result

    return {
        "ok": False,
        "reply": f"[OpenClaw 调用失败] 网关失败: {gw_result.get('error')} / CLI失败: {cli_result.get('error')}",
        "provider": "failed:gateway+cli",
        "model": binding.model,
        "session_key": binding.session_key,
        "agent_id": binding.agent_id,
    }


def parse_plan_steps(plan_text: str):
    lines = [ln.strip() for ln in (plan_text or "").splitlines() if ln.strip()]
    steps = []
    for ln in lines:
        cleaned = re.sub(r"^[-*•\d\.\)\s]+", "", ln).strip()
        if cleaned:
            steps.append(cleaned)
    return steps[:8]


def summarize_text(text: str, max_len: int = 120):
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= max_len else text[:max_len] + "…"


def run_dual_agent_cycle(user: User, binding: UserAgentBinding, user_text: str, attachment_hint: str | None = None):
    started = utcnow()
    planner_binding = type("PlannerBinding", (), {
        "agent_id": binding.planner_agent_id or binding.agent_id,
        "session_key": binding.planner_session_key or binding.session_key,
        "model": binding.model,
    })()
    worker_binding = type("WorkerBinding", (), {
        "agent_id": binding.worker_agent_id,
        "session_key": binding.worker_session_key,
        "model": binding.model,
    })()

    planner_prompt = (
        "你是 planner。将用户需求拆解为最多5条可执行步骤，输出中文条目。"
        "除权限阻塞外不要要求用户补充信息。\n\n"
        f"用户需求：{user_text}"
    )
    planner_res = call_openclaw(user, planner_binding, inject_memory_context(user.id, planner_prompt), attachment_hint)
    plan_text = planner_res.get("reply", "")

    worker_prompt = (
        "你是 worker。根据以下计划直接执行并产出结果。"
        "如果遇到权限阻塞，明确标注“权限阻塞”。否则直接给出成品结果。\n\n"
        f"计划：\n{plan_text}\n\n用户原始需求：{user_text}"
    )
    worker_res = call_openclaw(user, worker_binding, inject_memory_context(user.id, worker_prompt), attachment_hint)
    worker_text = worker_res.get("reply", "")

    verify_prompt = (
        "你是 planner。请验证worker结果是否完成需求，并输出最终交付。"
        "格式：\n1) 完成状态\n2) 最终结果\n3) 如有阻塞仅列权限问题。\n\n"
        f"用户需求：{user_text}\n\n计划：{plan_text}\n\nworker结果：{worker_text}"
    )
    final_res = call_openclaw(user, planner_binding, inject_memory_context(user.id, verify_prompt))
    duration_ms = int((utcnow() - started).total_seconds() * 1000)

    return {
        "planner": planner_res,
        "worker": worker_res,
        "final": final_res,
        "plan_text": plan_text,
        "worker_text": worker_text,
        "final_text": final_res.get("reply", ""),
        "duration_ms": duration_ms,
    }


@app.before_request
def enforce_security_onboarding():
    if not current_user.is_authenticated:
        return
    if request.endpoint in {"logout", "security_setup", "onboarding", "complete_onboarding", "snooze_onboarding", "static", "uploaded_file", "gateway_health"}:
        return
    if is_admin_user(current_user) and (current_user.force_password_change or is_weak_secret()):
        return redirect(url_for("security_setup"))
    if not current_user.onboarding_completed:
        return redirect(url_for("onboarding"))


@app.route("/healthz")
def healthz():
    return {"ok": True, "service": "home-agent-app"}


@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("chat"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()

        allowed, remain = check_login_rate_limit(username, ip)
        if not allowed:
            flash(f"登录尝试过多，请 {remain} 秒后再试", "danger")
            return render_template("login.html")

        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            clear_login_failure(username, ip)
            login_user(user)
            return redirect(url_for("chat"))

        mark_login_failure(username, ip)
        flash("用户名或密码错误", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/security/setup", methods=["GET", "POST"])
@login_required
def security_setup():
    if not is_admin_user(current_user):
        abort(403)

    if request.method == "POST":
        action = request.form.get("action")
        if action == "change_password":
            old_pwd = request.form.get("old_password", "")
            new_pwd = request.form.get("new_password", "")
            confirm_pwd = request.form.get("confirm_password", "")
            if not check_password_hash(current_user.password_hash, old_pwd):
                flash("当前密码错误", "danger")
            elif len(new_pwd) < 10:
                flash("新密码至少 10 位", "warning")
            elif new_pwd != confirm_pwd:
                flash("两次输入的新密码不一致", "warning")
            else:
                current_user.password_hash = generate_password_hash(new_pwd)
                current_user.force_password_change = False
                db.session.commit()
                flash("密码已更新", "success")

    suggest_secret = secrets.token_urlsafe(36)
    secret_weak = is_weak_secret()
    return render_template("security_setup.html", secret_weak=secret_weak, suggest_secret=suggest_secret)


@app.route("/onboarding")
@login_required
def onboarding():
    return render_template("onboarding.html", completed=current_user.onboarding_completed)


@app.route("/onboarding/complete", methods=["POST"])
@login_required
def complete_onboarding():
    current_user.onboarding_completed = True
    db.session.commit()
    flash("引导已完成，欢迎开始使用 🎉", "success")
    return redirect(url_for("chat"))


@app.route("/onboarding/snooze", methods=["POST"])
@login_required
def snooze_onboarding():
    flash("已跳过本次引导，你可以稍后在聊天页随时查看。", "info")
    return redirect(url_for("chat"))


@app.route("/chat", methods=["GET", "POST"])
@login_required
def chat():
    binding = ensure_user_agent_binding(current_user.id)
    conv = ensure_user_conversation(current_user.id)
    ensure_system_memories(current_user.id)

    if request.method == "POST":
        text = request.form.get("message", "").strip()
        file = request.files.get("attachment")
        attachment_name = None
        attachment_path = None
        attachment_hint = None

        if file and file.filename:
            if not allowed_file(file.filename):
                flash("文件类型不支持，仅允许常见图片/文档/压缩包", "danger")
                return redirect(url_for("chat"))
            safe_name = secure_filename(file.filename)
            unique_name = f"u{current_user.id}_{uuid.uuid4().hex[:8]}_{safe_name}"
            store_path = UPLOAD_DIR / unique_name
            file.save(store_path)
            attachment_name = safe_name
            attachment_path = unique_name
            attachment_hint = f"附件: {safe_name}, 本地路径: /uploads/{unique_name}"

        if not text and not attachment_name:
            flash("请输入消息或上传附件", "warning")
            return redirect(url_for("chat"))

        user_text = text or "[仅上传附件]"

        # 手动记忆控制
        if user_text.startswith("/remember "):
            ok, msg = memory_remember(current_user.id, user_text.replace("/remember ", "", 1))
            db.session.add(Message(conversation_id=conv.id, user_id=current_user.id, role="user", content=user_text,
                                   attachment_name=attachment_name, attachment_path=attachment_path))
            db.session.add(Message(conversation_id=conv.id, user_id=current_user.id, role="assistant", content=f"[记忆] {msg if ok else '失败'}"))
            db.session.commit()
            return redirect(url_for("chat"))
        if user_text.startswith("/forget "):
            n = memory_forget(current_user.id, user_text.replace("/forget ", "", 1))
            db.session.add(Message(conversation_id=conv.id, user_id=current_user.id, role="user", content=user_text,
                                   attachment_name=attachment_name, attachment_path=attachment_path))
            db.session.add(Message(conversation_id=conv.id, user_id=current_user.id, role="assistant", content=f"[记忆] 已删除 {n} 条"))
            db.session.commit()
            return redirect(url_for("chat"))

        db.session.add(Message(conversation_id=conv.id, user_id=current_user.id, role="user", content=user_text,
                               attachment_name=attachment_name, attachment_path=attachment_path))
        db.session.commit()

        auto_extract_memories(current_user.id, user_text)
        ensure_system_memories(current_user.id)
        run = run_dual_agent_cycle(current_user, binding, user_text, attachment_hint)
        result = run["final"]

        binding.agent_id = binding.planner_agent_id or binding.agent_id
        binding.session_key = binding.planner_session_key or binding.session_key
        binding.model = result.get("model") or binding.model
        binding.last_provider = f"dual:{run['planner'].get('provider')}->{run['worker'].get('provider')}->{result.get('provider')}"
        binding.last_called_at = utcnow()

        conv.agent_id = binding.agent_id
        conv.model = binding.model
        conv.session_key = binding.session_key
        conv.last_provider = binding.last_provider
        conv.last_called_at = binding.last_called_at

        db.session.add(TaskRunAudit(
            conversation_id=conv.id,
            user_id=current_user.id,
            user_message=user_text,
            planner_plan=run["plan_text"][:4000],
            worker_output=run["worker_text"][:4000],
            final_summary=run["final_text"][:4000],
            duration_ms=run.get("duration_ms"),
            dual_agent_triggered=True,
        ))
        db.session.add(Message(conversation_id=conv.id, user_id=current_user.id, role="assistant", content=result.get("reply", "")))
        db.session.commit()

        return redirect(url_for("chat"))

    msgs = Message.query.filter_by(conversation_id=conv.id).order_by(Message.created_at.asc()).all()
    gateway = resolve_gateway(current_user)
    memories = get_user_memories(current_user.id, limit=12)
    latest_run = TaskRunAudit.query.filter_by(conversation_id=conv.id).order_by(TaskRunAudit.id.desc()).first()
    known_memories = [m for m in memories if m.kind in {"fact", "preference", "goal", "manual"}][:6]
    memory_updates = memories[:5]
    plan_steps = parse_plan_steps(latest_run.planner_plan if latest_run else "")
    if latest_run and plan_steps:
        doing_steps = [{"step": s, "status": "done"} for s in plan_steps]
    elif plan_steps:
        doing_steps = [{"step": s, "status": "pending"} for s in plan_steps]
    else:
        doing_steps = []

    dual_agent_summary = {
        "planner_role": "拆解需求并验证结果",
        "worker_role": "执行计划并产出结果",
        "last_result": summarize_text(latest_run.final_summary if latest_run else ""),
        "duration_ms": latest_run.duration_ms if latest_run else None,
        "worker_excerpt": summarize_text(latest_run.worker_output if latest_run else ""),
    }

    capability_prompts = [
        "帮我把这个需求拆成 5 个可执行步骤，并给出今天就能开始的行动清单。",
        "我上传了一份文档，请先总结要点，再提取可直接执行的 TODO。",
        "请基于我们历史对话记忆，给我一个本周家庭自动化优化计划。",
        "请帮我写一条给家人的通知：说明新系统上线、怎么提需求、注意隐私边界。",
        "把我这段想法整理成结构化方案：目标、限制、方案、下一步。",
    ]
    return render_template("chat.html", msgs=msgs, conv=conv, gateway=gateway, memories=memories, binding=binding,
                           latest_run=latest_run,
                           known_memories=known_memories,
                           memory_updates=memory_updates,
                           plan_steps=plan_steps,
                           doing_steps=doing_steps,
                           dual_agent_summary=dual_agent_summary,
                           capability_prompts=capability_prompts,
                           onboarding_completed=current_user.onboarding_completed,
                           max_upload_mb=app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024))


@app.route("/gateway/health")
@login_required
def gateway_health():
    return jsonify(resolve_gateway(current_user))


@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename, as_attachment=False)


@app.route("/admin/users", methods=["GET", "POST"])
@login_required
@admin_required
def admin_users():

    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            role = request.form.get("role", "user")
            note = request.form.get("note", "").strip() or None
            token = request.form.get("openclaw_token", "").strip() or None
            if not username or not password:
                flash("用户名与初始密码必填", "warning")
            elif len(password) < 8:
                flash("初始密码至少 8 位", "warning")
            elif User.query.filter_by(username=username).first():
                flash("用户名已存在", "warning")
            else:
                user = User(
                    username=username,
                    password_hash=generate_password_hash(password),
                    role=normalize_role(role),
                    openclaw_token=token,
                    note=note,
                    memory_namespace=f"user-{uuid.uuid4().hex[:12]}",
                    force_password_change=True,
                    onboarding_completed=False,
                )
                db.session.add(user)
                db.session.commit()
                ensure_user_agent_binding(user.id)
                ensure_user_conversation(user.id)
                ensure_system_memories(user.id)
                flash(f"用户 {username} 创建成功：已初始化双Agent与系统记忆，首次登录将进入新手引导。", "success")
        elif action == "reset_pwd":
            user_id = int(request.form.get("user_id", "0"))
            new_pwd = request.form.get("new_password", "")
            user = db.session.get(User, user_id)
            if user and new_pwd:
                user.password_hash = generate_password_hash(new_pwd)
                user.force_password_change = True
                db.session.commit()
                flash(f"已重置 {user.username} 密码，用户下次登录将被要求改密", "success")

    users = User.query.order_by(User.id.asc()).all()
    return render_template("admin_users.html", users=users)


@app.route("/admin/chats")
@login_required
@admin_required
def admin_chats():

    records = db.session.query(Message, User, Conversation).join(User, Message.user_id == User.id).join(
        Conversation, Message.conversation_id == Conversation.id
    ).order_by(Message.created_at.desc()).limit(500).all()
    runs = TaskRunAudit.query.order_by(TaskRunAudit.created_at.desc()).limit(500).all()
    audit_by_conv = {}
    for r in runs:
        audit_by_conv.setdefault(r.conversation_id, r)
    return render_template("admin_chats.html", records=records, audit_by_conv=audit_by_conv)


@app.route("/admin/memories")
@login_required
@admin_required
def admin_memories():
    entries = db.session.query(MemoryEntry, User).join(User, MemoryEntry.user_id == User.id).order_by(MemoryEntry.created_at.desc()).limit(800).all()
    return render_template("admin_memories.html", entries=entries)


@app.route("/admin/agents")
@login_required
@admin_required
def admin_agents():
    rows = db.session.query(UserAgentBinding, User).join(User, UserAgentBinding.user_id == User.id).order_by(User.id.asc()).all()
    latest_user_runs = {}
    for run in TaskRunAudit.query.order_by(TaskRunAudit.id.desc()).all():
        if run.user_id not in latest_user_runs:
            latest_user_runs[run.user_id] = run
    return render_template("admin_agents.html", rows=rows, latest_user_runs=latest_user_runs,
                           parse_plan_steps=parse_plan_steps, summarize_text=summarize_text)


@app.route("/admin/session-audit")
@login_required
@admin_required
def admin_session_audit():
    conversations = db.session.query(Conversation, User).join(User, Conversation.user_id == User.id).order_by(Conversation.id.desc()).limit(300).all()
    rows = []
    for conv, user in conversations:
        msg_count = Message.query.filter_by(conversation_id=conv.id).count()
        last_msg = Message.query.filter_by(conversation_id=conv.id).order_by(Message.created_at.desc()).first()
        latest_run = TaskRunAudit.query.filter_by(conversation_id=conv.id).order_by(TaskRunAudit.id.desc()).first()
        rows.append({
            "conversation_id": conv.id,
            "username": user.username,
            "role": normalize_role(user.role),
            "session_key": conv.session_key,
            "agent_id": conv.agent_id,
            "model": conv.model,
            "last_provider": conv.last_provider,
            "created_at": conv.created_at,
            "last_called_at": conv.last_called_at,
            "message_count": msg_count,
            "last_message_at": last_msg.created_at if last_msg else None,
            "last_run_at": latest_run.created_at if latest_run else None,
            "last_duration_ms": latest_run.duration_ms if latest_run else None,
            "dual_agent_triggered": latest_run.dual_agent_triggered if latest_run else False,
        })
    return render_template("admin_session_audit.html", rows=rows)


@app.route("/api/admin/session-audit")
@login_required
@admin_required
def api_admin_session_audit():
    limit = min(max(int(request.args.get("limit", "100")), 1), 500)
    conversations = db.session.query(Conversation, User).join(User, Conversation.user_id == User.id).order_by(Conversation.id.desc()).limit(limit).all()
    items = []
    for conv, user in conversations:
        msg_count = Message.query.filter_by(conversation_id=conv.id).count()
        latest_run = TaskRunAudit.query.filter_by(conversation_id=conv.id).order_by(TaskRunAudit.id.desc()).first()
        items.append({
            "conversation_id": conv.id,
            "username": user.username,
            "role": normalize_role(user.role),
            "session_key": conv.session_key,
            "agent_id": conv.agent_id,
            "model": conv.model,
            "last_provider": conv.last_provider,
            "created_at": conv.created_at.isoformat() if conv.created_at else None,
            "last_called_at": conv.last_called_at.isoformat() if conv.last_called_at else None,
            "message_count": msg_count,
            "last_run_at": latest_run.created_at.isoformat() if latest_run and latest_run.created_at else None,
            "last_duration_ms": latest_run.duration_ms if latest_run else None,
            "dual_agent_triggered": bool(latest_run.dual_agent_triggered) if latest_run else False,
        })
    return jsonify({"ok": True, "count": len(items), "items": items})


@app.cli.command("init-db")
def init_db_cmd():
    ensure_schema_compat()
    print("db initialized")


if __name__ == "__main__":
    with app.app_context():
        ensure_schema_compat()
    app.run(host="0.0.0.0", port=8000)
