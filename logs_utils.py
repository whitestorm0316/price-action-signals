"""统一日志配置：控制台 + 滚动文件（logs/ 目录，10MB 轮转保留 5 份）。

Usage:
    from logs_utils import get_logger
    log = get_logger("price-action")       # 控制台+文件
    log_err = get_logger("price-action", console=False)   # 只写文件
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / "logs"
_FORMATTER = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
_configured_loggers: set[str] = set()


def get_logger(
    name: str = "price-action",
    level: int = logging.INFO,
    console: bool = True,
    filename: str = "system.log",
) -> logging.Logger:
    """获取配置好的 logger（文件 + 可选控制台），幂等配置。"""
    base = logging.getLogger(name)
    key = f"{name}:{filename}:{console}:{level}"
    if key in _configured_loggers:
        return base
    base.setLevel(level)
    base.propagate = False

    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            _LOG_DIR / filename, maxBytes=5 * 1024 * 1024,
            backupCount=5, encoding="utf-8",
        )
        fh.setFormatter(_FORMATTER)
        base.addHandler(fh)
    except OSError:
        pass  # 文件不可写时至少保留控制台输出

    if console and not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in base.handlers
    ):
        ch = logging.StreamHandler()
        ch.setFormatter(_FORMATTER)
        base.addHandler(ch)

    _configured_loggers.add(key)
    return base
