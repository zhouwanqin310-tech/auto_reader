"""
MiniMax AI 摘要生成模块
"""
import json
import time
import threading
import requests
from datetime import datetime
from utils.logger import configure_logging


_API_SERIAL_LOCK = threading.Lock()
_LAST_API_CALL_TS = 0.0


def paced_api_wait(min_interval=1.0):
    """串行化 MiniMax 调用，并在两次请求之间至少等待指定秒数。"""
    global _LAST_API_CALL_TS
    with _API_SERIAL_LOCK:
        now = time.time()
        wait_seconds = max(0.0, min_interval - (now - _LAST_API_CALL_TS))
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        _LAST_API_CALL_TS = time.time()


class MiniMaxSummarizer:
    """MiniMax API 论文摘要生成器"""

    SECTION_FORMAT_REQUIREMENT = """
请严格遵守以下 Markdown 层级规范输出：
1. 整个最终文档只能有一个一级标题 #，且该一级标题由系统统一生成；你返回的内容中绝对不要出现任何 # 一级标题
2. 你生成的内容将被系统放入固定的 ## 二级章节中，所以正文内部绝对不要出现 ## 二级标题
3. 正文内部只允许使用 ###、####、列表、表格、引用等 Markdown 结构
4. 不要添加与当前任务无关的引导语、前言、结语
5. 输出必须是可直接渲染的 Markdown
6. 如果你输出了任何 # 或 ## 级别标题，视为格式错误
"""

    def __init__(self, config):
        self.config = config
        self.logger = configure_logging()
        self.api_key = config.get("miniMax", {}).get("api_key", "")
        self.base_url = config.get("miniMax", {}).get("base_url", "https://api.minimax.chat/v1")
        self.model = config.get("miniMax", {}).get("model", "MiniMax-Text-01")

        self.summary_config = config.get("ai_summary", {})
        self.output_config = config.get("output", {})

    def _target_language_hint(self):
        language = str(self.output_config.get("language", "chinese")).strip().lower()
        if language in {"english", "en"}:
            return "请使用英文输出。"
        return "请使用中文输出。"

    def _term_style_hint(self):
        include_english_terms = bool(self.output_config.get("include_english_terms", True))
        if include_english_terms:
            return "专业术语尽量保留英文原文（可附中文解释）。"
        return "优先使用中文术语表达，除非无法避免，不要额外保留英文术语。"

    def generate_summary(self, paper):
        """
        为论文生成完整摘要

        Args:
            paper: 论文信息字典

        Returns:
            摘要结果字典
        """
        self.logger.info("开始生成摘要：%s", (paper.get('title', '') or '')[:120])

        result = {
            "title": paper.get("title", ""),
            "basic_info": self._generate_basic_info(paper),
            "abstract_translation": "",
            "section_analysis": "",
            "method_analysis": "",
            "conclusion_analysis": "",
            "critical_review": "",
            "keywords": self._extract_keywords(paper),
            "similar_papers": ""
        }

        if self.summary_config.get("include_abstract_translation", True):
            result["abstract_translation"] = self._translate_abstract(paper)
        if self.summary_config.get("include_section_analysis", True):
            result["section_analysis"] = self._analyze_sections(paper)
        if self.summary_config.get("include_method_analysis", True):
            result["method_analysis"] = self._analyze_methods(paper)
        if self.summary_config.get("include_conclusion_analysis", True):
            result["conclusion_analysis"] = self._analyze_conclusion(paper)
        if self.summary_config.get("include_critical_review", True):
            result["critical_review"] = self._generate_critical_review(paper)
        if self.summary_config.get("include_similar_papers", True):
            result["similar_papers"] = self._find_similar_papers(paper)

        return result

    def _call_api(self, prompt, system_prompt=None, temperature=0.7, max_tokens=4000):
        """
        调用 MiniMax API

        Args:
            prompt: 用户提示
            system_prompt: 系统提示
            temperature: 温度参数
            max_tokens: 最大 token 数

        Returns:
            API 响应文本
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens
        }

        max_retries = 4
        base_sleep = 3.0
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                paced_api_wait(1.0)
                response = requests.post(
                    f"{self.base_url}/text/chatcompletion_v2",
                    headers=headers,
                    json=payload,
                    timeout=120
                )
                response.raise_for_status()
                data = response.json()

                # 检查 API 错误
                if data.get("base_resp", {}).get("status_code") != 0:
                    error_msg = data.get("base_resp", {}).get("status_msg", "Unknown error")
                    self.logger.warning("MiniMax API 错误：%s", error_msg)
                    return f"[API 错误: {error_msg}]"

                choices = data.get("choices")
                if choices and len(choices) > 0:
                    return choices[0].get("message", {}).get("content", "")

                return ""

            except requests.exceptions.Timeout as e:
                last_error = e
                if attempt >= max_retries:
                    break
                sleep_seconds = base_sleep * (2 ** attempt)
                self.logger.warning("MiniMax API 请求超时，%ss 后重试（%s/%s）", int(sleep_seconds), attempt + 1, max_retries)
                time.sleep(sleep_seconds)
            except Exception as e:
                last_error = e
                self.logger.exception("MiniMax API 调用失败：%s", str(e))
                break

        if last_error:
            self.logger.warning("MiniMax API 最终失败：%s", str(last_error))
        return ""

    def _generate_basic_info(self, paper):
        """生成基本信息"""
        prompt = f"""请提取以下论文的基本信息，以 Markdown 格式输出：

