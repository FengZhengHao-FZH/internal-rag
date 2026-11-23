import hashlib
import json
import os
import re
import shutil
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

import rag_app
from auth import (
    create_token,
    decode_token,
    enforcer,
    get_current_user,
    hash_password,
    oauth2_scheme,
    permissions_of,
    require,
    revoke_token,
    roles_of,
    VALID_ROLES,
    verify_password,
)
from database import AuditLog, Conversation, Message, SessionLocal, User, get_db, write_audit

STATIC_DIR = BASE_DIR / "static"
DEPARTMENT_MAP_FILE = BASE_DIR / "department_map.json"
DOCS_ROOT = Path(rag_app.DOCS_DIR).resolve()

DOC_TYPE_DIRS = {
    ".xlsx": "xlsx",
    ".xls": "xlsx",
    ".csv": "xlsx",
    ".docx": "word",
    ".pptx": "ppt",
    ".md": "md",
    ".txt": "txt",
    ".pdf": "pdf",
}

index_lock = threading.Lock()
rag_chain = None
current_vectorstore = None


class LoginRequest(BaseModel):
    username: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class AskRequest(BaseModel):
    question: str
    conversation_id: Optional[int] = None


class ConversationTitleRequest(BaseModel):
    title: str


class UserCreateRequest(BaseModel):
    username: str
    password: str
    department: str = ""
    role: str = "reader"


class UserUpdateRequest(BaseModel):
    is_active: Optional[bool] = None
    department: Optional[str] = None
    role: Optional[str] = None


class PasswordResetRequest(BaseModel):
    password: str


def success(data):
    return {"code": 200, "message": "请求成功", "data": data}


def failure(code, message):
    return {"code": code, "message": message, "data": None}


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def make_conversation_title(question: str, limit: int = 20) -> str:
    text = " ".join(question.strip().split())
    if len(text) <= limit:
        return text or "新对话"
    return text[:limit] + "…"


def build_history(db, conv):
    if conv is None:
        return "无", False
    msgs = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id)
        .order_by(Message.id.asc())
        .all()
    )
    if not msgs:
        return "无", False
    history = "\n".join(
        f"{'用户' if m.role == 'user' else '助手'}：{m.content}" for m in msgs
    )
    return history, True


# 问答限流：按用户做滑动窗口，防止单个账号刷爆付费 API
ASK_RATE_LIMIT = int(os.getenv("ASK_RATE_LIMIT", "20"))
ASK_RATE_WINDOW = int(os.getenv("ASK_RATE_WINDOW", "60"))
_ask_hits = defaultdict(deque)
_ask_lock = threading.Lock()


def ask_rate_limit(current_user: User = Depends(require("ask", "query"))) -> User:
    """每个用户在滑动窗口内最多 ASK_RATE_LIMIT 次问答，超出返回 429。"""
    now = time.time()
    with _ask_lock:
        hits = _ask_hits[current_user.username]
        cutoff = now - ASK_RATE_WINDOW
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= ASK_RATE_LIMIT:
            raise HTTPException(
                status_code=429,
                detail=f"提问过于频繁，每分钟最多 {ASK_RATE_LIMIT} 次，请稍后再试",
            )
        hits.append(now)
    return current_user


def load_department_map() -> dict:
    if not DEPARTMENT_MAP_FILE.exists():
        return {}
    try:
        return json.loads(DEPARTMENT_MAP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def doc_metadata_provider(file_path: str):
    try:
        rel = Path(file_path).resolve().relative_to(DOCS_ROOT)
    except ValueError:
        return {}
    parts = rel.parts
    if len(parts) < 2:
        return {}
    folder = parts[0]
    if folder == "public":
        return {}
    return {"department": load_department_map().get(folder, folder)}


rag_app.DOC_METADATA_PROVIDER = doc_metadata_provider


def file_count():
    return sum(
        1
        for root, _, files in os.walk(rag_app.DOCS_DIR)
        for name in files
        if Path(name).suffix.lower() in rag_app.SUPPORTED_EXTS
    )


def store_stats(vectorstore):
    return {
        "file_count": file_count(),
        "vector_count": vectorstore.index.ntotal if vectorstore is not None else 0,
    }


def refresh_chain(vectorstore):
    global rag_chain, current_vectorstore
    current_vectorstore = vectorstore
    rag_chain = rag_app.create_rag_chain(vectorstore) if vectorstore is not None else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    with index_lock:
        refresh_chain(rag_app.sync_index())
    yield


app = FastAPI(title="内部文档 RAG 服务", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=200, content=failure(exc.status_code, str(exc.detail)))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    detail = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", "")} for e in exc.errors()]
    return JSONResponse(status_code=200, content=failure(400, f"参数错误: {detail}"))


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


# ---------- 认证 ----------

@app.post("/api/auth/login")
def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == req.username).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已禁用")
    write_audit(db, user, "login", ip=client_ip(request))
    return success(
        {
            "access_token": create_token(user.username, "access"),
            "refresh_token": create_token(user.username, "refresh"),
        }
    )


