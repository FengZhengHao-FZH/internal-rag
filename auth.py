import os
from datetime import datetime, timedelta

import bcrypt
import casbin
import jwt
from casbin_sqlalchemy_adapter import Adapter
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from database import DATABASE_URL, User, get_db

SECRET_KEY = os.getenv("JWT_SECRET", "change-this-secret-in-production")
ALGORITHM = "HS256"
ACCESS_MINUTES = 30
REFRESH_DAYS = 7

MAX_PASSWORD_BYTES = 72
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

MODEL_PATH = os.path.join(os.path.dirname(__file__), "rbac_model.conf")
adapter = Adapter(DATABASE_URL)
enforcer = casbin.Enforcer(MODEL_PATH, adapter)

PERMISSION_POINTS = [
    ("doc", "upload"),
    ("doc", "rebuild"),
    ("ask", "query"),
    ("user", "manage"),
]

VALID_ROLES = ["user", "admin"]

_revoked_tokens = set()


def _password_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:MAX_PASSWORD_BYTES]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_password_bytes(plain), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_token(username: str, token_type: str = "access") -> str:
    now = datetime.utcnow()
    expire = now + (
        timedelta(minutes=ACCESS_MINUTES)
        if token_type == "access"
        else timedelta(days=REFRESH_DAYS)
    )
    payload = {
        "sub": username,
        "type": token_type,
        "iat": now,
        "exp": expire,
        "jti": os.urandom(16).hex(),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="登录已失效")


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    if not token:
        raise HTTPException(status_code=401, detail="请先登录")
    payload = decode_token(token)
    if payload.get("type") != "access" or payload.get("jti") in _revoked_tokens:
        raise HTTPException(status_code=401, detail="登录已失效")
    user = db.query(User).filter(User.username == payload["sub"]).first()
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已禁用")
    return user


def revoke_token(token: str):
    try:
        payload = jwt.decode(
            token, SECRET_KEY, algorithms=[ALGORITHM], options={"verify_exp": False}
        )
        _revoked_tokens.add(payload.get("jti"))
    except jwt.PyJWTError:
        pass


def require(obj: str, act: str):
    def dependency(current_user: User = Depends(get_current_user)) -> User:
        if not enforcer.enforce(current_user.username, obj, act):
            raise HTTPException(status_code=403, detail="没有操作权限")
        return current_user

    return dependency


def permissions_of(username: str) -> list:
    return [
        f"{obj}:{act}"
        for obj, act in PERMISSION_POINTS
        if enforcer.enforce(username, obj, act)
    ]


def roles_of(username: str) -> list:
    return enforcer.get_roles_for_user(username)


def can_read_all(username: str) -> bool:
    return enforcer.enforce(username, "doc", "read_all")
