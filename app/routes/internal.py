import hashlib
import hmac
import json
from datetime import datetime
from pathlib import Path

from flask import Blueprint, abort, jsonify, request, send_from_directory

from extensions import csrf, db
from models import Message, TaskRunAudit
from services.bridge_client import parse_internal_upload_token
from services.runtime_records import add_run_artifact, add_run_event
from settings import UPLOAD_DIR, get_bridge_shared_secret


internal_bp = Blueprint("internal", __name__, url_prefix="/internal")


def _verify_signature() -> bool:
    timestamp = request.headers.get("X-Home-Agent-Timestamp", "").strip()
    signature = request.headers.get("X-Home-Agent-Signature", "").strip()
    if not timestamp or not signature:
        return False
    try:
        now_ts = int(datetime.utcnow().timestamp())
        request_ts = int(timestamp)
    except ValueError:
        return False
    if abs(now_ts - request_ts) > 300:
        return False
    body = request.get_data(as_text=True) or ""
    message = "\n".join([request.method.upper(), request.path, timestamp, body]).encode("utf-8")
    expected = hmac.new(get_bridge_shared_secret().encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


@internal_bp.before_request
def _guard_internal():
    if request.endpoint == "internal.internal_upload":
        return None
    if not _verify_signature():
        abort(403)
    return None


@internal_bp.route("/uploads/<token>")
@csrf.exempt
def internal_upload(token: str):
    try:
        payload = parse_internal_upload_token(token)
    except Exception:
        abort(403)
    expires_at = payload.get("expires_at")
    try:
        if not expires_at or datetime.utcnow() > datetime.fromisoformat(expires_at):
            abort(403)
    except ValueError:
        abort(403)
    message = db.session.get(Message, int(payload.get("message_id", 0) or 0))
    if not message or message.attachment_path != payload.get("attachment_path"):
        abort(404)

    rel = Path(message.attachment_path)
    if rel.is_absolute() or ".." in rel.parts:
        abort(400)
    base_dir = UPLOAD_DIR
    if len(rel.parts) > 1:
        base_dir = UPLOAD_DIR / rel.parts[0]
        rel_name = str(Path(*rel.parts[1:]))
    else:
        rel_name = rel.name
    return send_from_directory(base_dir, rel_name, as_attachment=False)


@internal_bp.route("/bridge/callback", methods=["POST"])
@csrf.exempt
def bridge_callback():
    payload = request.get_json(silent=True) or {}
    run = db.session.get(TaskRunAudit, payload.get("run_id"))
    if not run:
        abort(404)
    for event in payload.get("events") or []:
        add_run_event(
            run.id,
            stage=event.get("stage") or "worker",
            event_type=event.get("event_type") or "bridge",
            status=event.get("status") or "running",
            label=event.get("label") or "桥回调",
            public_text=event.get("public_text") or "",
            admin_text=event.get("admin_text") or "",
            progress_percent=event.get("progress_percent"),
            meta=event.get("meta") or {},
        )
    return jsonify({"ok": True})


@internal_bp.route("/runs/<int:run_id>/artifacts", methods=["POST"])
@csrf.exempt
def internal_run_artifacts(run_id: int):
    run = db.session.get(TaskRunAudit, run_id)
    if not run:
        abort(404)
    payload = request.get_json(silent=True) or {}
    stored = []
    for item in payload.get("artifacts") or []:
        artifact = add_run_artifact(
            run_id,
            kind=item.get("kind") or "output_file",
            title=item.get("title") or "执行产物",
            summary=item.get("summary") or "",
            source_url=item.get("source_url"),
            file_path=item.get("file_path"),
            mime_type=item.get("mime_type"),
            preview_image_path=item.get("preview_image_path"),
            visibility=item.get("visibility") or "user",
            message_id=item.get("message_id"),
            meta=item.get("meta") or {},
        )
        stored.append(artifact.id)
    return jsonify({"ok": True, "stored": stored})
