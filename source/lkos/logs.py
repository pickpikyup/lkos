"""运行日志：写入 <数据目录>/logs/lkos.log（按大小滚动），同时在内存里保留最近的记录，供应用的“日志”页显示。

日志不记录文档全文：问答只记问题、选中片段的文件名 / 位置 / 分数、耗时与 token 数。

保留期限（默认 7 天，“设置”里可改，存在 ui-settings.json 的 log_days）：应用和后台服务启动时、之后每 6 小时，
删掉更早的记录——运行日志（lkos.log 及滚动出去的 .1 … .5）、启动日志（launcher.log）、模型调用记录（call_logs 表，
由调用方用 Repository.prune_calls 清理）。日志页可以一键删除全部日志。
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from collections import deque
from datetime import datetime, timedelta
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
                    "seq": self.seq, "date": logging.Formatter().formatTime(record, "%Y-%m-%d"),
                    "time": logging.Formatter().formatTime(record, "%H:%M:%S"),
                    "ms": int(record.msecs), "level": record.levelname,
                    "key": logging.Formatter().formatTime(record, "%Y-%m-%d %H:%M:%S") + f",{int(record.msecs):03d}", "source": record.name.removeprefix("lkos."),
                    "msg": msg,
                })
        except Exception:  # noqa: BLE001 - 日志本身不能让程序出错
            self.handleError(record)

    def clear(self) -> None:
        with self._lock2:
            self.records.clear()

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


_LINE = None


def read_history(log_dir: Path, before: str, min_level: int = logging.DEBUG, limit: int = 300) -> tuple[list[dict], bool]:
    """从日志文件（含滚动出去的 lkos.log.1 … .5）读 before（"YYYY-mm-dd HH:MM:SS,mmm"）之前的记录，
    按时间从早到晚返回最后 limit 条；第二个值表示更早的还有没有。"""
    import re

    global _LINE
    if _LINE is None:
        _LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),(\d{3}) (\w+)\s+(\S+?): (.*)$")
    files = [Path(log_dir) / "lkos.log"] + [Path(log_dir) / f"lkos.log.{i}" for i in range(1, 10)]
    out: list[dict] = []
    for f in files:                      # 从最新的文件往前读，够了就停
        if not f.exists():
            continue
        try:
            text = f.read_text("utf-8", errors="replace")
        except OSError:
            continue
        entries: list[dict] = []
        for line in text.splitlines():
            m = _LINE.match(line)
            if m:
                d, t, ms, lv, src, msg = m.groups()
                entries.append({"date": d, "time": t, "ms": int(ms), "level": lv, "source": src.removeprefix("lkos."),
                                "msg": msg, "key": f"{d} {t},{ms}"})
            elif entries:
                entries[-1]["msg"] += "\n" + line          # 出错时的调用栈等多行内容
        keep = [e for e in entries if e["key"] < before and logging.getLevelName(e["level"]) >= min_level]
        out = keep + out
        if len(out) > limit:
            return out[-limit:], True
    return out, False


def set_level(debug: bool) -> None:
    logging.getLogger(LOGGER_NAME).setLevel(logging.DEBUG if debug else logging.INFO)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


# ---------- 保留期限与删除 ----------
DEFAULT_LOG_DAYS = 7
PRUNE_EVERY_SECONDS = 6 * 3600
_ENTRY = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} ")
_LAUNCH = re.compile(r"^== (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")


def log_days(home: Path) -> int:
    """日志保留天数（ui-settings.json 的 log_days，1–365，默认 7）。后台服务也按这个清理。"""
    try:
        v = int(json.loads((Path(home) / "ui-settings.json").read_text("utf-8")).get("log_days") or DEFAULT_LOG_DAYS)
    except (OSError, ValueError, TypeError, AttributeError):
        v = DEFAULT_LOG_DAYS
    return max(1, min(365, v))


def log_files(home: Path) -> list[Path]:
    d = Path(home) / "logs"
    return [d / "lkos.log"] + [d / f"lkos.log.{i}" for i in range(1, 10)]


def _keep_since(lines: list[str], cutoff: str, start: re.Pattern) -> list[str]:
    """按“每条以时间开头、后面可能跟多行”切分，只留 cutoff（含）之后的条目。"""
    out, keeping = [], False
    for line in lines:
        m = start.match(line)
        if m:
            keeping = m.group(1) >= cutoff
        if keeping:
            out.append(line)
    return out


def _rewrite(path: Path, lines: list[str]) -> None:
    # 原地截断后写回（同一个文件）：正在写日志的进程以追加方式打开，之后的内容照常写到末尾
    with open(path, "r+", encoding="utf-8") as fh:
        fh.seek(0)
        fh.write("".join(lines))
        fh.truncate()


def prune_log_files(home: Path, days: int, now: datetime | None = None) -> int:
    """删掉 days 天之前的日志行；整个文件都过期的滚动文件直接删除。返回删掉的行数。"""
    cutoff = ((now or datetime.now()) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    removed = 0
    targets = [(p, _ENTRY) for p in log_files(home)] + [(Path(home) / "logs" / "launcher.log", _LAUNCH)]
    for path, start in targets:
        if not path.exists():
            continue
        try:
            lines = path.read_text("utf-8", errors="replace").splitlines(keepends=True)
            keep = _keep_since(lines, cutoff, start)
            if len(keep) == len(lines):
                continue
            removed += len(lines) - len(keep)
            if not keep and path.name not in ("lkos.log", "launcher.log"):
                path.unlink()
            else:
                _rewrite(path, keep)
        except OSError:
            continue
    return removed


def clear_log_files(home: Path) -> None:
    """删除全部日志文件内容：当前的 lkos.log、launcher.log 清空，滚动出去的旧文件删除。"""
    for path in log_files(home) + [Path(home) / "logs" / "launcher.log"]:
        if not path.exists():
            continue
        try:
            if path.name in ("lkos.log", "launcher.log"):
                _rewrite(path, [])
            else:
                path.unlink()
        except OSError:
            continue
    if _ring is not None:
        _ring.clear()


def calls_cutoff(days: int) -> str:
    """call_logs.ts 是 UTC 的 ISO 时间：同样格式的截止时间。"""
    from datetime import timezone

    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
