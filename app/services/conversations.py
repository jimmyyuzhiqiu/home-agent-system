import uuid
from pathlib import Path

from sqlalchemy import or_

from extensions import db
from models import Conversation, User, UserAgentBinding
from settings import USER_DATA_ROOT, get_env
from services.system_state import DEFAULT_PROVIDER_MODEL, get_bootstrap_state, get_provider_credential
from utils import sanitize_namespace, summarize_text, utcnow


def user_storage_paths(user: User):
    namespace = sanitize_namespace(getattr(user, "memory_namespace", ""))
    root = USER_DATA_ROOT / namespace
    return {
        "root": root,
        "sessions": root / "sessions",
        "memories": root / "memories",
    }


def ensure_user_isolation(user: User):
    changed = False
    safe_ns = sanitize_namespace(getattr(user, "memory_namespace", ""))
    if user.memory_namespace != safe_ns:
        user.memory_namespace = safe_ns
        changed = True
    paths = user_storage_paths(user)
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths, changed


def _build_session_pair(user: User) -> tuple[str, str]:
    namespace = sanitize_namespace(user.memory_namespace)
    suffix = uuid.uuid4().hex[:12]
    return (f"planner-s-{namespace}-{suffix}", f"worker-s-{namespace}-{suffix}")


def _bridge_workspace_root() -> str:
    return get_env("HOME_AGENT_RUNTIME_WORKROOT", get_env("HOME_AGENT_BRIDGE_WORKROOT", "/runtime/workspaces/users"))


def _default_workspace(namespace: str, role: str) -> str:
    return f"{_bridge_workspace_root().rstrip('/')}/{namespace}/{role}-workspace"


def _normalize_model_name(model: str | None) -> str:
    raw = (model or "").strip()
    if not raw:
        credential = get_provider_credential()
        bootstrap = get_bootstrap_state()
        return (credential.default_model if credential and credential.default_model else bootstrap.preferred_model) or get_env("OPENCLAW_MODEL", DEFAULT_PROVIDER_MODEL)
    if "/" in raw:
        return raw
    provider = get_env("OPENCLAW_MODEL_PROVIDER", "openai-codex")
    return f"{provider}/{raw}"


def ensure_user_agent_binding(user_id: int):
    user = db.session.get(User, user_id)
    if user:
        _, changed = ensure_user_isolation(user)
        if changed:
            db.session.commit()

    binding = UserAgentBinding.query.filter_by(user_id=user_id).first()
    namespace = sanitize_namespace(user.memory_namespace) if user else f"u{user_id}"

    if binding:
        changed = False
        if not binding.bridge_namespace:
            binding.bridge_namespace = namespace
            changed = True
        if not binding.planner_agent_id:
            binding.planner_agent_id = binding.agent_id or f"planner-{namespace}"
            changed = True
        if not binding.worker_agent_id:
            binding.worker_agent_id = f"worker-{namespace}"
            changed = True
        if not binding.planner_session_key:
            binding.planner_session_key = binding.session_key or f"planner-s-{namespace}-{uuid.uuid4().hex[:12]}"
            changed = True
        if not binding.worker_session_key:
            binding.worker_session_key = f"worker-s-{namespace}-{uuid.uuid4().hex[:12]}"
            changed = True
        if not binding.planner_workspace:
            binding.planner_workspace = _default_workspace(namespace, "planner")
            changed = True
        if not binding.worker_workspace:
            binding.worker_workspace = _default_workspace(namespace, "worker")
            changed = True
        normalized_model = _normalize_model_name(binding.model)
        if binding.model != normalized_model:
            binding.model = normalized_model
            changed = True
        if binding.agent_id != binding.planner_agent_id:
            binding.agent_id = binding.planner_agent_id
            changed = True
        if binding.session_key != binding.planner_session_key:
            binding.session_key = binding.planner_session_key
            changed = True
        if changed:
            db.session.commit()
        return binding

    planner_session_key, worker_session_key = _build_session_pair(user)
    planner_agent_id = f"planner-{namespace}"
    worker_agent_id = f"worker-{namespace}"
    binding = UserAgentBinding(
        user_id=user_id,
        agent_id=planner_agent_id,
        model=_normalize_model_name(None),
        session_key=planner_session_key,
        planner_agent_id=planner_agent_id,
        planner_session_key=planner_session_key,
        worker_agent_id=worker_agent_id,
        worker_session_key=worker_session_key,
        planner_workspace=_default_workspace(namespace, "planner"),
        worker_workspace=_default_workspace(namespace, "worker"),
        bridge_namespace=namespace,
    )
    db.session.add(binding)
    db.session.commit()
    return binding


