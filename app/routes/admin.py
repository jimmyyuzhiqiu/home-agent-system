import uuid

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from werkzeug.security import generate_password_hash

from access import admin_required, normalize_role
from extensions import db
from models import Conversation, MemoryEntry, Message, OutboundDelivery, ProviderAuthSession, RunArtifact, RunEvent, TaskRunAudit, User, UserAgentBinding
from services.bridge_client import runtime_provider_status
from services.agent_runtime import build_run_status_payload, build_timeline, resolve_gateway
from services.conversations import ensure_user_agent_binding, ensure_user_conversation, ensure_user_isolation
from services.memory import ensure_system_memories
from services.system_state import get_bootstrap_state, get_provider_credential, serialize_provider_credential, setup_context_summary, update_bootstrap_state, upsert_provider_credential
from utils import in_date_range, summarize_text, utcnow


admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _last_active_map():
    latest = {}
    conversations = Conversation.query.order_by(Conversation.updated_at.desc(), Conversation.id.desc()).all()
    for conversation in conversations:
        latest.setdefault(conversation.user_id, conversation.updated_at or conversation.created_at)
    return latest


def _latest_run_by_user():
    latest = {}
    for run in TaskRunAudit.query.order_by(TaskRunAudit.id.desc()).all():
        latest.setdefault(run.user_id, run)
    return latest


def _binding_map():
    latest = {}
    for binding in UserAgentBinding.query.order_by(UserAgentBinding.id.desc()).all():
        latest[binding.user_id] = binding
    return latest


@admin_bp.route("/")
@login_required
@admin_required
def root():
    return redirect(url_for("admin.overview"))


@admin_bp.route("/overview")
@login_required
@admin_required
def overview():
    today = utcnow().date()
    recent_runs = TaskRunAudit.query.order_by(TaskRunAudit.created_at.desc()).limit(200).all()
    recent_deliveries = OutboundDelivery.query.order_by(OutboundDelivery.created_at.desc()).limit(200).all()
    bridge_state = resolve_gateway(current_user)
    active_user_ids = {
        conversation.user_id
        for conversation in Conversation.query.all()
        if conversation.updated_at and (utcnow() - conversation.updated_at).days < 7
    }
    durations = [run.duration_ms for run in recent_runs if run.duration_ms]
    failed_runs = [run for run in recent_runs if run.status in {"failed", "blocked"}]
    upload_count = Message.query.filter(Message.attachment_path.isnot(None)).count()
    metrics = [
        {"label": "7日活跃用户", "value": len(active_user_ids), "hint": "最近 7 天有会话更新的用户数"},
        {"label": "今日运行数", "value": sum(1 for run in recent_runs if run.created_at and run.created_at.date() == today), "hint": "今日触发的 planner/worker 流程"},
        {"label": "平均耗时", "value": f"{(sum(durations) / len(durations) / 1000):.1f}s" if durations else "0s", "hint": "最近运行的平均交付时间"},
        {"label": "异常率", "value": f"{(len(failed_runs) / len(recent_runs) * 100):.0f}%" if recent_runs else "0%", "hint": "失败或阻塞任务占比"},
        {"label": "投递失败", "value": sum(1 for row in recent_deliveries if row.status == "failed"), "hint": "最近 BlueBubbles 回发失败次数"},
        {"label": "安全提醒", "value": 1 if len(current_app.config.get("SECRET_KEY", "")) < 24 else 0, "hint": "需要管理员留意的高优先级安全项"},
        {"label": "附件总量", "value": upload_count, "hint": "已上传并留存在聊天记录中的文件"},
    ]
    latest_items = []
    for run in recent_runs[:8]:
        user = db.session.get(User, run.user_id)
        latest_items.append(
            {
                "username": user.username if user else "-",
                "status": run.status,
                "summary": summarize_text(run.final_summary or run.public_error_message or run.error_message or run.user_message, 100),
                "created_at": run.created_at,
            }
        )
    stale_runs = [run for run in recent_runs if run.status in {"queued", "running"} and run.created_at and (utcnow() - run.created_at).total_seconds() > 300]
    risk_items = [
        {
            "title": "最近失败 / 阻塞任务",
            "status": "failed" if failed_runs else "done",
            "summary": f"{len(failed_runs)} 条任务需要关注",
            "url": url_for("admin.admin_chats"),
        },
        {
            "title": "长时间未结束的任务",
            "status": "blocked" if stale_runs else "done",
            "summary": f"{len(stale_runs)} 条运行超过 5 分钟",
            "url": url_for("admin.admin_agents"),
        },
        {
            "title": "系统安全提醒",
            "status": "blocked" if len(current_app.config.get("SECRET_KEY", "")) < 24 else "done",
            "summary": "检查 SECRET_KEY 和管理员口令策略",
            "url": url_for("auth.security_setup"),
        },
        {
            "title": "容器内 Runtime",
            "status": "done" if bridge_state.get("chat_ready") else "failed",
            "summary": bridge_state.get("compact_label") or bridge_state.get("reason") or "待检查",
            "url": url_for("admin.runtime_settings"),
        },
    ]
    quick_links = [
        {"label": "查看聊天审计", "url": url_for("admin.admin_chats")},
        {"label": "查看会话审计", "url": url_for("admin.admin_session_audit")},
        {"label": "查看记忆审计", "url": url_for("admin.admin_memories")},
        {"label": "查看投递审计", "url": url_for("admin.admin_deliveries")},
        {"label": "Runtime 与 Provider", "url": url_for("admin.runtime_settings")},
        {"label": "前往安全中心", "url": url_for("auth.security_setup")},
    ]
    return render_template(
        "admin_overview.html",
        metrics=metrics,
        latest_items=latest_items,
        risk_items=risk_items,
        quick_links=quick_links,
        bridge_state=bridge_state,
        page_title="后台总览",
        page_subtitle="从系统健康、运行效率和审计密度三条线查看整体状态。",
    )


