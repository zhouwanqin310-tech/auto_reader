"""
工具函数模块
"""
import os
import json
import hashlib
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path


def get_project_root():
    """获取项目根目录"""
    return Path(__file__).parent.parent


def ensure_dir(path):
    """确保目录存在"""
    os.makedirs(path, exist_ok=True)
    return path


def get_today_date():
    """获取今天的日期字符串"""
    return datetime.now().strftime("%Y-%m-%d")


def get_date_filename(prefix="", extension="md"):
    """生成日期格式的文件名"""
    date_str = get_today_date()
    if prefix:
        return f"{prefix}_{date_str}.{extension}"
    return f"{date_str}.{extension}"


def paper_to_hash(paper_info):
    """生成论文的稳定唯一哈希值，用于去重（优先使用 DOI/ArXiv/PMID 等稳定标识）。"""
    def _norm(value: str) -> str:
        return ' '.join((value or '').strip().lower().split())

    doi = _norm(str(paper_info.get("doi") or ""))
    if doi:
        return hashlib.sha256(f"doi:{doi}".encode()).hexdigest()[:16]

    arxiv_id = _norm(str(paper_info.get("arxiv_id") or ""))
    if arxiv_id:
        return hashlib.sha256(f"arxiv:{arxiv_id}".encode()).hexdigest()[:16]

    pmid = _norm(str(paper_info.get("pmid") or ""))
    if pmid:
        return hashlib.sha256(f"pmid:{pmid}".encode()).hexdigest()[:16]

    title = _norm(str(paper_info.get("title") or ""))
    published = _norm(str(paper_info.get("published") or paper_info.get("updated") or ""))
    authors = paper_info.get("authors") or []
    if isinstance(authors, (list, tuple)):
        first_author = _norm(str(authors[0])) if authors else ""
    else:
        first_author = _norm(str(authors))

    content = f"title:{title}|author:{first_author}|date:{published}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


@contextmanager
def _locked_file(lock_path: str):
    """
    跨线程/跨进程文件锁（Unix: fcntl.flock）。用于保护 JSON 历史读写的原子性。
    """
    lock_dir = os.path.dirname(lock_path) or "."
    os.makedirs(lock_dir, exist_ok=True)
    lock_fp = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            import fcntl  # Unix only
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        except Exception:
            # 如果环境不支持 flock（极少），退化为无锁；但仍保持原子写，尽量减少损坏概率
            pass
        yield lock_fp
    finally:
        try:
            import fcntl
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            lock_fp.close()
        except Exception:
            pass


def load_history(cache_file):
    """加载历史论文记录"""
    cache_file = str(cache_file)
    lock_path = f"{cache_file}.lock"
    with _locked_file(lock_path):
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict) and "papers" in data:
                    data.setdefault("dates", [])
                    return data
            except Exception:
                # 如果文件损坏/写入中断：降级为空结构，避免直接崩溃
                return {"papers": [], "dates": []}
        return {"papers": [], "dates": []}


def save_history(cache_file, history):
    """保存历史论文记录"""
    cache_file = str(cache_file)
    lock_path = f"{cache_file}.lock"
    dir_name = os.path.dirname(cache_file) or "."
    os.makedirs(dir_name, exist_ok=True)

    # 原子写：先写临时文件，再 replace 覆盖
    with _locked_file(lock_path):
        fd, tmp_path = tempfile.mkstemp(prefix=".paper_history.", suffix=".json", dir=dir_name, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, cache_file)
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass


def is_paper_in_history(paper_hash, history, days=7):
    """检查论文是否在最近N天的历史记录中"""
    cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    for entry in history.get("papers", []):
        if entry.get("hash") == paper_hash:
            entry_date = entry.get("date", "")
            if entry_date >= cutoff_date:
                return True
    return False


def is_paper_ever_in_history(paper_hash, history):
    """检查论文是否曾经在历史记录中（全局幂等，不受 days 影响）。"""
    for entry in history.get("papers", []):
        if entry.get("hash") == paper_hash:
            return True
    return False


def add_paper_to_history(paper_info, history):
    """将论文添加到历史记录"""
    paper_hash = paper_to_hash(paper_info)

    # 全局幂等：同一 hash 只写一次（避免 days 窗口过期后重复推送）
    if not is_paper_ever_in_history(paper_hash, history):
        history["papers"].append({
            "hash": paper_hash,
            "title": paper_info.get("title", ""),
            "date": get_today_date(),
            "added_at": datetime.now().isoformat()
        })

    if get_today_date() not in history["dates"]:
        history["dates"].append(get_today_date())

    # 只保留最近30天的记录
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    history["dates"] = [d for d in history["dates"] if d >= cutoff]
    history["papers"] = [p for p in history["papers"] if p.get("date", "") >= cutoff]

    return history


def format_markdown_header(title, level=1):
    """格式化 Markdown 标题"""
    return f"{'#' * level} {title}\n\n"


def sanitize_filename(filename):
    """清理文件名中的非法字符"""
    illegal_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
    for char in illegal_chars:
        filename = filename.replace(char, '_')
    return filename[:200]  # 限制文件名长度
