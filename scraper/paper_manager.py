"""
论文管理器 - 负责论文去重、历史记录管理
"""
import os
import json
from datetime import datetime, timedelta
from pathlib import Path
from utils.helpers import (
    get_project_root, ensure_dir, get_today_date,
    load_history, save_history, paper_to_hash,
    is_paper_in_history, is_paper_ever_in_history, add_paper_to_history
)
from utils.logger import configure_logging


class PaperManager:
    """论文管理器"""

    def __init__(self, config):
        self.config = config
        self.logger = configure_logging()
        self.storage = config.get("storage", {})
        self.base_dir = Path(self.storage.get("base_dir", "./"))
        self.papers_dir = self.base_dir / self.storage.get("papers_dir", "papers")
        self.pdf_dir = self.base_dir / self.storage.get("pdf_dir", "pdfs")
        self.cache_file = self.base_dir / self.storage.get("cache_file", "paper_history.json")

        # 确保目录存在
        ensure_dir(self.papers_dir)
        ensure_dir(self.pdf_dir)

        # 加载历史记录
        self.history = load_history(str(self.cache_file))

    def filter_new_papers(self, papers, days=None):
        """
        过滤出不在历史记录中的新论文

        Args:
            papers: 论文列表
            days: 若为整数，排除最近 N 天内处理过的论文；若为 None，执行全局幂等去重（推荐）

        Returns:
            新论文列表
        """
        # 每次过滤前重新加载一次历史（配合文件锁/原子写，避免并发任务用旧快照导致重复）
        self.history = load_history(str(self.cache_file))
        new_papers = []

        for paper in papers:
            paper_hash = paper_to_hash(paper)

            # 检查是否在历史记录中
            if days is None:
                duplicated = is_paper_ever_in_history(paper_hash, self.history)
            else:
                duplicated = is_paper_in_history(paper_hash, self.history, days=days)

            if not duplicated:
                new_papers.append(paper)
            else:
                self.logger.info("论文已在历史记录中，跳过：%s", (paper.get('title', '') or '')[:80])

        return new_papers

    def save_paper(self, paper, markdown_content):
        """
        保存论文 Markdown 文件

        Args:
            paper: 论文信息
            markdown_content: Markdown 内容

        Returns:
            保存的文件路径
        """
        # 生成文件名
        source = paper.get("source", "paper").lower().replace(" ", "_")
        paper_id = paper.get("arxiv_id", "") or paper.get("doi", "").split("/")[-1][:20] or paper.get("pmid", "")

        date_prefix = get_today_date()
        filename = f"{date_prefix}_{source}_{paper_id}.md"

        filepath = self.papers_dir / filename

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(markdown_content)

        self.logger.info("论文已保存：%s", str(filepath))
        return filepath

    def get_paper_pdf_path(self, paper, filename=None):
        """获取论文 PDF 的保存路径"""
        if not filename:
            source = paper.get("source", "paper").lower().replace(" ", "_")
            paper_id = paper.get("arxiv_id", "") or paper.get("doi", "").split("/")[-1][:20] or paper.get("pmid", "")
            filename = f"{source}_{paper_id}"

        filename = f"{filename}.pdf"
        return self.pdf_dir / filename

    def add_to_history(self, paper):
        """将论文添加到历史记录"""
        # 写入前再加载一次，避免并发写导致覆盖丢数据
        self.history = load_history(str(self.cache_file))
        self.history = add_paper_to_history(paper, self.history)
        save_history(str(self.cache_file), self.history)

    def get_daily_papers(self):
        """获取今天已保存的论文"""
        today = get_today_date()
        today_papers = []

        for paper in self.history.get("papers", []):
            if paper.get("date") == today:
                today_papers.append(paper)

        return today_papers

    def is_duplicate_today(self, paper):
        """检查论文是否今天已处理"""
        paper_hash = paper_to_hash(paper)
        today = get_today_date()

        for entry in self.history.get("papers", []):
            if entry.get("hash") == paper_hash and entry.get("date") == today:
                return True

        return False

    def get_statistics(self):
        """获取统计信息"""
        stats = {
            "total_papers": len(self.history.get("papers", [])),
            "days_active": len(self.history.get("dates", [])),
            "today_papers": len(self.get_daily_papers()),
            "last_updated": self.history.get("dates", [None])[-1]
        }
        return stats
