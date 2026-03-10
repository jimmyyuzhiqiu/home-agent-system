import hashlib
import hmac
import json
import os
import pty
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, abort, jsonify, request


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_HOME = Path(os.getenv("OPENCLAW_RUNTIME_HOME", "/runtime-state")).expanduser()
RUNTIME_PROFILE = os.getenv("OPENCLAW_RUNTIME_PROFILE", "runtime").strip() or "runtime"
SHARED_UPLOAD_ROOT = Path(os.getenv("HOME_AGENT_SHARED_UPLOAD_ROOT", "/runtime/uploads")).expanduser()
WORKSPACE_ROOT = Path(os.getenv("HOME_AGENT_RUNTIME_WORKROOT", "/runtime/workspaces/users")).expanduser()
GATEWAY_PORT = int(os.getenv("OPENCLAW_GATEWAY_PORT", "3333"))
ENABLE_GATEWAY = os.getenv("OPENCLAW_ENABLE_GATEWAY", "1").strip().lower() in {"1", "true", "yes", "on"}
DEFAULT_MODEL = os.getenv("OPENCLAW_MODEL", "openai/gpt-5.3-codex").strip() or "openai/gpt-5.3-codex"

_OAUTH_SESSIONS = {}
_OAUTH_LOCK = threading.Lock()
_GATEWAY_LOCK = threading.Lock()
_GATEWAY_PROC = None
_GATEWAY_ERROR = ""


def load_env_file(path: Path):
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env_file(PROJECT_ROOT / ".env")


def utcnow():
    return datetime.now(timezone.utc)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def bridge_secret() -> str:
    return os.getenv("HOME_AGENT_BRIDGE_SHARED_SECRET", os.getenv("SECRET_KEY", "change-me")).strip() or "change-me"


def chromium_path() -> str | None:
    explicit = os.getenv("CHROME_BIN", "").strip()
    if explicit and Path(explicit).exists():
        return explicit
    for candidate in (
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("google-chrome"),
    ):
        if candidate:
            return candidate
    matches = sorted(Path("/ms-playwright").glob("chromium-*/chrome-linux/chrome"))
    return str(matches[0]) if matches else None


def runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(ensure_dir(RUNTIME_HOME))
    env.setdefault("CHROME_BIN", chromium_path() or "")
    return env


def openclaw_cmd(*parts: str) -> list[str]:
    return ["openclaw", "--profile", RUNTIME_PROFILE, *parts]


def normalize_model_name(model: str | None) -> str:
    raw = (model or "").strip()
    if not raw:
        return DEFAULT_MODEL
    if "/" in raw:
        return raw
    provider = os.getenv("OPENCLAW_MODEL_PROVIDER", "openai")
    return f"{provider}/{raw}"


