import csv
import hashlib
import io
import json
import os
import re
import shutil
import sys
import urllib.request
import warnings
from pathlib import Path

# langchain_community 里没有 FAISS 和本地文件 loader 的独立替代包，导入时必弹这条警告，
# 只屏蔽这一句，别的 DeprecationWarning 照常提示
warnings.filterwarnings(
    "ignore",
    message="`langchain-community` is being sunset",
    category=DeprecationWarning,
)

import numpy as np
import openpyxl
from rank_bm25 import BM25Okapi
from dotenv import load_dotenv

load_dotenv()
from langchain_community.document_loaders import (
    Docx2txtLoader,
    UnstructuredPowerPointLoader,
)
from langchain_community.document_loaders import PyPDFLoader
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

# 千问走百炼的 OpenAI 兼容接口：只需换 base_url + 模型名，不用换 SDK
API_BASE_URL = os.getenv(
    "OPENAI_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3.8-max")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3.7-text-embedding")

# reranker：走百炼 gte-rerank 的 OpenAI 兼容接口；关闭或失败时自动降级为混合检索原排序
RERANK_ENABLED = os.getenv("RERANK_ENABLED", "1") not in ("0", "false", "no")
RERANK_MODEL = os.getenv("RERANK_MODEL", "gte-rerank-v2")
RERANK_API_URL = os.getenv(
    "RERANK_API_URL",
    "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
)
RERANK_TIMEOUT = int(os.getenv("RERANK_TIMEOUT", "5"))

# chromadb 的 Rust 绑定在本机 collection.add 时会 access violation 崩掉进程，改用 faiss
DOCS_DIR = "./docs"
DB_DIR = "./faiss_index"
INDEX_NAME = "index"

# docs 下按「部门 / 文档类型」两级目录存放，扫描时递归遍历：
#   docs/<部门目录>/<类型目录>/文件
#   部门目录名即部门标识（如 rizecore-nb），public 表示公开文档
#   类型目录：xlsx / word / ppt / md / txt / pdf

# 增量入库用：按文件内容 hash 缓存已算好的向量，命中缓存的文件不再调 embedding
CACHE_META = os.path.join(DB_DIR, "chunks.json")
CACHE_VECS = os.path.join(DB_DIR, "vectors.npz")
# 解析或切分逻辑变了就 +1，让旧缓存整体失效（否则文件没变就不会重新解析）
CACHE_VERSION = 6

# 由服务层注入：接收文件路径，返回要合并进 chunk 的元数据（如部门、密级）
DOC_METADATA_PROVIDER = None

SUPPORTED_EXTS = {
    ".xlsx", ".xls", ".csv",  # 表格
    ".docx",                  # Word
    ".pptx",                  # PPT
    ".md",                    # Markdown
    ".txt",                   # 纯文本
    ".pdf",                   # PDF
}


CHUNK_SIZE = 600


def format_row(row):
    """单行压成文本：单格直接写，两格写「键：值」，多格用 | 连接。"""
    cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
    if not cells:
        return ""
    if len(cells) == 1:
        return cells[0]
    if len(cells) == 2:
        return f"{cells[0]}：{cells[1]}"
    return " | ".join(cells)


def rows_to_lines(rows):
    lines = [line for line in (format_row(row) for row in rows) if line]
    return lines


def rows_to_table_docs(rows, file_path: str, sheet: str):
    """按行边界切表格：每块都带表头，不切断单行，并保留 sheet 用于溯源。"""
    all_rows = [row for row in rows if any(c is not None and str(c).strip() for c in row)]
    if not all_rows:
        return []

    header_line = format_row(all_rows[0])
    data_lines = [line for line in (format_row(row) for row in all_rows[1:]) if line]
    if not header_line and not data_lines:
        return []

    prefix = f"工作表：{sheet}\n表头：{header_line}\n" if header_line else f"工作表：{sheet}\n"
    budget = CHUNK_SIZE - len(prefix)
    docs = []
    current = []
    current_len = 0

    for line in data_lines:
        line_len = len(line) + 1
        if current and current_len + line_len > budget:
            text = prefix + "\n".join(current)
            docs.append(
                Document(page_content=text, metadata={"source": file_path, "sheet": sheet, "no_split": True})
            )
            current, current_len = [], 0
        current.append(line)
        current_len += line_len

    if current:
        text = prefix + "\n".join(current)
        docs.append(
            Document(page_content=text, metadata={"source": file_path, "sheet": sheet, "no_split": True})
        )
    return docs


