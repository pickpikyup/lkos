"""Obsidian：识别仓库（含 .obsidian 文件夹的目录）、读取 Obsidian 记录的仓库列表、在 Obsidian 中打开。

Obsidian 的笔记就是普通的 .md 文件，直接按文件夹索引；Obsidian 特有的写法（双链、嵌入、注释、提示块、
属性、标签等）由 lkos.parsers.markdown 处理，白板（.canvas）由 lkos.parsers.canvas 处理。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

CONFIG_DIR = ".obsidian"


def config_file() -> Path:
    """Obsidian 记录仓库列表的文件（obsidian.json）。"""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", Path.home())) / "obsidian" / "obsidian.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "obsidian" / "obsidian.json"


@lru_cache(maxsize=8192)
def _vault_of_dir(d: str) -> str | None:
    p = Path(d)
    for cur in (p, *p.parents):
        try:
            if (cur / CONFIG_DIR).is_dir():
                return str(cur)
        except OSError:
            return None
    return None


def vault_of(path: str | Path) -> str | None:
    """文件所在的 Obsidian 仓库根目录；不在仓库里返回 None。"""
    p = Path(path)
    return _vault_of_dir(str(p if p.is_dir() else p.parent))


def is_vault(folder: str | Path) -> bool:
    try:
        return (Path(folder) / CONFIG_DIR).is_dir()
    except OSError:
        return False


def discover(path: Path | None = None) -> list[dict]:
    """Obsidian 打开过的仓库：[{"path", "name", "exists", "open"}]，读不到时返回空列表。"""
    try:
        data = json.loads(Path(path or config_file()).read_text("utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for v in (data.get("vaults") or {}).values():
        p = str(v.get("path") or "")
        if not p:
            continue
        out.append({"path": p, "name": Path(p).name, "exists": Path(p).is_dir(), "open": bool(v.get("open")),
                    "ts": v.get("ts") or 0})
    out.sort(key=lambda x: -x["ts"])
    return out


def installed() -> bool:
    if sys.platform != "darwin":
        return False
    return any(Path(p).exists() for p in ("/Applications/Obsidian.app",
                                         str(Path.home() / "Applications" / "Obsidian.app")))


def open_url(path: str) -> str:
    return "obsidian://open?path=" + quote(str(path), safe="")


def open_in_obsidian(path: str) -> str | None:
    """在 Obsidian 里打开这条笔记；返回错误信息或 None。"""
    if not Path(path).exists():
        return f"文件已不存在：{path}"
    if not shutil.which("open"):
        return "找不到 open 命令"
    try:
        subprocess.Popen(["open", open_url(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        return f"无法打开 Obsidian：{e}"
    return None
