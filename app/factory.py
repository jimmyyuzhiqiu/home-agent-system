from flask import Flask, redirect, request, session, url_for
from flask_login import current_user, logout_user

from access import is_admin_user
from extensions import csrf, db, login_manager
from models import TaskRunAudit, User
from services.system_state import has_admin_account, is_bootstrap_complete, setup_context_summary
from schema import ensure_schema_compat
from settings import WEAK_SECRET_KEYS, configure_app
from utils import render_markdown, summarize_text


def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")
    configure_app(app)

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)

    from routes.admin import admin_bp
    from routes.auth import auth_bp
    from routes.chat import chat_bp
    from routes.internal import internal_bp

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    app.jinja_env.filters["render_markdown"] = render_markdown

    @app.before_request
    def enforce_onboarding():
        setup_endpoints = {
            "auth.setup",
            "auth.setup_admin",
            "auth.setup_provider_api_key",
            "auth.setup_provider_oauth_start",
            "auth.setup_complete",
            "auth.login",
            "auth.logout",
            "auth.api_admin_provider_status",
            "auth.api_admin_provider_oauth",
            "auth.api_admin_provider_oauth_cancel",
            "chat.healthz",
            "static",
        }
        if not has_admin_account():
            if request.endpoint in setup_endpoints:
                return
            return redirect(url_for("auth.setup"))
        if not is_bootstrap_complete():
            if request.endpoint in setup_endpoints:
                return
            if not current_user.is_authenticated:
                return redirect(url_for("auth.login"))
            if not is_admin_user(current_user):
                logout_user()
                return redirect(url_for("auth.login"))
            return redirect(url_for("auth.setup"))
        if not current_user.is_authenticated:
            return
        if session.get("onboarding_snoozed"):
            return
        allowed = {
            "auth.logout",
            "auth.onboarding",
            "auth.complete_onboarding",
            "auth.snooze_onboarding",
            "chat.uploaded_file",
            "chat.gateway_health",
            "internal.internal_upload",
            "internal.bridge_callback",
            "internal.internal_run_artifacts",
            "static",
        }
        if request.endpoint in allowed:
            return
        if not current_user.onboarding_completed:
            return redirect(url_for("auth.onboarding"))

    @app.context_processor
    def inject_security_alert():
        secret = app.config["SECRET_KEY"]
        is_weak = (not secret) or (secret in WEAK_SECRET_KEYS) or len(secret) < 24
        latest_run = None
        latest_run_label = ""
        latest_summary = ""
        gateway_state = None
        if current_user.is_authenticated:
            latest_run = TaskRunAudit.query.filter_by(user_id=current_user.id).order_by(TaskRunAudit.id.desc()).first()
            if latest_run:
                latest_run_label = latest_run.public_status_label or {
                    "queued": "等待队列",
                    "running": "进行中",
                    "done": "已完成",
                    "failed": "失败",
                    "blocked": "阻塞",
                }.get(latest_run.status, "空闲")
            latest_summary = summarize_text((latest_run.final_summary or latest_run.public_error_message or latest_run.error_message) if latest_run else "", 84)
            from services.bridge_client import resolve_bridge_state

            gateway_state = resolve_bridge_state()
        return {
            "security_alert": {
                "show": bool(current_user.is_authenticated and is_admin_user(current_user) and is_weak),
                "message": "检测到 SECRET_KEY 强度不足，建议尽快在安全中心处理（当前不阻断访问）。"
                if current_user.is_authenticated and is_admin_user(current_user) and is_weak
                else "",
            },
            "gateway_state": gateway_state,
            "latest_run": latest_run,
            "latest_run_label": latest_run_label,
            "latest_summary": latest_summary,
            "setup_state": setup_context_summary(),
        }

    app.register_blueprint(auth_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(internal_bp)

    with app.app_context():
        ensure_schema_compat()

    return app
