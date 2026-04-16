# 论文阅读助手（Paper Assistant）

一个基于 `Python + Flask` 的本地论文阅读与“按需推送”工具：支持论文列表浏览、Markdown 阅读、笔记、AI 对话，以及按主题抓取并生成结构化分析。

## 功能概览

- 论文抓取：`ArXiv` + 开放获取来源（`PLOS / PubMed Central / DOAJ`）
- 主题筛选与按需推送：时间窗口过滤 + 规则粗召回/精筛 + AI 主题匹配 + 补救策略
- 摘要生成与 Markdown 入库：调用 `MiniMax API` 生成多段内容并写入本地 `papers/` 目录
- AI 对话：围绕当前论文内容回答问题，并将关键对话增量总结到笔记中
- 管理能力：收藏/取消收藏、删除论文（保留/清理历史）、清空对话、查看 PDF
- 可视化前端：前端将 Markdown 按章节分块渲染，并提供“按需推送”实时进度面板

## 环境要求

- Python：建议 `3.9+`（项目使用 `venv` + `pip` 安装）
- 依赖网络：需要访问 ArXiv、开放获取接口以及 `MiniMax API`
- Windows 运行方式建议：使用 `Git Bash` 或 `WSL` 来执行 `.sh` 脚本

## 如何启动（本地）

### 1）准备配置

编辑 `config.yaml`：

- 你需要填写：`miniMax.api_key`
- 文档里不会提供真实密钥；请在本地填写后再运行

建议示例（不要提交真实 key 到公开仓库）：

```yaml
miniMax:
  api_key: "<YOUR_API_KEY>"
  base_url: "https://api.minimax.chat/v1"
  model: "MiniMax-M2.7"
  fallback_model: "MiniMax-Text-01"
```

### 2）一键装配（推荐）

项目根目录已有跨平台装配脚本：`env_detect_and_setup.sh`

在 Ubuntu / WSL / Git Bash 中执行：

```bash
chmod +x env_detect_and_setup.sh
./env_detect_and_setup.sh
```

装配完成后，前台启动：

```bash
./env_detect_and_setup.sh --start
```

启动后访问：

- `http://localhost:5001`

### 3）按需选择启动脚本

- 前台启动（推荐调试）：`./start_web.sh`
- 后台启动（Ubuntu/macOS 更可靠）：`./start_server.sh`
  - 注意：`start_server.sh` 内部使用 `lsof` + `nohup` 来管理端口/后台进程；在部分 Windows 环境可能不可用

## 环境检验（建议在首次运行时做）

1. 依赖导入校验（在装配后的当前 Python/venv 环境里执行）

```bash
python -c "import flask, yaml, requests, schedule; print('deps_ok')"
```

2. 离线测试：验证筛选配置解析与论文指纹稳定性

```bash
python scripts/test_filter_logic.py
```

3. 校验已生成 Markdown 的结构约束（必须只有一个 H1，且至少包含 2 个 H2）

```bash
python scripts/check_markdown_structure.py
```

4. 查看日志

- Ubuntu/macOS：`tail -f server.log`
- Windows PowerShell：`Get-Content server.log -Wait`

## config.yaml 参数详解

`web/app.py` 会加载 `config.yaml` 并用于：

- 初始化 `miniMax` 调用参数
- 计算推送数量与时间窗口策略
- 加载 AI 主题匹配 persona 与阈值
- 配置存储目录（历史文件、papers、pdfs）

### miniMax（MiniMax API 配置）

- `api_key`：MiniMax 密钥。用于构造 `Authorization: Bearer <api_key>`
- `base_url`：API 根地址（默认 `https://api.minimax.chat/v1`）
- `model`：主模型（摘要生成/解析/对话的默认模型）
- `fallback_model`：备用模型（用于请求失败/降级）

### storage（本地存储路径）

- `base_dir`：项目根目录（用于拼接 `papers/`、`pdfs/`、`paper_history.json` 等）
  - 重要：建议把它设置成你本地项目实际路径，否则新生成的论文可能写到项目外，前端不一定能看到
- `papers_dir`：Markdown 存放目录名（默认 `papers`）
- `pdf_dir`：PDF 存放目录名（默认 `pdfs`）
- `cache_file`：历史记录 JSON 文件名（默认 `paper_history.json`）

### push（按需推送/定时推送配置）

- `time`：每日定时推送的触发时间（24 小时制，`HH:MM`）
- `min_papers` / `max_papers`：每次推送目标保留数量区间
- `check_interval_hours`：除固定时间点外，每隔多少小时检查一次并执行定时推送

