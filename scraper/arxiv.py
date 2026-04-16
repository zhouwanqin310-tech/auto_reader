"""
ArXiv 论文搜索和下载模块
"""
import os
import re
import time
import requests
from utils.helpers import sanitize_filename
from utils.logger import configure_logging


class ArxivScraper:
    """ArXiv 论文爬虫"""

    BASE_URL = "https://export.arxiv.org/api/query"
    REQUEST_HEADERS = {
        "User-Agent": "paper-assistant/1.0 (+https://localhost)"
    }

    def __init__(self, pdf_dir):
        self.pdf_dir = pdf_dir
        os.makedirs(pdf_dir, exist_ok=True)
        self.logger = configure_logging()

    def build_search_query(self, query):
        """构建 ArXiv 查询语句，优先短语匹配并兼顾宽松召回"""
        normalized = ' '.join((query or '').split()).strip()
        if not normalized:
            return "all:*"

        safe_phrase = normalized.replace('"', '')
        tokens = [token for token in re.split(r'\s+', safe_phrase) if token]

        parts = [f'all:"{safe_phrase}"']
        if len(tokens) > 1:
            parts.extend([
                f'ti:"{safe_phrase}"',
                f'abs:"{safe_phrase}"'
            ])

        for token in tokens:
            parts.append(f'all:{token}')

        unique_parts = list(dict.fromkeys(parts))
        return '(' + ' OR '.join(unique_parts) + ')'

    def search_papers(self, query, max_results=10, sort_by="relevance"):
        """
        搜索 ArXiv 论文

        Args:
            query: 搜索关键词
            max_results: 最大返回数量
            sort_by: 排序方式 (relevance, lastUpdatedDate, submittedDate)

        Returns:
            论文列表
        """
        sort_mapping = {
            "relevance": "relevance",
            "published_date": "lastUpdatedDate",
            "submitted_date": "submittedDate",
            "citation_count": "relevance"  # ArXiv 不支持引用量排序
        }

        def _request(sort_key):
            params = {
                "search_query": self.build_search_query(query),
                "start": 0,
                "max_results": max_results,
                "sortBy": sort_key,
                "sortOrder": "descending"
            }
            response = requests.get(
                self.BASE_URL,
                params=params,
                headers=self.REQUEST_HEADERS,
                timeout=30
            )
            if response.status_code == 429:
                time.sleep(5)
                response = requests.get(
                    self.BASE_URL,
                    params=params,
                    headers=self.REQUEST_HEADERS,
                    timeout=30
                )
            response.raise_for_status()
            return self._parse_atom_response(response.text)

        # 召回兜底：先按指定 sort_by；如果 0 命中或异常，再退到 lastUpdatedDate
        primary_sort = sort_mapping.get(sort_by, "relevance")
        fallback_sorts = []
        if primary_sort != "lastUpdatedDate":
            fallback_sorts.append("lastUpdatedDate")
        if primary_sort != "submittedDate":
            fallback_sorts.append("submittedDate")

        try:
            papers = _request(primary_sort)
            if papers:
                return papers
            for fallback_sort in fallback_sorts:
                papers = _request(fallback_sort)
                if papers:
                    return papers
            return []
        except Exception as e:
            self.logger.exception("ArXiv 搜索出错：query=%s｜%s", query, str(e))
            return []

    def search_by_fields(self, fields, max_results_per_field=5, sort_by="relevance"):
        """按多个领域搜索论文"""
        all_papers = []

        for field in fields:
            print(f"正在搜索领域: {field}")
            papers = self.search_papers(
                query=field,
                max_results=max_results_per_field,
                sort_by=sort_by
            )
            for paper in papers:
                paper["search_field"] = field
            all_papers.extend(papers)
            time.sleep(1)

        return self._deduplicate_papers(all_papers)

    def _parse_atom_response(self, xml_text):
        """解析 ArXiv ATOM 格式响应"""
        papers = []
        entry_pattern = re.compile(r'<entry>(.*?)</entry>', re.DOTALL)
        entries = entry_pattern.findall(xml_text)

        for entry in entries:
            paper = {}

            title_match = re.search(r'<title>(.*?)</title>', entry, re.DOTALL)
            if title_match:
                paper["title"] = self._clean_text(title_match.group(1))

            summary_match = re.search(r'<summary>(.*?)</summary>', entry, re.DOTALL)
            if summary_match:
                paper["abstract"] = self._clean_text(summary_match.group(1))

            authors = re.findall(r'<name>(.*?)</name>', entry)
            paper["authors"] = authors

            published_match = re.search(r'<published>(.*?)</published>', entry)
            if published_match:
                paper["published"] = published_match.group(1)[:10]

            updated_match = re.search(r'<updated>(.*?)</updated>', entry)
            if updated_match:
                paper["updated"] = updated_match.group(1)[:10]

            id_match = re.search(r'<id>(.*?)</id>', entry)
            if id_match:
                paper["arxiv_id"] = id_match.group(1).split('/')[-1]
                paper["url"] = f"https://arxiv.org/abs/{paper['arxiv_id']}"
                paper["pdf_url"] = f"https://arxiv.org/pdf/{paper['arxiv_id']}.pdf"

            categories = re.findall(r'<category term="(.*?)"', entry)
            paper["categories"] = categories[:5]

            doi_match = re.search(r'<arxiv:doi>(.*?)</arxiv:doi>', entry)
            if doi_match:
                paper["doi"] = doi_match.group(1)

            journal_match = re.search(r'<arxiv:journal_ref>(.*?)</arxiv:journal_ref>', entry)
            if journal_match:
                paper["journal"] = journal_match.group(1)

            comment_match = re.search(r'<arxiv:comment>(.*?)</arxiv:comment>', entry)
            if comment_match:
                paper["comment"] = self._clean_text(comment_match.group(1))

            paper["source"] = "ArXiv"

            if paper.get("title"):
                papers.append(paper)

        return papers

    def _clean_text(self, text):
        """清理文本中的多余空白"""
        text = text.replace('\n', ' ').replace('\r', ' ')
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def _deduplicate_papers(self, papers):
        """基于标题去重"""
        seen_titles = set()
        unique_papers = []

        for paper in papers:
            title_lower = paper.get("title", "").lower()
            if title_lower not in seen_titles:
                seen_titles.add(title_lower)
                unique_papers.append(paper)

        return unique_papers

    def download_pdf(self, paper, filename=None):
        """
        下载论文 PDF

        Args:
            paper: 论文信息字典
            filename: 自定义文件名（不含扩展名）

        Returns:
            PDF 文件路径，失败返回 None
        """
        pdf_url = paper.get("pdf_url")
        if not pdf_url:
            return None

        if not filename:
            filename = f"{paper.get('arxiv_id', 'paper')}_{sanitize_filename(paper.get('title', 'untitled'))[:50]}"

        filename = sanitize_filename(filename) + ".pdf"
        filepath = os.path.join(self.pdf_dir, filename)

        if os.path.exists(filepath):
            self.logger.info("ArXiv PDF 已存在：%s", filepath)
            return filepath

        try:
            response = requests.get(pdf_url, headers=self.REQUEST_HEADERS, timeout=60, stream=True)
            response.raise_for_status()

            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            self.logger.info("ArXiv PDF 下载成功：%s", filepath)
            return filepath
        except Exception as e:
            self.logger.exception("ArXiv PDF 下载失败：%s", str(e))
            return None
