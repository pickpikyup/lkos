"""运行日志：写入 <数据目录>/logs/lkos.log（按大小滚动），同时在内存里保留最近的记录，供应用的“日志”页显示。

日志不记录文档全文：问答只记问题、选中片段的文件名 / 位置 / 分数、耗时与 token 数。
"""

from __future__ import annotations

import logging
import sys
import threading
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "lkos"
FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class RingHandler(logging.Handler):
    """内存中的最近 N 条日志，每条带递增序号，界面按序号增量拉取。"""

    def __init__(self, capacity: int = 3000):
        super().__init__(level=logging.DEBUG)
        self.records: deque[dict] = deque(maxlen=capacity)
        self.seq = 0
        self._lock2 = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += "\n" + logging.Formatter().formatException(record.exc_info)
            with self._lock2:
                self.seq += 1
                self.records.append({
                    "seq": self.seq, "time": logging.Formatter().formatTime(record, "%H:%M:%S"),
                    "ms": int(record.msecs), "level": record.levelname, "source": record.name.removeprefix("lkos."),
                    "msg": msg,
                })
        except Exception:  # noqa: BLE001 - 日志本身不能让程序出错
            self.handleError(record)

    def since(self, seq: int, min_level: int = logging.DEBUG, limit: int = 1000) -> list[dict]:
        with self._lock2:
            items = [r for r in self.records if r["seq"] > seq and logging.getLevelName(r["level"]) >= min_level]
        return items[-limit:]


_ring: RingHandler | None = None


def setup_logging(home: Path, level: int = logging.INFO) -> RingHandler:
    """配置 lkos 日志（重复调用只生效一次）。返回内存日志，供界面读取。"""
    global _ring
    logger = logging.getLogger(LOGGER_NAME)
    if _ring is not None:
        return _ring
    log_dir = Path(home) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(log_dir / "lkos.log", maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(FORMAT))
    file_handler.setLevel(logging.DEBUG)
    _ring = RingHandler()
    logger.addHandler(file_handler)
    logger.addHandler(_ring)
    logger.setLevel(level)
    logger.propagate = False

    def hook(args: threading.ExceptHookArgs) -> None:  # 后台线程里没被捕获的异常也记下来
        logger.error("线程 %s 出错", args.thread.name if args.thread else "?",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = hook
    prev = sys.excepthook

    def sys_hook(t, v, tb):
        logger.critical("未处理的错误", exc_info=(t, v, tb))
        prev(t, v, tb)

    sys.excepthook = sys_hook
    return _ring


def set_level(debug: bool) -> None:
    logging.getLogger(LOGGER_NAME).setLevel(logging.DEBUG if debug else logging.INFO)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