@app.post("/api/auth/refresh")
def refresh(req: RefreshRequest):
    payload = decode_token(req.refresh_token)
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="refresh token 无效")
    return success({"access_token": create_token(payload["sub"], "access")})


@app.post("/api/auth/logout")
def logout(
    request: Request,
    token: str = Depends(oauth2_scheme),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    revoke_token(token or "")
    write_audit(db, current_user, "logout", ip=client_ip(request))
    return success({"message": "已退出登录"})


@app.get("/api/auth/me")
def me(current_user: User = Depends(get_current_user)):
    return success(
        {
            "username": current_user.username,
            "department": current_user.department,
            "roles": roles_of(current_user.username),
            "permissions": permissions_of(current_user.username),
        }
    )


# ---------- 用户管理 ----------

@app.get("/api/roles")
def list_roles(current_user: User = Depends(require("user", "manage"))):
    return success({"items": VALID_ROLES})


@app.post("/api/users")
def create_user(
    req: UserCreateRequest,
    request: Request,
    current_user: User = Depends(require("user", "manage")),
    db: Session = Depends(get_db),
):
    if len(req.password) < 8:
        return failure(400, "密码至少 8 位")
    if req.role not in VALID_ROLES:
        return failure(400, "角色不合法")
    if db.query(User).filter(User.username == req.username).first():
        return failure(400, "用户名已存在")

    user = User(
        username=req.username,
        password_hash=hash_password(req.password),
        department=req.department,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    enforcer.add_role_for_user(req.username, req.role)
    enforcer.save_policy()
    write_audit(
        db, current_user, "user_create",
        f"创建用户 {req.username}（角色 {req.role}）", client_ip(request),
    )
    return success({"id": user.id, "username": user.username})


@app.get("/api/users")
def list_users(
    keyword: Optional[str] = None,
    department: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    current_user: User = Depends(require("user", "manage")),
    db: Session = Depends(get_db),
):
    query = db.query(User)
    if keyword and keyword.strip():
        query = query.filter(User.username.like(f"%{keyword.strip()}%"))
    if department and department.strip():
        dept = department.strip()
        query = query.filter(User.department == ("" if dept == "公开" else dept))

    total = query.count()
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    users = query.order_by(User.id.asc()).offset((page - 1) * page_size).limit(page_size).all()
    return success(
        {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [
                {
                    "id": u.id,
                    "username": u.username,
                    "department": u.department,
                    "is_active": u.is_active,
                    "roles": roles_of(u.username),
                }
                for u in users
            ],
        }
    )


@app.patch("/api/users/{user_id}")
def update_user(
    user_id: int,
    req: UserUpdateRequest,
    request: Request,
    current_user: User = Depends(require("user", "manage")),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return failure(404, "用户不存在")

    if req.is_active is not None:
        user.is_active = req.is_active
    if req.department is not None:
        user.department = req.department
    if req.role is not None:
        if req.role not in VALID_ROLES:
            return failure(400, "角色不合法")
        enforcer.delete_roles_for_user(user.username)
        enforcer.add_role_for_user(user.username, req.role)
        enforcer.save_policy()

    db.commit()
    write_audit(db, current_user, "user_update", f"更新用户 {user.username}", client_ip(request))
    return success({"username": user.username})


@app.patch("/api/users/{user_id}/password")
def reset_password(
    user_id: int,
    req: PasswordResetRequest,
    request: Request,
    current_user: User = Depends(require("user", "manage")),
    db: Session = Depends(get_db),
):
    if len(req.password) < 8:
        return failure(400, "密码至少 8 位")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return failure(404, "用户不存在")

    user.password_hash = hash_password(req.password)
    db.commit()
    write_audit(db, current_user, "password_reset", f"重置 {user.username} 密码", client_ip(request))
    return success({"username": user.username})


AUDIT_ACTIONS = [
    "login", "logout", "user_create", "user_update", "password_reset",
    "doc_upload", "doc_delete", "doc_rebuild", "ask",
]


@app.get("/api/audit-actions")
def list_audit_actions(current_user: User = Depends(require("user", "manage"))):
    return success({"items": AUDIT_ACTIONS})


@app.get("/api/audit-logs")
def list_audit_logs(
    keyword: Optional[str] = None,
    action: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    current_user: User = Depends(require("user", "manage")),
    db: Session = Depends(get_db),
):
    query = db.query(AuditLog)
    if keyword and keyword.strip():
        kw = f"%{keyword.strip()}%"
        query = query.filter(
            AuditLog.username.like(kw) | AuditLog.detail.like(kw) | AuditLog.ip.like(kw)
        )
    if action and action.strip():
        query = query.filter(AuditLog.action == action.strip())

    total = query.count()
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    logs = (
        query.order_by(AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return success(
        {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [
                {
                    "id": log.id,
                    "username": log.username,
                    "action": log.action,
                    "detail": log.detail,
                    "ip": log.ip,
                    "created_at": log.created_at.isoformat() if log.created_at else None,
                }
                for log in logs
            ],
        }
    )


# ---------- 文档与问答 ----------

@app.get("/api/status")
def status(current_user: User = Depends(get_current_user)):
    return success(store_stats(current_vectorstore))


@app.get("/api/departments")
def departments(current_user: User = Depends(get_current_user)):
    dept_map = load_department_map()
    folders = set(dept_map) | {"public"}
    if os.path.isdir(rag_app.DOCS_DIR):
        for entry in os.scandir(rag_app.DOCS_DIR):
            if entry.is_dir():
                folders.add(entry.name)

    return success(
        {
            "items": [
                {
                    "folder": f,
                    "label": "公开" if f == "public" else dept_map.get(f, f),
                }
                for f in sorted(folders)
            ]
        }
    )


@app.get("/api/docs")
def list_docs(
    keyword: Optional[str] = None,
    department: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    current_user: User = Depends(require("doc", "upload")),
):
    dept_map = load_department_map()
    items = []
    kw = keyword.strip().lower() if keyword and keyword.strip() else ""
    for root, _, files in os.walk(rag_app.DOCS_DIR):
        for name in files:
            path = Path(root) / name
            if path.suffix.lower() not in rag_app.SUPPORTED_EXTS:
                continue
            if kw and kw not in name.lower():
                continue
            try:
                rel = path.resolve().relative_to(DOCS_ROOT)
            except ValueError:
                continue
            parts = rel.parts
            folder = parts[0] if len(parts) > 1 else ""
            if department and department.strip() and department.strip() != folder:
                continue
            stat = path.stat()
            items.append(
                {
                    "path": rel.as_posix(),
                    "filename": name,
                    "department": folder,
                    "department_label": "公开" if folder == "public" else dept_map.get(folder, folder or "-"),
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )
    items.sort(key=lambda x: x["modified_at"], reverse=True)
    total = len(items)
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    start = (page - 1) * page_size
    return success(
        {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": items[start : start + page_size],
        }
    )


@app.delete("/api/docs")
def delete_doc(
    path: str,
    request: Request,
    current_user: User = Depends(require("doc", "upload")),
    db: Session = Depends(get_db),
):
    target = (DOCS_ROOT / path).resolve()
    if not target.is_relative_to(DOCS_ROOT):
        return failure(400, "路径不合法")
    if not target.is_file() or target.suffix.lower() not in rag_app.SUPPORTED_EXTS:
        return failure(404, "文件不存在或类型不支持")

    target.unlink()
    with index_lock:
        try:
            vectorstore = rag_app.sync_index()
        except Exception as e:
            return failure(500, f"重建索引失败: {e}")
        refresh_chain(vectorstore)

    write_audit(db, current_user, "doc_delete", path, client_ip(request))
    return success({"path": path})


@app.post("/api/docs/upload")
def upload_doc(
    request: Request,
    file: UploadFile = File(...),
    department: str = Form("public"),
    current_user: User = Depends(require("doc", "upload")),
    db: Session = Depends(get_db),
):
    if not re.fullmatch(r"[\w-]+", department):
        return failure(400, "部门目录名不合法")

    name = Path(file.filename or "").name
    if not name:
        return failure(400, "文件名无效")

    ext = Path(name).suffix.lower()
    if ext not in DOC_TYPE_DIRS or ext not in rag_app.SUPPORTED_EXTS:
        return failure(400, f"不支持的文件类型: {ext or '(无扩展名)'}")

    content = file.file.read()
    if not content:
        return failure(400, "文件内容为空")

    sha = hashlib.sha256(content).hexdigest()
    cache = rag_app.load_cache()
    is_incremental = cache is None or sha not in cache

    target_dir = os.path.join(rag_app.DOCS_DIR, department, DOC_TYPE_DIRS[ext])
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, name)

    with index_lock:
        with open(target, "wb") as f:
            f.write(content)
        try:
            vectorstore = rag_app.sync_index()
        except Exception as e:
            return failure(500, f"切片失败: {e}")
        refresh_chain(vectorstore)

    data = store_stats(vectorstore)
    status_text = "已新增并切片" if is_incremental else "内容已存在，跳过切片"
    data.update(
        {
            "filename": name,
            "department": department,
            "saved_to": target.replace("\\", "/"),
            "status": status_text,
        }
    )
    write_audit(
        db, current_user, "doc_upload", f"{department}/{name} - {status_text}", client_ip(request)
    )
    return success(data)


@app.post("/api/docs/rebuild")
def rebuild_all(
    request: Request,
    current_user: User = Depends(require("doc", "rebuild")),
    db: Session = Depends(get_db),
):
    with index_lock:
        if os.path.isdir(rag_app.DB_DIR):
            shutil.rmtree(rag_app.DB_DIR)
        try:
            vectorstore = rag_app.sync_index()
        except Exception as e:
            return failure(500, f"重建失败: {e}")
        refresh_chain(vectorstore)

    if vectorstore is None:
        return failure(500, "所有文档都解析不到内容，向量库为空")

    data = store_stats(vectorstore)
    write_audit(db, current_user, "doc_rebuild", f"{data['vector_count']} 个向量", client_ip(request))
    return success(data)


@app.post("/api/conversations")
def create_conversation(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = Conversation(user_id=current_user.id, title="新对话")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return success({"id": conv.id, "title": conv.title})


@app.get("/api/conversations")
def list_conversations(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items = (
        db.query(Conversation)
        .filter(Conversation.user_id == current_user.id)
        .order_by(Conversation.updated_at.desc())
        .all()
    )
    return success(
        {
            "items": [
                {
                    "id": c.id,
                    "title": c.title,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                    "updated_at": c.updated_at.isoformat() if c.updated_at else None,
                }
                for c in items
            ]
        }
    )


@app.get("/api/conversations/{conversation_id}")
def get_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = (
        db.query(Conversation)
        .filter(Conversation.id == conversation_id, Conversation.user_id == current_user.id)
        .first()
    )
    if not conv:
        return failure(404, "会话不存在")

    msgs = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.id.asc())
        .all()
    )
    return success(
        {
            "id": conv.id,
            "title": conv.title,
            "messages": [{"role": m.role, "content": m.content} for m in msgs],
        }
    )


@app.patch("/api/conversations/{conversation_id}/title")
def update_conversation_title(
    conversation_id: int,
    req: ConversationTitleRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = (
        db.query(Conversation)
        .filter(Conversation.id == conversation_id, Conversation.user_id == current_user.id)
        .first()
    )
    if not conv:
        return failure(404, "会话不存在")
    title = " ".join(req.title.strip().split())
    if not title:
        return failure(400, "标题不能为空")
    conv.title = title[:128]
    db.commit()
    return success({"id": conv.id, "title": conv.title})


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = (
        db.query(Conversation)
        .filter(Conversation.id == conversation_id, Conversation.user_id == current_user.id)
        .first()
    )
    if not conv:
        return failure(404, "会话不存在")
    db.query(Message).filter(Message.conversation_id == conversation_id).delete()
    db.delete(conv)
    db.commit()
    return success({"id": conversation_id})


@app.post("/api/ask")
def ask(
    req: AskRequest,
    request: Request,
    current_user: User = Depends(ask_rate_limit),
    db: Session = Depends(get_db),
):
    if not req.question.strip():
        return failure(400, "问题不能为空")
    if current_vectorstore is None or rag_chain is None:
        return failure(503, "知识库尚未就绪，请先上传文档或执行全量切片")

    conv = None
    if req.conversation_id is not None:
        conv = (
            db.query(Conversation)
            .filter(Conversation.id == req.conversation_id, Conversation.user_id == current_user.id)
            .first()
        )
        if not conv:
            return failure(404, "会话不存在")

    history, has_message = build_history(db, conv)

    try:
        answer = rag_chain.invoke({"question": req.question, "history": history})
    except Exception as e:
        return failure(500, f"回答失败: {e}")

    if conv is not None:
        db.add(Message(conversation_id=conv.id, role="user", content=req.question.strip()))
        db.add(Message(conversation_id=conv.id, role="assistant", content=answer))
        if not has_message:
            conv.title = make_conversation_title(req.question)
        db.commit()

    write_audit(db, current_user, "ask", req.question[:200], client_ip(request))
    return success({"answer": answer})


@app.post("/api/ask/stream")
def ask_stream(
    req: AskRequest,
    request: Request,
    current_user: User = Depends(ask_rate_limit),
    db: Session = Depends(get_db),
):
    if not req.question.strip():
        return failure(400, "问题不能为空")
    if current_vectorstore is None or rag_chain is None:
        return failure(503, "知识库尚未就绪，请先上传文档或执行全量切片")

    conv = None
    if req.conversation_id is not None:
        conv = (
            db.query(Conversation)
            .filter(Conversation.id == req.conversation_id, Conversation.user_id == current_user.id)
            .first()
        )
        if not conv:
            return failure(404, "会话不存在")

    history, has_message = build_history(db, conv)
    question = req.question.strip()
    chain = rag_chain
    conv_id = conv.id if conv is not None else None
    user_id = current_user.id
    client_addr = client_ip(request)

    def sse(payload):
        return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

    def generate():
        inputs = {"question": question, "history": history}
        chunks = []

        try:
            for chunk in chain.stream(inputs):
                if chunk:
                    chunks.append(chunk)
                    yield sse({"type": "delta", "content": chunk})
        except Exception:
            if chunks:
                yield sse({"type": "warning", "message": "回答生成中断，已保存部分内容"})
            else:
                try:
                    answer = chain.invoke(inputs)
                    if answer:
                        chunks = [answer]
                        yield sse({"type": "delta", "content": answer})
                except Exception as e:
                    yield sse({"type": "error", "message": f"回答失败: {e}"})
                    return

        answer = "".join(chunks)
        write_db = SessionLocal()
        try:
            if conv_id is not None:
                saved_conv = write_db.query(Conversation).filter(Conversation.id == conv_id).first()
                if saved_conv is not None:
                    write_db.add(Message(conversation_id=conv_id, role="user", content=question))
                    write_db.add(Message(conversation_id=conv_id, role="assistant", content=answer))
                    if not has_message:
                        saved_conv.title = make_conversation_title(question)
                    write_db.commit()
            saved_user = write_db.query(User).filter(User.id == user_id).first()
            if saved_user is None:
                saved_user = current_user
            write_audit(write_db, saved_user, "ask", question[:200], client_addr)
        except Exception as e:
            yield sse({"type": "warning", "message": f"保存会话失败: {e}"})
        finally:
            write_db.close()

        yield sse({"type": "done", "answer": answer})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("API_HOST", "0.0.0.0"),
        port=int(os.getenv("API_PORT", "8000")),
    )