@admin_bp.route("/users", methods=["GET", "POST"])
@login_required
@admin_required
def admin_users():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            username = request.form.get("username", "").strip()
            display_name = request.form.get("display_name", "").strip() or username
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
                    display_name=display_name,
                    password_hash=generate_password_hash(password),
                    role=normalize_role(role),
                    execution_profile="admin_full" if normalize_role(role) == "admin" else "family_full",
                    openclaw_token=token,
                    note=note,
                    memory_namespace=f"user-{uuid.uuid4().hex[:12]}",
                    force_password_change=False,
                    onboarding_completed=False,
                )
                db.session.add(user)
                db.session.commit()
                ensure_user_agent_binding(user.id)
                ensure_user_conversation(user.id)
                ensure_system_memories(user.id)
                flash(f"用户 {username} 创建成功", "success")
        elif action == "reset_pwd":
            user_id = request.form.get("user_id", type=int)
            new_password = request.form.get("new_password", "")
            user = db.session.get(User, user_id)
            if user and new_password:
                user.password_hash = generate_password_hash(new_password)
                user.force_password_change = False
                db.session.commit()
                flash(f"已重置 {user.username} 的密码", "success")
        return redirect(url_for("admin.admin_users"))

    keyword = request.args.get("q", "").strip()
    role = request.args.get("role", "").strip()
    onboarding = request.args.get("onboarding", "").strip()
    users = User.query.order_by(User.id.asc()).all()
    last_active = _last_active_map()
    latest_user_runs = _latest_run_by_user()
    bindings = _binding_map()
    filtered = []
    for user in users:
        if keyword and keyword.lower() not in f"{user.username} {user.note or ''}".lower():
            continue
        if role and user.role_normalized != role:
            continue
        if onboarding == "done" and not user.onboarding_completed:
            continue
        if onboarding == "pending" and user.onboarding_completed:
            continue
        filtered.append(
            {
                "user": user,
                "last_active_at": last_active.get(user.id),
                "conversation_count": Conversation.query.filter_by(user_id=user.id).count(),
                "memory_count": MemoryEntry.query.filter_by(user_id=user.id).count(),
                "last_run": latest_user_runs.get(user.id),
                "binding": bindings.get(user.id),
            }
        )
    return render_template(
        "admin_users.html",
        rows=filtered,
        filters={"q": keyword, "role": role, "onboarding": onboarding},
        page_title="用户管理",
        page_subtitle="创建账户、筛选角色与查看近期活跃状态。",
    )


