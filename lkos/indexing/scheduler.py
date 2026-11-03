"""闲时索引：只在“接通电源 + 用户空闲 + 没有在问答”时计算向量，否则暂停等待。

原理：建索引（bge-m3）和问答（Qwen）都跑在同一块 GPU 上，8 GB 统一内存也只够其中一边宽裕地用。
两者同时运行不会更快，只会互相拖慢、加剧换页。所以索引让路：
- 问答进程在检索 / 生成期间写一个活动标记文件（data/qa.active），结束后删除；
  索引进程每个文件、每批向量之前检查它，标记存在就暂停，直到问答结束；
- 用户正在用电脑（键盘鼠标最近 idle_seconds 秒内有操作）时暂停，空闲后继续；
- 使用电池时暂停，接通电源后继续。

macOS 上用 `pmset -g batt` 判断电源、用 `ioreg -c IOHIDSystem` 读取 HIDIdleTime；
其他系统没有这些命令时视为“已接电源、已空闲”。探针都可以注入，便于测试。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

# ---------------- 问答活动标记 ----------------


class ActivityMarker:
    """问答进程在检索 / 生成期间持有的标记文件。同一进程内可嵌套（检索包在问答里）。

    标记文件写入进程号与时间；超过 stale_seconds 未更新的标记视为残留（比如问答进程崩溃），会被忽略。
    """

    def __init__(self, path: Path, stale_seconds: float = 300):
        self.path = Path(path)
        self.stale_seconds = stale_seconds
        self._depth = 0
        self._lock = threading.Lock()

    @contextmanager
    def active(self) -> Iterator[None]:
        with self._lock:
            self._depth += 1
            if self._depth == 1:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(f"{os.getpid()} {time.time():.0f}\n", encoding="utf-8")
        try:
            yield
        finally:
            with self._lock:
                self._depth -= 1
                if self._depth == 0:
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass

    def is_active(self, now: float | None = None) -> bool:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return False
        return ((now or time.time()) - mtime) < self.stale_seconds


# ---------------- 系统探针（macOS） ----------------


def _run(cmd: list[str]) -> str | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def mac_on_ac_power() -> bool:
    out = _run(["pmset", "-g", "batt"])
    if out is None:
        return True  # 没有 pmset（非 macOS）：不限制
    return "AC Power" in out or "Battery Power" not in out


def mac_idle_seconds() -> float:
    out = _run(["ioreg", "-c", "IOHIDSystem", "-d", "4"])
    if not out:
        return float("inf")  # 读不到：不限制
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
    return int(m.group(1)) / 1e9 if m else float("inf")


def default_probes() -> tuple[Callable[[], bool], Callable[[], float]]:
    if sys.platform == "darwin":
        return mac_on_ac_power, mac_idle_seconds
    return (lambda: True), (lambda: float("inf"))


# ---------------- 闸门 ----------------


@dataclass
class GateConfig:
    idle_seconds: float = 120       # 用户无操作多久后才开始索引
    require_ac_power: bool = True   # 使用电池时暂停
    poll_seconds: float = 5         # 暂停时多久检查一次


class IndexGate:
    """索引进程在每个文件、每批向量之前调用 wait()；条件不满足就阻塞等待。"""

    def __init__(self, cfg: GateConfig, marker: ActivityMarker,
                 on_ac: Callable[[], bool] | None = None, idle: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 on_pause: Callable[[str], None] | None = None):
        default_ac, default_idle = default_probes()
        self.cfg = cfg
        self.marker = marker
        self.on_ac = on_ac or default_ac
        self.idle = idle or default_idle
        self.sleep = sleep
        self.on_pause = on_pause
        self.paused_seconds = 0.0
        self.pauses: dict[str, int] = {"qa": 0, "busy": 0, "battery": 0}

    def blocker(self) -> str | None:
        """返回当前不能索引的原因：qa（正在问答）/ battery（用电池）/ busy（用户在用电脑）；可以索引时返回 None。"""
        if self.marker.is_active():
            return "qa"
        if self.cfg.require_ac_power and not self.on_ac():
            return "battery"
        if self.idle() < self.cfg.idle_seconds:
            return "busy"
        return None

    def wait(self) -> None:
        last: str | None = None
        while (reason := self.blocker()) is not None:
            if reason != last:
                self.pauses[reason] += 1
                if self.on_pause:
                    self.on_pause(reason)
                last = reason
            self.sleep(self.cfg.poll_seconds)
            self.paused_seconds += self.cfg.poll_seconds
        if last is not None and self.on_pause:
            self.on_pause("resume")


REASONS = {
    "qa": "正在问答，索引暂停",
    "battery": "使用电池，接通电源后继续",
    "busy": "检测到你在使用电脑，空闲后继续",
    "resume": "继续索引",
}
