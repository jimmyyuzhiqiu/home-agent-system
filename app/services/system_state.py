import json
import uuid

from extensions import db
from models import ProviderAuthSession, ProviderCredential, SystemBootstrapState, User
from settings import oauth_is_available
from utils import utcnow


DEFAULT_PROVIDER_ID = "openai"
DEFAULT_PROVIDER_MODEL = "openai/gpt-5.3-codex"


def has_admin_account() -> bool:
    return User.query.filter_by(role="admin").count() > 0


def get_bootstrap_state() -> SystemBootstrapState:
    state = db.session.get(SystemBootstrapState, 1)
    if state:
        return state
    state = SystemBootstrapState(id=1, setup_completed=False, preferred_model=DEFAULT_PROVIDER_MODEL)
    db.session.add(state)
    db.session.commit()
    return state


def update_bootstrap_state(*, setup_completed: bool | None = None, preferred_model: str | None = None, runtime_health_enabled: bool | None = None) -> SystemBootstrapState:
    state = get_bootstrap_state()
    if setup_completed is not None:
        state.setup_completed = bool(setup_completed)
    if preferred_model is not None:
        state.preferred_model = preferred_model.strip() or DEFAULT_PROVIDER_MODEL
    if runtime_health_enabled is not None:
        state.runtime_health_enabled = bool(runtime_health_enabled)
    state.updated_at = utcnow()
    db.session.commit()
    return state


def is_bootstrap_complete() -> bool:
    return bool(has_admin_account() and get_bootstrap_state().setup_completed)


def get_provider_credential(provider_id: str = DEFAULT_PROVIDER_ID) -> ProviderCredential | None:
    return ProviderCredential.query.filter_by(provider_id=provider_id).first()


def upsert_provider_credential(
    *,
    provider_id: str = DEFAULT_PROVIDER_ID,
    auth_mode: str,
    status: str,
    default_model: str | None = None,
    profile_id: str | None = None,
    encrypted_secret: str | None = None,
    secret_hint: str | None = None,
    expires_at=None,
    last_error: str | None = None,
    meta: dict | None = None,
) -> ProviderCredential:
    record = get_provider_credential(provider_id)
    if not record:
        record = ProviderCredential(provider_id=provider_id)
        db.session.add(record)
    record.auth_mode = auth_mode
    record.status = status
    record.default_model = (default_model or record.default_model or DEFAULT_PROVIDER_MODEL).strip()
    record.profile_id = profile_id or record.profile_id
    if encrypted_secret is not None:
        record.encrypted_secret = encrypted_secret or None
    if secret_hint is not None:
        record.secret_hint = secret_hint or None
    record.expires_at = expires_at
    record.last_error = last_error or None
    record.meta_json = json.dumps(meta or {}, ensure_ascii=False) if meta else None
    record.updated_at = utcnow()
    db.session.commit()
    return record


def serialize_provider_credential(record: ProviderCredential | None) -> dict:
    if not record:
        return {
            "provider_id": DEFAULT_PROVIDER_ID,
            "auth_mode": "api_key",
            "status": "pending",
            "default_model": DEFAULT_PROVIDER_MODEL,
            "profile_id": None,
            "secret_hint": None,
            "last_error": None,
            "meta": {},
        }
    return {
        "provider_id": record.provider_id,
        "auth_mode": record.auth_mode,
        "status": record.status,
        "default_model": record.default_model or DEFAULT_PROVIDER_MODEL,
        "profile_id": record.profile_id,
        "secret_hint": record.secret_hint,
        "last_error": record.last_error,
        "meta": json.loads(record.meta_json) if record.meta_json else {},
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


def provider_ready(provider_id: str = DEFAULT_PROVIDER_ID) -> bool:
    record = get_provider_credential(provider_id)
    return bool(record and record.status == "ready")


def create_provider_auth_session(*, provider_id: str = DEFAULT_PROVIDER_ID, auth_mode: str = "oauth", runtime_session_id: str | None = None) -> ProviderAuthSession:
    session = ProviderAuthSession(
        id=f"pa_{uuid.uuid4().hex[:16]}",
        provider_id=provider_id,
        auth_mode=auth_mode,
        runtime_session_id=runtime_session_id,
        status="pending",
    )
    db.session.add(session)
    db.session.commit()
    return session


def get_provider_auth_session(session_id: str) -> ProviderAuthSession | None:
    return db.session.get(ProviderAuthSession, session_id)


def update_provider_auth_session(session_id: str, **fields) -> ProviderAuthSession | None:
    session = get_provider_auth_session(session_id)
    if not session:
        return None
    for key, value in fields.items():
        setattr(session, key, value)
    session.updated_at = utcnow()
    if session.status in {"ready", "failed", "cancelled"} and not session.completed_at:
        session.completed_at = utcnow()
    db.session.commit()
    return session


def serialize_provider_auth_session(session: ProviderAuthSession | None) -> dict | None:
    if not session:
        return None
    return {
        "id": session.id,
        "provider_id": session.provider_id,
        "auth_mode": session.auth_mode,
        "status": session.status,
        "runtime_session_id": session.runtime_session_id,
        "auth_url": session.auth_url,
        "device_code": session.device_code,
        "output_log": session.output_log or "",
        "error_message": session.error_message,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
        "completed_at": session.completed_at.isoformat() if session.completed_at else None,
    }


def setup_context_summary() -> dict:
    bootstrap = get_bootstrap_state()
    provider = serialize_provider_credential(get_provider_credential())
    return {
        "has_admin": has_admin_account(),
        "setup_completed": bootstrap.setup_completed,
        "preferred_model": bootstrap.preferred_model or DEFAULT_PROVIDER_MODEL,
        "runtime_health_enabled": bootstrap.runtime_health_enabled,
        "provider": provider,
        "oauth_available": oauth_is_available(),
    }
