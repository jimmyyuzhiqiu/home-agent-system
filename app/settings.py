import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
USER_DATA_ROOT = Path(os.getenv("USER_DATA_ROOT", str((BASE_DIR.parent / "data" / "users").resolve())).strip())

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
USER_DATA_ROOT.mkdir(parents=True, exist_ok=True)

WEAK_SECRET_KEYS = {"change-me", "replace_with_random_secret", "default_secret", "secret", "123456"}
DEFAULT_ADMIN_USERNAME = "Jimmy"
DEFAULT_ADMIN_PASSWORD = "Jimmy11a@123"
DEFAULT_BRIDGE_URL = "http://runtime:18888"
DEFAULT_PUBLIC_BASE_URL = "http://127.0.0.1:8088"
SYSTEM_MEMORY_SEEDS = [
    ("manual", "默认自主执行"),
    ("manual", "仅在权限问题请求用户"),
    ("manual", "结果优先成品交付"),
]
ALLOWED_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "webp", "pdf", "txt", "doc", "docx", "xlsx", "csv", "md", "zip"
}


def get_env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def get_env_bool(key: str, default: bool = False) -> bool:
    raw = get_env(key, "1" if default else "0").lower()
    return raw in {"1", "true", "yes", "on"}


def get_openclaw_discovery() -> list[str]:
    return [
        get_env("OPENCLAW_BASE_URL", "http://runtime:3333"),
        "http://runtime:3333",
        "http://gateway:3333",
        "http://127.0.0.1:3333",
        "http://localhost:3333",
    ]


def get_bridge_url() -> str:
    return get_env("HOME_AGENT_BRIDGE_URL", DEFAULT_BRIDGE_URL)


def get_runtime_profile() -> str:
    return get_env("OPENCLAW_RUNTIME_PROFILE", "runtime")


def get_runtime_workspace_root() -> str:
    return get_env("HOME_AGENT_RUNTIME_WORKROOT", "/runtime/workspaces/users")


def oauth_is_available() -> bool:
    base_url = get_public_base_url()
    parsed = urlparse(base_url)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host not in {"localhost", "127.0.0.1"}


def get_public_base_url() -> str:
    return get_env("HOME_AGENT_PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL)


def get_bridge_shared_secret() -> str:
    return get_env("HOME_AGENT_BRIDGE_SHARED_SECRET", get_env("SECRET_KEY", "change-me"))


def resolve_database_uri(raw_uri: str) -> str:
    raw_uri = (raw_uri or "").strip() or "sqlite:////data/app.db"
    if not raw_uri.startswith("sqlite:////"):
        return raw_uri
    absolute_path = Path(raw_uri.replace("sqlite:////", "/", 1))
    if absolute_path.parent.exists():
        return raw_uri
    fallback = (BASE_DIR.parent / "data" / absolute_path.name).resolve()
    fallback.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{fallback}"


def configure_app(app) -> None:
    app.config["SECRET_KEY"] = get_env("SECRET_KEY", "change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = resolve_database_uri(get_env("DATABASE_URL", "sqlite:////data/app.db"))
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = int(get_env("MAX_UPLOAD_MB", "20")) * 1024 * 1024
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=int(get_env("SESSION_EXPIRE_MINUTES", "120")))
