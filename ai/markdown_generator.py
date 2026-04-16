"""
Markdown 文档生成模块
"""
import re
from datetime import datetime


class MarkdownGenerator:
    """生成精美的论文 Markdown 文档"""

    FIXED_SECTIONS = [
        ("basic_info", "## 📋 基本信息"),
        ("abstract_translation", "## 📄 摘要翻译"),
        ("section_analysis", "## 📖 章节分析"),
        ("method_analysis", "## ⚙️ 方法详解"),
        ("conclusion_analysis", "## 🎯 结论剖析"),
        ("critical_review", "## 💡 批判评价"),
        ("keywords", "## 🔑 核心术语"),
    ]

    def __init__(self, config):
        self.config = config
        self.output_config = config.get("output", {})

    def generate(self, paper, summary, pdf_path=None):
        """
        生成完整的论文 Markdown 文档

        Args:
            paper: 论文信息字典
            summary: AI 摘要结果字典
            pdf_path: PDF 文件路径（如果有）

        Returns:
            Markdown 格式的字符串
        """
        md = []

        md.append(self._generate_header(paper))

        for idx, (summary_key, title) in enumerate(self.FIXED_SECTIONS):
            content = self._sanitize_section_markdown((summary or {}).get(summary_key, ""))
            if summary_key == "keywords":
                content = self._sanitize_keywords_section(content)
            if not content:
                continue
            if idx > 0:
                md.append("---\n\n")
            md.append(f"{title}\n\n")
            md.append(content)
            md.append("\n\n")

        md.append("---\n\n")
        md.append("## 📥 原文获取\n\n")
        if pdf_path:
            md.append(f"**本地 PDF**: `{pdf_path}`\n\n")
        if paper.get("pdf_url"):
            md.append(f"**在线 PDF**: [{paper.get('pdf_url')}]({paper.get('pdf_url')})\n\n")
        if paper.get("url"):
            md.append(f"**论文链接**: [{paper.get('url')}]({paper.get('url')})\n\n")

        md.append("---\n\n")
        md.append(self._generate_notes_section(paper))
        md.append(self._generate_footer(paper))

        final_markdown = "".join(md)
        self.validate_document_structure(final_markdown)
        return final_markdown

    def _sanitize_section_markdown(self, content):
        """清理 AI 返回内容，禁止额外的 # / ## 标题"""
        text = (content or "").strip()
        if not text:
            return ""

        sanitized_lines = []
        for line in text.splitlines():
            if re.match(r'^#(?!#)\s+', line):
                line = re.sub(r'^#\s+', '### ', line)
            elif re.match(r'^##(?!#)\s+', line):
                line = re.sub(r'^##\s+', '### ', line)
            sanitized_lines.append(line)

        return "\n".join(sanitized_lines).strip()

    def _sanitize_keywords_section(self, content):
        """核心术语区专用清理：去掉误混入的个人笔记区内容。"""
        text = (content or "").strip()
        if not text:
            return ""

        cutoff_patterns = [
            r"\n##\s*📝\s*个人笔记区",
            r"\n##\s*个人笔记区",
            r"\n###\s*📝\s*个人笔记区",
            r"\n###\s*个人笔记区",
            r"\n####\s*📝\s*个人笔记区",
            r"\n####\s*个人笔记区",
        ]
        for pattern in cutoff_patterns:
            match = re.search(pattern, text)
            if match:
                text = text[:match.start()].rstrip()
                break

        return text

    def validate_document_structure(self, markdown_content):
        """校验 Markdown 只有一个一级标题，且至少有多个二级标题"""
        lines = markdown_content.splitlines()
        h1_count = sum(1 for line in lines if re.match(r'^#(?!#)\s+', line))
        h2_count = sum(1 for line in lines if re.match(r'^##(?!#)\s+', line))

        if h1_count != 1:
            raise ValueError(f"Markdown 结构错误：一级标题数量应为 1，实际为 {h1_count}")
        if h2_count < 2:
            raise ValueError(f"Markdown 结构错误：二级标题数量应至少为 2，实际为 {h2_count}")

    def _generate_header(self, paper):
        """生成文档头部"""
        title = paper.get("title", "无标题论文")
        date = datetime.now().strftime("%Y-%m-%d %H:%M")

        header = f"""# 📚 论文阅读笔记

> **论文**: {title}
> **生成日期**: {date}
> **数据来源**: {paper.get('source', 'N/A')}

---

"""

        return header

    def _generate_notes_section(self, paper):
        """生成笔记区"""
        notes = f"""## 📝 个人笔记区

> 💡 在此记录你的阅读感想、疑问和批注

### 阅读进度
- [ ] 初读
- [ ] 精读
- [ ] 已整理笔记

### 疑问与问题
1.

### 重要摘录

>

### 阅读感想

"""
        return notes

    def _generate_footer(self, paper):
        """生成文档尾部"""
        footer = f"""
---

*本文档由论文阅读助手自动生成*
*最后更新: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}*

"""
        return footer

    def generate_index(self, papers, date=None):
        """
        生成论文索引页面

        Args:
            papers: 论文列表
            date: 日期（可选）

        Returns:
            Markdown 格式的索引页面
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        md = [f"""# 📅 论文日索引

**日期**: {date}
**论文数量**: {len(papers)}

---

## 今日论文列表

"""]

        for i, paper in enumerate(papers, 1):
            title = paper.get("title", "无标题")
            source = paper.get("source", "N/A")
            published = paper.get("published", "N/A")

            md.append(f"""### {i}. {title}

- **来源**: {source}
- **发表日期**: {published}
- **作者**: {', '.join(paper.get('authors', [])[:3])}
- **状态**: ⏳ 待阅读

---
""")

        md.append(f"\n*索引生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n")

        return "".join(md)