def create_conversation(user_id: int, title: str | None = None):
    user = db.session.get(User, user_id)
    binding = ensure_user_agent_binding(user_id)
    planner_session_key, worker_session_key = _build_session_pair(user)
    conversation = Conversation(
        user_id=user_id,
        title=(title or "新会话").strip()[:120] or "新会话",
        session_key=planner_session_key,
        worker_session_key=worker_session_key,
        agent_id=binding.planner_agent_id or binding.agent_id,
        model=binding.model,
        last_message_preview="准备开始新的对话",
        updated_at=utcnow(),
    )
    db.session.add(conversation)
    db.session.commit()
    return conversation


def ensure_user_conversation(user_id: int):
    binding = ensure_user_agent_binding(user_id)
    conversation = (
        Conversation.query.filter_by(user_id=user_id)
        .filter(Conversation.archived_at.is_(None))
        .order_by(Conversation.pinned.desc(), Conversation.updated_at.desc(), Conversation.id.desc())
        .first()
    )
    if conversation:
        changed = False
        if not conversation.worker_session_key:
            _, worker_session_key = _build_session_pair(db.session.get(User, user_id))
            conversation.worker_session_key = worker_session_key
            changed = True
        if not conversation.agent_id:
            conversation.agent_id = binding.planner_agent_id or binding.agent_id
            changed = True
        if not conversation.model:
            conversation.model = binding.model
            changed = True
        else:
            normalized_model = _normalize_model_name(conversation.model)
            if conversation.model != normalized_model:
                conversation.model = normalized_model
                changed = True
        if not conversation.updated_at:
            conversation.updated_at = conversation.created_at
            changed = True
        if changed:
            db.session.commit()
        return conversation
    return create_conversation(user_id, "默认会话")


def get_user_conversation(user_id: int, conversation_id: int | None = None, include_archived: bool = False):
    query = Conversation.query.filter_by(user_id=user_id)
    if not include_archived:
        query = query.filter(Conversation.archived_at.is_(None))
    if conversation_id:
        conversation = query.filter_by(id=conversation_id).first()
        if conversation:
            return conversation
    return ensure_user_conversation(user_id)


def list_user_conversations(user_id: int, limit: int = 20, include_archived: bool = False):
    query = Conversation.query.filter_by(user_id=user_id)
    if not include_archived:
        query = query.filter(Conversation.archived_at.is_(None))
    return (
        query.order_by(Conversation.pinned.desc(), Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(limit)
        .all()
    )


def search_user_conversations(user_id: int, keyword: str = "", scope: str = "active", limit: int = 50):
    query = Conversation.query.filter_by(user_id=user_id)
    if scope == "archived":
        query = query.filter(Conversation.archived_at.isnot(None))
    elif scope == "all":
        pass
    else:
        query = query.filter(Conversation.archived_at.is_(None))
    normalized = (keyword or "").strip()
    if normalized:
        query = query.filter(
            or_(
                Conversation.title.contains(normalized),
                Conversation.last_message_preview.contains(normalized),
            )
        )
    return (
        query.order_by(Conversation.pinned.desc(), Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(limit)
        .all()
    )


def rename_conversation(user_id: int, conversation_id: int, title: str):
    conversation = Conversation.query.filter_by(user_id=user_id, id=conversation_id).first()
    if not conversation:
        return None
    normalized = (title or "").strip()[:120]
    if not normalized:
        return conversation
    conversation.title = normalized
    conversation.updated_at = utcnow()
    db.session.commit()
    return conversation


def toggle_pin_conversation(user_id: int, conversation_id: int):
    conversation = Conversation.query.filter_by(user_id=user_id, id=conversation_id).first()
    if not conversation:
        return None
    conversation.pinned = not bool(conversation.pinned)
    conversation.updated_at = utcnow()
    db.session.commit()
    return conversation


def archive_conversation(user_id: int, conversation_id: int):
    conversation = Conversation.query.filter_by(user_id=user_id, id=conversation_id).first()
    if not conversation:
        return None
    conversation.archived_at = utcnow()
    conversation.pinned = False
    conversation.archived_reason = "user"
    db.session.commit()
    fallback = (
        Conversation.query.filter_by(user_id=user_id)
        .filter(Conversation.archived_at.is_(None))
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .first()
    )
    if fallback:
        return fallback
    return create_conversation(user_id, "新会话")


def restore_conversation(user_id: int, conversation_id: int):
    conversation = Conversation.query.filter_by(user_id=user_id, id=conversation_id).first()
    if not conversation:
        return None
    conversation.archived_at = None
    conversation.archived_reason = None
    conversation.updated_at = utcnow()
    db.session.commit()
    return conversation


def touch_conversation(conversation: Conversation, preview_text: str, provider: str | None = None, role: str | None = None, has_attachment: bool = False):
    conversation.last_message_preview = summarize_text(preview_text, 96)
    conversation.updated_at = utcnow()
    if role:
        conversation.last_message_role = role
    conversation.has_recent_attachment = bool(has_attachment)
    if provider:
        conversation.last_provider = provider
        conversation.last_called_at = utcnow()
    db.session.commit()
    return conversation
