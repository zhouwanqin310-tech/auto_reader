"""
开放获取期刊论文搜索模块
支持 PLOS, Frontiers, MDPI, PubMed Central 等开放获取期刊
"""
import os
import re
import time
import requests
from urllib.parse import quote_plus
from utils.helpers import sanitize_filename
from utils.logger import configure_logging


class OpenAccessScraper:
    """开放获取期刊论文爬虫"""

    REQUEST_HEADERS = {
        "User-Agent": "paper-assistant/1.0 (+https://localhost)"
    }

    def __init__(self, pdf_dir):
        self.pdf_dir = pdf_dir
        os.makedirs(pdf_dir, exist_ok=True)
        self.logger = configure_logging()

        # 各平台搜索 API
        self.apis = {
            "plos": "https://api.plos.org/search",
            "pubmed": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            "pubmed_fetch": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
            "doaj": "https://doaj.org/api/v2/search/articles"
        }

    def _build_plos_query(self, query):
        """构建兼容 PLOS Search API 的查询"""
        normalized = ' '.join((query or '').split()).strip()
        if not normalized:
            return '*:*'
        normalized = normalized.replace('"', '')
        return f'"{normalized}"'

    def _extract_doaj_value(self, article, key, default=""):
        """从 DOAJ bibjson 结构中提取字段"""
        bibjson = article.get("bibjson", {}) if isinstance(article, dict) else {}
        value = bibjson.get(key, default)
        return value if value not in (None, []) else default

    def _extract_doaj_identifier(self, article, id_type):
        """提取 DOAJ 标识符，例如 DOI"""
        bibjson = article.get("bibjson", {}) if isinstance(article, dict) else {}
        for identifier in bibjson.get("identifier", []):
            if identifier.get("type") == id_type and identifier.get("id"):
                return identifier["id"]
        return ""

    def _extract_doaj_links(self, article):
        """提取 DOAJ 文章与全文链接"""
        bibjson = article.get("bibjson", {}) if isinstance(article, dict) else {}
        article_url = ""
        pdf_url = ""
        for link in bibjson.get("link", []):
            link_url = link.get("url", "")
            content_type = (link.get("content_type") or "").lower()
            link_type = (link.get("type") or "").lower()
            if not article_url and link_url:
                article_url = link_url
            if link_url and ('pdf' in content_type or 'pdf' in link_type or link_url.lower().endswith('.pdf')):
                pdf_url = link_url
        return article_url, pdf_url

    def search_papers(self, query, source="plos", max_results=10, sort_by="relevance"):
        """
        搜索开放获取期刊论文

        Args:
            query: 搜索关键词
            source: 数据源 (plos, pubmed, doaj)
            max_results: 最大返回数量
            sort_by: 排序方式

        Returns:
            论文列表
        """
        if source == "plos":
            return self._search_plos(query, max_results, sort_by)
        elif source == "pubmed":
            return self._search_pubmed(query, max_results, sort_by)
        elif source == "doaj":
            return self._search_doaj(query, max_results, sort_by)
        else:
            return []

    def _search_plos(self, query, max_results, sort_by):
        """搜索 PLOS 期刊"""
        params = {
            "q": self._build_plos_query(query),
            "rows": max_results,
            "wt": "json",
            "sort": "score desc" if sort_by == "relevance" else "publication_date desc"
        }

        try:
            response = requests.get(
                self.apis["plos"],
                params=params,
                headers=self.REQUEST_HEADERS,
                timeout=30
            )
            response.raise_for_status()
            data = response.json()

            papers = []
            for doc in data.get("response", {}).get("docs", []):
                paper = {
                    "title": doc.get("title_display", ""),
                    "abstract": doc.get("abstract", [""])[0] if doc.get("abstract") else "",
                    "authors": [a.get("display_name", "") for a in doc.get("author", [])],
                    "published": doc.get("publication_date", "")[:10],
                    "journal": doc.get("journal", ""),
                    "doi": doc.get("id", ""),
                    "url": f"https://doi.org/{doc.get('id', '')}",
                    "pdf_url": doc.get("article_url", "").replace("abstract", "pdf"),
                    "keywords": doc.get("keywords", [])[:10],
                    "source": "PLOS"
                }
                if paper.get("title"):
                    papers.append(paper)

            return papers
        except Exception as e:
            self.logger.exception("PLOS 搜索出错：query=%s｜%s", query, str(e))
            return []

    def _search_pubmed(self, query, max_results, sort_by):
        """搜索 PubMed Central"""
        try:
            # 先搜索获取 IDs
            search_params = {
                "db": "pmc",
                "term": query,
                "retmax": max_results,
                "retmode": "json",
                "sort": "relevance" if sort_by == "relevance" else "pdat"
            }

            search_response = requests.get(
                self.apis["pubmed"],
                params=search_params,
                headers=self.REQUEST_HEADERS,
                timeout=30
            )
            search_response.raise_for_status()
            search_data = search_response.json()

            id_list = search_data.get("esearchresult", {}).get("idlist", [])
            if not id_list:
                return []

            # 获取论文详情
            time.sleep(0.5)  # 避免请求过快
            fetch_params = {
                "db": "pmc",
                "id": ",".join(id_list),
                "retmode": "json"
            }

            fetch_response = requests.get(
                self.apis["pubmed_fetch"],
                params=fetch_params,
                headers=self.REQUEST_HEADERS,
                timeout=30
            )
            fetch_response.raise_for_status()
            fetch_data = fetch_response.json()

            papers = []
            result = fetch_data.get("result", {})

            def _normalize_date(value):
                value = (value or "").strip()
                if not value:
                    return ""
                # 常见返回包括 "2024 Jan 12" / "2024" / "2024-01-12"
                iso_match = re.search(r"(\d{4}-\d{2}-\d{2})", value)
                if iso_match:
                    return iso_match.group(1)
                ymd_match = re.search(r"(\d{4})[ /-](\d{1,2})[ /-](\d{1,2})", value)
                if ymd_match:
                    y, m, d = ymd_match.groups()
                    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
                year_match = re.search(r"(\d{4})", value)
                if year_match:
                    return year_match.group(1)
                return value[:10]

            for pmid in id_list:
                pub = result.get(pmid, {})
                if pub.get("title"):
            # 获取 PDF 链接
                    pdf_url = pub.get("pmc_url", "")
                    if isinstance(pdf_url, list):
                        pdf_url = pdf_url[0] if pdf_url else ""

                    published_raw = (
                        pub.get("pubdate")
                        or pub.get("epubdate")
                        or pub.get("sortpubdate")
                        or ""
                    )
                    published = _normalize_date(published_raw)
                    # 如果只有年份，仍然填入，后续时间窗口会按解析器处理（能解析到年份就不至于直接被剔除）

                    paper = {
                        "title": pub.get("title", ""),
                        "abstract": pub.get("abstract", [""])[0] if pub.get("abstract") else "",
                        "authors": [author.get("name", "") if isinstance(author, dict) else str(author) for author in pub.get("authors", [])],
                        "published": published,
                        "journal": pub.get("source", ""),
                        "doi": pub.get("doi", ""),
                        "pmid": pmid,
                        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                        "pdf_url": pdf_url,
                        "source": "PubMed Central"
                    }
                    papers.append(paper)

            return papers
        except Exception as e:
            self.logger.exception("PubMed 搜索出错：query=%s｜%s", query, str(e))
            return []

    def _search_doaj(self, query, max_results, sort_by):
        """搜索 DOAJ (Directory of Open Access Journals)"""
        encoded_query = quote_plus((query or '').strip())
        request_url = f"{self.apis['doaj']}/{encoded_query}" if encoded_query else self.apis['doaj']
        params = {
            "pageSize": max_results,
            "page": 1
        }

        headers = {
            "Accept": "application/json",
            **self.REQUEST_HEADERS
        }

        try:
            response = requests.get(
                request_url,
                params=params,
                headers=headers,
                timeout=30
            )
            response.raise_for_status()
            data = response.json()

            papers = []
            for article in data.get("results", []):
                bibjson = article.get("bibjson", {}) if isinstance(article, dict) else {}

                authors = []
                for author in bibjson.get("author", []):
                    name = author.get("name", "")
                    if name:
                        authors.append(name)

                article_url, pdf_url = self._extract_doaj_links(article)
                doi = self._extract_doaj_identifier(article, "doi")
                journal = bibjson.get("journal", {}) if isinstance(bibjson.get("journal", {}), dict) else {}
                keywords = bibjson.get("keywords", []) if isinstance(bibjson.get("keywords", []), list) else []

                paper = {
                    "title": self._extract_doaj_value(article, "title", ""),
                    "abstract": self._extract_doaj_value(article, "abstract", ""),
                    "authors": authors,
                    "published": article.get("created_date", "")[:10] if article.get("created_date") else "",
                    "journal": journal.get("title", "") or self._extract_doaj_value(article, "journal_title", ""),
                    "doi": doi,
                    "url": article_url,
                    "pdf_url": pdf_url,
                    "keywords": keywords[:10],
                    "source": "DOAJ"
                }

                if paper.get("title"):
                    papers.append(paper)

            return papers
        except Exception as e:
            self.logger.exception("DOAJ 搜索出错：query=%s｜%s", query, str(e))
            return []

    def search_all_sources(self, query, max_results_per_source=5, sort_by="relevance"):
        """从所有开放获取源搜索"""
        all_papers = []

        sources = ["plos", "pubmed", "doaj"]
        for source in sources:
            print(f"正在搜索 {source}...")
            papers = self.search_papers(
                query=query,
                source=source,
                max_results=max_results_per_source,
                sort_by=sort_by
            )
            for paper in papers:
                paper["search_field"] = query
            all_papers.extend(papers)
            time.sleep(1)

        return self._deduplicate_papers(all_papers)

    def search_by_fields(self, fields, max_results_per_field=3, sort_by="relevance"):
        """按多个领域搜索论文"""
        all_papers = []

        for field in fields:
            print(f"正在搜索领域: {field}")
            papers = self.search_all_sources(
                query=field,
                max_results_per_source=max_results_per_field,
                sort_by=sort_by
            )
            all_papers.extend(papers)
            time.sleep(0.5)

        return self._deduplicate_papers(all_papers)

    def _deduplicate_papers(self, papers):
        """基于 DOI 和标题去重"""
        seen_dois = set()
        seen_titles = set()
        unique_papers = []

        for paper in papers:
            doi = paper.get("doi", "").lower()
            title_lower = paper.get("title", "").lower()

            if doi and doi not in seen_dois:
                seen_dois.add(doi)
                unique_papers.append(paper)
            elif not doi and title_lower not in seen_titles:
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
            # 尝试从 DOI 构建 PDF URL
            doi = paper.get("doi", "")
            if doi:
                pdf_url = f"https://doi.org/{doi}"

        if not pdf_url:
            paper["pdf_pending_url"] = paper.get("url", "") or (f"https://doi.org/{paper.get('doi', '')}" if paper.get('doi') else "")
            return None

        if not filename:
            # 使用标题作为文件名
            source = paper.get("source", "oa").lower()
            paper_id = paper.get("doi", "").split("/")[-1] or paper.get("arxiv_id", "paper")
            filename = f"{source}_{paper_id}_{sanitize_filename(paper.get('title', 'untitled'))[:50]}"

        filename = sanitize_filename(filename) + ".pdf"
        filepath = os.path.join(self.pdf_dir, filename)

        # 如果已存在则跳过
        if os.path.exists(filepath):
            self.logger.info("开放获取 PDF 已存在：%s", filepath)
            return filepath

        try:
            # 对于某些期刊，可能需要特殊的 headers
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            }

            response = requests.get(pdf_url, timeout=60, headers=headers, stream=True)
            response.raise_for_status()

            # 检查是否真的是 PDF
            content_type = response.headers.get("Content-Type", "")
            if "pdf" not in content_type.lower() and not response.content[:4].startswith(b"%PDF"):
                self.logger.warning("开放获取链接不是 PDF：content_type=%s｜url=%s", content_type, pdf_url)
                # 保存原始 URL 以供手动下载
                paper["pdf_pending_url"] = pdf_url
                return None

            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            self.logger.info("开放获取 PDF 下载成功：%s", filepath)
            return filepath
        except Exception as e:
            self.logger.exception("开放获取 PDF 下载失败：%s", str(e))
            return None

    def get_paper_citations(self, paper):
        """获取论文引用数（如果可用）"""
        # PubMed 可以通过 Europe PMC 获取引用数
        doi = paper.get("doi", "")
        if not doi:
            return None

        try:
            url = f"https://api.elsevier.com/content/abstract/doi/{doi}"
            # 这里需要 Elsevier API key，暂时返回 None
            return None
        except Exception:
            return None
