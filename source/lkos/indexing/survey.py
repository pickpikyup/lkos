"""资料体检：扫描一个文件夹，按扩展名统计文件，说明哪些能索引、哪些不能、为什么、怎么处理。

只看文件名、大小和系统标记，不读文件内容，几万个文件通常几秒钟；结果只在本机界面上显示。
- 能索引：有解析器，且没被文件夹设置排除、没超过大小上限；
- 需要处理：认识但解析不了（.ppt、.xls、WPS、iWork），给出另存为的办法；
- 不支持：图片、音视频、压缩包等，按类别说明；
- 另外列出：隐藏文件、iCloud 中还没下载到本机的文件、跳过的程序包 / 图库文件夹、发现的 Obsidian 仓库。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from lkos.indexing.indexer import is_excluded, skip_dir
from lkos.parsers import parser_for

SF_DATALESS = 0x40000000          # macOS：文件内容只在 iCloud，本机只有占位
CATEGORIES = [
    ("图片", {"jpg", "jpeg", "png", "heic", "heif", "gif", "webp", "tif", "tiff", "bmp", "raw", "cr2", "nef", "arw",
             "dng", "psd", "ai", "sketch", "fig", "ico", "icns"},
     "图片里的文字暂不识别；如果是拍的文档，可以合成 PDF 后在文件夹设置里打开“文字识别”"),
    ("音频 / 视频", {"mp3", "m4a", "wav", "aac", "flac", "ogg", "amr", "mp4", "mov", "m4v", "avi", "mkv", "wmv", "flv",
                   "webm", "3gp"}, "不含可检索的文字"),
    ("压缩包", {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "dmg", "iso"}, "解压后，里面的文件才能索引"),
    ("电子书（其他格式）", {"mobi", "azw", "azw3", "kfx", "djvu", "fb2", "cbz", "cbr"},
     "暂不支持；能转成 EPUB 或 PDF 的话就可以索引（如用 Calibre 转换）"),
    ("思维导图", {"xmind", "mm", "mmap", "opml"}, "暂不支持；可以从思维导图软件导出为 Markdown 或 PDF"),
    ("代码 / 数据", {"py", "js", "ts", "java", "c", "cpp", "h", "go", "rs", "rb", "php", "sh", "sql", "json", "xml",
                  "yaml", "yml", "toml", "ini", "log", "ipynb", "r", "m", "swift", "kt", "css", "scss", "db",
                  "sqlite", "parquet"}, "代码和数据文件暂不索引"),
    ("邮件", {"eml", "emlx", "msg", "mbox", "pst"}, "暂不支持；可以把重要邮件存为 PDF"),
    ("快捷方式 / 系统文件", {"lnk", "url", "webloc", "alias", "ds_store", "localized", "plist", "icloud", "tmp", "bak",
                        "part", "crdownload", "download"}, "不是资料，跳过"),
]


def _category(ext: str) -> tuple[str, str]:
    for name, exts, hint in CATEGORIES:
        if ext in exts:
            return name, hint
    return "其他", "暂不支持这种格式"


def survey(folder: str | Path, extensions: list[str], max_mb: float, exclude: dict | None = None,
           skip_hidden: bool = True, max_files: int = 500_000, max_seconds: float = 120) -> dict:
    folder = Path(folder).expanduser().resolve()
    t0 = time.monotonic()
    patterns = (exclude or {}).get("patterns") or []
    skip_types = {"." + t for t in (exclude or {}).get("types") or []}
    max_bytes = max_mb * 1024 * 1024
    by_ext: dict[str, dict] = {}
    extra = {"hidden": 0, "cloud_only": 0, "cloud_placeholders": 0, "skipped_dirs": [], "vaults": [], "too_large": []}
    total = files = 0
    truncated = False

    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        d = Path(dirpath)
        if ".obsidian" in dirnames:
            extra["vaults"].append(str(d))
        rel_dir = d.relative_to(folder)
        keep = []
        for name in sorted(dirnames):
            if name.startswith(".") and skip_hidden:
                continue
            if skip_dir(name, filenames):
                if len(extra["skipped_dirs"]) < 50:
                    extra["skipped_dirs"].append(str(rel_dir / name))
                continue
            if patterns and is_excluded(rel_dir / name, patterns):
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            files += 1
            if files > max_files or time.monotonic() - t0 > max_seconds:
                truncated = True
                break
            p = d / name
            if name.startswith("~$"):                 # Office / WPS 打开文件时的临时锁文件
                continue
            if name.startswith("."):
                if name.endswith(".icloud"):          # 旧版 macOS：还没下载的 iCloud 文件是 .名字.icloud 占位
                    extra["cloud_placeholders"] += 1
                elif name not in (".DS_Store", ".localized"):
                    extra["hidden"] += 1
                if skip_hidden:
                    continue
            try:
                st = p.lstat()
            except OSError:
                continue
            if p.is_symlink():
                continue
            total += st.st_size
            ext = p.suffix.lower()
            key = ext.lstrip(".") or "(无扩展名)"
            row = by_ext.setdefault(key, {"ext": key, "count": 0, "bytes": 0, "indexable": 0, "excluded": 0,
                                          "too_large": 0, "cloud_only": 0, "status": "", "label": "", "hint": ""})
            row["count"] += 1
            row["bytes"] += st.st_size
            if getattr(st, "st_flags", 0) & SF_DATALESS:
                row["cloud_only"] += 1
                extra["cloud_only"] += 1
            parser = parser_for(p, extensions) if ext else None
            if parser is None:
                continue
            if ext in skip_types or (patterns and is_excluded(rel_dir / name, patterns)):
                row["excluded"] += 1
            elif st.st_size > max_bytes:
                row["too_large"] += 1
                if len(extra["too_large"]) < 20:
                    extra["too_large"].append({"path": str(p), "bytes": st.st_size})
            elif getattr(parser, "supported", True):
                row["indexable"] += 1
        if truncated:
            break

    from lkos.parsers.base import _registry

    known = {e.lstrip("."): p for p in _registry() for e in p.extensions if e in extensions}
    rows = []
    for key, row in by_ext.items():
        p = known.get(key)
        if p is not None and getattr(p, "supported", True):
            row["status"], row["label"] = "ok", getattr(p, "label", key.upper())
            notes = []
            if row["excluded"]:
                notes.append(f"{row['excluded']} 个被文件夹设置排除")
            if row["too_large"]:
                notes.append(f"{row['too_large']} 个超过 {max_mb:g} MB 上限")
            row["hint"] = "；".join(notes)
        elif p is not None:
            row["status"], row["label"] = "convert", getattr(p, "label", key.upper())
            try:
                list(p.parse(Path("x." + key)))
            except Exception as e:  # noqa: BLE001 - 取“另存为”的提示文字
                row["hint"] = str(e)
        else:
            row["status"] = "unsupported"
            row["label"], row["hint"] = _category(key)
        rows.append(row)
    order = {"ok": 0, "convert": 1, "unsupported": 2}
    rows.sort(key=lambda r: (order[r["status"]], -r["count"], r["ext"]))
    return {"folder": str(folder), "files": sum(r["count"] for r in rows), "bytes": total,
            "indexable": sum(r["indexable"] for r in rows),
            "convert": sum(r["count"] for r in rows if r["status"] == "convert"),
            "unsupported": sum(r["count"] for r in rows if r["status"] == "unsupported"),
            "types": rows, "truncated": truncated, "seconds": round(time.monotonic() - t0, 1), **extra}
