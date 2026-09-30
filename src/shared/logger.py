"""日志配置。

- 控制台输出便于命令行调试
- 同时写入 `logs/app.log`（按大小轮转），前端可通过 /api/logs 查看
"""

from __future__ import annotations

import collections
import logging
import logging.handlers
import sys
import threading

from .path_helpers import LOGS_DIR, ensure_dirs

_CONFIGURED = False
LOG_FILE = "app.log"
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"
_MEMORY_LIMIT = 500


class MemoryLogHandler(logging.Handler):
    """把最近的日志留在内存里，供前端 `/api/logs` 查看。"""

    def __init__(self, limit: int = _MEMORY_LIMIT):
        super().__init__()
        self.records: collections.deque = collections.deque(maxlen=limit)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            return
        with self._lock:
            self.records.append(message)

    def tail(self, lines: int = 200) -> str:
        with self._lock:
            items = list(self.records)
        return "\n".join(items[-max(1, lines):])


memory_handler = MemoryLogHandler()


def setup_logging(level: int = logging.INFO, quiet: bool = False) -> logging.Logger:
    """初始化根日志器（幂等，多次调用只生效一次）。"""
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED:
        return root

    ensure_dirs()
    root.setLevel(logging.DEBUG)
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    if not quiet:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        console.setLevel(level)
        root.addHandler(console)

    memory_handler.setFormatter(formatter)
    memory_handler.setLevel(logging.INFO)
    root.addHandler(memory_handler)

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            LOGS_DIR / LOG_FILE,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        root.addHandler(file_handler)
    except Exception:  # 日志文件不可写不应导致程序启动失败
        root.warning("无法写入日志文件，仅输出到控制台", exc_info=True)

    # 降噪：第三方库的 INFO 日志太多
    for noisy in ("urllib3", "requests", "werkzeug", "PIL", "fitz", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    return root


def get_recent_logs(lines: int = 200) -> str:
    """返回最近的日志文本（内存缓冲）。"""
    return memory_handler.tail(lines)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