def verify_request_signature() -> bool:
    if request.path == "/health":
        return True
    timestamp = request.headers.get("X-Home-Agent-Timestamp", "").strip()
    signature = request.headers.get("X-Home-Agent-Signature", "").strip()
    if not timestamp or not signature:
        return False
    try:
        now_ts = int(utcnow().timestamp())
        request_ts = int(timestamp)
    except ValueError:
        return False
    if abs(now_ts - request_ts) > 300:
        return False
    body = request.get_data(as_text=True) or ""
    message = "\n".join([request.method.upper(), request.path, timestamp, body]).encode("utf-8")
    expected = hmac.new(bridge_secret().encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def run_command(cmd: list[str], timeout: int = 120, input_text: str | None = None):
    proc = subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=runtime_env(),
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(stderr or stdout or f"命令失败({proc.returncode})")
    return stdout


def openclaw_path() -> str | None:
    return shutil.which("openclaw")


def load_json(raw: str):
    return json.loads(raw)


def workspace_paths(workspace: str):
    root = ensure_dir(Path(workspace).expanduser())
    return {
        "root": root,
        "inputs": ensure_dir(root / "inputs"),
        "outputs": ensure_dir(root / "outputs"),
    }


def _provider_status_from_models(probe: bool = False):
    command = openclaw_cmd("models", "status", "--json")
    if probe:
        command.append("--probe")
    raw = run_command(command, timeout=90 if probe else 40)
    data = load_json(raw)
    auth = data.get("auth") or {}
    providers = auth.get("providers") or []
    oauth_profiles = ((auth.get("oauth") or {}).get("profiles")) or []
    oauth_providers = ((auth.get("oauth") or {}).get("providers")) or []
    probe_results = ((auth.get("probes") or {}).get("results")) or []
    provider_ready = False
    if any(item.get("status") == "ok" for item in probe_results):
        provider_ready = True
    elif any(item.get("status") == "ok" for item in oauth_profiles):
        provider_ready = True
    else:
        for item in providers:
            counts = item.get("profiles") or {}
            if (counts.get("oauth") or 0) > 0 or (counts.get("apiKey") or 0) > 0:
                provider_ready = True
                break
    return {
        "ok": True,
        "status": "ready" if provider_ready else "pending",
        "provider_ready": provider_ready,
        "default_model": data.get("resolvedDefault") or data.get("defaultModel") or DEFAULT_MODEL,
        "config_path": data.get("configPath"),
        "agent_dir": data.get("agentDir"),
        "providers": providers,
        "oauth_profiles": oauth_profiles,
        "oauth_providers": oauth_providers,
        "probe_results": probe_results,
        "raw": data,
    }


def provider_status_payload(probe: bool = False):
    try:
        payload = _provider_status_from_models(probe=probe)
        payload["bridge_version"] = "2026.03-runtime"
        payload["browser_available"] = bool(chromium_path())
        payload["gateway_running"] = gateway_running()
        if _GATEWAY_ERROR:
            payload["gateway_error"] = _GATEWAY_ERROR
        return payload
    except Exception as exc:
        return {
            "ok": False,
            "status": "failed",
            "provider_ready": False,
            "default_model": DEFAULT_MODEL,
            "config_path": None,
            "agent_dir": None,
            "providers": [],
            "oauth_profiles": [],
            "oauth_providers": [],
            "probe_results": [],
            "bridge_version": "2026.03-runtime",
            "browser_available": bool(chromium_path()),
            "gateway_running": gateway_running(),
            "last_error": str(exc),
        }


def gateway_running() -> bool:
    return bool(_GATEWAY_PROC and _GATEWAY_PROC.poll() is None)


def ensure_gateway_process():
    global _GATEWAY_PROC, _GATEWAY_ERROR
    if not ENABLE_GATEWAY:
        return False
    with _GATEWAY_LOCK:
        if _GATEWAY_PROC and _GATEWAY_PROC.poll() is None:
            return True
        try:
            ensure_dir(RUNTIME_HOME)
            log_file = ensure_dir(RUNTIME_HOME / "logs") / "gateway.log"
            handle = log_file.open("ab")
            _GATEWAY_PROC = subprocess.Popen(
                openclaw_cmd(
                    "gateway",
                    "run",
                    "--allow-unconfigured",
                    "--bind",
                    "loopback",
                    "--force",
                    "--port",
                    str(GATEWAY_PORT),
                ),
                stdout=handle,
                stderr=subprocess.STDOUT,
                env=runtime_env(),
            )
            _GATEWAY_ERROR = ""
            return True
        except Exception as exc:
            _GATEWAY_ERROR = str(exc)
            return False


def health_payload():
    ensure_dir(RUNTIME_HOME)
    ensure_dir(SHARED_UPLOAD_ROOT)
    ensure_dir(WORKSPACE_ROOT)
    if ENABLE_GATEWAY:
        ensure_gateway_process()
    provider = provider_status_payload(probe=False)
    reason_parts = []
    if not openclaw_path():
        reason_parts.append("未找到 openclaw CLI")
    if not provider.get("provider_ready"):
        reason_parts.append(provider.get("last_error") or "尚未完成 Provider 认证")
    if _GATEWAY_ERROR:
        reason_parts.append(f"Gateway: {_GATEWAY_ERROR}")
    return {
        "ok": bool(openclaw_path()),
        "chat_ready": bool(openclaw_path() and provider.get("provider_ready")),
        "bridge_version": "2026.03-runtime",
        "openclaw_available": bool(openclaw_path()),
        "browser_available": bool(chromium_path()),
        "gateway_running": gateway_running(),
        "provider_ready": bool(provider.get("provider_ready")),
        "default_model": provider.get("default_model") or DEFAULT_MODEL,
        "detail": provider.get("last_error") or ("runtime ready" if provider.get("provider_ready") else "provider pending"),
        "reason": "；".join([item for item in reason_parts if item]) or "运行时可用",
        "runtime_profile": RUNTIME_PROFILE,
        "runtime_home": str(RUNTIME_HOME),
        "workroot": str(WORKSPACE_ROOT),
    }


def _workspace_root_for_namespace(namespace: str, role: str) -> Path:
    safe_ns = re.sub(r"[^a-zA-Z0-9_.-]+", "-", (namespace or "default")).strip("-") or "default"
    return ensure_dir(WORKSPACE_ROOT / safe_ns / f"{role}-workspace")


def agents_cache():
    try:
        raw = run_command(openclaw_cmd("agents", "list", "--json"), timeout=25)
        return load_json(raw)
    except Exception:
        return []


def agent_lookup(agent_id: str):
    for item in agents_cache():
        if item.get("id") == agent_id:
            return item
    return None


def repair_agent_config(agent_id: str, workspace: str, model: str):
    try:
        # Keep agent definitions aligned without failing the turn if config is already correct.
        run_command(openclaw_cmd("agents", "update", agent_id, "--workspace", workspace, "--model", model, "--json"), timeout=40)
        return True
    except Exception:
        return False


def ensure_agent(agent_id: str, workspace: str, model: str):
    existing = agent_lookup(agent_id)
    if existing:
        repair_agent_config(agent_id, workspace, model)
        return existing
    ensure_dir(Path(workspace).expanduser())
    raw = run_command(
        openclaw_cmd(
            "agents",
            "add",
            agent_id,
            "--workspace",
            str(Path(workspace).expanduser()),
            "--model",
            model,
            "--non-interactive",
            "--json",
        ),
        timeout=60,
    )
    try:
        return load_json(raw)
    except Exception:
        return {"id": agent_id, "workspace": workspace}


def session_log_path(agent_id: str, session_id: str | None):
    agent = agent_lookup(agent_id)
    if not agent or not session_id:
        return None
    agent_dir = agent.get("agentDir")
    if not agent_dir:
        return None
    return Path(agent_dir).parent / "sessions" / f"{session_id}.jsonl"


def fetch_upload(signed_url: str, target_path: Path):
    response = requests.get(signed_url, timeout=90)
    response.raise_for_status()
    target_path.write_bytes(response.content)
    return {
        "ok": True,
        "local_path": str(target_path),
        "size": len(response.content),
        "sha256": hashlib.sha256(response.content).hexdigest(),
    }


def stage_attachment(ref: dict, target_dir: Path):
    safe_name = Path(ref.get("name") or "attachment").name
    target_path = target_dir / safe_name
    rel_path = (ref.get("path") or "").strip()
    if rel_path:
        rel = Path(rel_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("附件路径非法")
        source = SHARED_UPLOAD_ROOT / rel
        if source.exists():
            target_path.write_bytes(source.read_bytes())
            return {
                "ok": True,
                "local_path": str(target_path),
                "size": source.stat().st_size,
                "sha256": hashlib.sha256(target_path.read_bytes()).hexdigest(),
            }
    signed_url = (ref.get("signed_url") or "").strip()
    if signed_url:
        return fetch_upload(signed_url, target_path)
    raise FileNotFoundError("未找到可用附件来源")


def _tool_public_label(tool_name: str) -> str:
    mapping = {
        "web_search": "正在检索网站",
        "web_fetch": "正在访问页面",
        "browser": "正在访问页面",
        "process": "正在整理信息",
        "exec": "正在处理任务",
        "read": "正在读取文件",
        "write": "正在生成文件",
    }
    return mapping.get(tool_name, f"正在执行 {tool_name}")


def extract_urls(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"https?://[^\s<>()]+", text or "")))[:8]


