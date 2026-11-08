"""资料库里的文件夹在访达里改名或移动后，自动认出来并更新位置（索引、设置、标签都保留，不用重建）。

怎么认：添加时记下文件夹的“身份”（所在磁盘 + 文件编号 inode）。改名、在同一块磁盘上移动都不会改变它。
文件夹找不到时，从原位置最近的、还存在的上级文件夹往下找同一身份的文件夹（只找几层，几千个文件夹以内），
再核对原来索引过的文件在新位置下确实存在，才算找到。改名前的路径记下来，以前对话里的出处照样能打开。
找不到（移到了别的磁盘、被删除）时由界面提示“重新定位”或“移除”。
"""

from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path

from lkos.logs import get_logger

log = get_logger("indexer")
MAX_DIRS = 5000
RELOCATIONS = "relocations"          # index_meta：[[旧路径, 新路径], ...]


def folder_ident(path: str | Path) -> str | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{st.st_dev}:{st.st_ino}"


def find_moved(old_path: str, ident: str, check: list[str] | None = None) -> str | None:
    """在原位置附近找身份相同的文件夹。check：原来索引过的几个文件路径，用来核对。"""
    old = Path(old_path)
    anchor, missing = old.parent, 1
    while not anchor.is_dir():
        if anchor.parent == anchor:
            return None
        anchor, missing = anchor.parent, missing + 1
    roots = [anchor]
    trash = Path.home() / ".Trash"
    if trash.is_dir() and trash != anchor:
        roots.append(trash)          # 只用来认出“被扔进废纸篓了”，不会自动定位过去
    seen = 0
    for root in roots:
        queue = deque([(root, 0)])
        limit = missing + 2 if root == anchor else 1
        while queue:
            d, depth = queue.popleft()
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                if not e.is_dir(follow_symlinks=False) or e.name.startswith("."):
                    continue
                seen += 1
                if seen > MAX_DIRS:
                    return None
                if folder_ident(e.path) == ident:
                    cand = os.path.realpath(e.path)
                    if root == trash:
                        log.info("文件夹 %s 已被移到废纸篓", old_path)
                        return None
                    if _verify(old_path, cand, check):
                        return cand
                if depth + 1 < limit:
                    queue.append((Path(e.path), depth + 1))
    return None


def _verify(old: str, new: str, check: list[str] | None) -> bool:
    if not check:
        return True
    rels = [os.path.relpath(p, old) for p in check]
    return any(os.path.exists(os.path.join(new, r)) for r in rels)


def remember_idents(repo) -> None:
    """给还没记下身份（或身份变了，如外接硬盘重新接上）的来源记下身份。"""
    for s in repo.list_sources():
        if Path(s["path"]).is_dir():
            ident = folder_ident(s["path"])
            if ident and ident != s.get("ident"):
                repo.set_source_ident(s["id"], ident)


def relocate_missing(repo) -> list[dict]:
    """找不到的来源：能认出新位置就更新。返回 [{"old", "new"}]。"""
    moved = []
    sources = repo.list_sources()
    paths = {s["path"] for s in sources}
    for s in sources:
        if Path(s["path"]).is_dir() or not s.get("ident"):
            continue
        check = [f.path for f in repo.files_of_source(s["id"])[:20]]
        new = find_moved(s["path"], s["ident"], check)
        if not new or new == s["path"]:
            continue
        if new in paths:                 # 新位置已经单独添加过：旧的这一项是多余的
            log.info("文件夹 %s 已改名为 %s（新位置已在资料库里），移除旧的一项", s["path"], new)
            repo.remove_source(s["id"])
            add_relocation(repo, s["path"], new)
        else:
            repo.relocate_source(s["id"], new)
            log.info("认出改名 / 移动的文件夹：%s → %s", s["path"], new)
        moved.append({"old": s["path"], "new": new})
    remember_idents(repo)
    return moved


def add_relocation(repo, old: str, new: str) -> None:
    try:
        items = json.loads(repo.get_meta(RELOCATIONS) or "[]")
    except ValueError:
        items = []
    items = [x for x in items if x[0] != old] + [[old, new]]
    repo.set_meta(RELOCATIONS, json.dumps(items[-200:], ensure_ascii=False))


def resolve_path(repo, path: str) -> str:
    """改名前的路径 → 现在的路径（以前对话里的出处用）；没有对应时原样返回。"""
    try:
        items = json.loads(repo.get_meta(RELOCATIONS) or "[]")
    except ValueError:
        return path
    for _ in range(5):                   # 改过几次名也能接上
        for old, new in reversed(items):
            if path == old or path.startswith(old.rstrip(os.sep) + os.sep):
                path = new + path[len(old):]
                break
        else:
            break
    return path
