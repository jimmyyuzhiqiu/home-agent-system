from pathlib import Path

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_from_directory, url_for
from flask_login import current_user, login_required

from access import is_admin_user
from extensions import db
from models import Conversation, MemoryEntry, Message, RunArtifact, TaskRunAudit
from services.agent_runtime import build_run_status_payload, build_timeline, execute_run, resolve_gateway, start_async_run
from services.runtime_records import add_run_event, serialize_run_artifacts, serialize_run_events
from services.conversations import (
    archive_conversation,
    create_conversation,
    ensure_user_agent_binding,
    ensure_user_conversation,
    ensure_user_isolation,
    get_user_conversation,
    restore_conversation,
    rename_conversation,
    search_user_conversations,
    toggle_pin_conversation,
    touch_conversation,
)
from services.memory import (
    archive_memory,
    auto_extract_memories,
    ensure_system_memories,
    get_user_memories,
    list_user_memories,
    memory_forget,
    memory_remember,
    restore_memory,
    toggle_memory_pin,
)
from services.uploads import save_uploaded_file
from settings import UPLOAD_DIR
from utils import render_markdown, summarize_text


chat_bp = Blueprint("chat", __name__)


CAPABILITY_PROMPTS = [
    "帮我把这个需求拆成 5 个可执行步骤，并给出今天就能开始的行动清单。",
    "我上传了一份文档，请先总结重点，再列出我今天能推进的事项。",
    "请基于我们历史对话记忆，给我一个本周家庭自动化优化计划。",
    "请把这段想法整理成结构化方案：目标、限制、方案、下一步。",
]


def _run_payload_for_view(run: TaskRunAudit | None):
    if not run:
        return None
    payload = build_run_status_payload(run)
    if not is_admin_user(current_user):
        payload.pop("admin_events", None)
        payload.pop("admin_artifacts", None)
        payload.pop("technical_error_message", None)
    return payload


def _conversation_or_default():
    selected_id = request.args.get("conversation", type=int)
    if request.method == "POST":
        selected_id = request.form.get("conversation_id", type=int) or selected_id
    scope = request.args.get("scope", "active").strip() or "active"
    conversation = get_user_conversation(current_user.id, selected_id, include_archived=(scope in {"archived", "all"}) or bool(selected_id))
    return conversation


def _create_run(conversation: Conversation, user_message: Message):
    run = TaskRunAudit(
        conversation_id=conversation.id,
        user_id=current_user.id,
        user_message_id=user_message.id,
        user_message=user_message.content,
        dual_agent_triggered=True,
        status="queued",
        public_status_label="等待队列",
        current_stage="queued",
        progress_percent=5,
        planner_status="pending",
        worker_status="pending",
        verify_status="pending",
    )
    db.session.add(run)
    db.session.commit()
    add_run_event(
        run.id,
        stage="queued",
        event_type="queued",
        status="queued",
        label="任务已进入队列",
        public_text="系统已接收任务，准备开始处理",
        admin_text=f"conversation={conversation.id}",
        progress_percent=5,
    )
    return run


def _create_message(conversation: Conversation, role: str, content: str, attachment_info: dict | None = None):
    message = Message(
        conversation_id=conversation.id,
        user_id=current_user.id,
        role=role,
        content=content,
        attachment_name=(attachment_info or {}).get("attachment_name"),
        attachment_path=(attachment_info or {}).get("attachment_path"),
    )
    db.session.add(message)
    db.session.commit()
    touch_conversation(
        conversation,
        content,
        role=role,
        has_attachment=bool((attachment_info or {}).get("attachment_path")),
    )
    return message


def _conversation_view_sections(current_conversation: Conversation):
    def enrich(items):
        for item in items:
            run = TaskRunAudit.query.filter_by(conversation_id=item.id).order_by(TaskRunAudit.id.desc()).first()
            item.last_status = run.status if run else "idle"
        return items

    keyword = request.args.get("q", "").strip()
    scope = request.args.get("scope", "active").strip() or "active"
    active_items = enrich(search_user_conversations(current_user.id, keyword=keyword, scope="active", limit=30))
    archived_items = enrich(search_user_conversations(current_user.id, keyword=keyword, scope="archived", limit=20))
    pinned_items = [item for item in active_items if item.pinned]
    recent_items = [item for item in active_items if not item.pinned]
    if current_conversation.archived_at and all(item.id != current_conversation.id for item in archived_items):
        archived_items = [current_conversation] + archived_items
    return {
        "pinned_items": pinned_items,
        "recent_items": recent_items,
        "archived_items": archived_items,
        "conversation_filters": {"q": keyword, "scope": scope},
    }