@admin_bp.route("/runtime")
@login_required
@admin_required
def runtime_settings():
    runtime_state = resolve_gateway(current_user)
    try:
        runtime_provider = runtime_provider_status(probe=False)
    except Exception as exc:
        runtime_provider = {"ok": False, "provider_ready": False, "last_error": str(exc)}
    bootstrap = get_bootstrap_state()
    latest_auth_session = ProviderAuthSession.query.order_by(ProviderAuthSession.created_at.desc()).first()
    return render_template(
        "admin_runtime.html",
        runtime_state=runtime_state,
        runtime_provider=runtime_provider,
        provider_record=serialize_provider_credential(get_provider_credential()),
        bootstrap_state=bootstrap,
        active_auth_session=latest_auth_session,
        setup_summary=setup_context_summary(),
        page_title="Runtime 与 Provider",
        page_subtitle="在浏览器内完成 API Key / OAuth 配置，并查看容器内 OpenClaw Runtime 的健康状态。",
    )


@admin_bp.route("/runtime/provider/default-model", methods=["POST"])
@login_required
@admin_required
def runtime_provider_default_model():
    preferred_model = request.form.get("preferred_model", "").strip()
    runtime_health_enabled = request.form.get("runtime_health_enabled") == "on"
    if not preferred_model:
        flash("默认模型不能为空", "warning")
        return redirect(url_for("admin.runtime_settings"))
    record = get_provider_credential()
    if record:
        upsert_provider_credential(
            provider_id=record.provider_id,
            auth_mode=record.auth_mode,
            status=record.status,
            default_model=preferred_model,
            profile_id=record.profile_id,
            encrypted_secret=record.encrypted_secret,
            secret_hint=record.secret_hint,
            expires_at=record.expires_at,
            last_error=record.last_error,
            meta=serialize_provider_credential(record).get("meta"),
        )
    update_bootstrap_state(preferred_model=preferred_model, runtime_health_enabled=runtime_health_enabled)
    flash("Runtime 默认模型已更新", "success")
    return redirect(url_for("admin.runtime_settings"))


@admin_bp.route("/users/<int:user_id>/bindings", methods=["POST"])
@login_required
@admin_required
def admin_user_bindings(user_id: int):
    user = db.session.get(User, user_id)
    if not user:
        flash("用户不存在", "danger")
        return redirect(url_for("admin.admin_users"))
    user.display_name = request.form.get("display_name", "").strip() or user.username
    desired_profile = "admin_full" if user.role_normalized == "admin" else "family_full"
    user.execution_profile = request.form.get("execution_profile", "").strip() or desired_profile
    if user.role_normalized == "admin":
        user.execution_profile = "admin_full"
    user.bluebubbles_enabled = request.form.get("bluebubbles_enabled") == "on"
    user.bluebubbles_recipient = request.form.get("bluebubbles_recipient", "").strip() or None
    user.bluebubbles_label = request.form.get("bluebubbles_label", "").strip() or None
    db.session.commit()
    flash("用户绑定已更新", "success")
    return redirect(url_for("admin.admin_users"))


@admin_bp.route("/users/<int:user_id>/repair-agent", methods=["POST"])
@login_required
@admin_required
def admin_repair_agent(user_id: int):
    user = db.session.get(User, user_id)
    if not user:
        flash("用户不存在", "danger")
        return redirect(url_for("admin.admin_users"))
    _, changed = ensure_user_isolation(user)
    if changed:
        db.session.commit()
    binding = ensure_user_agent_binding(user.id)
    ensure_user_conversation(user.id)
    ensure_system_memories(user.id)
    flash(f"已修复 {user.username} 的 agent / 会话 / 记忆绑定", "success")
    return redirect(url_for("admin.admin_agents"))


