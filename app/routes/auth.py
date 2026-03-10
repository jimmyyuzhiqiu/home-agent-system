import secrets
from datetime import timedelta

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from access import is_admin_user
from extensions import db
from models import LoginAttempt, ProviderAuthSession, User
from services.bridge_client import resolve_bridge_state, runtime_cancel_oauth_session, runtime_get_oauth_session, runtime_provider_status, runtime_set_api_key, runtime_start_oauth
from services.conversations import ensure_user_agent_binding, ensure_user_conversation
from services.credential_store import encrypt_secret
from services.memory import ensure_system_memories
from services.system_state import (
    DEFAULT_PROVIDER_ID,
    create_provider_auth_session,
    get_provider_auth_session,
    get_provider_credential,
    has_admin_account,
    is_bootstrap_complete,
    serialize_provider_auth_session,
    serialize_provider_credential,
    setup_context_summary,
    update_bootstrap_state,
    update_provider_auth_session,
    upsert_provider_credential,
)
from settings import WEAK_SECRET_KEYS, oauth_is_available
from utils import utcnow


auth_bp = Blueprint("auth", __name__)


def get_login_guard(username: str, ip: str):
    record = LoginAttempt.query.filter_by(username=username, ip=ip).first()
    if not record:
        record = LoginAttempt(username=username, ip=ip)
        db.session.add(record)
        db.session.commit()
    return record


def check_login_rate_limit(username: str, ip: str):
    now = utcnow()
    record = get_login_guard(username, ip)
    if record.locked_until and record.locked_until > now:
        remain = int((record.locked_until - now).total_seconds())
        return False, max(remain, 1)
    if (now - record.updated_at) > timedelta(minutes=15):
        record.fail_count = 0
        record.locked_until = None
        record.updated_at = now
        db.session.commit()
    return True, 0


def mark_login_failure(username: str, ip: str):
    now = utcnow()
    record = get_login_guard(username, ip)
    if (now - record.updated_at) > timedelta(minutes=15):
        record.fail_count = 0
    record.fail_count += 1
    record.updated_at = now
    if record.fail_count >= 8:
        record.locked_until = now + timedelta(minutes=15)
    db.session.commit()


def clear_login_failure(username: str, ip: str):
    record = LoginAttempt.query.filter_by(username=username, ip=ip).first()
    if record:
        record.fail_count = 0
        record.locked_until = None
        record.updated_at = utcnow()
        db.session.commit()


