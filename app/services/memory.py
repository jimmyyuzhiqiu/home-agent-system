import re

from extensions import db
from models import MemoryEntry
from settings import SYSTEM_MEMORY_SEEDS
from utils import utcnow


def ensure_system_memories(user_id: int):
    added = 0
    for kind, content in SYSTEM_MEMORY_SEEDS:
        existed = MemoryEntry.query.filter_by(user_id=user_id, content=content).first()
        if existed:
            continue
        db.session.add(MemoryEntry(user_id=user_id, kind=kind, content=content, source="system"))
        added += 1
    if added:
        db.session.commit()
    return added


def get_user_memories(user_id: int, limit: int = 10, include_archived: bool = False):
    query = MemoryEntry.query.filter_by(user_id=user_id)
    if not include_archived:
        query = query.filter(MemoryEntry.archived_at.is_(None))
    return query.order_by(MemoryEntry.pinned.desc(), MemoryEntry.created_at.desc()).limit(limit).all()


def list_user_memories(
    user_id: int,
    kind: str = "",
    source: str = "",
    keyword: str = "",
    include_archived: bool = False,
    sort: str = "recent",
    limit: int = 200,
):
    query = MemoryEntry.query.filter_by(user_id=user_id)
    if not include_archived:
        query = query.filter(MemoryEntry.archived_at.is_(None))
    if kind:
        query = query.filter_by(kind=kind)
    if source:
        query = query.filter_by(source=source)
    if keyword:
        query = query.filter(MemoryEntry.content.contains(keyword.strip()))
    if sort == "pinned":
        query = query.order_by(MemoryEntry.pinned.desc(), MemoryEntry.created_at.desc())
    else:
        query = query.order_by(MemoryEntry.created_at.desc(), MemoryEntry.pinned.desc())
    return query.limit(limit).all()


def auto_extract_memories(user_id: int, user_text: str):
    patterns = [
        ("preference", r"(?:我喜欢|我偏好|我习惯|请优先|以后都)([^。！？\n]{2,80})"),
        ("goal", r"(?:我的目标是|我计划|我要|希望在)([^。！？\n]{2,80})"),
        ("fact", r"(?:我是|我在|我用|我家里有|我的)([^。！？\n]{2,80})"),
    ]
    extracted = []
    for kind, pattern in patterns:
        for match in re.finditer(pattern, user_text):
            snippet = match.group(0).strip()
            if len(snippet) >= 4:
                extracted.append((kind, snippet))

    added = 0
    for kind, content in extracted[:5]:
        existed = MemoryEntry.query.filter_by(user_id=user_id, content=content).first()
        if existed:
            continue
        db.session.add(MemoryEntry(user_id=user_id, kind=kind, content=content, source="auto"))
        added += 1
    if added:
        db.session.commit()
    return added


def memory_remember(user_id: int, content: str):
    content = (content or "").strip()
    if not content:
        return False, "内容为空"
    db.session.add(MemoryEntry(user_id=user_id, kind="manual", content=content, source="manual"))
    db.session.commit()
    return True, "已记住"


def memory_forget(user_id: int, content: str):
    content = (content or "").strip()
    if not content:
        return 0
    matches = (
        MemoryEntry.query.filter_by(user_id=user_id)
        .filter(MemoryEntry.content.contains(content))
        .filter(MemoryEntry.source != "system")
        .all()
    )
    for item in matches:
        db.session.delete(item)
    db.session.commit()
    return len(matches)


def toggle_memory_pin(user_id: int, memory_id: int):
    entry = MemoryEntry.query.filter_by(user_id=user_id, id=memory_id).first()
    if not entry:
        return None
    entry.pinned = not bool(entry.pinned)
    db.session.commit()
    return entry


def archive_memory(user_id: int, memory_id: int):
    entry = MemoryEntry.query.filter_by(user_id=user_id, id=memory_id).first()
    if not entry:
        return None
    entry.archived_at = utcnow()
    entry.pinned = False
    db.session.commit()
    return entry


def restore_memory(user_id: int, memory_id: int):
    entry = MemoryEntry.query.filter_by(user_id=user_id, id=memory_id).first()
    if not entry:
        return None
    entry.archived_at = None
    db.session.commit()
    return entry