def load_xlsx_sheets(file_path: str):
    """用 openpyxl 本地解析 xlsx：一个 sheet 一个 Document。

    不走 unstructured，那个路径会去 GitHub 下 spaCy 模型，断网时先卡几分钟再失败。
    """
    workbook = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
    docs = []
    try:
        for sheet in workbook.worksheets:
            docs.extend(rows_to_table_docs(sheet.iter_rows(values_only=True), file_path, sheet.title))
    finally:
        workbook.close()
    return docs


def load_xls_sheets(file_path: str):
    """用 xlrd 解析老版 xls：一个 sheet 一个 Document（xlrd 2.x 只支持 .xls）。"""
    try:
        import xlrd
    except ImportError as e:
        raise ImportError("解析 .xls 需要先安装 xlrd：pip install xlrd") from e

    book = xlrd.open_workbook(file_path)
    docs = []
    try:
        for sheet in book.sheets():
            rows = (
                [sheet.cell_value(r, c) for c in range(sheet.ncols)]
                for r in range(sheet.nrows)
            )
            docs.extend(rows_to_table_docs(rows, file_path, sheet.name))
    finally:
        book.release_resources()
    return docs


def _read_text_file(file_path: str) -> str:
    """中文纯文本常见 utf-8-sig / gbk 两种编码，依次尝试。"""
    last_error = None
    for enc in ("utf-8-sig", "gbk"):
        try:
            with open(file_path, encoding=enc, newline="") as f:
                return f.read()
        except UnicodeDecodeError as e:
            last_error = e
    raise last_error


def load_csv(file_path: str):
    """用标准库 csv 解析，按行切分并保留文件名作为来源。"""
    return rows_to_table_docs(
        csv.reader(io.StringIO(_read_text_file(file_path))),
        file_path,
        Path(file_path).name,
    )


def load_plain_text(file_path: str):
    """md/txt 等纯文本：整篇作为一个 Document，兼容 utf-8 / gbk。"""
    text = _read_text_file(file_path)
    if not text.strip():
        return []
    return [Document(page_content=text, metadata={"source": file_path})]


def load_file(file_path: str):
    ext = Path(file_path).suffix.lower()

    if ext == ".docx":
        return Docx2txtLoader(file_path).load()
    if ext == ".xlsx":
        return load_xlsx_sheets(file_path)
    if ext == ".xls":
        return load_xls_sheets(file_path)
    if ext == ".csv":
        return load_csv(file_path)
    if ext == ".pdf":
        return PyPDFLoader(file_path).load()
    if ext == ".pptx":
        return UnstructuredPowerPointLoader(file_path, mode="single").load()
    if ext == ".md" or ext == ".txt":
        return load_plain_text(file_path)

    return []


def file_sha256(file_path: str) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def scan_doc_files(directory: str):
    """返回 {文件内容 hash: 文件路径}，内容完全相同的文件只入库一次。"""
    by_hash = {}
    for root, _, files in os.walk(directory):
        for name in files:
            path = os.path.join(root, name)
            if Path(name).suffix.lower() in SUPPORTED_EXTS:
                by_hash.setdefault(file_sha256(path), path)
    return by_hash


def load_one_file(file_path: str):
    """解析单个文件，过滤掉无内容的片段。"""
    try:
        docs = load_file(file_path)
    except Exception as e:
        print(f"加载失败: {file_path}, {e}")
        return []

    docs = [d for d in docs if d.page_content.strip()]
    if not docs:
        print(f"   解析结果为空，跳过: {Path(file_path).name}")
    for doc in docs:
        doc.metadata["source_file"] = Path(file_path).name
        if DOC_METADATA_PROVIDER:
            doc.metadata.update(DOC_METADATA_PROVIDER(file_path) or {})
    return docs


def split_docs(docs):
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=80,
        separators=["\n\n", "\n", "。", "！", "？", "；", "，", " "],
    )
    ready = [d for d in docs if d.metadata.get("no_split")]
    rest = [d for d in docs if not d.metadata.get("no_split")]
    return ready + splitter.split_documents(rest)


def build_embeddings():
    # check_embedding_ctx_length=False：跳过 tiktoken 计数（tiktoken 不认识千问模型名）
    # chunk_size=10：千问 embedding 单次请求最多 20 行，默认 1000 会报错
    return OpenAIEmbeddings(
        model=EMBEDDING_MODEL,
        base_url=API_BASE_URL,
        check_embedding_ctx_length=False,
        chunk_size=10,
    )


