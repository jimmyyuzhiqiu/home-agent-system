import uuid
from pathlib import Path

from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from settings import UPLOAD_DIR
from utils import allowed_file, sanitize_namespace


def save_uploaded_file(user, file_storage: FileStorage):
    if not file_storage or not file_storage.filename:
        return None
    if not allowed_file(file_storage.filename):
        raise ValueError("文件类型不支持，仅允许常见图片/文档/压缩包")
    safe_name = secure_filename(file_storage.filename)
    namespace = sanitize_namespace(user.memory_namespace)
    user_upload_dir = UPLOAD_DIR / namespace
    user_upload_dir.mkdir(parents=True, exist_ok=True)
    unique_name = f"u{user.id}_{uuid.uuid4().hex[:8]}_{safe_name}"
    store_path = user_upload_dir / unique_name
    file_storage.save(store_path)
    rel_path = f"{namespace}/{unique_name}"
    return {
        "attachment_name": safe_name,
        "attachment_path": rel_path,
        "attachment_hint": f"附件: {safe_name}, 本地路径: /uploads/{rel_path}",
    }


def save_generated_artifact(user, filename: str, content: str, suffix: str | None = None):
    namespace = sanitize_namespace(user.memory_namespace)
    user_upload_dir = UPLOAD_DIR / namespace
    user_upload_dir.mkdir(parents=True, exist_ok=True)
    base_name = secure_filename(filename or "result")
    stem = Path(base_name).stem or "result"
    ext = suffix or Path(base_name).suffix or ".md"
    unique_name = f"generated_u{user.id}_{uuid.uuid4().hex[:8]}_{stem}{ext}"
    store_path = user_upload_dir / unique_name
    store_path.write_text(content or "", encoding="utf-8")
    return {
        "attachment_name": f"{stem}{ext}",
        "attachment_path": f"{namespace}/{unique_name}",
        "mime_type": "text/markdown" if ext == ".md" else "text/plain",
    }
