"""
统一日志配置。

目标：
- 所有检索与分析流程写入项目根目录的 server.log
- 兼容在 Flask 进程与独立脚本中使用
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


_CONFIGURED = False


def get_log_path(project_root: str | Path | None = None) -> Path:
    root = Path(project_root) if project_root else Path(__file__).parent.parent
    return (root / "server.log").resolve()


def configure_logging(project_root: str | Path | None = None) -> logging.Logger:
    """配置全局日志（幂等）。返回统一 logger。"""
    global _CONFIGURED
    logger = logging.getLogger("paper_assistant")
    if _CONFIGURED:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_path = get_log_path(project_root)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        filename=str(log_path),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    # 避免重复添加 handler
    logger.handlers = []
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    _CONFIGURED = True
    logger.info("日志系统已初始化，写入 %s", str(log_path))
    return logger