def embed_file(file_path: str):
    """解析 + 切分 + 调 embedding，返回 (texts, metas, vectors)。"""
    chunks = split_docs(load_one_file(file_path))
    if not chunks:
        return [], [], np.zeros(0, dtype=np.float32)

    texts = [c.page_content for c in chunks]
    metas = [c.metadata for c in chunks]
    vectors = np.asarray(build_embeddings().embed_documents(texts), dtype=np.float32)
    return texts, metas, vectors


def build_store_from_cache(cache):
    """用缓存里的向量本地重建索引，不再调用 embedding 接口。"""
    pairs = []
    metas = []
    for item in cache.values():
        for text, meta, vec in zip(item["texts"], item["metas"], item["vectors"]):
            pairs.append((text, vec.tolist()))
            metas.append(meta)

    if not pairs:
        return None

    vectorstore = FAISS.from_embeddings(pairs, build_embeddings(), metadatas=metas)
    os.makedirs(DB_DIR, exist_ok=True)
    vectorstore.save_local(DB_DIR, index_name=INDEX_NAME)
    return vectorstore


def format_docs(docs):
    parts = []
    for i, doc in enumerate(docs, 1):
        source = doc.metadata.get("source_file", "未知文件")
        loc = []
        sheet = doc.metadata.get("sheet")
        page = doc.metadata.get("page")
        if sheet:
            loc.append(f"工作表 {sheet}")
        if page is not None:
            loc.append(f"第 {page + 1} 页")
        where = f"（{'，'.join(loc)}）" if loc else ""
        parts.append(f"[{i}] 来源：{source}{where}\n{doc.page_content}")
    return "\n\n".join(parts)


def tokenize_for_bm25(text: str):
    """中文按 2-gram、英文数字按整词切分，用于 BM25 关键词召回。"""
    text = text.lower()
    tokens = re.findall(r"[a-z0-9]+", text)
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(seg) == 1:
            tokens.append(seg)
        else:
            tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
    return tokens


def build_bm25(vectorstore):
    """从向量库的文档集合构建 BM25 索引；空库返回 None。"""
    docs = list(vectorstore.docstore._dict.values())
    if not docs:
        return None, []
    corpus = [tokenize_for_bm25(d.page_content) for d in docs]
    return BM25Okapi(corpus), docs