def parse_tool_results(agent_id: str, session_id: str | None, started_at: datetime, ended_at: datetime):
    log_path = session_log_path(agent_id, session_id)
    events = []
    artifacts = []
    seen_urls = set()
    if not log_path or not log_path.exists():
        return events, artifacts
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return events, artifacts
    lower = started_at.timestamp() - 15
    upper = ended_at.timestamp() + 15
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if item.get("type") != "message":
            continue
        message = item.get("message") or {}
        if message.get("role") != "toolResult":
            continue
        timestamp = item.get("timestamp")
        try:
            when = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
        except Exception:
            when = None
        if when is not None and not (lower <= when <= upper):
            continue
        tool_name = message.get("toolName") or "tool"
        details = item.get("details") or {}
        events.append(
            {
                "stage": "worker",
                "event_type": tool_name,
                "status": "done",
                "label": tool_name,
                "public_text": _tool_public_label(tool_name),
                "admin_text": json.dumps(details or message, ensure_ascii=False)[:2400],
                "meta": details if isinstance(details, dict) else {},
            }
        )
        url = None
        title = None
        summary = None
        if isinstance(details, dict):
            url = details.get("url") or details.get("finalUrl")
            title = details.get("title")
            text = details.get("text") or ""
            summary = text[:240] if isinstance(text, str) else None
        if not url:
            for content in message.get("content") or []:
                if isinstance(content, dict):
                    text = content.get("text") or ""
                    urls = extract_urls(text)
                    if urls:
                        url = urls[0]
                        summary = summary or text[:240]
                        break
        if url and url not in seen_urls:
            parsed = urlparse(url)
            seen_urls.add(url)
            artifacts.append(
                {
                    "kind": "crawl_snapshot" if tool_name == "browser" else "search_result",
                    "title": title or parsed.netloc or url,
                    "summary": summary or "已记录网页结果",
                    "source_url": url,
                    "visibility": "user",
                }
            )
    return events, artifacts


