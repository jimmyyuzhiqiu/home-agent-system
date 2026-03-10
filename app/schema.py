from datetime import timedelta

from sqlalchemy import inspect

from access import normalize_role
from extensions import db
from models import (
    Conversation,
    LoginAttempt,
    MemoryEntry,
    Message,
    OutboundDelivery,
    ProviderAuthSession,
    ProviderCredential,
    RunArtifact,
    RunEvent,
    SystemBootstrapState,
    TaskRunAudit,
    User,
    UserAgentBinding,
)
from services.system_state import DEFAULT_PROVIDER_MODEL
from settings import get_env
from utils import summarize_text, utcnow


def _normalize_model_name(model: str | None) -> str:
    raw = (model or "").strip()
    if not raw:
        return get_env("OPENCLAW_MODEL", DEFAULT_PROVIDER_MODEL)
    if "/" in raw:
        return raw
    provider = get_env("OPENCLAW_MODEL_PROVIDER", "openai-codex")
    return f"{provider}/{raw}"


def ensure_schema_compat():
    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    for model in [
        User,
        UserAgentBinding,
        Conversation,
        Message,
        MemoryEntry,
        TaskRunAudit,
        RunEvent,
        RunArtifact,
        OutboundDelivery,
        ProviderCredential,
        ProviderAuthSession,
        SystemBootstrapState,
        LoginAttempt,
    ]:
        if model.__tablename__ not in existing_tables:
            model.__table__.create(bind=db.engine, checkfirst=True)
            existing_tables.add(model.__tablename__)
    conn = db.engine.raw_connection()
    cur = conn.cursor()

    def column_names(table_name: str) -> set[str]:
        cur.execute(f"PRAGMA table_info({table_name})")
        return {row[1] for row in cur.fetchall()}

    def ensure_column(table_name: str, column_name: str, ddl: str):
        if column_name not in column_names(table_name):
            cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")

    user_cols = column_names("user")
    if "force_password_change" not in user_cols:
        cur.execute("ALTER TABLE user ADD COLUMN force_password_change BOOLEAN NOT NULL DEFAULT 0")
    if "note" not in user_cols:
        cur.execute("ALTER TABLE user ADD COLUMN note VARCHAR(255)")
    if "onboarding_completed" not in user_cols:
        cur.execute("ALTER TABLE user ADD COLUMN onboarding_completed BOOLEAN NOT NULL DEFAULT 1")
    if "last_login_at" not in user_cols:
        cur.execute("ALTER TABLE user ADD COLUMN last_login_at DATETIME")
    if "last_login_ip" not in user_cols:
        cur.execute("ALTER TABLE user ADD COLUMN last_login_ip VARCHAR(64)")
    if "display_name" not in user_cols:
        cur.execute("ALTER TABLE user ADD COLUMN display_name VARCHAR(80)")
    if "execution_profile" not in user_cols:
        cur.execute("ALTER TABLE user ADD COLUMN execution_profile VARCHAR(20) NOT NULL DEFAULT 'family_full'")
    if "bluebubbles_enabled" not in user_cols:
        cur.execute("ALTER TABLE user ADD COLUMN bluebubbles_enabled BOOLEAN NOT NULL DEFAULT 0")
    if "bluebubbles_recipient" not in user_cols:
        cur.execute("ALTER TABLE user ADD COLUMN bluebubbles_recipient VARCHAR(160)")
    if "bluebubbles_label" not in user_cols:
        cur.execute("ALTER TABLE user ADD COLUMN bluebubbles_label VARCHAR(120)")
    cur.execute("UPDATE user SET force_password_change = 0")
    cur.execute("UPDATE user SET role='user' WHERE role IS NULL OR TRIM(role)='' OR role='member'")
    cur.execute("UPDATE user SET onboarding_completed = 1 WHERE onboarding_completed IS NULL")
    cur.execute("UPDATE user SET display_name = username WHERE display_name IS NULL OR TRIM(display_name)=''")
    cur.execute("UPDATE user SET execution_profile = CASE WHEN role='admin' THEN 'admin_full' ELSE 'family_full' END WHERE execution_profile IS NULL OR TRIM(execution_profile)=''")

    ensure_column("conversation", "worker_session_key", "worker_session_key VARCHAR(120)")
    ensure_column("conversation", "last_message_preview", "last_message_preview VARCHAR(255)")
    ensure_column("conversation", "last_message_role", "last_message_role VARCHAR(20)")
    ensure_column("conversation", "has_recent_attachment", "has_recent_attachment BOOLEAN NOT NULL DEFAULT 0")
    ensure_column("conversation", "pinned", "pinned BOOLEAN NOT NULL DEFAULT 0")
    ensure_column("conversation", "archived_at", "archived_at DATETIME")
    ensure_column("conversation", "archived_reason", "archived_reason VARCHAR(50)")
    ensure_column("conversation", "updated_at", "updated_at DATETIME")
    ensure_column("conversation", "agent_id", "agent_id VARCHAR(120)")
    ensure_column("conversation", "model", "model VARCHAR(120)")
    ensure_column("conversation", "last_provider", "last_provider VARCHAR(80)")
    ensure_column("conversation", "last_called_at", "last_called_at DATETIME")
    ensure_column("conversation", "delivery_channel", "delivery_channel VARCHAR(30)")
    ensure_column("conversation", "delivery_recipient", "delivery_recipient VARCHAR(160)")
    ensure_column("conversation", "last_delivery_status", "last_delivery_status VARCHAR(20)")

    ensure_column("memory_entry", "pinned", "pinned BOOLEAN NOT NULL DEFAULT 0")
    ensure_column("memory_entry", "archived_at", "archived_at DATETIME")

    ensure_column("message", "render_mode", "render_mode VARCHAR(20)")
    ensure_column("message", "artifact_count", "artifact_count INTEGER NOT NULL DEFAULT 0")

    ensure_column("task_run_audit", "user_message_id", "user_message_id INTEGER")
    ensure_column("task_run_audit", "assistant_message_id", "assistant_message_id INTEGER")
    ensure_column("task_run_audit", "status", "status VARCHAR(20)")
    ensure_column("task_run_audit", "current_stage", "current_stage VARCHAR(30)")
    ensure_column("task_run_audit", "progress_percent", "progress_percent INTEGER")
    ensure_column("task_run_audit", "error_message", "error_message TEXT")
    ensure_column("task_run_audit", "bridge_run_id", "bridge_run_id VARCHAR(120)")
    ensure_column("task_run_audit", "public_status_label", "public_status_label VARCHAR(80)")
    ensure_column("task_run_audit", "public_error_message", "public_error_message TEXT")
    ensure_column("task_run_audit", "technical_error_message", "technical_error_message TEXT")
    ensure_column("task_run_audit", "tool_trace_count", "tool_trace_count INTEGER NOT NULL DEFAULT 0")
    ensure_column("task_run_audit", "planner_status", "planner_status VARCHAR(20)")
    ensure_column("task_run_audit", "worker_status", "worker_status VARCHAR(20)")
    ensure_column("task_run_audit", "verify_status", "verify_status VARCHAR(20)")
    ensure_column("task_run_audit", "duration_ms", "duration_ms INTEGER")
    ensure_column("task_run_audit", "delivery_status", "delivery_status VARCHAR(20)")
    ensure_column("task_run_audit", "delivery_error", "delivery_error TEXT")

    ensure_column("user_agent_binding", "planner_agent_id", "planner_agent_id VARCHAR(120)")
    ensure_column("user_agent_binding", "planner_session_key", "planner_session_key VARCHAR(120)")
    ensure_column("user_agent_binding", "worker_agent_id", "worker_agent_id VARCHAR(120)")
    ensure_column("user_agent_binding", "worker_session_key", "worker_session_key VARCHAR(120)")
    ensure_column("user_agent_binding", "planner_workspace", "planner_workspace VARCHAR(255)")
    ensure_column("user_agent_binding", "worker_workspace", "worker_workspace VARCHAR(255)")
    ensure_column("user_agent_binding", "bridge_namespace", "bridge_namespace VARCHAR(120)")
    ensure_column("user_agent_binding", "last_bridge_status", "last_bridge_status VARCHAR(20)")
    ensure_column("user_agent_binding", "last_bridge_at", "last_bridge_at DATETIME")

    conn.commit()
    conn.close()

    for user in User.query.order_by(User.id.asc()).all():
        user.role = normalize_role(user.role)
        if user.onboarding_completed is None:
            user.onboarding_completed = True
        if not user.display_name:
            user.display_name = user.username
        desired_profile = "admin_full" if user.role == "admin" else "family_full"
        if user.execution_profile != desired_profile:
            user.execution_profile = desired_profile

    for conversation in Conversation.query.order_by(Conversation.id.asc()).all():
        latest_message = (
            Message.query.filter_by(conversation_id=conversation.id)
            .order_by(Message.created_at.desc())
            .first()
        )
        normalized_model = _normalize_model_name(conversation.model)
        if conversation.model != normalized_model:
            conversation.model = normalized_model
        if not conversation.updated_at:
            conversation.updated_at = latest_message.created_at if latest_message else conversation.created_at
        if not conversation.last_message_preview:
            conversation.last_message_preview = summarize_text(latest_message.content, 90) if latest_message else "新会话"
        if latest_message:
            if not conversation.last_message_role:
                conversation.last_message_role = latest_message.role
            if latest_message.attachment_path and not conversation.has_recent_attachment:
                conversation.has_recent_attachment = True

    for binding in UserAgentBinding.query.order_by(UserAgentBinding.id.asc()).all():
        normalized_model = _normalize_model_name(binding.model)
        if binding.model != normalized_model:
            binding.model = normalized_model

    bootstrap = db.session.get(SystemBootstrapState, 1)
    if not bootstrap:
        bootstrap = SystemBootstrapState(id=1, setup_completed=False, preferred_model=_normalize_model_name(get_env("OPENCLAW_MODEL", DEFAULT_PROVIDER_MODEL)))
        db.session.add(bootstrap)
    elif not bootstrap.preferred_model:
        bootstrap.preferred_model = _normalize_model_name(get_env("OPENCLAW_MODEL", DEFAULT_PROVIDER_MODEL))

    stale_before = utcnow() - timedelta(minutes=15)
    for run in TaskRunAudit.query.order_by(TaskRunAudit.id.asc()).all():
        if not run.status:
            run.status = "done" if run.final_summary else "failed"
        if not run.public_status_label:
            run.public_status_label = {
                "queued": "等待队列",
                "running": "进行中",
                "done": "已完成",
                "failed": "失败",
                "blocked": "阻塞",
            }.get(run.status, "空闲")
        if not run.current_stage:
            run.current_stage = "verify" if run.status == "done" else "failed"
        if run.progress_percent is None:
            run.progress_percent = 100 if run.status in {"done", "blocked"} else 0
        if not run.planner_status:
            run.planner_status = "done" if run.planner_plan else "pending"
        if not run.worker_status:
            run.worker_status = "done" if run.worker_output else "pending"
        if not run.verify_status:
            run.verify_status = "done" if run.final_summary else "pending"
        if run.status in {"queued", "running"} and run.created_at and run.created_at < stale_before:
            run.status = "failed"
            run.current_stage = "failed"
            run.error_message = run.error_message or "系统重启前的任务未完成，已自动标记失败。"
            run.public_error_message = run.public_error_message or "系统重启前的任务未完成，已自动标记失败。"
            run.progress_percent = min(run.progress_percent or 0, 90)
            if run.planner_status == "running":
                run.planner_status = "failed"
            if run.worker_status == "running":
                run.worker_status = "failed"
            if run.verify_status == "running":
                run.verify_status = "failed"

    db.session.commit()