说明：当你在前端发起“按需推送”时，前端会把 `topic/date/count` 发给后端；后端会再通过 `ai_parse_input`（AI + 规则兜底）解析成结构化参数。

### fields（默认主题池）

- `fields` 是“默认主题候选池”
- 当你在按需推送里不指定 `topic`（或后端解析回退时），会从这里取若干主题并做同义扩展，用于抓取与筛选

### filter（筛选画像与阈值）

#### `filter.profile.ai_match_persona`

这是传给 AI 主题匹配的“人设/判定标准”。AI 会根据：

- 领域画像（例如：计算语言学、CALL、教育/人文社科结合）
- 论文是否具备明确研究问题落点

输出结构化 JSON（包含 `matched`、`domain_fit`、`topic_fit`、`confidence` 与原因）。

#### `filter.profile.thresholds`

用于“规则层粗召回 + 精筛 + AI 置信度阈值”的阈值标准：

- `rule_recall_min_score`：规则层粗召回阈值（分数越低，召回越全）
- `rule_precision_min_score`：规则层精筛阈值（分数越高，精度更高）
- `ai_min_confidence`：AI 接纳阈值（`confidence` 小于该值会拒绝）
- `max_ai_candidates`：当前用于写入任务元信息（meta），实际并未硬性截断候选列表（如需硬限制可再增强代码）
- `max_ai_calls_per_job`：当前主要用于写入任务元信息（meta），实际调用中只统计次数不做硬中断（如需可进一步增强代码）

### search_priority（排序优先级）

- 允许值：`relevance` / `published_date` / `citation_count`
- 影响最终排序 `rank_papers_by_priority`
- `citation_count` 在代码里是启发式估计（基于 `journal/comment` 文本线索），并非真实引文 API

### output（AI 生成输出偏好）

- `language`：摘要生成语言（如 `chinese`）
- `include_english_terms`：是否保留英文术语（便于专业阅读）
- `save_pdf`：推送流程中是否尝试下载 PDF

### ai_match（AI 主题匹配 persona 兜底）

- `ai_match.persona`：当 `filter.profile.ai_match_persona` 未配置或为空时，会回退使用这里的 persona

### ai_summary（AI 摘要生成开关）

- `include_abstract_translation`：是否包含摘要翻译
- `include_section_analysis`：是否包含章节分析
- `include_method_analysis`：是否包含方法详解
- `include_conclusion_analysis`：是否包含结论剖析
- `include_critical_review`：是否包含批判性评价
- `include_similar_papers`：是否包含相似论文推荐
- `max_similar_papers`：相似论文推荐数量

## 整体工作流（从前端点击到 Markdown 入库）

### 1）前端发起按需推送

- 前端调用：`POST /api/push`
- 提交参数：`{ topic, date, count }`

### 2）后端创建任务并异步执行

- `web/app.py` 创建 `job_id`，并启动后台线程执行 `run_push_job`
- 前端通过 `GET /api/push/<job_id>` 轮询实时进度（包含步骤、meta、日志）

### 3）解析推送参数（AI + 规则兜底）

- `ai_parse_input()`：把自然语言 `topic/date/count` 解析成结构化：
  - `topics`（英文检索短语集合）
  - `days`（时间窗口天数）
  - `count`（目标数量区间）

### 4）抓取候选论文

- `ArxivScraper.search_papers()`：使用 ArXiv ATOM API 搜索并解析条目
- `OpenAccessScraper.search_all_sources()`：依次搜索 PLOS / PubMed Central / DOAJ

### 5）时间窗口过滤与历史去重

- 按发布时间/更新解析时间，并严格过滤最近 N 天的候选
- 使用 `PaperManager.filter_new_papers(..., days=None)` 做全局幂等去重
  - 去重指纹由 `utils/helpers.py` 的 `paper_to_hash()` 生成（优先 DOI / arxiv_id / pmid，否则退化到标题+作者+日期等组合）

### 6）规则层粗召回 + 精筛，再进入 AI 主题匹配

- `topic_rule_score()`：根据标题/摘要/分类/检索字段计算匹配分数
- `rule_recall_min_score`：粗召回
- `rule_precision_min_score`：精筛
- 精筛后的候选进入 `ai_topic_matches_paper()`：
  - AI 输出 JSON
  - `confidence >= ai_min_confidence` 且 `matched==true` 才接纳

### 7）补救策略（结果不足时）

当结果数少于 `min_papers`，会触发补救：

