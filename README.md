# 内部知识助手

基于企业文档的 RAG（检索增强生成）问答系统。上传文档后，登录用户可用自然语言提问，系统从文档中检索相关内容并生成带来源的回答。

## 技术栈

- **后端**：Python 3.11 + FastAPI + SQLAlchemy + PyMySQL
- **前端**：原生 HTML / CSS / JavaScript（无构建步骤）
- **检索**：FAISS 向量检索 + BM25 关键词检索 + 百炼 gte-rerank 精排
- **模型**：百炼 OpenAI 兼容接口（LLM + Embedding）
- **鉴权**：JWT + Casbin（两角色：只读用户 `user` / 管理员 `admin`）
- **部署**：Docker Compose（MySQL 8 + 应用 + Nginx）

## 功能

- 文档上传、列表、删除（按文件名/部门筛选，分页）
- 全量重建索引、按文件 hash 增量切片
- 表格按行切片，保留表头与工作表信息
- 问答支持引用溯源（来源文件、工作表、页码）
- SSE 流式回答 + 多轮对话上下文
- 用户管理（创建、角色/部门/状态修改、重置密码，分页）
- 审计日志（按用户/操作筛选，分页）
- 独立登录页，首次启动自动初始化管理员

## 快速开始（Docker，推荐）

```bash
# 1. 准备配置：复制模板并填写 API key、JWT 密钥、管理员密码等
cp .env.example .env

# 2. 启动（MySQL + 应用 + Nginx）
docker compose up -d --build

# 3. 查看初始化日志
docker compose logs -f app

# 4. 浏览器访问
#    http://服务器IP
```

首次启动会自动建表、创建管理员账号、初始化权限策略。管理员账号密码取自 `.env` 的 `ADMIN_USERNAME` / `ADMIN_PASSWORD`。

### 常用操作

```bash
# 重启 / 停止
docker compose restart
docker compose down

# 全量重建索引（改了切片逻辑或模型后执行）
docker compose exec app python rag_app.py --rebuild

# 查看各服务日志
docker compose logs -f mysql
docker compose logs -f nginx
```

数据持久化：MySQL 数据在 `mysql_data` 卷，文档在 `docs/`，索引在 `faiss_index/`，容器重建不丢失。

## 本地开发

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # Linux/macOS

# 2. 准备 .env（填写 API key、数据库地址、JWT 密钥等）
cp .env.example .env

# 3. 初始化数据库与管理员
.venv\Scripts\python init_db.py

# 4. 首次构建索引
.venv\Scripts\python rag_app.py --rebuild

# 5. 启动服务
.venv\Scripts\python api_server.py
# 访问 http://localhost:8000
```

本地开发若要用远程 MySQL，直接在 `.env` 配 `DATABASE_URL` 即可；Docker 部署时 compose 会用容器内的 MySQL 覆盖该变量。

## 配置说明（.env）

| 变量 | 说明 | 必填 |
|---|---|---|
| `OPENAI_API_KEY` | 百炼 API key | 是 |
| `OPENAI_BASE_URL` | OpenAI 兼容接口地址，默认百炼 | 否 |
| `LLM_MODEL` / `EMBEDDING_MODEL` | 模型名 | 否 |
| `JWT_SECRET` | JWT 签名密钥，至少 32 字节随机串 | 是 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 首次初始化的管理员账号 | 是 |
| `RERANK_ENABLED` | `0` 关闭 reranker，回退混合检索排序 | 否 |
| `RERANK_MODEL` / `RERANK_TIMEOUT` | 精排模型与超时（秒） | 否 |
| `ASK_RATE_LIMIT` / `ASK_RATE_WINDOW` | 问答限流：窗口内每用户最多次数 / 窗口秒数，默认 20 次 / 60 秒（进程内计数，重启清零） | 否 |
| `MYSQL_ROOT_PASSWORD` / `MYSQL_PASSWORD` | compose 内 MySQL 密码 | 否（有默认值） |
| `HTTP_PORT` | Nginx 对外端口，默认 80 | 否 |

生成随机密钥：`python -c "import secrets;print(secrets.token_urlsafe(48))"`

## 目录结构

```
.
├── api_server.py          # FastAPI 接口层
├── rag_app.py             # RAG 核心：解析、切片、索引、检索
├── auth.py                # JWT 与 Casbin 鉴权
├── database.py            # ORM 模型与数据库会话
├── init_db.py             # 初始化：建表、管理员、权限策略
├── department_map.json    # 部门目录标签映射（非权限边界）
├── rbac_model.conf        # Casbin 模型
├── static/
│   ├── index.html         # 页面骨架
│   ├── app.css            # 样式
│   └── app.js             # 前端逻辑
├── docs/                  # 原始文档（按 部门/类型 分目录）
├── faiss_index/           # 向量索引与切片缓存
├── Dockerfile
├── docker-compose.yml
├── nginx.conf
├── requirements.txt
└── .env.example
```

## 主要 API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/login` | 登录 |
| POST | `/api/auth/refresh` | 刷新 token |
| POST | `/api/auth/logout` | 退出 |
| GET | `/api/auth/me` | 当前用户信息与权限 |
| GET | `/api/roles` | 角色列表 |
| GET/POST/PATCH | `/api/users` | 用户管理（分页） |
| GET | `/api/audit-logs` / `/api/audit-actions` | 审计日志（分页） |
| GET | `/api/departments` | 部门标签列表 |
| GET/POST/DELETE | `/api/docs` | 文档列表/上传/删除（分页） |
| POST | `/api/docs/rebuild` | 全量重建索引 |
| GET/POST/DELETE/PATCH | `/api/conversations` | 会话管理 |
| POST | `/api/ask` | 问答（非流式） |
| POST | `/api/ask/stream` | 问答（SSE 流式） |

## 权限模型

- `user`（只读用户）：可问答、查看自己的历史会话。
- `admin`（管理员）：全部权限，含文档上传/删除/重建、用户管理、审计日志。

部门（departments）只是文档目录标签，用于上传归类和筛选，**不参与权限判断**。所有登录用户可检索全部文档。

## 上线注意事项

- 生产环境务必修改 `.env` 中的 `JWT_SECRET` 和管理员密码；改密钥后所有人需重新登录。
- 对外访问建议配置 HTTPS；Nginx 已为 SSE 关闭缓冲，若前置其他网关也需确保不缓冲响应。
- 当前为**单进程**部署：`index_lock` 是进程内锁，不要盲目开多 worker，否则并发重建索引会写坏文件。
- 定期备份 MySQL 数据与 `docs/` 目录；`faiss_index/` 可由文档重建。
- `.env` 含敏感信息，已加入 `.gitignore`，请勿提交到代码仓库。
