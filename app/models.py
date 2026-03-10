from datetime import datetime

from flask_login import UserMixin

from access import normalize_role
from extensions import db


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    display_name = db.Column(db.String(80), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="user", nullable=False)
    execution_profile = db.Column(db.String(20), default="family_full", nullable=False)
    openclaw_token = db.Column(db.String(255), nullable=True)
    note = db.Column(db.String(255), nullable=True)
    memory_namespace = db.Column(db.String(120), nullable=False, unique=True)
    bluebubbles_enabled = db.Column(db.Boolean, default=False, nullable=False)
    bluebubbles_recipient = db.Column(db.String(160), nullable=True)
    bluebubbles_label = db.Column(db.String(120), nullable=True)
    force_password_change = db.Column(db.Boolean, default=False, nullable=False)
    onboarding_completed = db.Column(db.Boolean, default=False, nullable=False)
    last_login_at = db.Column(db.DateTime, nullable=True)
    last_login_ip = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def role_normalized(self):
        return normalize_role(self.role)

    @property
    def is_admin(self):
        return self.role_normalized == "admin"


class UserAgentBinding(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True, index=True)
    agent_id = db.Column(db.String(120), nullable=False, unique=True)
    model = db.Column(db.String(120), nullable=False, default="unknown")
    session_key = db.Column(db.String(120), nullable=False, unique=True)
    planner_agent_id = db.Column(db.String(120), nullable=True)
    planner_session_key = db.Column(db.String(120), nullable=True)
    worker_agent_id = db.Column(db.String(120), nullable=True)
    worker_session_key = db.Column(db.String(120), nullable=True)
    planner_workspace = db.Column(db.String(255), nullable=True)
    worker_workspace = db.Column(db.String(255), nullable=True)
    bridge_namespace = db.Column(db.String(120), nullable=True)
    last_bridge_status = db.Column(db.String(20), nullable=True)
    last_bridge_at = db.Column(db.DateTime, nullable=True)
    last_provider = db.Column(db.String(80), nullable=True)
    last_called_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Conversation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(120), default="默认会话")
    session_key = db.Column(db.String(120), nullable=False, unique=True)
    worker_session_key = db.Column(db.String(120), nullable=True)
    agent_id = db.Column(db.String(120), nullable=True)
    model = db.Column(db.String(120), nullable=True)
    last_provider = db.Column(db.String(80), nullable=True)
    last_called_at = db.Column(db.DateTime, nullable=True)
    last_message_preview = db.Column(db.String(255), nullable=True)
    last_message_role = db.Column(db.String(20), nullable=True)
    has_recent_attachment = db.Column(db.Boolean, default=False, nullable=False)
    pinned = db.Column(db.Boolean, default=False, nullable=False)
    archived_at = db.Column(db.DateTime, nullable=True)
    archived_reason = db.Column(db.String(50), nullable=True)
    updated_at = db.Column(db.DateTime, nullable=True)
    delivery_channel = db.Column(db.String(30), nullable=True)
    delivery_recipient = db.Column(db.String(160), nullable=True)
    last_delivery_status = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversation.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    attachment_name = db.Column(db.String(255), nullable=True)
    attachment_path = db.Column(db.String(255), nullable=True)
    render_mode = db.Column(db.String(20), nullable=True)
    artifact_count = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class MemoryEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    kind = db.Column(db.String(20), nullable=False, default="fact")
    content = db.Column(db.Text, nullable=False)
    source = db.Column(db.String(20), nullable=False, default="auto")
    pinned = db.Column(db.Boolean, default=False, nullable=False)
    archived_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class TaskRunAudit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversation.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    user_message_id = db.Column(db.Integer, db.ForeignKey("message.id"), nullable=True)
    assistant_message_id = db.Column(db.Integer, db.ForeignKey("message.id"), nullable=True)
    user_message = db.Column(db.Text, nullable=False)
    planner_plan = db.Column(db.Text, nullable=True)
    worker_output = db.Column(db.Text, nullable=True)
    final_summary = db.Column(db.Text, nullable=True)
    duration_ms = db.Column(db.Integer, nullable=True)
    dual_agent_triggered = db.Column(db.Boolean, default=False, nullable=False)
    status = db.Column(db.String(20), nullable=True)
    current_stage = db.Column(db.String(30), nullable=True)
    progress_percent = db.Column(db.Integer, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    bridge_run_id = db.Column(db.String(120), nullable=True)
    public_status_label = db.Column(db.String(80), nullable=True)
    public_error_message = db.Column(db.Text, nullable=True)
    technical_error_message = db.Column(db.Text, nullable=True)
    tool_trace_count = db.Column(db.Integer, default=0, nullable=False)
    planner_status = db.Column(db.String(20), nullable=True)
    worker_status = db.Column(db.String(20), nullable=True)
    verify_status = db.Column(db.String(20), nullable=True)
    delivery_status = db.Column(db.String(20), nullable=True)
    delivery_error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class RunEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey("task_run_audit.id"), nullable=False, index=True)
    stage = db.Column(db.String(30), nullable=False)
    event_type = db.Column(db.String(40), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")
    label = db.Column(db.String(120), nullable=False)
    public_text = db.Column(db.Text, nullable=True)
    admin_text = db.Column(db.Text, nullable=True)
    progress_percent = db.Column(db.Integer, nullable=True)
    meta_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class RunArtifact(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey("task_run_audit.id"), nullable=False, index=True)
    message_id = db.Column(db.Integer, db.ForeignKey("message.id"), nullable=True, index=True)
    kind = db.Column(db.String(40), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    summary = db.Column(db.Text, nullable=True)
    source_url = db.Column(db.String(1024), nullable=True)
    file_path = db.Column(db.String(255), nullable=True)
    mime_type = db.Column(db.String(120), nullable=True)
    preview_image_path = db.Column(db.String(255), nullable=True)
    visibility = db.Column(db.String(20), nullable=False, default="user")
    meta_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class OutboundDelivery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    run_id = db.Column(db.Integer, db.ForeignKey("task_run_audit.id"), nullable=False, index=True)
    channel = db.Column(db.String(30), nullable=False)
    recipient = db.Column(db.String(160), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")
    message_preview = db.Column(db.String(255), nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    provider_ref = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    delivered_at = db.Column(db.DateTime, nullable=True)


class ProviderCredential(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    provider_id = db.Column(db.String(60), nullable=False, unique=True, index=True)
    auth_mode = db.Column(db.String(20), nullable=False, default="api_key")
    status = db.Column(db.String(20), nullable=False, default="pending")
    default_model = db.Column(db.String(120), nullable=True)
    profile_id = db.Column(db.String(120), nullable=True)
    encrypted_secret = db.Column(db.Text, nullable=True)
    secret_hint = db.Column(db.String(32), nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    meta_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ProviderAuthSession(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    provider_id = db.Column(db.String(60), nullable=False, index=True)
    auth_mode = db.Column(db.String(20), nullable=False, default="oauth")
    status = db.Column(db.String(20), nullable=False, default="pending")
    runtime_session_id = db.Column(db.String(120), nullable=True)
    auth_url = db.Column(db.Text, nullable=True)
    device_code = db.Column(db.String(120), nullable=True)
    output_log = db.Column(db.Text, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)


class SystemBootstrapState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    setup_completed = db.Column(db.Boolean, default=False, nullable=False)
    preferred_model = db.Column(db.String(120), nullable=True)
    runtime_health_enabled = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class LoginAttempt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False)
    ip = db.Column(db.String(64), nullable=False)
    fail_count = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