- 补救 1：放宽精筛阈值继续评估（不额外抓取，降低成本）
- 补救 2：放宽时间窗口 2x 后重新筛选（基于同批次抓取结果）

### 8）排序、下载、生成摘要与 Markdown 入库

- 排序：`rank_papers_by_priority()`
- 处理每篇论文：
  - 下载 PDF（如 `output.save_pdf=true` 且可行）
  - `MiniMaxSummarizer` 生成摘要结构
  - `MarkdownGenerator` 将摘要结构拼成固定层级的 Markdown（并校验结构）
  - `PaperManager.save_paper()` 入库到 `papers/`，并写入历史记录

## API 路由一览（后端）

- `GET /`：前端页面
- `GET /api/papers`：获取论文列表（支持 `scope=favorites`）
- `GET /api/paper/<paper_id>`：获取论文详情
- `GET /api/paper/<paper_id>/note`：获取笔记
- `POST /api/paper/<paper_id>/note`：保存笔记
- `DELETE /api/paper/<paper_id>/chat`：清空该论文的对话记录
- `POST /api/paper/<paper_id>/delete?mode=keep_history|purge`：删除论文（两种模式）
- `POST /api/paper/<paper_id>/favorite`：收藏
- `POST /api/paper/<paper_id>/unfavorite`：取消收藏
- `POST /api/history/dedupe`：对历史记录做全量去重（调试用）
- `GET /api/search?q=...`：关键词搜索（标题/正文）
- `POST /api/push`：创建按需推送任务
- `GET /api/push/<job_id>`：获取任务实时状态
- `GET /api/daily-push-status`：定时推送状态（前端展示用）
- `POST /api/chat`：对某篇论文进行 AI 问答
- `GET /pdf/<paper_id>`：查看 PDF（在 `pdfs/` 与 `pdfs/favorites/` 中按 id/标题关键词匹配）

## 代码层次（模块如何组织）

目录结构（关键模块）：

```text
自动阅读/
├── web/                      # Flask 入口与路由（业务编排）
│   └── app.py
├── scraper/                  # 抓取与历史管理（数据层）
│   ├── arxiv.py              # ArxivScraper
│   ├── open_access.py       # OpenAccessScraper（PLOS / PubMed Central / DOAJ）
│   └── paper_manager.py    # PaperManager（保存、去重、历史文件）
├── ai/                       # AI 调用与 Markdown 生成（推理层）
│   ├── summarizer.py        # MiniMaxSummarizer（摘要结构生成）
│   └── markdown_generator.py# MarkdownGenerator（固定层级拼装+校验）
├── utils/                    # 工具与配置解析（基础设施层）
│   ├── logger.py            # 日志系统（写入 server.log）
│   ├── helpers.py           # 指纹、历史读写、文件名清理
│   └── filter_profile.py    # persona/阈值读取与类型标准化
├── scripts/                  # 本地脚本（测试/校验）
│   ├── test_filter_logic.py
│   └── check_markdown_structure.py
└── env_detect_and_setup.sh  # 可选：跨环境装配脚本（本次新增）
```

核心入口与职责：

- `web/app.py`
  - 加载 `config.yaml`、初始化目录
  - 定义所有 HTTP 路由
  - 实现按需推送的整体工作流（解析 -> 抓取 -> 过滤 -> 规则/AI 匹配 -> 下载/生成 -> 入库）
  - 实现 AI 对话（`/api/chat`）
- `scraper/*`
  - `ArxivScraper` / `OpenAccessScraper` 负责“拿到候选论文及 PDF 链接”
  - `PaperManager` 负责“写 Markdown、维护历史去重”
- `ai/*`
  - `MiniMaxSummarizer` 负责“把论文元信息/摘要交给模型，生成结构化内容”
  - `MarkdownGenerator` 负责“把结构化内容拼成固定层级 Markdown，并做格式校验”
- `utils/*`
  - `helpers.py` 提供稳定指纹、历史 JSON 的原子写与锁
  - `filter_profile.py` 把 YAML 中 persona/阈值标准化为可用类型
  - `logger.py` 提供统一日志输出

## 安全提醒（非常重要）

- `config.yaml` 里包含敏感信息（`miniMax.api_key`）
- 不要把真实密钥提交到 GitHub
- 如需要公开仓库，建议：
  - 把 `api_key` 改成占位符并在本地自行填写
  - 或者进一步把代码改为从环境变量读取（这需要少量代码改动）

## 说明

本项目只供学术研究使用
