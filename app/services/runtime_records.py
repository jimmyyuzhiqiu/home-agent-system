import json

from extensions import db
from models import Message, OutboundDelivery, RunArtifact, RunEvent, TaskRunAudit
from utils import summarize_text, utcnow


PUBLIC_VISIBILITY = {"user", "admin"}


def add_run_event(
    run_id: int,
    *,
    stage: str,
    event_type: str,
    status: str,
    label: str,
    public_text: str = "",
    admin_text: str = "",
    progress_percent: int | None = None,
    meta: dict | None = None,
):
    event = RunEvent(
        run_id=run_id,
        stage=stage,
        event_type=event_type,
        status=status,
        label=label[:120],
        public_text=(public_text or "")[:4000],
        admin_text=(admin_text or "")[:12000],
        progress_percent=progress_percent,
        meta_json=json.dumps(meta or {}, ensure_ascii=False) if meta else None,
    )
    db.session.add(event)
    db.session.commit()
    return event


def add_run_artifact(
    run_id: int,
    *,
    kind: str,
    title: str,
    summary: str = "",
    source_url: str | None = None,
    file_path: str | None = None,
    mime_type: str | None = None,
    preview_image_path: str | None = None,
    visibility: str = "user",
    message_id: int | None = None,
    meta: dict | None = None,
):
    artifact = RunArtifact(
        run_id=run_id,
        message_id=message_id,
        kind=kind,
        title=(title or kind)[:255],
        summary=(summary or "")[:4000],
        source_url=source_url,
        file_path=file_path,
        mime_type=mime_type,
        preview_image_path=preview_image_path,
        visibility=visibility if visibility in PUBLIC_VISIBILITY else "user",
        meta_json=json.dumps(meta or {}, ensure_ascii=False) if meta else None,
    )
    db.session.add(artifact)
    if message_id:
        message = db.session.get(Message, message_id)
        if message:
            message.artifact_count = (message.artifact_count or 0) + 1
    run = db.session.get(TaskRunAudit, run_id)
    if run:
        run.tool_trace_count = (run.tool_trace_count or 0) + 1
    db.session.commit()
    return artifact


def record_delivery(
    user_id: int,
    run_id: int,
    *,
    channel: str,
    recipient: str,
    status: str,
    message_preview: str = "",
    error_message: str = "",
    provider_ref: str = "",
):
    delivery = OutboundDelivery(
        user_id=user_id,
        run_id=run_id,
        channel=channel,
        recipient=recipient[:160],
        status=status,
        message_preview=summarize_text(message_preview, 255) if message_preview else None,
        error_message=(error_message or "")[:4000] or None,
        provider_ref=(provider_ref or "")[:255] or None,
        delivered_at=utcnow() if status == "done" else None,
    )
    db.session.add(delivery)
    run = db.session.get(TaskRunAudit, run_id)
    if run:
        run.delivery_status = status
        run.delivery_error = delivery.error_message
    db.session.commit()
    return delivery


def serialize_run_events(run_id: int, visibility: str = "user") -> list[dict]:
    items = RunEvent.query.filter_by(run_id=run_id).order_by(RunEvent.created_at.asc(), RunEvent.id.asc()).all()
    is_admin = visibility == "admin"
    payload = []
    for item in items:
        payload.append(
            {
                "id": item.id,
                "stage": item.stage,
                "event_type": item.event_type,
                "status": item.status,
                "label": item.label,
                "text": item.admin_text if is_admin and item.admin_text else item.public_text,
                "progress_percent": item.progress_percent,
                "meta": json.loads(item.meta_json) if item.meta_json else {},
                "created_at": item.created_at.isoformat() if item.created_at else None,
            }
        )
    return payload


def serialize_run_artifacts(run_id: int, visibility: str = "user") -> list[dict]:
    items = RunArtifact.query.filter_by(run_id=run_id).order_by(RunArtifact.created_at.asc(), RunArtifact.id.asc()).all()
    allow_admin = visibility == "admin"
    payload = []
    for item in items:
        if item.visibility == "admin" and not allow_admin:
            continue
        payload.append(
            {
                "id": item.id,
                "kind": item.kind,
                "title": item.title,
                "summary": item.summary,
                "source_url": item.source_url,
                "file_path": item.file_path,
                "mime_type": item.mime_type,
                "preview_image_path": item.preview_image_path,
                "visibility": item.visibility,
                "meta": json.loads(item.meta_json) if item.meta_json else {},
                "created_at": item.created_at.isoformat() if item.created_at else None,
            }
        )
    return payload
