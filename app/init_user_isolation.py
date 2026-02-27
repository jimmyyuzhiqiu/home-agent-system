from app import app, db, User, ensure_user_isolation, ensure_user_agent_binding, ensure_user_conversation, ensure_system_memories, sanitize_namespace


def main():
    with app.app_context():
        users = User.query.order_by(User.id.asc()).all()
        if not users:
            print("no users found, skip")
            return

        changed_users = 0
        initialized = 0
        for user in users:
            _, changed = ensure_user_isolation(user)
            if changed:
                changed_users += 1
            ensure_user_agent_binding(user.id)
            ensure_user_conversation(user.id)
            ensure_system_memories(user.id)
            initialized += 1

        db.session.commit()
        print(f"initialized users={initialized}, namespace_updated={changed_users}")
        for user in users:
            ns = sanitize_namespace(user.memory_namespace)
            print(f"user={user.username} namespace={ns}")


if __name__ == "__main__":
    main()
