import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, abort, jsonify, request


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"


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
load_env_file(PROJECT_ROOT / ".env.runtime")


def get_env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def utcnow():
    return datetime.now(UTC)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def bridge_secret() -> str:
    return get_env("HOME_AGENT_BRIDGE_SHARED_SECRET", get_env("SECRET_KEY", "change-me"))


def bridge_root() -> Path:
    return ensure_dir(Path(get_env("HOME_AGENT_BRIDGE_DATA_ROOT", "~/Library/Application Support/home-agent-bridge")).expanduser())


def normalize_model_name(model: str | None) -> str:
    raw = (model or "").strip()
    if not raw:
        return get_env("OPENCLAW_MODEL", "openai-codex/gpt-5.3-codex")
    if "/" in raw:
        return raw
    provider = get_env("OPENCLAW_MODEL_PROVIDER", "openai-codex")
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
    ok = hmac.compare_digest(signature, expected)
    if not ok:
        print(
            "[bridge-signature-mismatch]",
            json.dumps(
                {
                    "path": request.path,
                    "timestamp": timestamp,
                    "got": signature,
                    "expected": expected,
                    "body": body,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return ok


def run_command(cmd: list[str], timeout: int = 120):
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(stderr or stdout or f"命令失败({proc.returncode})")
    return stdout


def openclaw_path() -> str | None:
    return shutil.which("openclaw")


def load_openclaw_config():
    if not OPENCLAW_CONFIG.exists():
        return {}
    try:
        return json.loads(OPENCLAW_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_openclaw_config(config: dict):
    OPENCLAW_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    OPENCLAW_CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def health_payload():
    config = load_openclaw_config()
    channels = (config.get("channels") or {})
    bluebubbles_enabled = bool(((channels.get("bluebubbles") or {}).get("enabled")))
    reason = []
    if not openclaw_path():
        reason.append("未找到 openclaw CLI")
    if not OPENCLAW_CONFIG.exists():
        reason.append("未找到 ~/.openclaw/openclaw.json")
    return {
        "ok": bool(openclaw_path()),
        "bridge_version": "2026.03-host-bridge",
        "openclaw_available": bool(openclaw_path()),
        "bluebubbles_available": bluebubbles_enabled,
        "browser_available": bool(openclaw_path()),
        "detail": "openclaw ready" if openclaw_path() else "openclaw unavailable",
        "reason": "；".join(reason) if reason else ("BlueBubbles 已启用" if bluebubbles_enabled else "BlueBubbles 未启用"),
    }


def agents_cache():
    try:
        raw = run_command(["openclaw", "agents", "list", "--json"], timeout=25)
        return json.loads(raw)
    except Exception:
        return []


def agent_lookup(agent_id: str):
    for item in agents_cache():
        if item.get("id") == agent_id:
            return item
    return None


def repair_agent_config(agent_id: str, workspace: str, model: str):
    config = load_openclaw_config()
    agents = (((config.setdefault("agents", {})).setdefault("list", [])))
    changed = False
    normalized_workspace = str(Path(workspace).expanduser())
    target = None
    for item in agents:
        if item.get("id") == agent_id:
            target = item
            break
    if not target:
        return False
    if target.get("workspace") != normalized_workspace:
        target["workspace"] = normalized_workspace
        changed = True
    if target.get("model") != model:
        target["model"] = model
        changed = True
    if not target.get("name"):
        target["name"] = agent_id
        changed = True
    if changed:
        meta = config.setdefault("meta", {})
        meta["lastTouchedVersion"] = meta.get("lastTouchedVersion") or "2026.2.15"
        meta["lastTouchedAt"] = utcnow().isoformat().replace("+00:00", "Z")
        save_openclaw_config(config)
    return changed


def ensure_agent(agent_id: str, workspace: str, model: str):
    existing = agent_lookup(agent_id)
    if existing:
        repair_agent_config(agent_id, workspace, model)
        return existing
    ensure_dir(Path(workspace).expanduser())
    raw = run_command(
        [
            "openclaw",
            "agents",
            "add",
            agent_id,
            "--workspace",
            str(Path(workspace).expanduser()),
            "--model",
            model,
            "--non-interactive",
            "--json",
        ],
        timeout=60,
    )
    try:
        return json.loads(raw)
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


def workspace_paths(workspace: str):
    root = ensure_dir(Path(workspace).expanduser())
    return {
        "root": root,
        "inputs": ensure_dir(root / "inputs"),
        "outputs": ensure_dir(root / "outputs"),
    }


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


def build_output_artifacts(agent_role: str, reply_text: str, workspace: str):
    paths = workspace_paths(workspace)
    output_path = paths["outputs"] / f"{agent_role}-{utcnow().strftime('%Y%m%d-%H%M%S')}.md"
    output_path.write_text(reply_text or "", encoding="utf-8")
    return [
        {
            "kind": "output_file",
            "title": "执行结果文件" if agent_role == "verify" else f"{agent_role} 输出",
            "summary": "可下载的文本结果",
            "visibility": "user" if agent_role == "verify" else "admin",
            "filename": output_path.name,
            "mime_type": "text/markdown",
            "inline_text": reply_text or "",
        }
    ]


def parse_agent_response(raw: str):
    data = json.loads(raw)
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


def create_app():
    app = Flask(__name__)

    @app.before_request
    def guard():
        if not verify_request_signature():
            abort(403)

    @app.get("/health")
    def health():
        return jsonify(health_payload())

    @app.post("/artifacts/fetch-upload")
    def artifacts_fetch_upload():
        payload = request.get_json(silent=True) or {}
        signed_url = payload.get("signed_url", "").strip()
        target_name = payload.get("target_name", "").strip() or "attachment"
        namespace = payload.get("user_namespace", "").strip() or "default"
        role = payload.get("agent_role", "worker").strip() or "worker"
        workspace = workspace_paths(f"{bridge_root() / 'users' / namespace / f'{role}-workspace'}")
        target_path = workspace["inputs"] / target_name
        try:
            return jsonify(fetch_upload(signed_url, target_path))
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502

    @app.post("/agent/turn")
    def agent_turn():
        payload = request.get_json(silent=True) or {}
        agent_role = (payload.get("agent_role") or "worker").strip()
        agent_id = (payload.get("agent_id") or "").strip()
        session_key = (payload.get("session_key") or "").strip()
        workspace = (payload.get("workspace") or "").strip()
        model = normalize_model_name(payload.get("model") or get_env("OPENCLAW_MODEL", "openai-codex/gpt-5.3-codex"))
        message = (payload.get("message") or "").strip()
        namespace = (payload.get("namespace") or "default").strip()
        attachment_refs = payload.get("attachment_refs") or []
        if not agent_id or not session_key or not workspace or not message:
            return jsonify({"ok": False, "error": "缺少 agent turn 必填字段"}), 400

        ensure_agent(agent_id, workspace, model)
        paths = workspace_paths(workspace)
        attachment_lines = []
        fetched = []
        for ref in attachment_refs:
            signed_url = (ref.get("signed_url") or "").strip()
            if not signed_url:
                continue
            safe_name = Path(ref.get("name") or "attachment").name
            target_path = paths["inputs"] / safe_name
            try:
                result = fetch_upload(signed_url, target_path)
                fetched.append({"name": safe_name, **result})
                attachment_lines.append(f"- {safe_name}: {target_path}")
            except Exception as exc:
                fetched.append({"name": safe_name, "error": str(exc)})
        final_message = message
        if attachment_lines:
            final_message += "\n\n[附件路径]\n" + "\n".join(attachment_lines)

        started_at = utcnow()
        try:
            raw = run_command(
                [
                    "openclaw",
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
                    get_env("HOME_AGENT_BRIDGE_AGENT_TIMEOUT_SEC", "180"),
                ],
                timeout=int(get_env("HOME_AGENT_BRIDGE_AGENT_TIMEOUT_SEC", "180")) + 20,
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
            artifacts.extend(build_output_artifacts(agent_role, parsed.get("reply_text") or "", workspace))
            return jsonify(
                {
                    "ok": True,
                    "bridge_run_id": parsed.get("bridge_run_id"),
                    "reply_text": parsed.get("reply_text"),
                    "provider": f"host-bridge:{parsed.get('provider')}",
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
        payload = request.get_json(silent=True) or {}
        recipient = (payload.get("recipient") or "").strip()
        text = (payload.get("text") or "").strip()
        if not recipient or not text:
            return jsonify({"ok": False, "status": "failed", "error": "recipient 与 text 必填"}), 400
        try:
            raw = run_command(
                [
                    "openclaw",
                    "message",
                    "send",
                    "--channel",
                    "bluebubbles",
                    "--target",
                    recipient,
                    "--message",
                    text,
                    "--json",
                ],
                timeout=60,
            )
            try:
                data = json.loads(raw)
            except Exception:
                data = {"raw": raw}
            provider_ref = data.get("messageId") or data.get("id") or data.get("raw") or "bluebubbles"
            return jsonify({"ok": True, "status": "done", "provider_ref": provider_ref})
        except Exception as exc:
            return jsonify({"ok": False, "status": "failed", "error": str(exc)}), 502

    return app


app = create_app()


if __name__ == "__main__":
    app.run(
        host=get_env("HOME_AGENT_BRIDGE_HOST", "127.0.0.1"),
        port=int(get_env("HOME_AGENT_BRIDGE_PORT", "18888")),
    )
