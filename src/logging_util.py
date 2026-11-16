"""确定性日志：默认写 results/run.log，可关时间戳便于复跑比对。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from . import config as cfg

_LOGGER_NAME = "f3_allocation"
_FORMAT_WITH_TIME = "%(asctime)s %(levelname)s %(name)s %(message)s"
_FORMAT_NO_TIME = "%(levelname)s %(name)s %(message)s"


def setup_logging(
    log_path: Optional[Path] = None,
    no_timestamp: bool = False,
    level: int = logging.INFO,
) -> logging.Logger:
    """初始化全局 logger：文件 + 控制台双通道，重复调用不会叠加 handler。

    ``no_timestamp=True`` 时日志不含时间戳，两次运行内容可逐行比对。
    """
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except OSError:
            pass

    fmt = logging.Formatter(
        _FORMAT_NO_TIME if no_timestamp else _FORMAT_WITH_TIME,
        datefmt=cfg.TIME_FORMAT,
    )

    if log_path is not None:
        path = Path(log_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(path, mode="w", encoding=cfg.CSV_ENCODING)
            file_handler.setLevel(level)
            file_handler.setFormatter(fmt)
            logger.addHandler(file_handler)
        except OSError as exc:
            # 结果目录不可写时仍保证控制台可用，由主流程按第五节上报
            sys.stderr.write(f"日志文件不可写 {path}: {exc}\n")

    stream_handler = logging.StreamHandler(stream=sys.stdout)
    stream_handler.setLevel(level)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)
    return logger


def get_logger() -> logging.Logger:
    """返回全局 logger；未初始化时挂一个 NullHandler，避免 No handler 警告。"""
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def log_issue(logger: logging.Logger, issue: Any) -> None:
    """把一条 DataIssue 写成一行 info 日志。"""
    logger.info(
        "数据边界 [%s] 位置=%s 原值=%s 处理=%s",
        issue.category, issue.location, issue.raw_value, issue.action,
    )


__all__ = ["setup_logging", "get_logger", "log_issue"]