@auth_bp.route("/")
def index():
    if not has_admin_account():
        return redirect(url_for("auth.setup"))
    if current_user.is_authenticated:
        if not is_bootstrap_complete():
            return redirect(url_for("auth.setup"))
        return redirect(url_for("chat.chat"))
    return redirect(url_for("auth.login"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if not has_admin_account():
        return redirect(url_for("auth.setup"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
        allowed, remain = check_login_rate_limit(username, ip)
        if not allowed:
            flash(f"登录尝试过多，请 {remain} 秒后再试", "danger")
            return render_template(
                "login.html",
                page_title="安全进入系统",
                page_subtitle="登录受登录限流与 CSRF 保护。",
            )

        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            clear_login_failure(username, ip)
            if not is_bootstrap_complete() and not user.is_admin:
                flash("系统仍在初始化，当前仅允许管理员完成首启配置。", "warning")
                return render_template(
                    "login.html",
                    page_title="安全进入系统",
                    page_subtitle="管理员完成首启后，家庭成员即可正常登录使用。",
                    bootstrap_pending=True,
                )
            user.last_login_at = utcnow()
            user.last_login_ip = ip
            db.session.commit()
            login_user(user)
            session.pop("onboarding_snoozed", None)
            if not is_bootstrap_complete():
                return redirect(url_for("auth.setup"))
            return redirect(url_for("chat.chat"))

        mark_login_failure(username, ip)
        flash("用户名或密码错误", "danger")

    return render_template(
        "login.html",
        page_title="安全进入系统",
        page_subtitle="面向家庭场景的双 Agent 工作台。",
        bootstrap_pending=not is_bootstrap_complete(),
    )


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    session.pop("onboarding_snoozed", None)
    return redirect(url_for("auth.login"))


def _require_setup_admin():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    if not is_admin_user(current_user):
        abort(403)
    return None


def _refresh_provider_from_runtime():
    try:
        runtime_status = runtime_provider_status(probe=False)
    except Exception as exc:
        return {
            "ok": False,
            "provider_ready": False,
            "last_error": str(exc),
            "default_model": setup_context_summary().get("preferred_model"),
        }
    if runtime_status.get("provider_ready"):
        oauth_profiles = runtime_status.get("oauth_profiles") or []
        profile_id = oauth_profiles[0].get("profileId") if oauth_profiles else None
        upsert_provider_credential(
            provider_id=DEFAULT_PROVIDER_ID,
            auth_mode="oauth" if oauth_profiles else "api_key",
            status="ready",
            default_model=runtime_status.get("default_model"),
            profile_id=profile_id,
            last_error="",
            meta={"runtime_provider_id": "openai-codex" if oauth_profiles else "openai"},
        )
    return runtime_status


def _safe_redirect_target(default_endpoint: str):
    target = request.form.get("next", "").strip()
    if target.startswith("/") and not target.startswith("//"):
        return target
    return url_for(default_endpoint)


def _sync_oauth_session(session_id: str):
    local_session = get_provider_auth_session(session_id)
    if not local_session:
        return None
    runtime_session_id = local_session.runtime_session_id
    if not runtime_session_id:
        return serialize_provider_auth_session(local_session)
    try:
        runtime_payload = runtime_get_oauth_session(runtime_session_id)
    except Exception as exc:
        update_provider_auth_session(session_id, status="failed", error_message=str(exc))
        local_session = get_provider_auth_session(session_id)
        return serialize_provider_auth_session(local_session)
    update_provider_auth_session(
        session_id,
        status=runtime_payload.get("status") or local_session.status,
        auth_url=runtime_payload.get("auth_url"),
        device_code=runtime_payload.get("device_code"),
        output_log=runtime_payload.get("output_log"),
        error_message=runtime_payload.get("error"),
    )
    local_session = get_provider_auth_session(session_id)
    if runtime_payload.get("status") == "ready":
        runtime_status = _refresh_provider_from_runtime()
        update_bootstrap_state(preferred_model=runtime_status.get("default_model"))
    return serialize_provider_auth_session(local_session)


@auth_bp.route("/setup", methods=["GET"])
def setup():
    summary = setup_context_summary()
    latest_auth_session = ProviderAuthSession.query.order_by(ProviderAuthSession.created_at.desc()).first() if has_admin_account() else None
    active_auth_session = _sync_oauth_session(latest_auth_session.id) if latest_auth_session else None
    return render_template(
        "setup.html",
        page_title="首次部署向导",
        page_subtitle="创建管理员、配置模型 Provider，并确认运行时已经就绪。",
        setup_summary=summary,
        runtime_state=resolve_bridge_state(),
        provider_record=serialize_provider_credential(get_provider_credential()),
        active_auth_session=active_auth_session,
        oauth_available=oauth_is_available(),
    )


@auth_bp.route("/setup/admin", methods=["POST"])
def setup_admin():
    if has_admin_account():
        flash("管理员已存在，请直接登录继续配置。", "info")
        return redirect(url_for("auth.setup"))
    username = request.form.get("username", "").strip() or "Jimmy"
    display_name = request.form.get("display_name", "").strip() or username
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")
    if len(password) < 10:
        flash("管理员密码至少 10 位", "warning")
        return redirect(url_for("auth.setup"))
    if password != confirm_password:
        flash("两次输入的密码不一致", "warning")
        return redirect(url_for("auth.setup"))
    user = User(
        username=username,
        display_name=display_name,
        password_hash=generate_password_hash(password),
        role="admin",
        execution_profile="admin_full",
        memory_namespace=f"user-admin-{secrets.token_hex(6)}",
        onboarding_completed=False,
    )
    db.session.add(user)
    db.session.commit()
    ensure_user_agent_binding(user.id)
    ensure_user_conversation(user.id)
    ensure_system_memories(user.id)
    login_user(user)
    flash("管理员创建成功，请继续配置模型 Provider。", "success")
    return redirect(url_for("auth.setup"))


@auth_bp.route("/setup/provider/api-key", methods=["POST"])
@login_required
def setup_provider_api_key():
    guard = _require_setup_admin()
    if guard:
        return guard
    redirect_target = _safe_redirect_target("auth.setup")
    api_key = request.form.get("api_key", "").strip()
    default_model = request.form.get("default_model", "").strip() or "openai/gpt-5.3-codex"
    if not api_key:
        flash("请输入 OpenAI API Key。", "warning")
        return redirect(redirect_target)
    encrypted = encrypt_secret(api_key)
    hint = f"...{api_key[-4:]}" if len(api_key) >= 4 else "***"
    try:
        runtime_status = runtime_set_api_key({"provider_id": "openai", "api_key": api_key, "default_model": default_model})
        if runtime_status.get("ok") is False:
            raise RuntimeError(runtime_status.get("error") or "Runtime 拒绝了 API Key 配置")
        upsert_provider_credential(
            provider_id=DEFAULT_PROVIDER_ID,
            auth_mode="api_key",
            status="ready" if runtime_status.get("provider_ready") else "pending",
            default_model=runtime_status.get("default_model") or default_model,
            encrypted_secret=encrypted,
            secret_hint=hint,
            last_error=runtime_status.get("last_error"),
            meta={"runtime_provider_id": "openai"},
        )
        update_bootstrap_state(preferred_model=runtime_status.get("default_model") or default_model)
        flash("API Key 已同步到容器内 Runtime。", "success" if runtime_status.get("provider_ready") else "warning")
    except Exception as exc:
        upsert_provider_credential(
            provider_id=DEFAULT_PROVIDER_ID,
            auth_mode="api_key",
            status="failed",
            default_model=default_model,
            encrypted_secret=encrypted,
            secret_hint=hint,
            last_error=str(exc),
            meta={"runtime_provider_id": "openai"},
        )
        flash(f"同步 API Key 失败：{exc}", "danger")
    return redirect(redirect_target)


@auth_bp.route("/setup/provider/oauth/start", methods=["POST"])
@login_required
def setup_provider_oauth_start():
    guard = _require_setup_admin()
    if guard:
        return guard
    local_session = create_provider_auth_session(provider_id="openai-codex", auth_mode="oauth")
    try:
        runtime_payload = runtime_start_oauth({"provider_id": "openai-codex"})
        if runtime_payload.get("ok") is False:
            raise RuntimeError(runtime_payload.get("error") or "Runtime 无法启动 OAuth 会话")
        update_provider_auth_session(
            local_session.id,
            runtime_session_id=runtime_payload.get("session_id"),
            status=runtime_payload.get("status") or "running",
            auth_url=runtime_payload.get("auth_url"),
            device_code=runtime_payload.get("device_code"),
            output_log=runtime_payload.get("output_log"),
            error_message=runtime_payload.get("error"),
        )
        payload = _sync_oauth_session(local_session.id)
        return jsonify({"ok": True, "session": payload})
    except Exception as exc:
        update_provider_auth_session(local_session.id, status="failed", error_message=str(exc))
        return jsonify({"ok": False, "error": str(exc)}), 502


@auth_bp.route("/setup/complete", methods=["POST"])
@login_required
def setup_complete():
    guard = _require_setup_admin()
    if guard:
        return guard
    runtime_state = resolve_bridge_state()
    provider_state = _refresh_provider_from_runtime()
    if not runtime_state.get("ok"):
        flash("Runtime 当前未连通，请先确认容器内执行环境已启动。", "warning")
        return redirect(url_for("auth.setup"))
    if not provider_state.get("provider_ready") or not runtime_state.get("chat_ready"):
        flash("运行时尚未完成 Provider 认证，请先完成 API Key 或 OAuth 配置。", "warning")
        return redirect(url_for("auth.setup"))
    update_bootstrap_state(
        setup_completed=True,
        preferred_model=request.form.get("preferred_model", "").strip() or provider_state.get("default_model") or "openai/gpt-5.3-codex",
        runtime_health_enabled=request.form.get("runtime_health_enabled") == "on",
    )
    flash("首次部署已完成，现在可以开始使用。", "success")
    return redirect(url_for("chat.chat"))


@auth_bp.route("/api/admin/provider-status")
@login_required
def api_admin_provider_status():
    if not is_admin_user(current_user):
        abort(403)
    runtime_state = resolve_bridge_state()
    runtime_status = _refresh_provider_from_runtime()
    return jsonify(
        {
            "ok": True,
            "runtime": runtime_state,
            "provider": serialize_provider_credential(get_provider_credential()),
            "runtime_provider": runtime_status,
            "setup": setup_context_summary(),
        }
    )


@auth_bp.route("/api/admin/provider-oauth/<session_id>")
@login_required
def api_admin_provider_oauth(session_id: str):
    if not is_admin_user(current_user):
        abort(403)
    payload = _sync_oauth_session(session_id)
    if not payload:
        abort(404)
    return jsonify({"ok": True, "session": payload, "provider": serialize_provider_credential(get_provider_credential())})


@auth_bp.route("/api/admin/provider-oauth/<session_id>/cancel", methods=["POST"])
@login_required
def api_admin_provider_oauth_cancel(session_id: str):
    if not is_admin_user(current_user):
        abort(403)
    local_session = get_provider_auth_session(session_id)
    if not local_session:
        abort(404)
    if local_session.runtime_session_id:
        runtime_cancel_oauth_session(local_session.runtime_session_id)
    update_provider_auth_session(session_id, status="cancelled", error_message="管理员已取消 OAuth 登录")
    return jsonify({"ok": True, "status": "cancelled"})


@auth_bp.route("/security/setup", methods=["GET", "POST"])
@login_required
def security_setup():
    if not is_admin_user(current_user):
        abort(403)

    if request.method == "POST":
        action = request.form.get("action")
        if action == "change_password":
            old_password = request.form.get("old_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")
            if not check_password_hash(current_user.password_hash, old_password):
                flash("当前密码错误", "danger")
            elif len(new_password) < 10:
                flash("新密码至少 10 位", "warning")
            elif new_password != confirm_password:
                flash("两次输入的新密码不一致", "warning")
            else:
                current_user.password_hash = generate_password_hash(new_password)
                current_user.force_password_change = False
                db.session.commit()
                flash("密码已更新", "success")

    secret = current_app.config["SECRET_KEY"]
    secret_weak = (not secret) or (secret in WEAK_SECRET_KEYS) or len(secret) < 24
    return render_template(
        "security_setup.html",
        secret_weak=secret_weak,
        suggest_secret=secrets.token_urlsafe(36),
        page_title="安全中心",
        page_subtitle="维护管理员口令、会话安全与部署密钥的基础健康度。",
    )


@auth_bp.route("/onboarding")
@login_required
def onboarding():
    return render_template(
        "onboarding.html",
        page_title="欢迎进入家庭智能中枢",
        page_subtitle="3 步完成核心使用认知，之后就可以直接开始。",
    )


@auth_bp.route("/onboarding/complete", methods=["POST"])
@login_required
def complete_onboarding():
    current_user.onboarding_completed = True
    session.pop("onboarding_snoozed", None)
    db.session.commit()
    flash("引导已完成，欢迎开始使用。", "success")
    return redirect(url_for("chat.chat"))


@auth_bp.route("/onboarding/snooze", methods=["POST"])
@login_required
def snooze_onboarding():
    session["onboarding_snoozed"] = True
    flash("本次会话已跳过引导，你可以稍后从侧边栏重新查看。", "info")
    return redirect(url_for("chat.chat"))
