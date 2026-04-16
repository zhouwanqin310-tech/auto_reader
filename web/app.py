#!/usr/bin/env python3
"""
论文阅读助手 Web 版
Flask 后端
"""
import os
import sys
import json
import time
import yaml
import re
import random
import schedule
import threading
import requests
import hashlib
from pathlib import Path
from datetime import datetime, timedelta

from flask import Flask, render_template, request, jsonify, send_file

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger import configure_logging
from utils.filter_profile import get_filter_config, get_ai_match_persona

app = Flask(__name__)
app.config['SECRET_KEY'] = 'paper-assistant-secret-key'
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

# 加载配置
CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
    CONFIG = yaml.safe_load(f)

LOGGER = configure_logging(Path(__file__).parent.parent)
FILTER_CFG = get_filter_config(CONFIG)
FILTER_THRESHOLDS = FILTER_CFG.get("thresholds") or {}
AI_MATCH_PERSONA = get_ai_match_persona(CONFIG)

PAPERS_DIR = Path(__file__).parent.parent / "papers"
PDFS_DIR = Path(__file__).parent.parent / "pdfs"
PDFS_FAVORITES_DIR = PDFS_DIR / "favorites"
NOTES_DIR = PAPERS_DIR / "notes"
CHAT_DIR = Path(__file__).parent.parent / "chat_history"
FAVORITES_FILE = Path(__file__).parent.parent / "paper_favorites.json"

# 确保目录存在
PAPERS_DIR.mkdir(exist_ok=True)
PDFS_DIR.mkdir(exist_ok=True)
PDFS_FAVORITES_DIR.mkdir(exist_ok=True)
NOTES_DIR.mkdir(exist_ok=True)
PUSH_JOBS = {}
PUSH_JOBS_LOCK = threading.Lock()
API_SERIAL_LOCK = threading.Lock()
LAST_API_CALL_TS = 0.0
PUSH_EXECUTION_LOCK = threading.Lock()
AI_MATCH_CACHE = {}
AI_MATCH_CACHE_LOCK = threading.Lock()
PARSE_CACHE = {}
PARSE_CACHE_LOCK = threading.Lock()


def paced_api_wait(min_interval=1.0):
    """串行化外部 API 调用，并在两次调用间至少等待指定秒数。"""
    global LAST_API_CALL_TS
    with API_SERIAL_LOCK:
        now = time.time()
        wait_seconds = max(0.0, min_interval - (now - LAST_API_CALL_TS))
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        LAST_API_CALL_TS = time.time()

def create_push_job(initial_payload=None):
    job_id = f"push-{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
    state = {
        "job_id": job_id,
        "status": "queued",
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "params": initial_payload or {},
        "results": [],
        "logs": [],
        "progress": {
            "current_step": "parse",
            "stage_label": "任务已创建，等待执行",
            "percent": 0,
            "step_details": {},
            "meta": {
                "total_candidates": 0,
                "after_time_filter": 0,
                "after_topic_prefilter": 0,
                "after_history_filter": 0,
                "time_rejected_count": 0,
                "matched_count": 0,
                "success_count": 0
            }
        },
        "meta": {
            "total_candidates": 0,
            "after_time_filter": 0,
            "after_topic_prefilter": 0,
            "after_history_filter": 0,
            "time_rejected_count": 0,
            "matched_count": 0,
            "rejected_count": 0,
            "rejected_papers": [],
            "success_count": 0,
            "failure_count": 0
        },
        "error": ""
    }
    with PUSH_JOBS_LOCK:
        PUSH_JOBS[job_id] = state
    return job_id


def get_push_job(job_id):
    with PUSH_JOBS_LOCK:
        job = PUSH_JOBS.get(job_id)
        if not job:
            return None
        return json.loads(json.dumps(job))


def update_push_job(job_id, **updates):
    with PUSH_JOBS_LOCK:
        job = PUSH_JOBS.get(job_id)
        if not job:
            return None
        for key, value in updates.items():
            if value is not None:
                job[key] = value
        job["updated_at"] = datetime.now().isoformat()
        return json.loads(json.dumps(job))


def append_push_job_log(job_id, message, step=None, percent=None, stage_label=None, meta=None):
    with PUSH_JOBS_LOCK:
        job = PUSH_JOBS.get(job_id)
        if not job:
            return None
        timestamp = datetime.now().strftime('%H:%M:%S')
        job["logs"].append({"time": timestamp, "message": message})
        try:
            LOGGER.info("[PUSH %s] %s", job_id, message)
        except Exception:
            pass
        progress = job.setdefault("progress", {})
        progress.setdefault("step_details", {})
        progress.setdefault("meta", {})
        if step:
            progress["current_step"] = step
            progress["step_details"][step] = message
        if percent is not None:
            progress["percent"] = percent
        if stage_label:
            progress["stage_label"] = stage_label
        if meta:
            progress["meta"].update(meta)
        job["updated_at"] = datetime.now().isoformat()
        return json.loads(json.dumps(job))

def get_paper_pdf_candidates(paper_id, title=""):
    """根据论文 ID 和标题推测可能的 PDF 文件"""
    candidates = []
    seen_paths = set()

    keywords = [paper_id]
    title_tokens = [token for token in re.split(r'[^\w]+', title or '') if len(token) >= 3][:5]
    keywords.extend(title_tokens)

    for search_dir in (PDFS_DIR, PDFS_FAVORITES_DIR):
        if not search_dir.exists():
            continue
        for pdf_file in search_dir.glob('*.pdf'):
            pdf_name = pdf_file.stem.lower()
            match_count = 0
            for keyword in keywords:
                keyword = (keyword or '').lower().strip()
                if keyword and keyword in pdf_name:
                    match_count += 1
            if match_count > 0:
                score = (match_count, pdf_file.stat().st_mtime)
                resolved = str(pdf_file.resolve())
                if resolved not in seen_paths:
                    seen_paths.add(resolved)
                    candidates.append((score, pdf_file))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return [file for _, file in candidates]


