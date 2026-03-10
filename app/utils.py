import re
from datetime import date, datetime, time, timedelta, timezone

import bleach
import markdown
from markupsafe import Markup

from settings import ALLOWED_EXTENSIONS


ALLOWED_MARKDOWN_TAGS = set(bleach.sanitizer.ALLOWED_TAGS).union(
    {"p", "pre", "code", "blockquote", "ul", "ol", "li", "hr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "span"}
)
ALLOWED_MARKDOWN_ATTRIBUTES = {
    "a": ["href", "title", "target", "rel"],
    "code": ["class"],
    "span": ["class"],
}


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def sanitize_namespace(value: str) -> str:
    raw = (value or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", raw).strip("-")
    return cleaned or "user-auto"


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def summarize_text(text: str, max_len: int = 120) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= max_len else text[:max_len] + "…"


def parse_plan_steps(plan_text: str) -> list[str]:
    lines = [ln.strip() for ln in (plan_text or "").splitlines() if ln.strip()]
    steps = []
    for line in lines:
        cleaned = re.sub(r"^[-*•\d\.\)\s]+", "", line).strip()
        if cleaned:
            steps.append(cleaned)
    return steps[:8]


def render_markdown(text: str) -> Markup:
    source = (text or "").strip()
    if not source:
        return Markup("")
    html = markdown.markdown(
        source,
        extensions=["fenced_code", "tables", "sane_lists", "nl2br"],
        output_format="html5",
    )
    clean = bleach.clean(
        html,
        tags=ALLOWED_MARKDOWN_TAGS,
        attributes=ALLOWED_MARKDOWN_ATTRIBUTES,
        protocols={"http", "https", "mailto"},
        strip=True,
    )
    linked = bleach.linkify(clean)
    return Markup(linked)


def parse_date(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def in_date_range(moment: datetime | None, start_date: str, end_date: str) -> bool:
    if not moment:
        return False
    start = parse_date(start_date)
    end = parse_date(end_date)
    current = moment.date()
    if start and current < start:
        return False
    if end and current > end:
        return False
    return True


def start_of_day(target: date | None = None) -> datetime:
    target = target or utcnow().date()
    return datetime.combine(target, time.min)


def end_of_day(target: date | None = None) -> datetime:
    return start_of_day(target) + timedelta(days=1)


def contains_permission_block(*texts: str) -> bool:
    return any("权限阻塞" in (text or "") for text in texts)


def status_badge(status: str | None) -> str:
    mapping = {
        "queued": "queued",
        "running": "running",
        "pending": "pending",
        "done": "success",
        "failed": "danger",
        "blocked": "warning",
        "archived": "muted",
    }
    return mapping.get((status or "").strip().lower(), "muted")