@admin_bp.route("/chats")
@login_required
@admin_required
def admin_chats():
    keyword = request.args.get("q", "").strip()
    role = request.args.get("role", "").strip()
    attachment = request.args.get("attachment", "").strip()
    dual = request.args.get("dual", "").strip()
    status = request.args.get("status", "").strip()
    stage = request.args.get("stage", "").strip()
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()

    records = (
        db.session.query(Message, User, Conversation)
        .join(User, Message.user_id == User.id)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .order_by(Message.created_at.desc())
        .limit(400)
        .all()
    )
    runs = TaskRunAudit.query.order_by(TaskRunAudit.created_at.desc()).limit(400).all()
    audit_by_conv = {}
    for run in runs:
        audit_by_conv.setdefault(run.conversation_id, run)

    filtered = []
    for message, user, conversation in records:
        run = audit_by_conv.get(conversation.id)
        if keyword and keyword.lower() not in f"{user.username} {message.content}".lower():
            continue
        if role and user.role_normalized != role:
            continue
        if attachment == "yes" and not message.attachment_path:
            continue
        if attachment == "no" and message.attachment_path:
            continue
        if dual == "yes" and not (run and run.dual_agent_triggered):
            continue
        if dual == "no" and run and run.dual_agent_triggered:
            continue
        if status and (not run or run.status != status):
            continue
        if stage and (not run or run.current_stage != stage):
            continue
        if not in_date_range(message.created_at, start_date, end_date):
            continue
        filtered.append({"message": message, "user": user, "conversation": conversation, "run": run, "run_payload": build_run_status_payload(run) if run else None})

    return render_template(
        "admin_chats.html",
        records=filtered,
        filters={"q": keyword, "role": role, "attachment": attachment, "dual": dual, "status": status, "stage": stage, "start_date": start_date, "end_date": end_date},
        page_title="聊天审计",
        page_subtitle="按用户、附件、双 Agent 执行情况过滤最近会话消息。",
    )


@admin_bp.route("/memories")
@login_required
@admin_required
def admin_memories():
    keyword = request.args.get("q", "").strip()
    kind = request.args.get("kind", "").strip()
    source = request.args.get("source", "").strip()
    username = request.args.get("username", "").strip()

    entries = (
        db.session.query(MemoryEntry, User)
        .join(User, MemoryEntry.user_id == User.id)
        .order_by(MemoryEntry.created_at.desc())
        .limit(800)
        .all()
    )
    filtered = []
    for entry, user in entries:
        if keyword and keyword.lower() not in entry.content.lower():
            continue
        if kind and entry.kind != kind:
            continue
        if source and entry.source != source:
            continue
        if username and username.lower() not in user.username.lower():
            continue
        filtered.append({"entry": entry, "user": user})

    return render_template(
        "admin_memories.html",
        rows=filtered,
        filters={"q": keyword, "kind": kind, "source": source, "username": username},
        page_title="记忆审计",
        page_subtitle="区分系统记忆、自动抽取和人工维护的长期上下文。",
    )


@admin_bp.route("/agents")
@login_required
@admin_required
def admin_agents():
    keyword = request.args.get("q", "").strip()
    rows = db.session.query(UserAgentBinding, User).join(User, UserAgentBinding.user_id == User.id).order_by(User.id.asc()).all()
    latest_user_runs = {}
    for run in TaskRunAudit.query.order_by(TaskRunAudit.id.desc()).all():
        latest_user_runs.setdefault(run.user_id, run)

    filtered = []
    for binding, user in rows:
        if keyword and keyword.lower() not in user.username.lower():
            continue
        run = latest_user_runs.get(user.id)
        filtered.append(
            {
                "binding": binding,
                "user": user,
                "run": run,
                "timeline": build_timeline(run),
                "result": summarize_text((run.final_summary or run.public_error_message or run.error_message) if run else "-", 100),
                "run_payload": build_run_status_payload(run) if run else None,
            }
        )

    return render_template(
        "admin_agents.html",
        rows=filtered,
        filters={"q": keyword},
        bridge_state=resolve_gateway(current_user),
        page_title="Agent 审计",
        page_subtitle="查看 planner / worker 最近状态、耗时与交付质量。",
    )