def _handle_memory_command(conversation: Conversation, user_text: str, attachment_info: dict | None = None):
    user_message = _create_message(conversation, "user", user_text, attachment_info)
    if user_text.startswith("/remember "):
        ok, message = memory_remember(current_user.id, user_text.replace("/remember ", "", 1))
        assistant_text = f"[记忆] {message if ok else '失败'}"
    else:
        deleted = memory_forget(current_user.id, user_text.replace("/forget ", "", 1))
        assistant_text = f"[记忆] 已删除 {deleted} 条"
    assistant_message = _create_message(conversation, "assistant", assistant_text)
    return user_message, assistant_message


def _handle_sync_send(conversation: Conversation, text: str, attachment_info: dict | None):
    user_text = text or "[仅上传附件]"
    if user_text.startswith("/remember ") or user_text.startswith("/forget "):
        _handle_memory_command(conversation, user_text, attachment_info)
        return None

    user_message = _create_message(conversation, "user", user_text, attachment_info)
    auto_extract_memories(current_user.id, user_text)
    ensure_system_memories(current_user.id)
    run = _create_run(conversation, user_message)
    execute_run(run.id)
    return run.id


def _build_chat_payload(conversation: Conversation):
    binding = ensure_user_agent_binding(current_user.id)
    messages = Message.query.filter_by(conversation_id=conversation.id).order_by(Message.created_at.asc()).all()
    latest_run = TaskRunAudit.query.filter_by(conversation_id=conversation.id).order_by(TaskRunAudit.id.desc()).first()
    active_run = (
        TaskRunAudit.query.filter_by(conversation_id=conversation.id)
        .filter(TaskRunAudit.status.in_(["queued", "running"]))
        .order_by(TaskRunAudit.id.desc())
        .first()
    )
    memories = get_user_memories(current_user.id, limit=8)
    memory_updates = get_user_memories(current_user.id, limit=5, include_archived=True)
    timeline = build_timeline(latest_run)
    gateway = resolve_gateway(current_user)
    latest_summary = summarize_text((latest_run.final_summary or latest_run.public_error_message or latest_run.error_message) if latest_run else "", 84)
    latest_run_payload = _run_payload_for_view(latest_run)
    sections = _conversation_view_sections(conversation)
    return {
        "conversation": conversation,
        "messages": messages,
        "binding": binding,
        "latest_run": latest_run,
        "active_run": active_run,
        "timeline": timeline,
        "memories": memories,
        "memory_updates": memory_updates,
        "gateway_state": gateway,
        "capability_prompts": CAPABILITY_PROMPTS,
        "latest_summary": latest_summary,
        "latest_run_payload": latest_run_payload,
        "page_title": conversation.title,
        "page_subtitle": conversation.last_message_preview or "描述你的目标、限制和结果格式，系统会先拆解再执行。",
        "conversation_sections": sections,
        "is_archived_view": bool(conversation.archived_at),
    }


@chat_bp.route("/healthz")
def healthz():
    return {"ok": True, "service": "home-agent-app"}


@chat_bp.route("/chat", methods=["GET", "POST"])
@login_required
def chat():
    _, changed = ensure_user_isolation(current_user)
    if changed:
        db.session.commit()
    ensure_user_agent_binding(current_user.id)
    ensure_system_memories(current_user.id)
    conversation = _conversation_or_default()

    if request.method == "POST":
        text = request.form.get("message", "").strip()
        file = request.files.get("attachment")
        attachment_info = None
        if conversation.archived_at:
            flash("当前会话已归档，请先恢复后再继续发送。", "warning")
            return redirect(url_for("chat.chat", conversation=conversation.id, scope="archived"))
        if file and file.filename:
            try:
                attachment_info = save_uploaded_file(current_user, file)
            except ValueError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("chat.chat", conversation=conversation.id))
        if not text and not attachment_info:
            flash("请输入消息或上传附件", "warning")
            return redirect(url_for("chat.chat", conversation=conversation.id))
        _handle_sync_send(conversation, text, attachment_info)
        return redirect(url_for("chat.chat", conversation=conversation.id))

    return render_template("chat.html", **_build_chat_payload(conversation))


@chat_bp.route("/chat/conversations", methods=["POST"])
@login_required
def create_chat_conversation():
    title = request.form.get("title", "").strip() or "新会话"
    conversation = create_conversation(current_user.id, title)
    flash("已创建新会话", "success")
    return redirect(url_for("chat.chat", conversation=conversation.id))


@chat_bp.route("/chat/conversations/<int:conversation_id>/rename", methods=["POST"])
@login_required
def rename_chat_conversation(conversation_id: int):
    title = request.form.get("title", "")
    conversation = rename_conversation(current_user.id, conversation_id, title)
    if not conversation:
        abort(404)
    flash("会话名称已更新", "success")
    return redirect(url_for("chat.chat", conversation=conversation.id))


