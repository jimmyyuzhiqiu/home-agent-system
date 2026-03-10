from functools import wraps

from flask import flash, redirect, url_for
from flask_login import current_user


def normalize_role(role: str) -> str:
    return "admin" if (role or "").strip().lower() == "admin" else "user"


def is_admin_user(user) -> bool:
    if not user:
        return False
    return normalize_role(getattr(user, "role", "")) == "admin"


def admin_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not is_admin_user(current_user):
            flash("仅管理员可访问", "danger")
            return redirect(url_for("chat.chat"))
        return view_func(*args, **kwargs)

    return wrapped