@admin_bp.route("/session-audit")
@login_required
@admin_required
def admin_session_audit():
    keyword = request.args.get("q", "").strip()
    role = request.args.get("role", "").strip()
    dual = request.args.get("dual", "").strip()
    status = request.args.get("status", "").strip()
    stage = request.args.get("stage", "").strip()
    archived = request.args.get("archived", "").strip()
    pinned = request.args.get("pinned", "").strip()
    has_attachment = request.args.get("has_attachment", "").strip()
    limit = min(max(request.args.get("limit", type=int, default=200), 50), 500)

    conversations = (
        db.session.query(Conversation, User)
        .join(User, Conversation.user_id == User.id)
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(limit)
        .all()
    )
    rows = []
    for conversation, user in conversations:
        latest_run = TaskRunAudit.query.filter_by(conversation_id=conversation.id).order_by(TaskRunAudit.id.desc()).first()
        message_count = Message.query.filter_by(conversation_id=conversation.id).count()
        if keyword and keyword.lower() not in f"{user.username} {conversation.title} {conversation.last_message_preview or ''}".lower():
            continue
        if role and user.role_normalized != role:
            continue
        if dual == "yes" and not (latest_run and latest_run.dual_agent_triggered):
            continue
        if dual == "no" and latest_run and latest_run.dual_agent_triggered:
            continue
        if status and (not latest_run or latest_run.status != status):
            continue
        if stage and (not latest_run or latest_run.current_stage != stage):
            continue
        if archived == "yes" and not conversation.archived_at:
            continue
        if archived == "no" and conversation.archived_at:
            continue
        if pinned == "yes" and not conversation.pinned:
            continue
        if pinned == "no" and conversation.pinned:
            continue
        if has_attachment == "yes" and not conversation.has_recent_attachment:
            continue
        if has_attachment == "no" and conversation.has_recent_attachment:
            continue
        rows.append(
            {
                "conversation_id": conversation.id,
                "username": user.username,
                "role": user.role_normalized,
                "title": conversation.title,
                "session_key": conversation.session_key,
                "agent_id": conversation.agent_id,
                "model": conversation.model,
                "last_provider": conversation.last_provider,
                "created_at": conversation.created_at,
                "last_called_at": conversation.last_called_at,
                "updated_at": conversation.updated_at,
                "message_count": message_count,
                "last_duration_ms": latest_run.duration_ms if latest_run else None,
                "dual_agent_triggered": latest_run.dual_agent_triggered if latest_run else False,
                "last_status": latest_run.status if latest_run else None,
                "last_summary": summarize_text((latest_run.final_summary or latest_run.public_error_message or latest_run.error_message) if latest_run else "", 120),
                "current_stage": latest_run.current_stage if latest_run else None,
                "archived": bool(conversation.archived_at),
                "pinned": bool(conversation.pinned),
                "has_attachment": bool(conversation.has_recent_attachment),
                "last_message_role": conversation.last_message_role,
                "last_delivery_status": conversation.last_delivery_status,
            }
        )

    return render_template(
        "admin_session_audit.html",
        rows=rows,
        filters={"q": keyword, "role": role, "dual": dual, "status": status, "stage": stage, "archived": archived, "pinned": pinned, "has_attachment": has_attachment, "limit": limit},
        page_title="会话审计",
        page_subtitle="从会话生命周期查看消息密度、模型路径与执行来源。",
    )


