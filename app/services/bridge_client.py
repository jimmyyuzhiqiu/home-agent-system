import hashlib
import hmac
import json
from datetime import timedelta
from urllib.parse import urlparse

import requests
from itsdangerous import URLSafeSerializer

from settings import get_bridge_shared_secret, get_bridge_url, get_env, get_public_base_url
from utils import normalize_url, summarize_text, utcnow


_BRIDGE_CACHE = {}


def _bridge_secret() -> str:
    return get_bridge_shared_secret() or "change-me"


def _bridge_candidates() -> list[str]:
    primary = normalize_url(get_bridge_url())
    parsed = urlparse(primary)
    candidates = [primary]
    if parsed.hostname in {"host.docker.internal", "runtime"}:
        local_host = f"{parsed.scheme or 'http'}://127.0.0.1"
        localhost = f"{parsed.scheme or 'http'}://localhost"
        if parsed.port:
            local_host = f"{local_host}:{parsed.port}"
            localhost = f"{localhost}:{parsed.port}"
        candidates.extend([local_host, localhost])
    deduped = []
    for item in candidates:
        if item and item not in deduped:
            deduped.append(item)
    return deduped


def _sign_payload(method: str, path: str, timestamp: str, body: str) -> str:
    message = "\n".join([method.upper(), path, timestamp, body]).encode("utf-8")
    return hmac.new(_bridge_secret().encode("utf-8"), message, hashlib.sha256).hexdigest()


def bridge_headers(method: str, path: str, body: str = "") -> dict[str, str]:
    timestamp = str(int(utcnow().timestamp()))
    return {
        "Content-Type": "application/json",
        "X-Home-Agent-Timestamp": timestamp,
        "X-Home-Agent-Signature": _sign_payload(method, path, timestamp, body),
    }


def bridge_request(method: str, path: str, payload: dict | None = None, timeout: float = 25):
    json_body = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")) if payload is not None else ""
    headers = bridge_headers(method, path, json_body)
    last_error = None
    for base_url in _bridge_candidates():
        try:
            response = requests.request(method.upper(), base_url + path, data=json_body if payload is not None else None, headers=headers, timeout=timeout)
            if "application/json" not in response.headers.get("Content-Type", ""):
                response.raise_for_status()
                raise ValueError("bridge 返回非 JSON")
            data = response.json()
            if response.status_code >= 400:
                if isinstance(data, dict) and data.get("ok") is False:
                    return data
                response.raise_for_status()
            return data
        except Exception as exc:
            last_error = exc
    if last_error:
        raise last_error
    raise RuntimeError("bridge 未配置")


def resolve_bridge_state():
    cache_key = "|".join(_bridge_candidates())
    cached = _BRIDGE_CACHE.get(cache_key)
    if cached and (utcnow() - cached["checked_at"]).total_seconds() < 12:
        return cached["value"]

    value = {
        "ok": False,
        "chat_ready": False,
        "bridge_url": _bridge_candidates()[0],
        "detail": "未探测",
        "status_label": "运行时离线",
        "compact_label": "运行时离线",
        "reason": "容器内运行时未连接",
        "control_url": normalize_url(get_env("OPENCLAW_BASE_URL", "")),
    }
    for base_url in _bridge_candidates():
        try:
            response = requests.get(base_url + "/health", timeout=4)
            response.raise_for_status()
            if "application/json" not in response.headers.get("Content-Type", ""):
                raise ValueError("bridge 返回非 JSON")
            data = response.json()
            value.update(
                {
                    "ok": bool(data.get("ok")),
                    "chat_ready": bool(data.get("chat_ready")),
                    "bridge_url": base_url,
                    "detail": data.get("detail") or "runtime online",
                    "status_label": "运行时在线" if data.get("chat_ready") else "运行时待配置",
                    "compact_label": "运行时在线" if data.get("chat_ready") else "运行时待配置",
                    "reason": summarize_text(data.get("reason") or data.get("detail") or "", 84),
                    "bridge_version": data.get("bridge_version"),
                    "openclaw_available": bool(data.get("openclaw_available")),
                    "browser_available": bool(data.get("browser_available")),
                    "gateway_running": bool(data.get("gateway_running")),
                    "provider_ready": bool(data.get("provider_ready")),
                    "default_model": data.get("default_model"),
                    "runtime_profile": data.get("runtime_profile"),
                }
            )
            break
        except Exception as exc:
            value["detail"] = str(exc)
            value["reason"] = "容器内运行时未连接或签名校验失败"
    _BRIDGE_CACHE[cache_key] = {"checked_at": utcnow(), "value": value}
    return value


def _upload_serializer():
    return URLSafeSerializer(_bridge_secret(), salt="internal-upload")


def build_internal_upload_token(message) -> str:
    expires_at = utcnow() + timedelta(minutes=15)
    payload = {
        "message_id": message.id,
        "attachment_path": message.attachment_path,
        "attachment_name": message.attachment_name,
        "expires_at": expires_at.isoformat(),
    }
    return _upload_serializer().dumps(payload)


def parse_internal_upload_token(token: str) -> dict:
    return _upload_serializer().loads(token)


def build_upload_ref(message) -> dict | None:
    if not getattr(message, "attachment_path", None):
        return None
    token = build_internal_upload_token(message)
    return {
        "name": message.attachment_name,
        "path": message.attachment_path,
        "signed_url": f"{normalize_url(get_public_base_url())}/internal/uploads/{token}",
    }


def bridge_agent_turn(payload: dict):
    return bridge_request("POST", "/agent/turn", payload=payload, timeout=float(get_env("HOME_AGENT_BRIDGE_TIMEOUT_SEC", "180")))


def bridge_deliver_bluebubbles(payload: dict):
    return bridge_request("POST", "/deliver/bluebubbles", payload=payload, timeout=35)


def runtime_provider_status(probe: bool = False):
    suffix = "?probe=1" if probe else ""
    return bridge_request("GET", f"/providers/status{suffix}", timeout=45)


def runtime_set_api_key(payload: dict):
    return bridge_request("POST", "/providers/api-key", payload=payload, timeout=180)


def runtime_start_oauth(payload: dict):
    return bridge_request("POST", "/providers/oauth/start", payload=payload, timeout=35)


def runtime_get_oauth_session(session_id: str):
    return bridge_request("GET", f"/providers/oauth/{session_id}", timeout=25)


def runtime_cancel_oauth_session(session_id: str):
    return bridge_request("POST", f"/providers/oauth/{session_id}/cancel", payload={}, timeout=25)