@chat_bp.route("/chat/conversations/<int:conversation_id>/pin", methods=["POST"])
@login_required
def pin_chat_conversation(conversation_id: int):
    conversation = toggle_pin_conversation(current_user.id, conversation_id)
    if not conversation:
        abort(404)
    flash("会话置顶状态已更新", "success")
    return redirect(url_for("chat.chat", conversation=conversation.id))


@chat_bp.route("/chat/conversations/<int:conversation_id>/archive", methods=["POST"])
@login_required
def archive_chat_conversation(conversation_id: int):
    next_conversation = archive_conversation(current_user.id, conversation_id)
    flash("会话已归档", "info")
    return redirect(url_for("chat.chat", conversation=next_conversation.id))


@chat_bp.route("/chat/conversations/<int:conversation_id>/restore", methods=["POST"])
@login_required
def restore_chat_conversation(conversation_id: int):
    conversation = restore_conversation(current_user.id, conversation_id)
    if not conversation:
        abort(404)
    flash("会话已恢复", "success")
    return redirect(url_for("chat.chat", conversation=conversation.id))


@chat_bp.route("/memories", methods=["GET", "POST"])
@login_required
def memories():
    ensure_system_memories(current_user.id)
    if request.method == "POST":
        content = request.form.get("content", "").strip()
        kind = request.form.get("kind", "manual").strip() or "manual"
        if not content:
            flash("请输入记忆内容", "warning")
        else:
            db.session.add(MemoryEntry(user_id=current_user.id, kind=kind, content=content, source="manual"))
            db.session.commit()
            flash("记忆已保存", "success")
        return redirect(url_for("chat.memories"))

    kind = request.args.get("kind", "").strip()
    source = request.args.get("source", "").strip()
    keyword = request.args.get("q", "").strip()
    include_archived = request.args.get("show") == "archived"
    sort = request.args.get("sort", "recent").strip() or "recent"
    entries = list_user_memories(current_user.id, kind=kind, source=source, keyword=keyword, include_archived=include_archived, sort=sort)
    grouped_entries = {
        "pinned": [entry for entry in entries if entry.pinned and entry.archived_at is None],
        "manual": [entry for entry in entries if entry.source == "manual" and entry.archived_at is None],
        "auto": [entry for entry in entries if entry.source == "auto" and entry.archived_at is None],
        "system": [entry for entry in entries if entry.source == "system" and entry.archived_at is None],
        "archived": [entry for entry in entries if entry.archived_at is not None],
    }
    latest_run = TaskRunAudit.query.filter_by(user_id=current_user.id).order_by(TaskRunAudit.id.desc()).first()
    return render_template(
        "memories.html",
        entries=entries,
        grouped_entries=grouped_entries,
        filters={"kind": kind, "source": source, "q": keyword, "show": "archived" if include_archived else "active", "sort": sort},
        page_title="我的记忆",
        page_subtitle="查看系统记住了什么、为什么记住，以及你希望它如何协作。",
        latest_summary=summarize_text((latest_run.final_summary or latest_run.public_error_message or latest_run.error_message) if latest_run else "", 84),
    )


@chat_bp.route("/memories/<int:memory_id>/pin", methods=["POST"])
@login_required
def pin_memory(memory_id: int):
    entry = toggle_memory_pin(current_user.id, memory_id)
    if not entry:
        abort(404)
    flash("记忆置顶状态已更新", "success")
    return redirect(url_for("chat.memories"))


@chat_bp.route("/memories/<int:memory_id>/archive", methods=["POST"])
@login_required
def archive_memory_entry(memory_id: int):
    entry = archive_memory(current_user.id, memory_id)
    if not entry:
        abort(404)
    flash("记忆已归档", "info")
    return redirect(url_for("chat.memories"))


@chat_bp.route("/memories/<int:memory_id>/restore", methods=["POST"])
@login_required
def restore_memory_entry(memory_id: int):
    entry = restore_memory(current_user.id, memory_id)
    if not entry:
        abort(404)
    flash("记忆已恢复", "success")
    return redirect(url_for("chat.memories"))