@admin_bp.route("/api/session-audit")
@login_required
@admin_required
def api_admin_session_audit():
    keyword = request.args.get("q", "").strip()
    role = request.args.get("role", "").strip()
    dual = request.args.get("dual", "").strip()
    status = request.args.get("status", "").strip()
    stage = request.args.get("stage", "").strip()
    archived = request.args.get("archived", "").strip()
    pinned = request.args.get("pinned", "").strip()
    has_attachment = request.args.get("has_attachment", "").strip()
    limit = min(max(int(request.args.get("limit", "100")), 1), 500)

    conversations = (
        db.session.query(Conversation, User)
        .join(User, Conversation.user_id == User.id)
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(limit)
        .all()
    )
    items = []
    for conversation, user in conversations:
        latest_run = TaskRunAudit.query.filter_by(conversation_id=conversation.id).order_by(TaskRunAudit.id.desc()).first()
        if keyword and keyword.lower() not in f"{user.username} {conversation.title} {conversation.last_message_preview or ''}".lower():
            continue
        if role and user.role_normalized != role:
            continue
        if dual == "yes" and not (latest_run and latest_run.dual_agent_triggered):
            continue
        if dual == "no" and latest_run and latest_run.dual_agent_triggered:
            continue
        if status and (not latest_run or latest_run.status != status):
            continue
        if stage and (not latest_run or latest_run.current_stage != stage):
            continue
        if archived == "yes" and not conversation.archived_at:
            continue
        if archived == "no" and conversation.archived_at:
            continue
        if pinned == "yes" and not conversation.pinned:
            continue
        if pinned == "no" and conversation.pinned:
            continue
        if has_attachment == "yes" and not conversation.has_recent_attachment:
            continue
        if has_attachment == "no" and conversation.has_recent_attachment:
            continue
        items.append(
            {
                "conversation_id": conversation.id,
                "username": user.username,
                "role": user.role_normalized,
                "title": conversation.title,
                "session_key": conversation.session_key,
                "agent_id": conversation.agent_id,
                "model": conversation.model,
                "last_provider": conversation.last_provider,
                "created_at": conversation.created_at.isoformat() if conversation.created_at else None,
                "updated_at": conversation.updated_at.isoformat() if conversation.updated_at else None,
                "last_called_at": conversation.last_called_at.isoformat() if conversation.last_called_at else None,
                "message_count": Message.query.filter_by(conversation_id=conversation.id).count(),
                "last_duration_ms": latest_run.duration_ms if latest_run else None,
                "dual_agent_triggered": bool(latest_run.dual_agent_triggered) if latest_run else False,
                "last_status": latest_run.status if latest_run else None,
                "current_stage": latest_run.current_stage if latest_run else None,
                "archived": bool(conversation.archived_at),
                "pinned": bool(conversation.pinned),
                "has_attachment": bool(conversation.has_recent_attachment),
                "last_message_role": conversation.last_message_role,
                "last_delivery_status": conversation.last_delivery_status,
            }
        )
    return jsonify({"ok": True, "count": len(items), "items": items})


@admin_bp.route("/deliveries")
@login_required
@admin_required
def admin_deliveries():
    keyword = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    rows = (
        db.session.query(OutboundDelivery, User)
        .join(User, OutboundDelivery.user_id == User.id)
        .order_by(OutboundDelivery.created_at.desc())
        .limit(300)
        .all()
    )
    filtered = []
    for delivery, user in rows:
        if keyword and keyword.lower() not in f"{user.username} {delivery.recipient} {delivery.message_preview or ''}".lower():
            continue
        if status and delivery.status != status:
            continue
        filtered.append({"delivery": delivery, "user": user, "run": db.session.get(TaskRunAudit, delivery.run_id)})
    return render_template(
        "admin_deliveries.html",
        rows=filtered,
        filters={"q": keyword, "status": status},
        page_title="投递审计",
        page_subtitle="查看 BlueBubbles 回发、失败原因和手动重发入口。",
    )


@admin_bp.route("/deliveries/<int:delivery_id>/retry", methods=["POST"])
@login_required
@admin_required
def retry_delivery(delivery_id: int):
    delivery = db.session.get(OutboundDelivery, delivery_id)
    if not delivery:
        flash("投递记录不存在", "danger")
        return redirect(url_for("admin.admin_deliveries"))
    run = db.session.get(TaskRunAudit, delivery.run_id)
    if not run:
        flash("关联运行不存在", "danger")
        return redirect(url_for("admin.admin_deliveries"))
    run.delivery_status = "pending"
    run.delivery_error = None
    db.session.commit()
    flash("已将该投递标记为待重发，请重新触发对应任务或后续补自动重发。", "info")
    return redirect(url_for("admin.admin_deliveries"))
