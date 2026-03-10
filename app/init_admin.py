import os
import uuid

from werkzeug.security import generate_password_hash

from extensions import db
from factory import create_app
from models import User
from schema import ensure_schema_compat
from services.conversations import ensure_user_agent_binding, ensure_user_conversation


app = create_app()


def main():
    admin_user = os.getenv("ADMIN_USERNAME", "Jimmy")
    admin_password = os.getenv("ADMIN_PASSWORD")
    if not admin_password:
        raise SystemExit("ADMIN_PASSWORD 未设置")

    with app.app_context():
        ensure_schema_compat()
        user = User.query.filter_by(username=admin_user).first()
        if user:
            user.password_hash = generate_password_hash(admin_password)
            user.role = "admin"
            user.display_name = user.display_name or admin_user
            user.execution_profile = "admin_full"
            user.force_password_change = False
            if not user.memory_namespace:
                user.memory_namespace = f"user-{uuid.uuid4().hex[:12]}"
            print(f"管理员 {admin_user} 已存在，已更新密码")
        else:
            user = User(
                username=admin_user,
                display_name=admin_user,
                password_hash=generate_password_hash(admin_password),
                role="admin",
                execution_profile="admin_full",
                memory_namespace=f"user-{uuid.uuid4().hex[:12]}",
                force_password_change=False,
                onboarding_completed=True,
            )
            db.session.add(user)
            db.session.flush()
            ensure_user_agent_binding(user.id)
            ensure_user_conversation(user.id)
            print(f"管理员 {admin_user} 创建成功")
        ensure_user_agent_binding(user.id)
        ensure_user_conversation(user.id)
        db.session.commit()


if __name__ == "__main__":
    main()