@chat_bp.route("/api/chat/send", methods=["POST"])
@login_required
def api_chat_send():
    _, changed = ensure_user_isolation(current_user)
    if changed:
        db.session.commit()
    ensure_system_memories(current_user.id)
    conversation = get_user_conversation(current_user.id, request.form.get("conversation_id", type=int))
    if conversation.archived_at:
        return jsonify({"ok": False, "error": "当前会话已归档，请先恢复后再继续发送。"}), 400
    text = request.form.get("message", "").strip()
    user_text = text or "[仅上传附件]"
    if not (user_text.startswith("/remember ") or user_text.startswith("/forget ")):
        backend_state = resolve_gateway(current_user)
        if not backend_state.get("chat_ready"):
            return (
                jsonify(
                    {
                        "ok": False,
                        "code": "execution_backend_unavailable",
                        "error": "执行后端未就绪，请联系管理员检查 Runtime 与 Provider 配置。",
                        "backend_status": backend_state.get("compact_label"),
                        "backend_reason": backend_state.get("reason"),
                    }
                ),
                503,
            )
    file = request.files.get("attachment")
    attachment_info = None
    if file and file.filename:
        try:
            attachment_info = save_uploaded_file(current_user, file)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    if not text and not attachment_info:
        return jsonify({"ok": False, "error": "请输入消息或上传附件"}), 400

    if user_text.startswith("/remember ") or user_text.startswith("/forget "):
        user_message, assistant_message = _handle_memory_command(conversation, user_text, attachment_info)
        return jsonify(
            {
                "ok": True,
                "command": True,
                "conversation_id": conversation.id,
                "user_message_id": user_message.id,
                "user_message_created_at": user_message.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "attachment_name": user_message.attachment_name,
                "attachment_path": user_message.attachment_path,
                "assistant_message_id": assistant_message.id,
                "assistant_html": str(render_markdown(assistant_message.content)),
                "assistant_text": assistant_message.content,
            }
        )

    user_message = _create_message(conversation, "user", user_text, attachment_info)
    auto_extract_memories(current_user.id, user_text)
    ensure_system_memories(current_user.id)
    run = _create_run(conversation, user_message)
    start_async_run(current_app._get_current_object(), run.id)
    return jsonify(
        {
            "ok": True,
            "conversation_id": conversation.id,
            "run_id": run.id,
            "user_message_id": user_message.id,
            "user_message_text": user_message.content,
            "user_message_created_at": user_message.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "attachment_name": user_message.attachment_name,
            "attachment_path": user_message.attachment_path,
        }
    )


@chat_bp.route("/api/runs/<int:run_id>/status")
@login_required
def api_run_status(run_id: int):
    run = TaskRunAudit.query.filter_by(id=run_id, user_id=current_user.id).first()
    if not run and is_admin_user(current_user):
        run = db.session.get(TaskRunAudit, run_id)
    if not run:
        abort(404)
    return jsonify(_run_payload_for_view(run))


@chat_bp.route("/api/runs/<int:run_id>/events")
@login_required
def api_run_events(run_id: int):
    run = TaskRunAudit.query.filter_by(id=run_id, user_id=current_user.id).first()
    if not run and is_admin_user(current_user):
        run = db.session.get(TaskRunAudit, run_id)
    if not run:
        abort(404)
    visibility = "admin" if is_admin_user(current_user) and request.args.get("visibility") == "admin" else "user"
    return jsonify({"ok": True, "items": serialize_run_events(run.id, visibility=visibility)})


@chat_bp.route("/api/runs/<int:run_id>/artifacts")
@login_required
def api_run_artifacts(run_id: int):
    run = TaskRunAudit.query.filter_by(id=run_id, user_id=current_user.id).first()
    if not run and is_admin_user(current_user):
        run = db.session.get(TaskRunAudit, run_id)
    if not run:
        abort(404)
    visibility = "admin" if is_admin_user(current_user) and request.args.get("visibility") == "admin" else "user"
    return jsonify({"ok": True, "items": serialize_run_artifacts(run.id, visibility=visibility)})


@chat_bp.route("/gateway/health")
@login_required
def gateway_health():
    return jsonify(resolve_gateway(current_user))


@chat_bp.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    query = Message.query.filter_by(attachment_path=filename)
    if not is_admin_user(current_user):
        query = query.filter_by(user_id=current_user.id)
    owned = query.first()
    if not owned:
        artifact_query = RunArtifact.query.filter_by(file_path=filename)
        if not is_admin_user(current_user):
            artifact_query = artifact_query.join(TaskRunAudit, RunArtifact.run_id == TaskRunAudit.id).filter(TaskRunAudit.user_id == current_user.id)
        owned = artifact_query.first()
    if not owned:
        abort(403)

    rel = Path(filename)
    if rel.is_absolute() or ".." in rel.parts:
        abort(400)
    base_dir = UPLOAD_DIR
    if len(rel.parts) > 1:
        base_dir = UPLOAD_DIR / rel.parts[0]
        rel_name = str(Path(*rel.parts[1:]))
    else:
        rel_name = rel.name
    return send_from_directory(base_dir, rel_name, as_attachment=False)
