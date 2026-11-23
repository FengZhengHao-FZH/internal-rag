import os

from dotenv import load_dotenv

from auth import enforcer, hash_password
from database import Base, SessionLocal, User, engine

load_dotenv()

POLICIES = [
    ("admin", "*", "*"),
    ("user", "ask", "query"),
]


def migrate_roles(db):
    """清理旧角色策略，把所有非 admin 用户统一为只读的 user 角色。"""
    for sub, obj, act in [("user", "doc", "upload"), ("reader", "ask", "query")]:
        enforcer.remove_policy(sub, obj, act)

    for user in db.query(User).all():
        roles = enforcer.get_roles_for_user(user.username)
        if "admin" in roles:
            continue
        enforcer.delete_roles_for_user(user.username)
        enforcer.add_role_for_user(user.username, "user")
    enforcer.save_policy()


def init():
    Base.metadata.create_all(bind=engine)

    for sub, obj, act in POLICIES:
        enforcer.add_policy(sub, obj, act)
    enforcer.save_policy()

    username = os.getenv("ADMIN_USERNAME", "admin")
    password = os.getenv("ADMIN_PASSWORD")
    if not password:
        raise RuntimeError("请在 .env 中设置 ADMIN_PASSWORD（至少 8 位强密码）")

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if not user:
            db.add(
                User(
                    username=username,
                    password_hash=hash_password(password),
                    department="",
                    is_active=True,
                )
            )
            db.commit()
            print(f"已创建管理员账号：{username}")
        else:
            print(f"管理员账号已存在：{username}")

        enforcer.add_role_for_user(username, "admin")
        enforcer.save_policy()

        migrate_roles(db)
    finally:
        db.close()

    print("初始化完成。")


if __name__ == "__main__":
    init()