## 基本信息

- **标题**: {paper.get('title', 'N/A')}
- **作者**: {', '.join(paper.get('authors', ['N/A'])[:10])}
- **发表日期**: {paper.get('published', 'N/A')}
- **期刊/会议**: {paper.get('journal', paper.get('source', 'N/A'))}
- **来源**: {paper.get('source', 'N/A')}
- **DOI/URL**: {paper.get('doi', paper.get('url', 'N/A'))}

请只输出 Markdown 表格或列表格式的基本信息，不要其他内容。"""

        return self._call_api(
            prompt,
            system_prompt=(
                "你是一个学术论文信息提取助手，专注于提取论文的元数据信息。"
                + self._target_language_hint()
                + self._term_style_hint()
                + self.SECTION_FORMAT_REQUIREMENT
            )
        )

    def _translate_abstract(self, paper):
        """翻译摘要"""
        abstract = paper.get("abstract", "")
        if not abstract:
            return ""

        prompt = f"""请将以下学术论文摘要翻译成中文，保持学术严谨性，并严格按 Markdown 输出：

## 英文摘要

{abstract}

## 要求

1. 翻译准确、流畅、符合学术规范
2. 保留专业术语的英文原文
3. {self._term_style_hint()}
4. 只输出以下两个三级标题及其内容，不要额外添加其他标题、前言或说明

### 原文摘要
[英文原文]

### 中文翻译
[中文翻译]
"""

        return self._call_api(
            prompt,
            system_prompt=(
                "你是一个专业的学术翻译助手，精通中英文学术论文翻译。"
                + self._target_language_hint()
                + self._term_style_hint()
                + self.SECTION_FORMAT_REQUIREMENT
            )
        )

    def _analyze_sections(self, paper):
        """分析论文各章节"""
        abstract = paper.get("abstract", "")

        prompt = f"""请分析以下学术论文的各部分结构，输出详细的章节梳理：

## 论文标题
{paper.get('title', '')}

## 论文摘要
{abstract}

## 论文关键词
{', '.join(paper.get('categories', paper.get('keywords', []))[:10])}

## 请分析并输出以下内容：

### 1. 研究背景与动机
- 论文要解决什么问题
- 为什么这个问题重要
- 现有方法有什么不足

### 2. 主要贡献
- 论文提出了什么新方法/理论
- 主要创新点是什么
- 解决了哪些技术挑战

### 3. 论文结构概览
- 各章节的主要内容
- 章节之间的逻辑关系

请用详细、专业的语言输出，每个部分不少于100字。{self._target_language_hint()}"""

        return self._call_api(
            prompt,
            system_prompt=(
                "你是一个资深的学术论文审稿人，擅长分析论文的结构和内容。"
                + self._target_language_hint()
                + self._term_style_hint()
                + self.SECTION_FORMAT_REQUIREMENT
            )
        )

    def _analyze_methods(self, paper):
        """方法论详细分析"""
        abstract = paper.get("abstract", "")

        prompt = f"""请对以下论文的研究方法进行详细解析：

## 论文标题
{paper.get('title', '')}

## 论文摘要
{abstract}

## 请从以下维度进行方法论分析：

### 1. 技术框架/方法论
- 采用了什么核心技术或算法
- 方法的理论基础是什么
- 技术实现的关键步骤

### 2. 数据集与实验设置
- 使用了什么数据集
- 数据集的规模和特点
- 实验参数设置

### 3. 评估指标
- 使用了什么评估指标
- 为什么选择这些指标
- 与其他工作的可比性

### 4. 方法的优势与局限
- 该方法相比其他方法的优势
- 可能的局限性或假设条件

请用详细、专业的语言输出，每个部分不少于100字。{self._target_language_hint()}"""

        return self._call_api(
            prompt,
            system_prompt=(
                "你是一个计算机科学和机器学习领域的方法论专家，擅长分析学术论文的技术细节。"
                + self._target_language_hint()
                + self._term_style_hint()
                + self.SECTION_FORMAT_REQUIREMENT
            )
        )

    def _analyze_conclusion(self, paper):
        """结论深入剖析"""
        prompt = f"""请对以下论文的结论进行深入剖析：

## 论文标题
{paper.get('title', '')}