def build_output_artifacts(agent_role: str, reply_text: str):
    return [
        {
            "kind": "output_file",
            "title": "执行结果文件" if agent_role == "verify" else f"{agent_role} 输出",
            "summary": "可下载的文本结果",
            "visibility": "user" if agent_role == "verify" else "admin",
            "filename": f"{agent_role}-result.md",
            "mime_type": "text/markdown",
            "inline_text": reply_text or "",
        }
    ]


def parse_agent_response(raw: str):
    data = load_json(raw)
    payloads = (((data.get("result") or {}).get("payloads")) or [])
    texts = [((payload or {}).get("text") or "").strip() for payload in payloads if ((payload or {}).get("text") or "").strip()]
    meta = ((data.get("result") or {}).get("meta")) or {}
    agent_meta = meta.get("agentMeta") or {}
    return {
        "bridge_run_id": data.get("runId"),
        "reply_text": "\n\n".join(texts).strip(),
        "provider": agent_meta.get("provider") or "openclaw",
        "model": agent_meta.get("model") or "",
        "duration_ms": meta.get("durationMs"),
        "session_id": agent_meta.get("sessionId"),
        "raw": data,
    }


def _trim_log(value: str) -> str:
    text = value[-24000:]
    return text


def _extract_device_code(text: str) -> str | None:
    match = re.search(r"\b[A-Z0-9]{4}(?:-[A-Z0-9]{4}){1,4}\b", text or "")
    return match.group(0) if match else None


def _oauth_session_payload(session_id: str):
    with _OAUTH_LOCK:
        item = _OAUTH_SESSIONS.get(session_id)
        if not item:
            return None
        proc = item.get("proc")
        if proc and item["status"] in {"pending", "running"} and proc.poll() is not None:
            item["status"] = "ready" if proc.returncode == 0 else "failed"
            item["completed_at"] = utcnow().isoformat()
            if proc.returncode != 0 and not item.get("error"):
                item["error"] = "OAuth 登录失败"
        return {
            "ok": True,
            "session_id": session_id,
            "status": item.get("status"),
            "provider_id": item.get("provider_id"),
            "auth_url": item.get("auth_url"),
            "device_code": item.get("device_code"),
            "output_log": item.get("output_log") or "",
            "error": item.get("error"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "completed_at": item.get("completed_at"),
        }


def _consume_oauth_session(session_id: str, master_fd: int, process: subprocess.Popen):
    try:
        while True:
            try:
                chunk = os.read(master_fd, 1024)
            except OSError:
                break
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="ignore")
            with _OAUTH_LOCK:
                item = _OAUTH_SESSIONS.get(session_id)
                if not item:
                    break
                item["status"] = "running"
                item["output_log"] = _trim_log((item.get("output_log") or "") + text)
                urls = extract_urls(text)
                if urls and not item.get("auth_url"):
                    item["auth_url"] = urls[0]
                code = _extract_device_code(text)
                if code and not item.get("device_code"):
                    item["device_code"] = code
                item["updated_at"] = utcnow().isoformat()
        returncode = process.wait()
        with _OAUTH_LOCK:
            item = _OAUTH_SESSIONS.get(session_id)
            if not item:
                return
            item["status"] = "ready" if returncode == 0 else ("cancelled" if item.get("cancelled") else "failed")
            item["completed_at"] = utcnow().isoformat()
            item["updated_at"] = item["completed_at"]
            if returncode != 0 and not item.get("error") and not item.get("cancelled"):
                item["error"] = f"OAuth 登录失败 (exit={returncode})"
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass


