"""Obsidian 白板（.canvas，JSON Canvas 格式）：文字卡片、引用的笔记、网页链接、分组名、连线上的文字。

卡片按所在分组（位置落在分组框里）归类，引用里显示分组名；卡片按从上到下、从左到右的顺序输出。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from lkos.errors import ParseError
from lkos.parsers.base import decode_text
from lkos.parsers.markdown import parse_markdown_text
from lkos.types import Block, Location


def _inside(n: dict, g: dict) -> bool:
    try:
        return (g["x"] <= n["x"] and g["y"] <= n["y"] and n["x"] + n.get("width", 0) <= g["x"] + g.get("width", 0)
                and n["y"] + n.get("height", 0) <= g["y"] + g.get("height", 0))
    except (KeyError, TypeError):
        return False


def _label(n: dict) -> str:
    t = n.get("type")
    if t == "text":
        return (n.get("text") or "").strip().split("\n", 1)[0].lstrip("# ").strip()[:40]
    if t == "file":
        return Path(str(n.get("file") or "")).stem
    if t == "link":
        return str(n.get("url") or "")
    return str(n.get("label") or "")


class CanvasParser:
    extensions = (".canvas",)
    file_type = "canvas"
    label = "Obsidian 白板"

    def parse(self, path: Path) -> Iterator[Block]:
        try:
            data = json.loads(decode_text(Path(path).read_bytes()) or "{}")
        except ValueError as e:
            raise ParseError(f"白板文件格式不对：{e}") from e
        nodes = [n for n in data.get("nodes") or [] if isinstance(n, dict)]
        groups = [n for n in nodes if n.get("type") == "group"]
        # 最小的分组优先（嵌套分组时归到最里层）
        groups.sort(key=lambda g: (g.get("width", 0) or 0) * (g.get("height", 0) or 0))

        def group_of(n: dict) -> str | None:
            names = [str(g.get("label") or "").strip() for g in groups if g is not n and _inside(n, g)]
            names = [x for x in names if x]
            return " > ".join(reversed(names)) or None

        for g in groups:
            if str(g.get("label") or "").strip():
                yield Block(text=str(g["label"]).strip(), location=Location(heading_path=group_of(g)))
        cards = sorted((n for n in nodes if n.get("type") != "group"),
                       key=lambda n: (n.get("y", 0) or 0, n.get("x", 0) or 0))
        for n in cards:
            head = group_of(n)
            t = n.get("type")
            if t == "text":
                for b in parse_markdown_text(str(n.get("text") or "")):
                    hp = " > ".join(x for x in (head, b.location.heading_path) if x) or None
                    yield Block(text=b.text, location=Location(heading_path=hp))
            elif t == "file":
                f = str(n.get("file") or "")
                if f:
                    yield Block(text=f"（卡片引用：{Path(f).stem}）", location=Location(heading_path=head))
            elif t == "link" and n.get("url"):
                yield Block(text=f"（网页：{n['url']}）", location=Location(heading_path=head))
        by_id = {n.get("id"): n for n in nodes}
        for e in data.get("edges") or []:
            if isinstance(e, dict) and str(e.get("label") or "").strip():
                a, b = by_id.get(e.get("fromNode"), {}), by_id.get(e.get("toNode"), {})
                yield Block(text=f"{_label(a)} → {_label(b)}：{str(e['label']).strip()}", location=Location())