## 论文摘要
{paper.get('abstract', '')}

## 请分析并输出：

### 1. 核心结论总结
- 论文的主要发现是什么
- 达到了什么效果/性能

### 2. 结论的有效性
- 结论是否有充分的实验支撑
- 数据是否具有说服力
- 是否存在过拟合或泛化能力不足的风险

### 3. 实际应用价值
- 研究成果在实际应用中的潜力
- 可能的落地场景

### 4. 未来研究方向
- 论文提出的未来工作是什么
- 还有哪些改进空间

请用详细、专业的语言输出，每个部分不少于100字。{self._target_language_hint()}"""

        return self._call_api(
            prompt,
            system_prompt=(
                "你是一个资深的学术审稿人和研究顾问，擅长评估论文结论的质量和应用价值。"
                + self._target_language_hint()
                + self._term_style_hint()
                + self.SECTION_FORMAT_REQUIREMENT
            )
        )

    def _generate_critical_review(self, paper):
        """生成总结和批判性评价"""
        prompt = f"""请对以下论文进行全面的总结和批判性评价：

## 论文标题
{paper.get('title', '')}

## 论文作者
{', '.join(paper.get('authors', [])[:5])}

## 论文摘要
{paper.get('abstract', '')}

## 请输出以下内容：

### 1. 论文总结（约200字）
用简洁的语言总结论文的核心内容和贡献。

### 2. 优点评价
- 论文的最大亮点是什么
- 在方法/理论/应用上有什么突出贡献

### 3. 局限性分析
- 论文存在哪些不足
- 实验设计是否完善
- 论证是否充分

### 4. 批判性思考
- 你认为论文的结论是否可靠
- 是否有被过度解读的地方
- 该研究与领域发展趋势的契合度

### 5. 个人见解
- 你对该论文的看法和评价
- 对你研究工作的启发

请用详细、专业、批判性的语言输出。{self._target_language_hint()}"""

        return self._call_api(
            prompt,
            system_prompt=(
                "你是一个严谨的学术审稿人，擅长进行批判性思考和公正评价。"
                + self._target_language_hint()
                + self._term_style_hint()
                + self.SECTION_FORMAT_REQUIREMENT
            )
        )

    def _extract_keywords(self, paper):
        """提取关键词/术语"""
        abstract = paper.get("abstract", "")
        categories = paper.get("categories", paper.get("keywords", []))

        prompt = f"""请从以下论文内容中提取10-15个最重要的专业术语和关键词：

## 论文标题
{paper.get('title', '')}

## 论文摘要
{abstract}

## 已有分类/标签
{', '.join(categories[:10])}

## 请输出：

### 核心术语表

| 英文术语 | 中文解释 |
|---------|---------|
| ...     | ...     |

请只输出术语表，不要其他内容。术语要准确、学术化。"""

        return self._call_api(
            prompt,
            system_prompt=(
                "你是一个学术术语专家，擅长提取和解释专业术语。"
                + self._target_language_hint()
                + self._term_style_hint()
                + self.SECTION_FORMAT_REQUIREMENT
            )
        )

    def _find_similar_papers(self, paper):
        """推荐相似论文"""
        prompt = f"""基于以下论文的内容，推荐 {self.summary_config.get('max_similar_papers', 3)} 篇相关的学术论文：

## 论文标题
{paper.get('title', '')}

## 论文摘要
{paper.get('abstract', '')}

## 论文领域
{', '.join(paper.get('categories', paper.get('keywords', []))[:5])}

## 请输出：

### 相似论文推荐

1. **论文标题**: ...
   - **来源**: ArXiv / PLOS / PubMed
   - **推荐理由**: ...
   - **URL**: ...

（依次类推，共3篇）

推荐时请考虑：
1. 研究方向相近
2. 发表时间较新（近2年内优先）
3. 来自知名期刊/会议
4. 提供可访问的链接"""

        return self._call_api(
            prompt,
            system_prompt=(
                "你是一个学术论文推荐系统，擅长根据论文内容推荐相关研究。"
                + self._target_language_hint()
                + self._term_style_hint()
                + self.SECTION_FORMAT_REQUIREMENT
            )
        )

    def batch_summarize(self, papers, delay=2):
        """
        批量生成摘要

        Args:
            papers: 论文列表
            delay: 请求间隔（秒）

        Returns:
            摘要结果列表
        """
        results = []

        for i, paper in enumerate(papers):
            print(f"正在处理第 {i+1}/{len(papers)} 篇论文...")
            try:
                summary = self.generate_summary(paper)
                results.append({
                    "paper": paper,
                    "summary": summary
                })
            except Exception as e:
                print(f"处理论文时出错: {e}")
                results.append({
                    "paper": paper,
                    "summary": None,
                    "error": str(e)
                })

            # 请求间隔，避免 API 限流
            if i < len(papers) - 1:
                time.sleep(delay)

        return results