def start_oauth_session(provider_id: str):
    session_id = f"rt_{uuid.uuid4().hex[:16]}"
    command = openclaw_cmd(
        "models",
        "auth",
        "login",
        "--provider",
        provider_id,
        "--method",
        "oauth",
        "--set-default",
    )
    master_fd, slave_fd = pty.openpty()
    process = subprocess.Popen(
        command,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=runtime_env(),
        close_fds=True,
    )
    os.close(slave_fd)
    with _OAUTH_LOCK:
        _OAUTH_SESSIONS[session_id] = {
            "session_id": session_id,
            "provider_id": provider_id,
            "status": "pending",
            "auth_url": None,
            "device_code": None,
            "output_log": "",
            "error": None,
            "proc": process,
            "created_at": utcnow().isoformat(),
            "updated_at": utcnow().isoformat(),
            "completed_at": None,
            "cancelled": False,
        }
    thread = threading.Thread(target=_consume_oauth_session, args=(session_id, master_fd, process), daemon=True)
    thread.start()
    return session_id


def sync_api_key(provider_id: str, api_key: str, default_model: str):
    bootstrap_workspace = ensure_dir(WORKSPACE_ROOT / "_bootstrap")
    run_command(
        openclaw_cmd(
            "onboard",
            "--non-interactive",
            "--accept-risk",
            "--mode",
            "local",
            "--auth-choice",
            "openai-api-key",
            "--openai-api-key",
            api_key,
            "--skip-channels",
            "--skip-daemon",
            "--skip-health",
            "--skip-skills",
            "--skip-ui",
            "--workspace",
            str(bootstrap_workspace),
            "--json",
        ),
        timeout=120,
    )
    run_command(openclaw_cmd("models", "set", normalize_model_name(default_model)), timeout=40)
    return provider_status_payload(probe=True)