def get_papers():
    """获取所有论文"""
    favorites = load_favorites()
    favorite_ids = set(favorites.get("paper_ids", []))
    papers = []
    for md_file in sorted(PAPERS_DIR.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        if md_file.name.endswith("_notes.md"):
            continue

        try:
            content = md_file.read_text(encoding='utf-8')

            # 提取标题
            title_match = content.find("**论文**:")
            if title_match != -1:
                title = content[title_match:].split("\n")[0].replace("**论文**:", "").strip()
            else:
                title = md_file.stem

            # 检查是否有 PDF
            stem = md_file.stem
            has_pdf = bool(get_paper_pdf_candidates(stem, title))

            # 检查是否有笔记
            note_file = NOTES_DIR / f"{stem}_notes.md"
            has_notes = note_file.exists()

            papers.append({
                "id": md_file.stem,
                "filename": md_file.name,
                "title": title,
                "path": str(md_file),
                "date": datetime.fromtimestamp(md_file.stat().st_mtime).strftime("%Y-%m-%d"),
                "has_pdf": has_pdf,
                "is_favorite": md_file.stem in favorite_ids,
                "has_notes": has_notes,
                "content": content
            })
        except Exception as e:
            LOGGER.exception("读取论文文件失败：%s｜%s", str(md_file), str(e))

    return papers


def get_paper(paper_id):
    """获取指定论文"""
    papers = get_papers()
    for p in papers:
        if p["id"] == paper_id:
            return p
    return None


def get_note(paper_id):
    """获取论文笔记"""
    note_file = NOTES_DIR / f"{paper_id}_notes.md"
    if note_file.exists():
        return note_file.read_text(encoding='utf-8')
    return ""


def save_note(paper_id, content):
    """保存笔记"""
    note_file = NOTES_DIR / f"{paper_id}_notes.md"
    note_file.write_text(content, encoding='utf-8')
    return True


def delete_paper(paper_id):
    """删除论文及相关文件"""
    try:
        deleted_md_files = []
        # 删除 MD 文件
        md_files = list(PAPERS_DIR.glob(f"{paper_id}.md"))
        if not md_files:
            md_files = list(PAPERS_DIR.glob(f"*{paper_id}*.md"))
        for f in md_files:
            f.unlink()
            deleted_md_files.append(f)

        # 删除 PDF（默认目录 + 收藏目录）
        for pdf_dir in (PDFS_DIR, PDFS_FAVORITES_DIR):
            for pdf in pdf_dir.glob(f"*{paper_id}*.pdf"):
                pdf.unlink()

        # 删除笔记
        note_file = NOTES_DIR / f"{paper_id}_notes.md"
        if note_file.exists():
            note_file.unlink()

        # 删除对话记录
        for chat in CHAT_DIR.glob(f"*{paper_id}*"):
            chat.unlink()

        # 从收藏状态移除
        remove_favorite(paper_id)

        return {
            "success": True,
            "deleted_md_files": [str(f) for f in deleted_md_files]
        }
    except Exception as e:
        LOGGER.exception("删除论文失败：%s｜%s", paper_id, str(e))
        return {
            "success": False,
            "deleted_md_files": []
        }


def load_favorites():
    """加载收藏信息"""
    default_data = {"paper_ids": []}
    if not FAVORITES_FILE.exists():
        return default_data
    try:
        data = json.loads(FAVORITES_FILE.read_text(encoding='utf-8'))
        paper_ids = data.get("paper_ids", [])
        normalized = [pid for pid in paper_ids if isinstance(pid, str) and pid.strip()]
        return {"paper_ids": normalized}
    except Exception:
        return default_data


def save_favorites(favorites_data):
    FAVORITES_FILE.write_text(
        json.dumps(favorites_data, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )


def add_favorite(paper_id):
    favorites = load_favorites()
    paper_ids = set(favorites.get("paper_ids", []))
    paper_ids.add(paper_id)
    save_favorites({"paper_ids": sorted(paper_ids)})


def remove_favorite(paper_id):
    favorites = load_favorites()
    paper_ids = [pid for pid in favorites.get("paper_ids", []) if pid != paper_id]
    save_favorites({"paper_ids": paper_ids})


def move_paper_pdf_between_dirs(paper_id, title="", target="favorites"):
    """
    迁移论文 PDF：
    - target='favorites': 从默认目录迁移到收藏目录
    - target='default': 从收藏目录迁移到默认目录
    """
    if target == "favorites":
        source_dir = PDFS_DIR
        destination_dir = PDFS_FAVORITES_DIR
    else:
        source_dir = PDFS_FAVORITES_DIR
        destination_dir = PDFS_DIR

    destination_dir.mkdir(exist_ok=True)
    moved_files = []
    for pdf_file in get_paper_pdf_candidates(paper_id, title):
        if pdf_file.parent.resolve() != source_dir.resolve():
            continue
        target_file = destination_dir / pdf_file.name
        if target_file.exists():
            target_file.unlink()
        pdf_file.rename(target_file)
        moved_files.append(str(target_file))
    return moved_files


def parse_paper_title_from_md(md_file):
    """从 Markdown 文件中解析论文标题"""
    try:
        content = md_file.read_text(encoding='utf-8')
        title_match = content.find("**论文**:")
        if title_match != -1:
            return content[title_match:].split("\n")[0].replace("**论文**:", "").strip()
    except Exception:
        return ""
    return ""


def load_history_file():
    """加载 paper_history.json"""
    history_path = Path(__file__).parent.parent / "paper_history.json"
    if not history_path.exists():
        return history_path, {"papers": [], "dates": []}
    try:
        return history_path, json.loads(history_path.read_text(encoding='utf-8'))
    except Exception:
        return history_path, {"papers": [], "dates": []}


def save_history_file(history_path, history_data):
    """保存 paper_history.json"""
    history_path.write_text(
        json.dumps(history_data, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )


def remove_entries_from_history_by_titles(titles):
    """按标题从历史记录中移除条目（用于直接删除）"""
    normalized_titles = {
        (title or "").strip().lower()
        for title in (titles or [])
        if (title or "").strip()
    }
    if not normalized_titles:
        return 0

    history_path, history_data = load_history_file()
    papers = history_data.get("papers", [])
    before_count = len(papers)
    kept = []
    for item in papers:
        item_title = (item.get("title") or "").strip().lower()
        if item_title in normalized_titles:
            continue
        kept.append(item)

    if len(kept) != before_count:
        history_data["papers"] = kept
        history_data["dates"] = sorted({
            p.get("date")
            for p in kept
            if p.get("date")
        })
        save_history_file(history_path, history_data)
    return before_count - len(kept)


def dedupe_history_all_entries():
    """对 paper_history.json 全量去重（哈希优先，标题兜底）"""
    history_path, history_data = load_history_file()
    papers = history_data.get("papers", [])
    seen_keys = set()
    deduped = []
    removed = 0

    for item in papers:
        hash_key = (item.get("hash") or "").strip().lower()
        title_key = (item.get("title") or "").strip().lower()
        key = f"hash:{hash_key}" if hash_key else f"title:{title_key}"
        if not key or key in seen_keys:
            removed += 1
            continue
        seen_keys.add(key)
        deduped.append(item)

    history_data["papers"] = deduped
    history_data["dates"] = sorted({
        p.get("date")
        for p in deduped
        if p.get("date")
    })
    save_history_file(history_path, history_data)
    return {
        "before": len(papers),
        "after": len(deduped),
        "removed": removed
    }


def search_papers(query):
    """搜索论文"""
    all_papers = get_papers()
    query = query.lower()
    return [p for p in all_papers if query in p["title"].lower() or query in p["content"].lower()]


# ========== AI 解析功能 ==========

def ai_parse_input(topic="", date_str="", count_str=""):
    """使用 AI + 规则解析用户的按需推送输入

    Args:
        topic: 主题（可能是中文）
        date_str: 时间区间（自然语言）
        count_str: 数量描述

    Returns:
        dict: 解析结果，包含 topics, days, count
    """

    def parse_days_rule(text):
        text = (text or "").strip().lower()
        if not text:
            return 7

        def parse_cn_number(raw):
            cn = (raw or "").strip()
            if not cn:
                return None
            digit_map = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
            if cn == "十":
                return 10
            if "十" in cn:
                left, right = cn.split("十", 1)
                tens = digit_map.get(left, 1 if left == "" else None)
                if tens is None:
                    return None
                ones = digit_map.get(right, 0) if right != "" else 0
                return tens * 10 + ones
            if cn in digit_map:
                return digit_map[cn]
            return None

        mappings = {
            "今天": 1,
            "昨日": 2,
            "昨天": 2,
            "最近一天": 1,
            "近一天": 1,
            "最近三天": 3,
            "近三天": 3,
            "最近一周": 7,
            "近一周": 7,
            "一周内": 7,
            "最近两周": 14,
            "近两周": 14,
            "最近半个月": 15,
            "近半个月": 15,
            "最近一个月": 30,
            "近一个月": 30,
            "最近三个月": 90,
            "近三个月": 90,
            "最近半年": 180,
            "近半年": 180,
            "最近一年": 365,
            "近一年": 365,
        }
        if text in mappings:
            return mappings[text]

        digit_match = re.search(r'(\d+)', text)
        if digit_match:
            value = int(digit_match.group(1))
            if any(unit in text for unit in ['天', 'day', 'days']):
                return max(1, value)
            if any(unit in text for unit in ['周', 'week', 'weeks']):
                return max(1, value * 7)
            if any(unit in text for unit in ['月', 'month', 'months']):
                return max(1, value * 30)
            if any(unit in text for unit in ['年', 'year', 'years']):
                return max(1, value * 365)
            # 仅数字时，默认把它当作“天数”
            return max(1, value)

        cn_match = re.search(r'([零一二两三四五六七八九十]+)\s*(天|日|周|月|年)', text)
        if cn_match:
            cn_value = parse_cn_number(cn_match.group(1))
            if cn_value:
                unit = cn_match.group(2)
                if unit in ['天', '日']:
                    return max(1, cn_value)
                if unit == '周':
                    return max(1, cn_value * 7)
                if unit == '月':
                    return max(1, cn_value * 30)
                if unit == '年':
                    return max(1, cn_value * 365)

        return 7

    def parse_count_rule(text):
        text = (text or '').strip()
        if not text:
            return [3, 5]

        range_match = re.search(r'(\d+)\s*[-~到至]\s*(\d+)', text)
        if range_match:
            low = int(range_match.group(1))
            high = int(range_match.group(2))
            low, high = sorted((low, high))
            return [max(1, low), max(1, high)]

        numbers = [int(n) for n in re.findall(r'\d+', text)]
        if len(numbers) >= 2:
            low, high = sorted(numbers[:2])
            return [max(1, low), max(1, high)]
        if len(numbers) == 1:
            value = max(1, numbers[0])
            return [value, value]

        return [3, 5]

    def parse_topic_rule(text):
        text = (text or '').strip()
        if not text:
            return None

        separators = r'[,，、;；\n]+'
        items = [item.strip() for item in re.split(separators, text) if item.strip()]
        return items or None

    rule_result = {
        "topics": expand_topics(parse_topic_rule(topic) or []),
        "days": parse_days_rule(date_str),
        "count": parse_count_rule(count_str)
    }
    def contains_cjk(text):
        return bool(re.search(r'[\u4e00-\u9fff]', text or ''))

    def translate_topics_fallback(topics):
        """当解析结果仍为中文主题时，尝试用 MiniMax 将其翻译/扩展为英文检索短语。"""
        raw_topics = [t.strip() for t in (topics or []) if t and t.strip()]
        if not raw_topics or not any(contains_cjk(t) for t in raw_topics):
            return raw_topics

        try:
            prompt = f"""请把下面这些中文主题翻译为适合学术检索的英文关键词/短语，并补充常见同义表达。

请尽量扩展为 8-20 个检索短语，覆盖：
- 领域主称呼
- 常见同义词
- 常见缩写（如 AI / NLP / LLM 等）
- 近义学术术语

只返回 JSON，格式如下：
{{"topics": ["english topic 1", "english topic 2", "..."]}}

中文主题：
{json.dumps(raw_topics, ensure_ascii=False)}
"""
            headers = {
                "Authorization": f"Bearer {CONFIG.get('miniMax', {}).get('api_key', '')}",
                "Content-Type": "application/json"
            }
            payload = {
                # 账号套餐可能不支持 fallback_model，优先使用主模型
                "model": CONFIG.get("miniMax", {}).get("model", "MiniMax-M2.7"),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 600
            }
            paced_api_wait(1.0)
            response = requests.post(
                f"{CONFIG.get('miniMax', {}).get('base_url', 'https://api.minimax.chat/v1')}/text/chatcompletion_v2",
                headers=headers,
                json=payload,
                timeout=60
            )
            if response.status_code != 200:
                return raw_topics
            data = response.json()
            message = data.get("choices", [{}])[0].get("message", {}) if data.get("choices") else {}
            content = (message.get("content") or "").strip()
            if not content:
                content = (message.get("reasoning_content") or "").strip()
            json_match = re.search(r'\{[\s\S]*\}', content)
            if not json_match:
                return raw_topics
            result = json.loads(json_match.group(0))
            translated = [t.strip() for t in (result.get("topics") or []) if t and t.strip()]
            return translated or raw_topics
        except Exception:
            return raw_topics

    # AI 解析主路径：用于更合理地理解“时间区间/主题/数量”
    # 规则只做兜底，避免解析失败导致默认 7 天。
    cache_key_raw = f"{(topic or '').strip().lower()}|{(date_str or '').strip().lower()}|{(count_str or '').strip().lower()}"
    cache_key = hashlib.sha256(cache_key_raw.encode("utf-8")).hexdigest()[:24]
    with PARSE_CACHE_LOCK:
        cached = PARSE_CACHE.get(cache_key)
    if cached:
        return cached

    try:
        import requests

        prompt = f"""你是一个论文推送助手。用户想要配置论文推送参数，请解析以下输入并返回结构化 JSON 结果。

## 用户输入
- 主题: \"{topic}\" (可能是中文或英文；请将其拆解为适合学术检索的英文同义词、近义短语、标准领域表达，可输出 3-10 个)
- 时间区间: \"{date_str}\" (自然语言描述，如\"最近一周\"、\"近一个月\"等)
- 推送数量: \"{count_str}\" (如\"3-5\"、\"8\"等)

## 排序优先级
1. 第一优先级：主题匹配度
2. 第二优先级：时间新近性
3. 第三优先级：引用/影响力（如果数据源不支持引用，可忽略）

## 要求
1. 如果主题是中文，请翻译成适合学术搜索的英文关键词或短语
2. 请尽量补充该主题的常见英文同义表达、近义术语、标准学科短语，避免漏检
3. 主题仅用于标题、摘要、关键词/分类检索，不做全文检索
4. 时间区间请转换为具体天数 days（必须是正整数）
   - 如果时间区间是“纯数字”（例如“14”或“30”），请直接把它当作 days（天数）
   - 如果是“2周/2 weeks”，请换算为 days=14；“3个月”换算为 days=90（近似即可）
5. 数量请返回最小和最大值 count（必须是长度为2的整数数组，如[3,5]）
6. 只返回 JSON，不要包含解释

## 输出格式
{{
    \"topics\": [\"english topic 1\", \"english topic 2\", \"english topic 3\"],
    \"days\": 7,
    \"count\": [3, 5]
}}
"""

        headers = {
            "Authorization": f"Bearer {CONFIG.get('miniMax', {}).get('api_key', '')}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": CONFIG.get("miniMax", {}).get("model", "MiniMax-M2.7"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 500
        }

        paced_api_wait(1.0)
        response = requests.post(
            f"{CONFIG.get('miniMax', {}).get('base_url', 'https://api.minimax.chat/v1')}/text/chatcompletion_v2",
            headers=headers,
            json=payload,
            timeout=60
        )

        if response.status_code == 200:
            data = response.json()
            message = data.get("choices", [{}])[0].get("message", {}) if data.get("choices") else {}
            content = (message.get("content") or "").strip()
            if not content:
                content = (message.get("reasoning_content") or "").strip()

            import json as json_module
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                result = json_module.loads(json_match.group(0))
                parsed_topics = result.get("topics") or []
                parsed_topics = [t.strip() for t in parsed_topics if isinstance(t, str) and t.strip()]
                # 过滤掉模型偶尔返回的模板占位内容
                if parsed_topics and any(re.search(r'\benglish topic\b', t.lower()) for t in parsed_topics):
                    parsed_topics = []

                translated_topics = translate_topics_fallback(parsed_topics or rule_result["topics"])
                topics = augment_topics_for_search(translated_topics or parsed_topics or rule_result["topics"])
                # 标准化 days/count
                days_raw = result.get("days", rule_result["days"])
                days = rule_result["days"]
                if isinstance(days_raw, (int, float)):
                    days = max(1, int(days_raw))
                else:
                    m = re.search(r"(\d+)", str(days_raw))
                    if m:
                        days = max(1, int(m.group(1)))

                count_raw = result.get("count", rule_result["count"])
                count = rule_result["count"]
                if isinstance(count_raw, (list, tuple)) and len(count_raw) >= 2:
                    try:
                        low = max(1, int(count_raw[0]))
                        high = max(1, int(count_raw[1]))
                        low, high = sorted((low, high))
                        count = [low, high]
                    except Exception:
                        count = rule_result["count"]
                elif isinstance(count_raw, (int, float)):
                    v = max(1, int(count_raw))
                    count = [v, v]

                parsed_out = {
                    "topics": topics,
                    "days": days,
                    "count": count
                }
                with PARSE_CACHE_LOCK:
                    PARSE_CACHE[cache_key] = parsed_out
                return parsed_out

        # AI 解析未能给出结构化结果时：若主题仍为中文，尝试再做一次翻译补全，避免 ArXiv 检索 0 命中
        translated = translate_topics_fallback(rule_result.get("topics") or [])
        if translated:
            return {**rule_result, "topics": augment_topics_for_search(translated)}
        return {**rule_result, "topics": augment_topics_for_search(rule_result.get("topics") or [])}

    except Exception as e:
        LOGGER.exception("AI 解析失败：%s", str(e))
        translated = translate_topics_fallback(rule_result.get("topics") or [])
        if translated:
            out = {**rule_result, "topics": augment_topics_for_search(translated)}
        else:
            out = {**rule_result, "topics": augment_topics_for_search(rule_result.get("topics") or [])}
        with PARSE_CACHE_LOCK:
            PARSE_CACHE[cache_key] = out
        return out


def expand_topics(topics):
    """扩展主题短语，补充常见同义表达，减少漏检"""
    alias_map = {
        "linguistics in ai": [
            "computational linguistics",
            "ai for linguistics",
            "linguistic analysis with ai",
            "language technology"
        ],
        "corpus linguistics": [
            "corpus-based linguistics",
            "corpus analysis",
            "linguistic corpora",
            "corpus study"
        ],
        "ai-assisted language learning": [
            "computer-assisted language learning",
            "intelligent language tutoring",
            "ai in language education",
            "technology-enhanced language learning"
        ],
        "language acquisition": [
            "second language acquisition",
            "first language acquisition",
            "language development",
            "language learning"
        ],
        "computational phonology": [
            "phonological modeling",
            "phonology in nlp",
            "speech phonology",
            "phonological analysis"
        ],
        "semantic analysis": [
            "lexical semantics",
            "semantic parsing",
            "semantic representation",
            "meaning representation"
        ]
    }

    expanded = []
    seen = set()
    for topic in topics or []:
        normalized = (topic or '').strip()
        if not normalized:
            continue

        candidates = [normalized]
        candidates.extend(alias_map.get(normalized.lower(), []))

        for candidate in candidates:
            candidate = candidate.strip()
            key = candidate.lower()
            if candidate and key not in seen:
                seen.add(key)
                expanded.append(candidate)

    return expanded


def augment_topics_for_search(topics, limit=20):
    """将主题扩展为更丰富的检索词集合，提升并行检索召回。"""
    expanded = expand_topics(topics or [])
    if not expanded:
        return []

    enriched = []
    seen = set()
    for topic in expanded:
        normalized = (topic or "").strip()
        if not normalized:
            continue

        variants = [normalized]
        lower = normalized.lower()

        # 兼容 ArXiv/OpenAccess 对缩写和全称的不同命中表现
        if lower == "ai":
            variants.extend(["artificial intelligence", "machine intelligence"])
        if lower == "nlp":
            variants.extend(["natural language processing", "computational linguistics"])
        if lower == "llm":
            variants.extend(["large language model", "large language models", "foundation model"])

        # 对复数形式做兜底，减少因单复数差异导致的漏检
        if len(lower) >= 4 and " " not in lower and not lower.endswith("s"):
            variants.append(f"{normalized}s")

        for variant in variants:
            candidate = variant.strip()
            key = candidate.lower()
            if candidate and key not in seen:
                seen.add(key)
                enriched.append(candidate)
                if len(enriched) >= limit:
                    return enriched

    return enriched


def normalize_push_window(days):
    """标准化推送时间窗口天数，确保严格按自然日边界过滤。"""
    try:
        normalized_days = int(days)
    except (TypeError, ValueError):
        normalized_days = 7
    return max(1, normalized_days)


def parse_paper_datetime(paper):
    """解析论文发布日期，兼容常见日期时间格式。"""
    raw_date = (paper.get('published') or paper.get('updated') or '').strip()
    if not raw_date:
        return None

    # 兼容仅返回年份的情况（如 PubMed 某些记录只给 "2019"）
    if re.fullmatch(r'\d{4}', raw_date):
        try:
            return datetime.strptime(raw_date, '%Y')
        except ValueError:
            return None

    normalized = raw_date.replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except ValueError:
        pass

    for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%Y/%m/%d', '%Y/%m/%d %H:%M:%S'):
        try:
            return datetime.strptime(raw_date[:19], fmt)
        except ValueError:
            continue

    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', raw_date)
    if date_match:
        try:
            return datetime.strptime(date_match.group(1), '%Y-%m-%d')
        except ValueError:
            return None

    return None


def filter_papers_by_time_window(papers, days):
    """严格按时间窗口过滤论文，仅保留窗口内发表/更新的论文。"""
    normalized_days = normalize_push_window(days)
    now = datetime.now()
    window_start = datetime.combine((now - timedelta(days=normalized_days - 1)).date(), datetime.min.time())

    matched_papers = []
    rejected_papers = []
    for paper in papers:
        paper_dt = parse_paper_datetime(paper)
        if paper_dt is None:
            rejected_papers.append({
                'paper': paper,
                'reason': '缺少可解析的发布时间'
            })
            continue
        if paper_dt < window_start or paper_dt > now:
            rejected_papers.append({
                'paper': paper,
                'reason': f"发布时间 {paper_dt.strftime('%Y-%m-%d')} 不在最近 {normalized_days} 天内"
            })
            continue
        matched_papers.append(paper)

    return matched_papers, rejected_papers, window_start


def topic_rule_score(paper, topics=None):
    """按“标题/摘要/分类/检索字段”对主题匹配度打分（数值越高越相关）。"""
    topics = [t.lower().strip() for t in (topics or []) if t and t.strip()]
    if not topics:
        return 0

    haystack = ' '.join([
        paper.get('title', ''),
        paper.get('abstract', ''),
        ' '.join(paper.get('categories', [])),
        paper.get('search_field', '')
    ]).lower()

    if not haystack.strip():
        return 0

    score = 0
    for topic in topics:
        if topic and topic in haystack:
            score += 20
            continue

        topic_words = [w for w in re.split(r'[^a-z0-9]+', topic) if len(w) >= 3]
        matched_words = sum(1 for w in topic_words if w in haystack)
        if topic_words and matched_words:
            score += matched_words * 6
            if matched_words == len(topic_words):
                score += 6

    return score


def topic_matches_paper(paper, topics=None, min_score=18):
    """判断论文是否与主题足够相关，仅检查标题/摘要/关键词（规则层）。"""
    if not topics:
        return True
    return topic_rule_score(paper, topics=topics) >= min_score


# 统一 AI 匹配画像来源（可被 config.filter.profile.ai_match_persona 覆盖）
AI_MATCHER_PERSONA = AI_MATCH_PERSONA


def get_push_runtime_config():
    """读取并标准化 push 配置，避免各处默认值不一致。"""
    push_config = CONFIG.get("push", {}) or {}

    push_time = str(push_config.get("time", "09:00")).strip() or "09:00"
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", push_time):
        push_time = "09:00"

    days = normalize_push_window(push_config.get("days", 7))

    try:
        min_papers = int(push_config.get("min_papers", 3))
    except (TypeError, ValueError):
        min_papers = 3
    try:
        max_papers = int(push_config.get("max_papers", 5))
    except (TypeError, ValueError):
        max_papers = 5

    min_papers = max(1, min_papers)
    max_papers = max(1, max_papers)
    min_papers, max_papers = sorted((min_papers, max_papers))

    topics = push_config.get("topics")
    if isinstance(topics, list):
        topics = [topic.strip() for topic in topics if isinstance(topic, str) and topic.strip()]
    else:
        topics = []

    if not topics:
        topics = [topic for topic in (CONFIG.get("fields", []) or []) if isinstance(topic, str) and topic.strip()][:3]

    return {
        "time": push_time,
        "days": days,
        "min_papers": min_papers,
        "max_papers": max_papers,
        "topics": topics,
        "check_interval_hours": push_config.get("check_interval_hours", 24)
    }


def get_search_priorities():
    """读取并标准化搜索排序优先级。"""
    configured = CONFIG.get("search_priority", []) or []
    allowed = {"relevance", "published_date", "citation_count"}
    priorities = []
    for item in configured:
        key = str(item).strip().lower()
        if key in allowed and key not in priorities:
            priorities.append(key)

    default_priorities = ["relevance", "published_date", "citation_count"]
    for key in default_priorities:
        if key not in priorities:
            priorities.append(key)
    return priorities


def ai_topic_matches_paper(paper, topics=None):
    """使用 AI 根据标题和摘要判断论文是否符合关注领域和主题"""
    topics = [t.strip() for t in (topics or []) if t and t.strip()]
    if not topics:
        return True, {"matched": True, "reason": "未提供主题，默认通过"}

    title = (paper.get('title') or '').strip()
    abstract = (paper.get('abstract') or '').strip()
    if not title and not abstract:
        return False, {"matched": False, "reason": "标题和摘要为空"}

    prompt = f"""请根据下面的人设和筛选目标，判断这篇论文是否应该被保留到推送结果中。

## 人设
{AI_MATCHER_PERSONA}

## 用户关注主题
{json.dumps(topics, ensure_ascii=False)}

## 待判断论文
标题：{title}
摘要：{abstract}

## 判定标准
- 只依据标题和摘要
- 判断两个层面：
  1. 是否符合该用户长期关注的领域画像
  2. 是否符合本次主题检索需求
- 如果论文主要是纯算法/纯模型/纯工程优化/通用 benchmark，但缺乏清晰的语言学/教育/人文社科研究问题落点，倾向于判定为不匹配或低匹配
- 如果论文明确面向语言分析/语料研究/语言教育/学习者建模/SLA/教学支持/CALL/评测框架，或与公平性/可解释性/交互设计/社会影响等主题存在可验证的研究问题关联，即使使用 LLM/AI，也可以判定为匹配

## 输出要求
只返回 JSON，不要输出其他解释。格式如下：
{{
  "matched": true,
  "domain_fit": true,
  "topic_fit": true,
  "confidence": 0.86,
  "reason": "不超过60字的中文理由"
}}
"""

    headers = {
        "Authorization": f"Bearer {CONFIG.get('miniMax', {}).get('api_key', '')}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": CONFIG.get("miniMax", {}).get("model", "MiniMax-M2.7"),
        "messages": [
            {
                "role": "system",
                "content": "你是一个严格的学术论文主题匹配助手，只能输出 JSON。"
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.1,
        # 给模型更充足输出空间，降低 JSON 被截断概率
        "max_tokens": 700
    }

    fallback_matched = topic_matches_paper(paper, topics=topics)
    fallback_result = {"matched": fallback_matched, "reason": "AI 匹配失败，已回退到规则匹配"}

    # AI 结果缓存：避免同一篇论文+主题在短时间内重复计费
    paper_id = (paper.get("doi") or paper.get("arxiv_id") or paper.get("pmid") or paper.get("title") or "").strip().lower()
    topics_key = "|".join(sorted([t.lower().strip() for t in topics if t and t.strip()]))[:500]
    persona_hash = hashlib.sha256(AI_MATCHER_PERSONA.encode("utf-8")).hexdigest()[:12]
    cache_key = hashlib.sha256(f"{paper_id}|{topics_key}|{persona_hash}".encode("utf-8")).hexdigest()[:24]

    with AI_MATCH_CACHE_LOCK:
        cached = AI_MATCH_CACHE.get(cache_key)
    if cached:
        return cached["matched"], cached["match_info"]

    def _cache_and_return(matched_val, match_info_val):
        with AI_MATCH_CACHE_LOCK:
            AI_MATCH_CACHE[cache_key] = {"matched": matched_val, "match_info": match_info_val}
        return matched_val, match_info_val

    def _extract_partial_result(raw_content):
        """尽量从被截断的 JSON 文本中恢复关键判定字段。"""
        text = (raw_content or "").strip()
        if not text:
            return None

        normalized = text
        if normalized.startswith("```"):
            normalized = re.sub(r"^```(?:json)?\s*", "", normalized, flags=re.IGNORECASE)
            normalized = normalized.replace("```", "").strip()

        # 优先尝试完整 JSON 解析
        json_match = re.search(r'\{[\s\S]*\}', normalized)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except Exception:
                pass

        # 如果被截断，尽量抽取核心字段，避免整篇回退规则
        lower_text = normalized.lower()
        has_any_signal = any(k in lower_text for k in ('"matched"', '"domain_fit"', '"topic_fit"', '"confidence"'))
        if not has_any_signal:
            return None

        def _extract_bool(field_name):
            m = re.search(rf'"{field_name}"\s*:\s*(true|false)', lower_text)
            if not m:
                return None
            return m.group(1) == "true"

        def _extract_confidence():
            m = re.search(r'"confidence"\s*:\s*([0-9]*\.?[0-9]+)', lower_text)
            if not m:
                return None
            try:
                return float(m.group(1))
            except ValueError:
                return None

        matched = _extract_bool("matched")
        domain_fit = _extract_bool("domain_fit")
        topic_fit = _extract_bool("topic_fit")
        confidence = _extract_confidence()
        if matched is None and (domain_fit is not None and topic_fit is not None):
            matched = domain_fit and topic_fit
        if matched is None:
            return None

        return {
            "matched": matched,
            "domain_fit": domain_fit if domain_fit is not None else matched,
            "topic_fit": topic_fit if topic_fit is not None else matched,
            "confidence": confidence if confidence is not None else (0.5 if matched else 0.2),
            "reason": "AI 输出疑似被截断，已提取关键字段判定",
            "detail": "partial_json_recovered"
        }

    try:
        paced_api_wait(1.0)
        response = requests.post(
            f"{CONFIG.get('miniMax', {}).get('base_url', 'https://api.minimax.chat/v1')}/text/chatcompletion_v2",
            headers=headers,
            json=payload,
            timeout=60
        )
        response.raise_for_status()
        data = response.json()
        if data.get("base_resp", {}).get("status_code") not in (None, 0):
            return _cache_and_return(fallback_matched, {
                **fallback_result,
                "detail": data.get("base_resp", {}).get("status_msg", "unknown")
            })
        message = data.get("choices", [{}])[0].get("message", {}) if data.get("choices") else {}
        content = (message.get("content") or "").strip()
        if not content:
            content = (message.get("reasoning_content") or "").strip()
        parsed_result = _extract_partial_result(content)
        if parsed_result:
            result = parsed_result
            matched = bool(result.get("matched"))
            return _cache_and_return(matched, result)
        # AI 没有返回可解析 JSON 时，回退到规则匹配，避免整批被过滤成 0
        return _cache_and_return(fallback_matched, {
            **fallback_result,
            "detail": "AI 返回内容不是合法 JSON，已回退到规则匹配"
        })
    except Exception as e:
        LOGGER.exception("AI 主题匹配失败：%s", str(e))
        return _cache_and_return(fallback_matched, fallback_result)


def rank_papers_by_priority(papers, topics=None, priorities=None):
    """按主题匹配度、时间、引用信号排序论文"""
    topics = [t.lower().strip() for t in (topics or []) if t and t.strip()]
    normalized_priorities = []
    for item in (priorities or []):
        key = str(item).strip().lower()
        if key in {"relevance", "published_date", "citation_count"} and key not in normalized_priorities:
            normalized_priorities.append(key)
    if not normalized_priorities:
        normalized_priorities = ["relevance", "published_date", "citation_count"]

    def score_topic(paper):
        # rule_score 优先（规则层粗/精筛的可解释数值）
        base = paper.get("rule_score")
        if base is None:
            base = 0
            if topics:
                haystack = ' '.join([
                    paper.get('title', ''),
                    paper.get('abstract', ''),
                    ' '.join(paper.get('categories', [])),
                    paper.get('journal', ''),
                    paper.get('search_field', ''),
                ]).lower()

                for topic in topics:
                    if topic in haystack:
                        base += 100
                    else:
                        topic_words = [w for w in re.split(r'[^a-z0-9]+', topic) if w]
                        base += sum(8 for w in topic_words if w in haystack)

        # AI 置信度用于增强相关性排序（matched 可能来自规则回退，也仍保留 confidence）
        ai_conf = (paper.get("ai_match") or {}).get("confidence")
        try:
            ai_conf_val = float(ai_conf)
        except Exception:
            ai_conf_val = 0.0
        # 将 confidence 映射为一个温和的额外加分，避免压过规则/时间主信号
        return int(base + ai_conf_val * 25)

    def score_time(paper):
        date_str = paper.get('published') or paper.get('updated') or ''
        try:
            dt = datetime.strptime(date_str[:10], '%Y-%m-%d')
            return dt.toordinal()
        except Exception:
            return 0

    def score_citation_signal(paper):
        signal_text = ' '.join([
            str(paper.get('journal', '')),
            str(paper.get('comment', ''))
        ]).lower()
        score = 0
        if signal_text:
            if 'conference' in signal_text or 'proceedings' in signal_text:
                score += 3
            if 'journal' in signal_text or 'springer' in signal_text or 'ieee' in signal_text or 'acm' in signal_text:
                score += 2
            number_hits = [int(n) for n in re.findall(r'\b(\d{2,4})\b', signal_text)]
            if number_hits:
                score += max(number_hits)
        return score

    ranked = sorted(
        papers,
        key=lambda paper: tuple(
            {
                "relevance": score_topic(paper),
                "published_date": score_time(paper),
                "citation_count": score_citation_signal(paper)
            }[priority]
            for priority in normalized_priorities
        ),
        reverse=True
    )

    # 附带排序解释（用于前端/调试；短文本避免污染文档）
    for p in ranked:
        rule_score = p.get("rule_score")
        ai_conf = (p.get("ai_match") or {}).get("confidence", None)
        try:
            ai_conf = float(ai_conf) if ai_conf is not None else None
        except Exception:
            ai_conf = None
        date_str = p.get("published") or p.get("updated") or ""
        short_date = (date_str or "")[:10]
        parts = []
        if rule_score is not None:
            parts.append(f"规则得分 {int(rule_score)}")
        if ai_conf is not None:
            parts.append(f"AI置信度 {ai_conf:.2f}")
        if short_date:
            parts.append(f"新近 {short_date}")
        p["ranking_explanation"] = "｜".join(parts) if parts else "综合评分"

    return ranked


def do_push(topics=None, days=7, count=(3, 5), job_id=None):
    """执行推送任务

    Args:
        topics: 主题列表（英文），None 表示使用默认
        days: 时间区间（天数）
        count: 推送数量 (min, max) 元组
    """
    from scraper.arxiv import ArxivScraper
    from scraper.open_access import OpenAccessScraper
    from scraper.paper_manager import PaperManager
    from ai.summarizer import MiniMaxSummarizer
    from ai.markdown_generator import MarkdownGenerator

    logs = []
    step_details = {}
    normalized_days = normalize_push_window(days)
    search_total_candidates = None  # UI uses meta.total_candidates (候选总数)

    def log(message, step=None, percent=None, stage_label=None, meta=None):
        timestamp = datetime.now().strftime('%H:%M:%S')
        logs.append({"time": timestamp, "message": message})
        LOGGER.info("[PUSH %s] %s", timestamp, message)
        if step:
            step_details[step] = message
        if job_id:
            append_push_job_log(
                job_id,
                message,
                step=step,
                percent=percent,
                stage_label=stage_label,
                meta=meta
            )

    pdf_dir = PDFS_DIR
    arxiv_scraper = ArxivScraper(pdf_dir)
    oa_scraper = OpenAccessScraper(pdf_dir)
    paper_manager = PaperManager({"miniMax": CONFIG.get("miniMax", {}), "storage": CONFIG.get("storage", {})})
    summarizer = MiniMaxSummarizer(CONFIG)
    markdown_gen = MarkdownGenerator(CONFIG)

    search_priorities = get_search_priorities()
    output_config = CONFIG.get("output", {}) or {}
    save_pdf_enabled = bool(output_config.get("save_pdf", True))

    log(
        "开始初始化推送依赖：ArXiv/OpenAccess 抓取器、论文管理器、摘要器、Markdown 生成器",
        step="parse",
        percent=5,
        stage_label="正在初始化推送任务"
    )

    # 如果没有指定主题：优先使用配置的前 3 个主题（可复现、便于调参），再扩展同义词
    if topics is None:
        configured_topics = [topic for topic in CONFIG.get("fields", []) if topic and topic.strip()]
        selected_topics = configured_topics[:3]
        topics = expand_topics(selected_topics)
        log(
            f"未指定主题，已从配置中随机选择主题并扩展：{', '.join(topics) if topics else '无'}",
            step="parse",
            percent=12,
            stage_label="参数解析完成"
        )
    else:
        topics = expand_topics(topics)
        log(
            f"已解析主题并完成扩展，共 {len(topics)} 个检索短语：{', '.join(topics) if topics else '无'}",
            step="parse",
            percent=12,
            stage_label="参数解析完成"
        )

    min_count, max_count = count
    search_size = max(5, max_count * 4)
    log(
        f"准备执行检索：严格时间范围最近 {normalized_days} 天，目标保留 {min_count}-{max_count} 篇，排序优先级 {search_priorities}，每个主题最多抓取 {search_size} 条 ArXiv 候选",
        step="search",
        percent=20,
        stage_label="正在检索候选论文"
    )

    all_papers = []
    recent_time_pool = []
    time_rejected_entries = []
    window_start = datetime.combine((datetime.now() - timedelta(days=normalized_days - 1)).date(), datetime.min.time())
    for field in topics:
        log(
            f"开始检索主题：{field}",
            step="search",
            percent=24,
            stage_label=f"正在检索主题：{field}",
            meta={
                "total_candidates": len(all_papers),
                "after_time_filter": 0,
                "after_history_filter": 0,
                "matched_count": 0,
                "success_count": 0
            }
        )
        arxiv_papers = arxiv_scraper.search_papers(field, max_results=search_size, sort_by=search_priorities[0])
        for paper in arxiv_papers:
            paper["search_field"] = field
        all_papers.extend(arxiv_papers)
        log(
            f"ArXiv 检索完成：主题 {field} 命中 {len(arxiv_papers)} 篇",
            step="search",
            percent=28,
            stage_label=f"主题 {field} 的 ArXiv 检索已完成",
            meta={
                "total_candidates": len(all_papers),
                "after_time_filter": 0,
                "after_history_filter": 0,
                "matched_count": 0,
                "success_count": 0
            }
        )
        time.sleep(1)

        oa_papers = oa_scraper.search_all_sources(field, max_results_per_source=max(3, max_count), sort_by=search_priorities[0])
        all_papers.extend(oa_papers)
        log(
            f"开放获取源检索完成：主题 {field} 命中 {len(oa_papers)} 篇",
            step="search",
            percent=34,
            stage_label=f"主题 {field} 的开放获取检索已完成",
            meta={
                "total_candidates": len(all_papers),
                "after_time_filter": 0,
                "after_history_filter": 0,
                "matched_count": 0,
                "success_count": 0
            }
        )
        time.sleep(0.5)

        topic_raw_batch = arxiv_papers + oa_papers
        topic_time_filtered, topic_time_rejected, topic_window_start = filter_papers_by_time_window(topic_raw_batch, normalized_days)
        window_start = topic_window_start
        recent_time_pool.extend(topic_time_filtered)
        time_rejected_entries.extend(topic_time_rejected)
        log(
            f"时间过滤完成：主题 {field} 在最近 {normalized_days} 天内保留 {len(topic_time_filtered)} 篇，过滤 {len(topic_time_rejected)} 篇",
            step="search",
            percent=40,
            stage_label=f"主题 {field} 时间过滤已完成",
            meta={
                "total_candidates": len(all_papers),
                "after_time_filter": len(recent_time_pool),
                "after_topic_prefilter": 0,
                "after_history_filter": 0,
                "matched_count": 0,
                "success_count": 0
            }
        )

    # 先进行时间过滤，再在时间窗口内进行主题检索/匹配
    filtered_by_time = recent_time_pool
    time_rejected = [
        {
            "title": entry["paper"].get("title", "未命名论文")[:120],
            "reason": entry["reason"]
        }
        for entry in time_rejected_entries[:20]
    ]
    log(
        f"全量时间过滤汇总完成：窗口起点 {window_start.strftime('%Y-%m-%d %H:%M')}，保留 {len(filtered_by_time)} 篇，过滤 {len(time_rejected_entries)} 篇",
        step="dedupe",
        percent=46,
        stage_label="先按时间过滤完成，准备执行主题检索",
        meta={"total_candidates": len(all_papers)}
    )

    recall_min_score = int(FILTER_THRESHOLDS.get("rule_recall_min_score", 10))
    precision_min_score = int(FILTER_THRESHOLDS.get("rule_precision_min_score", 18))
    ai_min_confidence = float(FILTER_THRESHOLDS.get("ai_min_confidence", 0.6))
    max_ai_candidates = int(FILTER_THRESHOLDS.get("max_ai_candidates", max_count * 10))
    max_ai_calls_per_job = int(FILTER_THRESHOLDS.get("max_ai_calls_per_job", max_ai_candidates))

    suggested_days = sorted(set([max(normalized_days * 2, normalized_days + 7), max(normalized_days * 4, normalized_days + 30)]))

    def paper_key(p):
        return (p.get("doi") or p.get("arxiv_id") or p.get("pmid") or p.get("title", "")).strip().lower()

    # 若时间窗口为空，直接给出建议放宽
    if not filtered_by_time:
        relax_reason = f"最近 {normalized_days} 天内未检索到可用论文。"
        log(
            f"任务提前结束：{relax_reason} 建议放宽时间到 {suggested_days[0]} 天或 {suggested_days[-1]} 天后重试。",
            step="done",
            percent=100,
            stage_label="未命中结果，建议放宽时间",
            meta={
                "total_candidates": len(all_papers),
                "after_time_filter": 0,
                "after_rule_recall": 0,
                "after_history_filter": 0,
                "matched_count": 0,
                "success_count": 0,
            },
        )
        return {
            "results": [],
            "logs": logs,
            "progress": {
                "current_step": "done",
                "stage_label": "未命中结果，建议放宽时间",
                "percent": 100,
                "step_details": step_details,
                "meta": {
                    "total_candidates": len(all_papers),
                    "after_time_filter": 0,
                    "after_topic_prefilter": 0,
                    "after_history_filter": 0,
                    "matched_count": 0,
                    "success_count": 0,
                },
            },
            "meta": {
                "total_candidates": len(all_papers),
                "after_time_filter": 0,
                "after_topic_prefilter": 0,
                "after_history_filter": 0,
                "matched_count": 0,
                "time_rejected_count": len(time_rejected_entries),
                "time_rejected_papers": time_rejected,
                "rejected_count": 0,
                "rejected_papers": [],
                "success_count": 0,
                "failure_count": 0,
                "needs_time_relaxation": True,
                "relax_reason": relax_reason,
                "suggested_days": suggested_days,
            },
        }

    # 规则层：先“粗召回”（低阈值），再用历史幂等做全局去重
    search_total_candidates = len(all_papers)
    rule_recall_pool = []
    for paper in filtered_by_time:
        score = topic_rule_score(paper, topics=topics)
        if score >= recall_min_score:
            paper["rule_score"] = score
            rule_recall_pool.append(paper)

    if not rule_recall_pool:
        relax_reason = f"最近 {normalized_days} 天内有论文，但与当前主题相关的规则召回结果为 0。"
        log(
            f"任务提前结束：{relax_reason} 建议放宽时间到 {suggested_days[0]} 天或 {suggested_days[-1]} 天后重试。",
            step="done",
            percent=100,
            stage_label="规则召回为空，建议放宽时间",
            meta={
                "total_candidates": len(all_papers),
                "after_time_filter": len(filtered_by_time),
                "after_rule_recall": 0,
                "after_history_filter": 0,
                "matched_count": 0,
                "success_count": 0,
            },
        )
        return {
            "results": [],
            "logs": logs,
            "progress": {
                "current_step": "done",
                "stage_label": "规则召回为空，建议放宽时间",
                "percent": 100,
                "step_details": step_details,
                "meta": {
                    "total_candidates": len(all_papers),
                    "after_time_filter": len(filtered_by_time),
                    "after_rule_recall": 0,
                    "after_history_filter": 0,
                    "matched_count": 0,
                    "success_count": 0,
                },
            },
            "meta": {
                "total_candidates": len(all_papers),
                "after_time_filter": len(filtered_by_time),
                "after_rule_recall": 0,
                "after_history_filter": 0,
                "matched_count": 0,
                "time_rejected_count": len(time_rejected_entries),
                "time_rejected_papers": time_rejected,
                "rejected_count": 0,
                "rejected_papers": [],
                "success_count": 0,
                "failure_count": 0,
                "needs_time_relaxation": True,
                "relax_reason": relax_reason,
                "suggested_days": suggested_days,
            },
        }

    # 主题预过滤：移除“截断规模”逻辑，让候选尽可能完整进入后续过滤
    log(
        f"规则粗召回完成：阈值 {recall_min_score}，保留 {len(rule_recall_pool)} 篇（不做主题预过滤截断）",
        step="dedupe",
        percent=50,
        stage_label="规则粗召回",
        meta={
            "total_candidates": len(all_papers),
            "after_time_filter": len(filtered_by_time),
            "after_rule_recall": len(rule_recall_pool),
        },
    )

    # 在规则粗召回的结果上去重
    seen = set()
    unique_papers = []
    for paper in rule_recall_pool:
        k = paper_key(paper)
        if k and k not in seen:
            seen.add(k)
            unique_papers.append(paper)

    topic_prefilter_count = len(unique_papers)
    total_candidates = search_total_candidates
    log(
        f"粗召回去重完成：去重前 {len(rule_recall_pool)} 篇，去重后 {topic_prefilter_count} 篇",
        step="dedupe",
        percent=54,
        stage_label="规则粗召回去重",
    )

    # 过滤历史（全局幂等去重：避免 days 窗口过期后重复推送）
    filtered = paper_manager.filter_new_papers(unique_papers, days=None)
    history_filtered_count = len(filtered)
    log(
        f"历史过滤完成：本窗口未处理的新论文共 {history_filtered_count} 篇",
        step="dedupe",
        percent=58,
        stage_label="历史过滤",
        meta={
            "total_candidates": total_candidates,
            "after_time_filter": len(filtered_by_time),
            "after_topic_prefilter": topic_prefilter_count,
            "after_rule_recall": len(rule_recall_pool),
            "after_history_filter": history_filtered_count,
        },
    )

    # 规则精筛：只把“可能匹配得更好”的候选送入 AI（节省成本，且保持精准）
    precision_pool = [p for p in filtered if p.get("rule_score", 0) >= precision_min_score]
    if not precision_pool:
        # 去掉候选数量上限：让“没命中 precision_min_score”的情况下也能尽可能完整进入 AI
        precision_pool = sorted(filtered, key=lambda p: p.get("rule_score", 0), reverse=True)

    precision_pool = sorted(precision_pool, key=lambda p: p.get("rule_score", 0), reverse=True)
    # 去掉 AI 候选上限：让 AI 评估覆盖全部候选
    ai_candidates = precision_pool

    ai_filtered = []
    ai_rejected = []
    ai_calls = 0
    ai_accepted = 0
    evaluated_keys = set()
    relax_attempts = []

    log(
        f"开始 AI 主题匹配：候选 {len(ai_candidates)} 篇（confidence阈值 {ai_min_confidence}，无候选/调用上限）",
        step="match",
        percent=56,
        stage_label="AI 精筛",
        meta={
            "total_candidates": total_candidates,
            "after_rule_recall": len(rule_recall_pool),
            "after_history_filter": history_filtered_count,
            "ai_candidates": len(ai_candidates),
        },
    )

    def evaluate_ai_candidates(candidate_list, confidence_floor, accepted_list, stage_prefix):
        """统一执行 AI 主题匹配并按 confidence 阈值接纳/拒绝（含日志）。"""
        nonlocal ai_calls, ai_accepted, ai_rejected
        for index, paper in enumerate(candidate_list, start=1):
            k = paper_key(paper)
            if not k or k in evaluated_keys:
                continue
            evaluated_keys.add(k)

            matched, match_info = ai_topic_matches_paper(paper, topics=topics)
            ai_calls += 1
            paper["ai_match"] = match_info

            paper_title = paper.get("title", "未命名论文")[:80]
            confidence = float(match_info.get("confidence", 0) or 0)

            if matched and confidence >= confidence_floor:
                accepted_list.append(paper)
                ai_accepted += 1
                detail_text = match_info.get("detail", "")
                reason_text = match_info.get("reason", "无")
                if detail_text:
                    reason_text = f"{reason_text}｜{detail_text}"
                log(
                    f"{stage_prefix} 接纳 [{index}/{len(candidate_list)}]：{paper_title}｜conf={confidence:.2f}｜理由：{reason_text}",
                    step="match",
                    percent=60,
                    stage_label=f"{stage_prefix} {index}/{len(candidate_list)}",
                    meta={"matched_count": ai_accepted},
                )
            else:
                ai_rejected.append({"title": paper.get("title", "未命名论文")[:120], "ai_match": match_info})
                detail_text = match_info.get("detail", "")
                reason_text = match_info.get("reason", "无")
                if detail_text:
                    reason_text = f"{reason_text}｜{detail_text}"
                log(
                    f"{stage_prefix} 拒绝 [{index}/{len(candidate_list)}]：{paper_title}｜conf={confidence:.2f}｜理由：{reason_text}",
                    step="match",
                    percent=60,
                    stage_label=f"{stage_prefix} {index}/{len(candidate_list)}",
                    meta={"matched_count": ai_accepted},
                )

    evaluate_ai_candidates(
        ai_candidates,
        ai_min_confidence,
        ai_filtered,
        stage_prefix="AI精筛",
    )

    # 标准化补救策略：仍不足则放宽精筛/时间窗口并继续评估
    filtered = ai_filtered
    if len(filtered) < min_count:
        log(
            f"结果不足：AI 接纳 {len(filtered)} 篇（目标至少 {min_count}），启动补救策略",
            step="match",
            percent=66,
            stage_label="结果不足，补救召回",
        )

        # 尝试 1：放宽精筛阈值并继续评估剩余候选（不额外抓取，成本最低）
        relaxed_precision = max(recall_min_score, precision_min_score - 4)
        relaxed_pool = [p for p in filtered_by_time if p.get("rule_score", 0) >= relaxed_precision]
        relaxed_pool = paper_manager.filter_new_papers(
            [p for p in relaxed_pool if paper_key(p) not in evaluated_keys],
            days=None
        )
        relaxed_pool = sorted(relaxed_pool, key=lambda p: p.get("rule_score", 0), reverse=True)
        relaxed_ai_candidates = relaxed_pool
        if relaxed_ai_candidates:
            relax_attempts.append({"level": 1, "type": "relax_precision", "relaxed_precision": relaxed_precision, "candidates": len(relaxed_ai_candidates)})
            evaluate_ai_candidates(
                relaxed_ai_candidates,
                max(0.4, ai_min_confidence - 0.15),
                filtered,
                stage_prefix="AI补救-精筛"
            )

        # 尝试 2：放宽时间窗口 2x（用已抓取 all_papers 重新筛选，避免立刻额外抓取）
        if len(filtered) < min_count:
            relaxed_days = int(min(normalized_days * 2, max(normalized_days * 4, normalized_days + 30)))
            relaxed_time_pool, _, _ = filter_papers_by_time_window(all_papers, relaxed_days)
            for p in relaxed_time_pool:
                p["rule_score"] = topic_rule_score(p, topics=topics)
            relaxed_time_pool = [p for p in relaxed_time_pool if p.get("rule_score", 0) >= recall_min_score]
            # 历史幂等过滤 + 去重
            unique_relaxed = []
            seen_relaxed = set()
            for p in relaxed_time_pool:
                k = paper_key(p)
                if k and k not in seen_relaxed:
                    seen_relaxed.add(k)
                    unique_relaxed.append(p)
            relaxed_history_filtered = paper_manager.filter_new_papers(unique_relaxed, days=None)
            precision_pool2 = [p for p in relaxed_history_filtered if p.get("rule_score", 0) >= precision_min_score]
            if not precision_pool2:
                precision_pool2 = sorted(relaxed_history_filtered, key=lambda p: p.get("rule_score", 0), reverse=True)
            ai_candidates2 = sorted(precision_pool2, key=lambda p: p.get("rule_score", 0), reverse=True)
            if ai_candidates2:
                relax_attempts.append({"level": 2, "type": "relax_time", "relaxed_days": relaxed_days, "ai_candidates": len(ai_candidates2)})
                evaluate_ai_candidates(
                    ai_candidates2,
                    max(0.4, ai_min_confidence - 0.15),
                    filtered,
                    stage_prefix="AI补救-时间窗"
                )

    # 同批次内再次去重（基于稳定标识 paper_key，而不是仅标题）
    seen_keys = set()
    batch_filtered = []
    for paper in filtered:
        k = paper_key(paper) or (paper.get("title", "") or "").strip().lower()
        if k and k not in seen_keys:
            seen_keys.add(k)
            batch_filtered.append(paper)
    filtered = batch_filtered

    filtered = rank_papers_by_priority(filtered, topics=topics, priorities=search_priorities)

    log(
        f"排序完成：AI 接纳后保留 {len(filtered)} 篇，准备处理前 {min(len(filtered), max_count)} 篇",
        step="rank",
        percent=72,
        stage_label="排序完成，准备进入论文处理阶段",
        meta={
            "total_candidates": total_candidates,
            "after_time_filter": len(filtered_by_time),
            "after_history_filter": history_filtered_count,
            "ai_calls": ai_calls,
            "ai_accepted": ai_accepted,
            "matched_count": len(filtered),
            "relax_attempts": relax_attempts,
        },
    )

    results = []
    success_count = 0
    target_batch = filtered[:max_count]
    log(
        f"进入论文处理阶段：本轮实际处理 {len(target_batch)} 篇",
        step="process",
        percent=80,
        stage_label="正在下载 PDF、生成摘要与保存结果",
        meta={
            "total_candidates": total_candidates,
            "after_time_filter": len(filtered_by_time),
            "after_history_filter": history_filtered_count,
            "matched_count": len(filtered),
            "success_count": success_count
        }
    )
    for index, paper in enumerate(target_batch, start=1):
        title = paper.get("title", '')[:50] or "未命名论文"
        try:
            log(
                f"开始处理第 {index}/{len(target_batch)} 篇：{title}",
                step="process",
                percent=84,
                stage_label=f"正在处理第 {index}/{len(target_batch)} 篇论文",
                meta={
                    "total_candidates": total_candidates,
                    "after_time_filter": len(filtered_by_time),
                    "after_history_filter": history_filtered_count,
                    "matched_count": len(filtered),
                    "success_count": success_count
                }
            )
            # 下载 PDF（可由 output.save_pdf 关闭）
            pdf_path = None
            if save_pdf_enabled and paper.get("source") == "ArXiv":
                pdf_path = arxiv_scraper.download_pdf(paper)
            else:
                if save_pdf_enabled:
                    pdf_path = oa_scraper.download_pdf(paper)
                    if not pdf_path and paper.get("pdf_url"):
                        pdf_path = arxiv_scraper.download_pdf(paper)
            log(
                f"PDF 阶段完成：{title}｜{'已获取 PDF' if pdf_path else ('已禁用 PDF 下载' if not save_pdf_enabled else '未获取到 PDF，继续后续处理')}",
                step="process",
                percent=88,
                stage_label=f"第 {index}/{len(target_batch)} 篇 PDF 阶段完成",
                meta={
                    "total_candidates": total_candidates,
                    "after_time_filter": len(filtered_by_time),
                    "after_history_filter": history_filtered_count,
                    "matched_count": len(filtered),
                    "success_count": success_count
                }
            )

            # 生成摘要
            summary = summarizer.generate_summary(paper)
            log(
                f"摘要生成完成：{title}",
                step="process",
                percent=91,
                stage_label=f"第 {index}/{len(target_batch)} 篇摘要生成完成",
                meta={
                    "total_candidates": total_candidates,
                    "after_time_filter": len(filtered_by_time),
                    "after_history_filter": history_filtered_count,
                    "matched_count": len(filtered),
                    "success_count": success_count
                }
            )

            # 生成 Markdown
            md = markdown_gen.generate(paper, summary, pdf_path)
            log(
                f"Markdown 生成完成：{title}",
                step="process",
                percent=94,
                stage_label=f"第 {index}/{len(target_batch)} 篇 Markdown 生成完成",
                meta={
                    "total_candidates": total_candidates,
                    "after_time_filter": len(filtered_by_time),
                    "after_history_filter": history_filtered_count,
                    "matched_count": len(filtered),
                    "success_count": success_count
                }
            )

            # 保存
            markdown_path = paper_manager.save_paper(paper, md)
            paper_manager.add_to_history(paper)
            success_count += 1
            log(
                f"保存完成：{title} -> {Path(markdown_path).name}",
                step="process",
                percent=96,
                stage_label=f"第 {index}/{len(target_batch)} 篇已保存",
                meta={
                    "total_candidates": total_candidates,
                    "after_time_filter": len(filtered_by_time),
                    "after_history_filter": history_filtered_count,
                    "matched_count": len(filtered),
                    "success_count": success_count
                }
            )

            results.append({
                "success": True,
                "title": title,
                "source": paper.get("source", ""),
                "paper_id": Path(markdown_path).stem,
                "has_pdf": bool(pdf_path),
                "pdf_pending_url": paper.get("pdf_pending_url", ""),
                "ai_match": paper.get("ai_match", {})
            })
        except Exception as e:
            error_text = str(e)
            friendly_error = "论文处理阶段失败"
            if "topic match" in error_text.lower() or "主题匹配" in error_text:
                friendly_error = "主题匹配阶段失败"
            log(
                f"处理失败：{title}｜{friendly_error}｜原始错误：{error_text}",
                step="process",
                percent=96,
                stage_label=f"第 {index}/{len(target_batch)} 篇处理失败",
                meta={
                    "total_candidates": total_candidates,
                    "after_time_filter": len(filtered_by_time),
                    "after_history_filter": history_filtered_count,
                    "matched_count": len(filtered),
                    "success_count": success_count
                }
            )

            results.append({
                "success": False,
                "title": title,
                "source": paper.get("source", ""),
                "error": friendly_error,
                "ai_match": paper.get("ai_match", {})
            })

    log(
        f"推送执行完成：成功 {success_count} 篇，失败 {len(results) - success_count} 篇，AI 过滤 {len(ai_rejected)} 篇",
        step="done",
        percent=100,
        stage_label="所有步骤已完成",
        meta={
            "total_candidates": total_candidates,
            "after_time_filter": len(filtered_by_time),
            "after_history_filter": history_filtered_count,
            "matched_count": len(filtered),
            "success_count": success_count
        }
    )

    return {
        "results": results,
        "logs": logs,
        "progress": {
            "current_step": "done",
            "stage_label": "所有步骤已完成",
            "percent": 100,
            "step_details": step_details,
            "meta": {
                "total_candidates": total_candidates,
                "after_time_filter": len(filtered_by_time),
                "after_topic_prefilter": topic_prefilter_count,
                "after_history_filter": history_filtered_count,
                "matched_count": len(filtered),
                "success_count": success_count
            }
        },
        "meta": {
            "total_candidates": total_candidates,
            "after_time_filter": len(filtered_by_time),
            "after_topic_prefilter": topic_prefilter_count,
            "after_history_filter": history_filtered_count,
            "matched_count": len(filtered),
            "after_rule_recall": len(rule_recall_pool),
            "after_rule_precision": len(precision_pool),
            "ai_candidates": len(ai_candidates),
            "ai_calls": ai_calls,
            "ai_accepted": ai_accepted,
            "relax_attempts": relax_attempts,
            "filter_thresholds": {
                "rule_recall_min_score": recall_min_score,
                "rule_precision_min_score": precision_min_score,
                "ai_min_confidence": ai_min_confidence,
                "max_ai_candidates": max_ai_candidates,
                "max_ai_calls_per_job": max_ai_calls_per_job,
            },
            "time_rejected_count": len(time_rejected_entries),
            "time_rejected_papers": time_rejected,
            "rejected_count": len(ai_rejected),
            "rejected_papers": ai_rejected[:20],
            "success_count": success_count,
            "failure_count": len(results) - success_count
        }
    }


def run_push_job(job_id, topic, date_str, count_str):
    try:
        # 同进程互斥：避免定时任务与按需任务同时跑导致历史/文件写入竞态
        if not PUSH_EXECUTION_LOCK.acquire(timeout=2):
            append_push_job_log(job_id, "当前已有推送任务在执行，已排队等待资源释放", step="parse", percent=2, stage_label="排队中")
            PUSH_EXECUTION_LOCK.acquire()
        try:
            update_push_job(job_id, status="running")
            append_push_job_log(job_id, "后台任务已启动，开始解析输入参数", step="parse", percent=3, stage_label="后台任务启动")
            parsed = ai_parse_input(topic, date_str, count_str)
            topics = parsed.get("topics")
            days = normalize_push_window(parsed.get("days", 7))
            count = parsed.get("count", [3, 5])
            if isinstance(count, (list, tuple)) and len(count) >= 2:
                min_count, max_count = sorted((max(1, int(count[0])), max(1, int(count[1]))))
                if min_count == max_count:
                    selected_count = min_count
                else:
                    selected_count = random.randint(min_count, max_count)
                count = [selected_count, selected_count]
            else:
                selected_count = 3
                count = [selected_count, selected_count]

            append_push_job_log(
                job_id,
                f"参数解析结果：主题 {', '.join(topics or CONFIG.get('fields', [])[:3])}｜近 {days} 天｜目标 {count[0]} 篇",
                step="parse",
                percent=10,
                stage_label="参数解析完成"
            )

            push_result = do_push(topics=topics, days=days, count=tuple(count), job_id=job_id)
            update_push_job(
                job_id,
                status="completed",
                params={
                    "topics": topics or CONFIG.get("fields", [])[:3],
                    "days": days,
                    "count": count
                },
                results=push_result.get("results", []),
                meta=push_result.get("meta", {}),
                logs=push_result.get("logs", []),
                progress=push_result.get("progress", {}),
                error=""
            )
        finally:
            try:
                PUSH_EXECUTION_LOCK.release()
            except Exception:
                pass
    except Exception as e:
        error_text = str(e)
        friendly_error = "推送失败，请稍后重试"
        if "topic match" in error_text.lower() or "主题匹配" in error_text:
            friendly_error = "主题匹配阶段失败"
        append_push_job_log(
            job_id,
            f"后台任务失败：{friendly_error}｜原始错误：{error_text}",
            step="done",
            percent=100,
            stage_label="执行中断"
        )
        job = get_push_job(job_id) or {}
        update_push_job(
            job_id,
            status="failed",
            error=friendly_error,
            progress={
                **(job.get("progress") or {}),
                "current_step": "done",
                "stage_label": "执行中断",
                "percent": 100,
                "meta": {
                    **((job.get("progress") or {}).get("meta") or {}),
                    "success_count": ((job.get("progress") or {}).get("meta") or {}).get("success_count", 0)
                }
            }
        )


def daily_push_job():
    """每日定时推送任务"""
    try:
        if not PUSH_EXECUTION_LOCK.acquire(timeout=1):
            LOGGER.info("已有推送任务执行中，跳过本次定时推送")
            return
        try:
            LOGGER.info("执行每日定时推送")
            runtime = get_push_runtime_config()
            topics = runtime["topics"]
            days = runtime["days"]
            count = (runtime["min_papers"], runtime["max_papers"])
            do_push(topics=topics, days=days, count=count, job_id=None)
        finally:
            try:
                PUSH_EXECUTION_LOCK.release()
            except Exception:
                pass
    except Exception as e:
        LOGGER.exception("每日定时推送出错：%s", str(e))


# 启动定时任务
def start_scheduler():
    """启动定时调度器"""
    runtime = get_push_runtime_config()
    push_time = runtime["time"]
    try:
        check_interval_hours = int(runtime.get("check_interval_hours", 24))
    except (TypeError, ValueError):
        check_interval_hours = 24
    check_interval_hours = max(1, check_interval_hours)

    schedule.every().day.at(push_time).do(daily_push_job)
    schedule.every(check_interval_hours).hours.do(daily_push_job)

    print(f"⏰ 定时推送已设置: 每天 {push_time} + 每 {check_interval_hours} 小时检查一次")
    LOGGER.info("定时推送已设置：每天 %s；检查间隔 %s 小时", push_time, check_interval_hours)

    while True:
        schedule.run_pending()
        time.sleep(60)


# 在后台线程中启动定时器
scheduler_thread = threading.Thread(target=start_scheduler, daemon=True)
scheduler_thread.start()


# ========== 路由 ==========

@app.route('/')
def index():
    """主页"""
    papers = get_papers()
    push_config = CONFIG.get("push", {})
    return render_template('index.html', papers=papers, config=CONFIG, push_config=push_config)


@app.route('/api/papers')
def api_papers():
    """获取论文列表 API"""
    papers = get_papers()
    scope = (request.args.get('scope') or 'all').strip().lower()
    if scope == 'favorites':
        papers = [paper for paper in papers if paper.get("is_favorite")]
    return jsonify(papers)


@app.route('/api/paper/<paper_id>')
def api_paper(paper_id):
    """获取论文详情"""
    paper = get_paper(paper_id)
    if paper:
        return jsonify(paper)
    return jsonify({"error": "Paper not found"}), 404


@app.route('/api/paper/<paper_id>/note', methods=['GET', 'POST'])
def api_paper_note(paper_id):
    """获取/保存笔记"""
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        content = data.get('content', '')
        save_note(paper_id, content)
        return jsonify({"success": True})
    else:
        note = get_note(paper_id)
        return jsonify({"note": note})


@app.route('/api/paper/<paper_id>/chat', methods=['DELETE'])
def api_clear_chat(paper_id):
    """清空指定论文的对话历史"""
    chat_file = CHAT_DIR / f"{paper_id}_chat.json"
    if chat_file.exists():
        chat_file.unlink()
    return jsonify({"success": True})


@app.route('/api/paper/<paper_id>/delete', methods=['POST'])
def api_delete_paper(paper_id):
    """删除论文（默认：只删文件，保留历史）"""
    mode = (request.args.get('mode') or '').strip().lower()
    # keep_history: 删除文件但不删除 paper_history.json 中对应记录
    # purge: 删除文件，并从 paper_history.json 删除对应记录
    delete_mode = mode if mode in {"keep_history", "purge"} else "keep_history"

    md_candidates = list(PAPERS_DIR.glob(f"{paper_id}.md"))
    if not md_candidates:
        md_candidates = list(PAPERS_DIR.glob(f"*{paper_id}*.md"))
    titles = [parse_paper_title_from_md(md_file) for md_file in md_candidates]

    delete_result = delete_paper(paper_id)
    if not delete_result.get("success"):
        return jsonify({"success": False, "error": "删除失败"}), 500

    removed_history_count = 0
    if delete_mode == "purge":
        removed_history_count = remove_entries_from_history_by_titles(titles)
        dedupe_history_all_entries()

    return jsonify({
        "success": True,
        "mode": delete_mode,
        "removed_history_count": removed_history_count
    })


@app.route('/api/paper/<paper_id>/favorite', methods=['POST'])
def api_favorite_paper(paper_id):
    """收藏论文并迁移 PDF 到收藏目录"""
    paper = get_paper(paper_id)
    if not paper:
        return jsonify({"success": False, "error": "论文不存在"}), 404

    add_favorite(paper_id)
    moved_files = move_paper_pdf_between_dirs(
        paper_id=paper_id,
        title=paper.get("title", ""),
        target="favorites"
    )
    return jsonify({
        "success": True,
        "paper_id": paper_id,
        "is_favorite": True,
        "moved_pdf_count": len(moved_files)
    })


@app.route('/api/paper/<paper_id>/unfavorite', methods=['POST'])
def api_unfavorite_paper(paper_id):
    """取消收藏并迁移 PDF 回默认目录"""
    paper = get_paper(paper_id)
    if not paper:
        return jsonify({"success": False, "error": "论文不存在"}), 404

    remove_favorite(paper_id)
    moved_files = move_paper_pdf_between_dirs(
        paper_id=paper_id,
        title=paper.get("title", ""),
        target="default"
    )
    return jsonify({
        "success": True,
        "paper_id": paper_id,
        "is_favorite": False,
        "moved_pdf_count": len(moved_files)
    })


@app.route('/api/history/dedupe', methods=['POST'])
def api_dedupe_history():
    """对历史记录执行全量匹配去重"""
    result = dedupe_history_all_entries()
    return jsonify({
        "success": True,
        **result
    })


@app.route('/api/search')
def api_search():
    """搜索论文"""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify(get_papers())
    papers = search_papers(query)
    return jsonify(papers)


@app.route('/api/push', methods=['POST'])
def api_push():
    """按需推送 - 异步启动实时任务"""
    try:
        data = request.json or {}
        topic = data.get('topic', '')
        date_str = data.get('date', '最近一周')
        count_str = data.get('count', '3-5')
        job_id = create_push_job({
            "topic": topic,
            "date": date_str,
            "count": count_str
        })
        update_push_job(job_id, status="running")

        worker = threading.Thread(
            target=run_push_job,
            args=(job_id, topic, date_str, count_str),
            daemon=True
        )
        worker.start()

        return jsonify({
            "success": True,
            "job_id": job_id,
            "status": "running"
        })

    except Exception as e:
        LOGGER.exception("推送接口失败：%s", str(e))
        error_text = str(e)
        friendly_error = "推送失败，请稍后重试"
        if "topic match" in error_text.lower() or "主题匹配" in error_text:
            friendly_error = "主题匹配阶段失败"
        return jsonify({
            "success": False,
            "error": friendly_error,
            "progress": {
                "current_step": "done",
                "stage_label": "执行中断",
                "percent": 100,
                "step_details": {"done": f"接口异常：{friendly_error}"},
                "meta": {
                    "total_candidates": 0,
                    "after_history_filter": 0,
                    "matched_count": 0,
                    "success_count": 0
                }
            },
            "logs": [{
                "time": datetime.now().strftime('%H:%M:%S'),
                "message": f"按需推送失败：{friendly_error}｜原始错误：{error_text}"
            }]
        }), 500


@app.route('/api/push/<job_id>')
def api_push_status(job_id):
    """获取按需推送任务实时状态"""
    job = get_push_job(job_id)
    if not job:
        return jsonify({"success": False, "error": "任务不存在"}), 404

    response_payload = {
        "success": job.get("status") != "failed",
        "job_id": job_id,
        "status": job.get("status"),
        "params": job.get("params", {}),
        "results": job.get("results", []),
        "meta": job.get("meta", {}),
        "progress": job.get("progress", {}),
        "logs": job.get("logs", []),
        "error": job.get("error", "")
    }
    return jsonify(response_payload)


@app.route('/api/daily-push-status')
def api_daily_push_status():
    """获取定时推送状态"""
    runtime = get_push_runtime_config()
    return jsonify({
        "enabled": True,
        "time": runtime["time"],
        "check_interval_hours": runtime["check_interval_hours"],
        "days": runtime["days"],
        "min_papers": runtime["min_papers"],
        "max_papers": runtime["max_papers"],
        "topics": runtime["topics"]
    })


@app.route('/api/chat', methods=['POST'])
def api_chat():
    """AI 对话"""
    try:
        import requests

        data = request.get_json(silent=True) or {}
        paper_id = data.get('paper_id', '')
        message = data.get('message', '')
        paper = get_paper(paper_id)

        if not paper:
            return jsonify({"error": "Paper not found"}), 404

        # 构建提示
        prompt = f"""你正在帮助用户阅读学术论文。

## 当前论文
标题: {paper['title']}

## 论文内容
{paper['content'][:5000]}

## 用户问题
{message}

## 要求
1. 基于论文内容回答问题
2. 用中文回答，保持学术严谨性
3. 回答要详细、有条理

请开始回答："""

        # 调用 MiniMax API（带指数退避和模型降级）
        answer = call_minimax_with_fallback(prompt)

        if answer:
            # 保存对话历史
            chat_file = CHAT_DIR / f"{paper_id}_chat.json"
            chat_history = []
            if chat_file.exists():
                chat_history = json.loads(chat_file.read_text(encoding='utf-8'))
            chat_history.append({"role": "user", "content": message})
            chat_history.append({"role": "assistant", "content": answer})
            chat_file.write_text(json.dumps(chat_history, ensure_ascii=False, indent=2), encoding='utf-8')

            summarize_result = maybe_summarize_chat_to_note(paper_id, chat_history)

            return jsonify({"success": True, "answer": answer, "chat_summary": summarize_result})
        else:
            return jsonify({"error": "AI 服务暂时不可用，请稍后重试"}), 500

    except Exception as e:
        LOGGER.exception("AI 对话异常：%s", str(e))
        return jsonify({"error": str(e)}), 500


def call_minimax_with_fallback(prompt, temperature=0.7, max_tokens=2000):
    """调用 MiniMax API，带指数退避和模型降级"""
    import requests
    import time

    base_url = CONFIG.get('miniMax', {}).get('base_url', 'https://api.minimax.chat/v1')
    api_key = CONFIG.get('miniMax', {}).get('api_key', '')
    primary_model = CONFIG.get('miniMax', {}).get('model', 'MiniMax-M2.7')
    fallback_model = CONFIG.get('miniMax', {}).get('fallback_model', 'MiniMax-Text-01')

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    # 先尝试主模型
    models_to_try = [primary_model, fallback_model]
    last_error = None

    for model in models_to_try:
        max_retries = 5
        for attempt in range(max_retries):
            try:
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": max_tokens
                }

                paced_api_wait(1.0)
                response = requests.post(
                    f"{base_url}/text/chatcompletion_v2",
                    headers=headers,
                    json=payload,
                    timeout=120
                )

                if response.status_code == 200:
                    result_data = response.json()
                    # 检查 API 内部错误码（类似 summarizer.py 的处理）
                    if result_data.get("base_resp", {}).get("status_code") != 0:
                        status_msg = result_data.get("base_resp", {}).get("status_msg", "unknown")
                        LOGGER.warning("[API] 模型 %s 返回内部错误: %s", model, status_msg)
                        last_error = f"API error: {status_msg}"
                        # 内部错误不重试，直接降级
                        break

                    message = result_data.get("choices", [{}])[0].get("message", {}) if result_data.get("choices") else {}
                    answer = (message.get("content") or "").strip()
                    if not answer:
                        answer = (message.get("reasoning_content") or "").strip()
                    if answer:
                        return answer
                    last_error = "Empty response from API"
                    break

                last_error = f"HTTP {response.status_code}"
                if response.status_code == 529:
                    # 指数退避：2, 4, 8, 16, 32 秒
                    wait_time = (2 ** attempt) * 2
                    LOGGER.warning("[API] 模型 %s 返回 529，%s 秒后重试 (attempt %s/%s)", model, wait_time, attempt + 1, max_retries)
                    time.sleep(wait_time)
                    continue
                else:
                    # 其他 HTTP 错误不重试
                    break

            except requests.exceptions.Timeout:
                last_error = "Request timeout"
                LOGGER.warning("[API] 模型 %s 请求超时，%s 秒后重试", model, (2 ** attempt) * 2)
                time.sleep((2 ** attempt) * 2)
                continue
            except Exception as e:
                last_error = str(e)
                LOGGER.exception("[API] 模型 %s 请求异常: %s", model, str(e))
                time.sleep((2 ** attempt) * 2)
                continue

        # 如果成功获取回答，函数会在循环内返回
        # 如果是 529 错误导致重试完，说明主模型有问题，继续降级
        LOGGER.warning("[API] 模型 %s 尝试完毕，最后错误: %s，尝试降级...", model, last_error)

    # 所有模型都失败
    LOGGER.error("[API] 所有模型均失败: %s", last_error)
    return None


def maybe_summarize_chat_to_note(paper_id, chat_history):
    """每累计 5 条新增对话记录，自动增量总结到对应笔记中（分阶段写入）"""
    if not chat_history:
        return {"triggered": False, "reason": "empty_chat"}

    user_message_count = sum(1 for item in chat_history if item.get('role') == 'user')
    # 降低阈值：从 10 改为 5，更及时地总结
    if user_message_count == 0 or user_message_count % 5 != 0:
        return {"triggered": False, "reason": "threshold_not_reached", "user_messages": user_message_count}

    note_content = get_note(paper_id)
    marker = "<!-- chat_summary_count:"
    last_summarized = 0
    marker_match = re.search(r'<!-- chat_summary_count:(\d+) -->', note_content)
    if marker_match:
        last_summarized = int(marker_match.group(1))

    if user_message_count <= last_summarized:
        return {"triggered": False, "reason": "already_summarized", "user_messages": user_message_count}

    # 收集上次总结后的新消息（只取最近 5 条，避免过长）
    recent_messages = []
    current_user_count = 0
    new_messages_collected = 0
    max_to_collect = 5  # 每次最多总结 5 条

    for item in chat_history:
        if item.get('role') == 'user':
            current_user_count += 1
        if current_user_count > last_summarized:
            if new_messages_collected < max_to_collect:
                recent_messages.append(item)
                if item.get('role') == 'user':
                    new_messages_collected += 1

    if not recent_messages:
        return {"triggered": False, "reason": "no_new_messages"}

    # 构建分阶段的总结提示
    transcript = []
    for item in recent_messages:
        role = '用户' if item.get('role') == 'user' else '助手'
        # 截断过长的消息
        content = item.get('content', '')[:500]
        transcript.append(f"{role}: {content}")

    summary_prompt = f"""请将以下围绕同一篇论文的新对话内容进行增量总结。

## 要求
1. 只总结和论文理解相关的信息
2. 提炼出：关键问题、关键解释、值得记录的观点
3. 输出格式（简洁，每个部分 2-4 句话）：

### 🤔 本次提问焦点
[简要说明这次对话主要讨论了什么问题]

### 💡 新增理解
[从对话中获得了什么新认识]

### ❓ 可继续追问
[还有什么相关问题值得进一步探讨]

4. 不要编造未出现的信息
5. 回复要简洁有条理

对话记录：
{'\n\n'.join(transcript)}
"""

    # 使用统一的 API 调用函数（带 pacing 和重试）
    summary_text = call_minimax_with_fallback(
        summary_prompt,
        temperature=0.3,
        max_tokens=800
    )

    if not summary_text:
        LOGGER.warning("自动总结对话失败：API 调用返回空")
        return {"triggered": False, "reason": "api_call_failed"}

    # 构建总结块（更结构化的格式）
    summary_block = (
        "\n\n---\n\n"
        f"## 🤖 对话阶段总结（第 {last_summarized + 1}-{user_message_count} 条提问）\n\n"
        f"*生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n\n"
        f"{summary_text}\n\n"
        f"<!-- chat_summary_count:{user_message_count} -->\n"
    )

    cleaned_note = re.sub(r'\n*<!-- chat_summary_count:\d+ -->\n*', '\n', note_content).rstrip()
    save_note(paper_id, (cleaned_note + summary_block).strip() + '\n')
    return {"triggered": True, "user_messages": user_message_count, "summarized_count": new_messages_collected}


@app.route('/pdf/<paper_id>')
def view_pdf(paper_id):
    """查看 PDF"""
    paper = get_paper(paper_id)
    title = paper.get('title', '') if paper else ''
    candidate_files = get_paper_pdf_candidates(paper_id, title)

    if candidate_files:
        best_match = candidate_files[0]
        return send_file(best_match, mimetype='application/pdf')
    return "PDF not found", 404


if __name__ == '__main__':
    print("=" * 50)
    print("📚 论文阅读助手 Web 版")
    print("=" * 50)
    print(f"🌐 请访问: http://localhost:5001")
    print("=" * 50)
    LOGGER.info("Web 服务启动：port=5001")
    app.run(host='0.0.0.0', port=5001, debug=False)