def rerank_documents(question, docs, top_n):
    """用百炼 rerank 接口对候选精排。关闭、无 key 或调用失败时返回 None，交回退逻辑。"""
    if not RERANK_ENABLED or not docs:
        return None
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return None

    payload = {
        "model": RERANK_MODEL,
        "input": {
            "query": question,
            "documents": [d.page_content for d in docs],
        },
        "parameters": {"return_documents": False, "top_n": top_n},
    }
    req = urllib.request.Request(
        RERANK_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=RERANK_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ranked = []
        for item in (data.get("output") or {}).get("results") or []:
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < len(docs):
                ranked.append(docs[idx])
        return ranked or None
    except Exception as e:
        print(f"[rerank] 调用失败，降级为混合检索原排序：{e}")
        return None


def hybrid_retrieve(question, vectorstore, bm25, bm25_docs, k=4, pool=20):
    """向量召回 + BM25 召回去重后，由 reranker 精排到 k 条；失败则降级为原排序。"""
    candidates = vectorstore.similarity_search(question, k=pool)
    if bm25 is not None:
        scores = bm25.get_scores(tokenize_for_bm25(question))
        top_idx = np.argsort(scores)[::-1][:pool]
        candidates += [bm25_docs[i] for i in top_idx if scores[i] > 0]

    seen = set()
    merged = []
    for doc in candidates:
        key = doc.metadata.get("source", "") + "||" + doc.page_content
        if key in seen:
            continue
        seen.add(key)
        merged.append(doc)

    ranked = rerank_documents(question, merged[:pool], k)
    return ranked if ranked else merged[:k]


def create_rag_chain(vectorstore, k=4):
    bm25, bm25_docs = build_bm25(vectorstore)

    def retrieve(question):
        return hybrid_retrieve(question, vectorstore, bm25, bm25_docs, k=k)

    prompt_text = (
        "你是一个内部知识助手。只能根据下方参考资料回答问题。\n"
        "如果参考资料中没有答案，请回答：未找到相关信息。\n\n"
        "参考之前的对话历史（可能包含指代，如「它」「上面」）：\n{history}\n\n"
        "参考资料：\n{context}\n\n"
        "问题：{question}\n"
    )
    prompt = ChatPromptTemplate.from_template(prompt_text)

    llm = ChatOpenAI(model=LLM_MODEL, base_url=API_BASE_URL, temperature=0, stream_usage=True)

    return (
        {
            "context": lambda x: format_docs(retrieve(x["question"])),
            "history": lambda x: x.get("history") or "无",
            "question": lambda x: x["question"],
        }

        | prompt
        | llm
        | StrOutputParser()
    )


def load_cache():
    """读回上次入库缓存。首次入库或换了 embedding 模型时返回 None（需全量重建）。"""
    if not (os.path.exists(CACHE_META) and os.path.exists(CACHE_VECS)):
        return None

    with open(CACHE_META, encoding="utf-8") as f:
        meta = json.load(f)

    if meta.get("version") != CACHE_VERSION:
        print("   解析/切分逻辑已更新，旧缓存作废，改为全量入库。")
        return None

    if meta.get("embedding_model") != EMBEDDING_MODEL:
        print(f"   向量模型已从 {meta.get('embedding_model')} 换成 {EMBEDDING_MODEL}，旧缓存不可复用。")
        print(f"   请删除 {DB_DIR} 目录后重新全量入库。")
        return None

    vectors = np.load(CACHE_VECS)
    cache = {}
    for sha, item in meta["files"].items():
        cache[sha] = {
            "path": item["path"],
            "texts": item["texts"],
            "metas": item["metas"],
            "vectors": vectors[sha] if sha in vectors.files else np.zeros(0, dtype=np.float32),
        }
    return cache


def save_cache(cache):
    os.makedirs(DB_DIR, exist_ok=True)

    meta = {
        "version": CACHE_VERSION,
        "embedding_model": EMBEDDING_MODEL,
        "files": {
            sha: {"path": v["path"], "texts": v["texts"], "metas": v["metas"]}
            for sha, v in cache.items()
        },
    }
    with open(CACHE_META, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)

    np.savez_compressed(
        CACHE_VECS,
        **{sha: np.asarray(v["vectors"], dtype=np.float32) for sha, v in cache.items()},
    )


def sync_index():
    """增量入库：只有新文件和内容改过的文件才重新调 embedding。"""
    files = scan_doc_files(DOCS_DIR)
    if not files:
        print("docs 目录下没有找到支持的文档")
        return None

    cache = load_cache()
    full_build = cache is None
    if full_build:
        cache = {}
        print("全量入库 ...")

    todo = [sha for sha in files if full_build or sha not in cache]
    removed = [sha for sha in cache if sha not in files]
    store_index = os.path.join(DB_DIR, f"{INDEX_NAME}.faiss")

    if not todo and not removed and os.path.exists(store_index):
        print(f"文档没有变化（{len(cache)} 个文件），直接加载现有向量库。")
        return load_existing_vectorstore()

    for sha in todo:
        path = files[sha]
        print(f"   处理 {Path(path).name} ...")
        texts, metas, vectors = embed_file(path)
        cache[sha] = {"path": path, "texts": texts, "metas": metas, "vectors": vectors}
        print(f"   embedding {Path(path).name} -> {len(texts)} 片段")

    for sha in removed:
        print(f"   移除已删除的文件 {Path(cache[sha]['path']).name}")
        del cache[sha]

    save_cache(cache)
    vectorstore = build_store_from_cache(cache)
    if vectorstore is None:
        print("   所有文档都解析不到内容，向量库为空")
        return None

    print(f"   向量库共 {vectorstore.index.ntotal} 个向量")
    return vectorstore


def load_existing_vectorstore():
    # faiss 的 pickle 元文件里存的是向量库自己生成的文档对象，本地重建所以开反序列化
    return FAISS.load_local(
        DB_DIR,
        build_embeddings(),
        index_name=INDEX_NAME,
        allow_dangerous_deserialization=True,
    )


def main():
    if "--rebuild" in sys.argv and os.path.isdir(DB_DIR):
        shutil.rmtree(DB_DIR)
        print("已按 --rebuild 清空向量库，将全量重建。")

    vectorstore = sync_index()

    if vectorstore is None:
        return

    chain = create_rag_chain(vectorstore)

    print("\nRAG 已就绪。输入问题开始问答，输入 q 退出。")
    while True:
        question = input("\n你：")
        if question.strip().lower() in {"q", "quit", "exit"}:
            break
        if not question.strip():
            continue
        answer = chain.invoke({"question": question, "history": "无"})
        print(f"\nAI：{answer}")


if __name__ == "__main__":
    main()