def create_app():
    app = Flask(__name__)

    @app.before_request
    def guard():
        if not verify_request_signature():
            abort(403)

    @app.get("/health")
    def health():
        return jsonify(health_payload())

    @app.get("/providers/status")
    def providers_status():
        probe = request.args.get("probe") == "1"
        payload = provider_status_payload(probe=probe)
        code = 200 if payload.get("ok") else 503
        return jsonify(payload), code

    @app.post("/providers/api-key")
    def providers_api_key():
        payload = request.get_json(silent=True) or {}
        provider_id = (payload.get("provider_id") or "openai").strip() or "openai"
        api_key = (payload.get("api_key") or "").strip()
        default_model = normalize_model_name(payload.get("default_model") or DEFAULT_MODEL)
        if not api_key:
            return jsonify({"ok": False, "error": "api_key 必填"}), 400
        try:
            status = sync_api_key(provider_id, api_key, default_model)
            return jsonify({"ok": True, "provider_id": provider_id, **status})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502

    @app.post("/providers/oauth/start")
    def providers_oauth_start():
        payload = request.get_json(silent=True) or {}
        provider_id = (payload.get("provider_id") or "openai-codex").strip() or "openai-codex"
        try:
            session_id = start_oauth_session(provider_id)
            return jsonify({"ok": True, **(_oauth_session_payload(session_id) or {"session_id": session_id})})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502

    @app.get("/providers/oauth/<session_id>")
    def provider_oauth_session(session_id: str):
        payload = _oauth_session_payload(session_id)
        if not payload:
            return jsonify({"ok": False, "error": "session 不存在"}), 404
        return jsonify(payload)

    @app.post("/providers/oauth/<session_id>/cancel")
    def provider_oauth_cancel(session_id: str):
        with _OAUTH_LOCK:
            item = _OAUTH_SESSIONS.get(session_id)
            if not item:
                return jsonify({"ok": False, "error": "session 不存在"}), 404
            proc = item.get("proc")
            item["cancelled"] = True
            item["status"] = "cancelled"
            item["completed_at"] = utcnow().isoformat()
            item["updated_at"] = item["completed_at"]
            if proc and proc.poll() is None:
                proc.kill()
        return jsonify({"ok": True, "status": "cancelled"})

    @app.post("/agent/turn")
    def agent_turn():
        payload = request.get_json(silent=True) or {}
        agent_role = (payload.get("agent_role") or "worker").strip() or "worker"
        agent_id = (payload.get("agent_id") or "").strip()
        session_key = (payload.get("session_key") or "").strip()
        namespace = (payload.get("namespace") or "default").strip() or "default"
        workspace = (payload.get("workspace") or "").strip() or str(_workspace_root_for_namespace(namespace, agent_role))
        model = normalize_model_name(payload.get("model") or provider_status_payload().get("default_model") or DEFAULT_MODEL)
        message = (payload.get("message") or "").strip()
        attachment_refs = payload.get("attachment_refs") or []
        if not agent_id or not session_key or not workspace or not message:
            return jsonify({"ok": False, "error": "缺少 agent turn 必填字段"}), 400

        ensure_agent(agent_id, workspace, model)
        paths = workspace_paths(workspace)
        attachment_lines = []
        fetched = []
        for ref in attachment_refs:
            safe_name = Path(ref.get("name") or "attachment").name
            try:
                result = stage_attachment(ref, paths["inputs"])
                fetched.append({"name": safe_name, **result})
                attachment_lines.append(f"- {safe_name}: {result.get('local_path')}")
            except Exception as exc:
                fetched.append({"name": safe_name, "error": str(exc)})
        final_message = message
        if attachment_lines:
            final_message += "\n\n[附件路径]\n" + "\n".join(attachment_lines)

        started_at = utcnow()
        try:
            raw = run_command(
                openclaw_cmd(
                    "agent",
                    "--agent",
                    agent_id,
                    "--session-id",
                    session_key,
                    "--message",
                    final_message,
                    "--json",
                    "--thinking",
                    "off",
                    "--timeout",
                    os.getenv("HOME_AGENT_BRIDGE_AGENT_TIMEOUT_SEC", "180"),
                ),
                timeout=int(os.getenv("HOME_AGENT_BRIDGE_AGENT_TIMEOUT_SEC", "180")) + 20,
            )
            ended_at = utcnow()
            parsed = parse_agent_response(raw)
            events, artifacts = parse_tool_results(agent_id, parsed.get("session_id"), started_at, ended_at)
            for url in extract_urls(parsed.get("reply_text") or ""):
                parsed_url = urlparse(url)
                artifacts.append(
                    {
                        "kind": "search_result",
                        "title": parsed_url.netloc or url,
                        "summary": "执行结果中引用了该链接",
                        "source_url": url,
                        "visibility": "user",
                    }
                )
            artifacts.extend(build_output_artifacts(agent_role, parsed.get("reply_text") or ""))
            return jsonify(
                {
                    "ok": True,
                    "bridge_run_id": parsed.get("bridge_run_id"),
                    "reply_text": parsed.get("reply_text"),
                    "provider": f"runtime:{parsed.get('provider')}",
                    "model": parsed.get("model") or model,
                    "duration_ms": parsed.get("duration_ms"),
                    "tool_events": events,
                    "artifacts": artifacts,
                    "raw_ref": f"{agent_id}:{parsed.get('session_id')}",
                    "meta": {
                        "session_id": parsed.get("session_id"),
                        "namespace": namespace,
                        "fetched_attachments": fetched,
                    },
                }
            )
        except Exception as exc:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": str(exc),
                        "tool_events": [],
                        "artifacts": [],
                        "meta": {"namespace": namespace, "fetched_attachments": fetched},
                    }
                ),
                502,
            )

    @app.post("/deliver/bluebubbles")
    def deliver_bluebubbles():
        return jsonify({"ok": False, "status": "disabled", "error": "纯 Docker 首版未启用 BlueBubbles 实发"}), 501

    return app


app = create_app()


if __name__ == "__main__":
    ensure_dir(RUNTIME_HOME)
    ensure_dir(SHARED_UPLOAD_ROOT)
    ensure_dir(WORKSPACE_ROOT)
    if ENABLE_GATEWAY:
        ensure_gateway_process()
    app.run(host="0.0.0.0", port=int(os.getenv("HOME_AGENT_BRIDGE_PORT", "18888")))
