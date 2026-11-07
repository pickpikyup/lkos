"""索引流程编排（设计文档第 9 节）。

按文件逐个提交事务：未变化的跳过，改名/移动的只改路径，变化的重新解析并整体替换文本块；
只有完整扫描（未设 limit）时才清理磁盘上已删除的文件。中断后重跑即自然续传。
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from lkos.config import Config, make_chunker
from lkos.errors import EncryptedFileError, LkosError, ParseError, UnsupportedFileError
from lkos.indexing.chunker import Chunker
from lkos.indexing.tokenize import to_fts_tokens
from lkos.logs import get_logger
from lkos.parsers import parse_file, parser_for
from lkos.sources import apple_notes
from lkos.storage.vectors import text_hash
from lkos.storage.repository import IndexRun, Repository, now_iso
from lkos.types import FileInfo, ProgressEvent

ProgressCallback = Callable[[ProgressEvent], None]


@dataclass
class IndexReport:
    run_id: int
    files_seen: int
    files_indexed: int
    files_skipped: int
    files_moved: int
    files_failed: int
    files_removed: int
    chunks_added: int
    seconds: float
    chunks_reused: int = 0
    parse_seconds: float = 0.0
    embed_seconds: float = 0.0
    vectors_pruned: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    cleanup_done: bool = True
    rechunked: bool = False   # 切块参数变化，本次重新切块了这个来源的所有文件


def sha256_of(path: Path, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(bufsize):
            h.update(chunk)
    return h.hexdigest()


def norm_path(path: Path) -> str:
    """统一为 NFC：macOS 文件名可能是 NFD，避免同一中文文件名出现两条记录。"""
    return unicodedata.normalize("NFC", str(path))


log = get_logger("index")


def is_excluded(rel: Path, patterns: list[str]) -> bool:
    """排除规则：不含“/”的规则匹配任意一级的文件夹名或文件名（如“归档”“*草稿*”“*.tmp”）；
    含“/”的规则匹配相对文件夹根的路径（如“2019/*”“项目/旧版”）。不区分大小写。"""
    parts = [x.lower() for x in rel.parts]
    full = "/".join(parts)
    for pat in patterns:
        pat = pat.strip().strip("/").lower()
        if not pat:
            continue
        if "/" in pat:
            if fnmatch.fnmatchcase(full, pat) or fnmatch.fnmatchcase(full, pat + "/*"):
                return True
        elif any(fnmatch.fnmatchcase(x, pat) for x in parts):
            return True
    return False


class Indexer:
    def __init__(self, cfg: Config, repo: Repository, gateway, chunker: Chunker | None = None):
        self.cfg = cfg
        self.repo = repo
        self.gateway = gateway
        self.chunker = chunker or make_chunker(cfg.indexing)
        self._ocr_engine = None
        self._ocr_resolved = False
        self.home: Path | None = None        # 数据目录（Mac 备忘录导出在 <home>/apple-notes）
        self.notes_fetch = None              # 读取备忘录的函数（测试可替换；默认用 osascript）

    @property
    def ocr_engine(self):
        """文字识别引擎（macOS Vision）；不可用时为 None。测试可以直接赋值替换。"""
        if not self._ocr_resolved:
            from lkos.parsers import ocr

            self._ocr_engine, self._ocr_resolved = ocr.engine(), True
        return self._ocr_engine

    @ocr_engine.setter
    def ocr_engine(self, fn) -> None:
        self._ocr_engine, self._ocr_resolved = fn, True

    # ---------- 遍历 ----------
    def walk(self, folder: Path) -> Iterator[Path]:
        """文件夹里要索引的文件（按这个文件夹的排除设置过滤）。"""
        folder = Path(folder)
        settings = self.repo.source_settings(norm_path(folder.expanduser().resolve()))
        exclude = settings["exclude"] if settings else {"patterns": [], "types": []}
        files = list(self._walk(folder, exclude))
        if self.cfg.indexing.order == "recent_first":
            # 最近修改的先处理：闲时索引被打断时，最常用的文档已经可以检索
            files.sort(key=lambda p: (-p.stat().st_mtime, str(p)))
        yield from files

    def _walk(self, folder: Path, exclude: dict | None = None) -> Iterator[Path]:
        max_bytes = self.cfg.indexing.max_file_size_mb * 1024 * 1024
        patterns = (exclude or {}).get("patterns") or []
        skip_types = {"." + t for t in (exclude or {}).get("types") or []}
        for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
            if self.cfg.indexing.skip_hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            rel_dir = Path(dirpath).relative_to(folder)
            if patterns:
                dirnames[:] = [d for d in dirnames if not is_excluded(rel_dir / d, patterns)]
            dirnames.sort()
            for name in sorted(filenames):
                if self.cfg.indexing.skip_hidden and name.startswith("."):
                    continue
                if skip_types and Path(name).suffix.lower() in skip_types:
                    continue
                if patterns and is_excluded(rel_dir / name, patterns):
                    continue
                if name.startswith("~$"):  # Office / WPS 锁文件
                    continue
                p = Path(dirpath) / name
                if p.is_symlink() or not p.is_file():
                    continue
                if parser_for(p, self.cfg.indexing.extensions) is None:
                    continue
                if p.stat().st_size > max_bytes:
                    continue
                yield p

    # ---------- 主流程 ----------
    def index_folder(self, folder: Path, limit: int | None = None,
                     on_progress: ProgressCallback | None = None, gate=None) -> IndexReport:
        """gate：闲时索引闸门（lkos.indexing.scheduler.IndexGate），每个文件、每批向量前检查，条件不满足时暂停。"""
        t0 = time.perf_counter()
        folder = Path(folder).expanduser().resolve()
        if self.home is not None and apple_notes.is_notes_dir(folder, self.home):
            # Mac 备忘录：先把备忘录导出成 Markdown（只重写有变化的），再按普通文件夹索引
            try:
                stats = apple_notes.sync(self.home, self.notes_fetch)
                log.info("备忘录已同步：%d 条，更新 %d、删除 %d", stats["total"], stats["written"], stats["removed"])
            except LkosError as e:            # 读不到备忘录（没授权、不在 macOS）：照旧索引上次导出的内容
                log.warning("同步备忘录失败，使用上次导出的内容：%s", e)
        if not folder.is_dir():
            raise ParseError(f"文件夹不存在：{folder}")
        source_id = self.repo.upsert_source(norm_path(folder))
        sig_key, pending_key = f"chunker:{source_id}", f"rechunk:{source_id}"
        signature = self.chunker.signature()
        # 切块参数变化（或旧索引没有记录参数）：即使文件没变也重新切块；未改动文本的向量从缓存复用。
        # 重新切块开始的时间记在 rechunk:<来源>，中断后续跑时，在这之后索引过的文件不再重做。
        rechunk_since: str | None = None
        if self.repo.get_meta(sig_key) != signature:
            if self.repo.files_of_source(source_id):
                pending = self.repo.get_meta(pending_key) or ""
                if pending.startswith(signature + "@"):
                    rechunk_since = pending.split("@", 1)[1]
                else:
                    rechunk_since = now_iso(precise=True)
                    self.repo.set_meta(pending_key, f"{signature}@{rechunk_since}")
            else:
                self.repo.set_meta(sig_key, signature)  # 新来源：直接按当前参数切块
        rechunk = rechunk_since is not None
        # 文字识别：文件夹设置里打开、且本机可用时，扫描页做识别；打开之前索引过的 PDF 重新处理一次
        settings = self.repo.source_settings(norm_path(folder)) or {}
        ocr = self.ocr_engine if settings.get("ocr") else None
        ocr_since = self.repo.get_meta(f"ocr:{source_id}") if ocr else None
        run = self.repo.start_run(source_id)
        seen: set[str] = set()
        failures: list[tuple[str, str]] = []

        def emit(path: str, action: str, detail: str | None = None) -> None:
            if on_progress:
                on_progress(ProgressEvent(path=path, action=action, files_seen=run.files_seen, detail=detail))

        try:
            for p in self.walk(folder):
                if limit is not None and run.files_seen >= limit:
                    break
                run.files_seen += 1
                path = norm_path(p)
                seen.add(path)
                if gate is not None:
                    gate.wait()
                action, detail = self._index_one(p, path, source_id, run, gate, rechunk_since, ocr, ocr_since)
                if action == "failed":
                    failures.append((path, detail or ""))
                emit(path, action, detail)
        except BaseException:  # 包括 Ollama 不可用和 Ctrl-C：已提交的文件保留，下次续传
            self.repo.finish_run(run, aborted=True)
            raise

        cleanup = limit is None
        if cleanup:
            removed = [f.id for f in self.repo.files_of_source(source_id) if f.path not in seen]
            self.repo.remove_files(removed)
            run.files_removed = len(removed)
        pruned = self.repo.prune_vectors() if (run.files_indexed or run.files_removed) else 0
        if rechunk and cleanup:  # 只处理了一部分文件（--limit）时，旧参数切的块还没换完，下次继续
            self.repo.set_meta(sig_key, signature)
            self.repo.conn.execute("DELETE FROM index_meta WHERE key = ?", (pending_key,))
        self.repo.finish_run(run)
        return IndexReport(
            run_id=run.id, files_seen=run.files_seen, files_indexed=run.files_indexed,
            files_skipped=run.files_skipped, files_moved=run.files_moved, files_failed=run.files_failed,
            files_removed=run.files_removed, chunks_added=run.chunks_added,
            seconds=round(time.perf_counter() - t0, 2), failures=failures, cleanup_done=cleanup,
            chunks_reused=run.chunks_reused, parse_seconds=round(run.parse_ms / 1000, 2),
            embed_seconds=round(run.embed_ms / 1000, 2), vectors_pruned=pruned, rechunked=rechunk,
        )

    def _index_one(self, p: Path, path: str, source_id: int, run: IndexRun, gate=None,
                   rechunk_since: str | None = None, ocr=None, ocr_since: str | None = None) -> tuple[str, str | None]:
        parser = parser_for(p, self.cfg.indexing.extensions)
        st = p.stat()
        record = self.repo.get_file(path)
        done = record is not None and record.status in ("indexed", "skipped")
        if done and rechunk_since and (record.indexed_at or "") < rechunk_since:
            done = False  # 用旧切块参数索引的文件：重新切块
        use_ocr = ocr if parser.file_type == "pdf" else None
        if done and use_ocr and ocr_since and (record.indexed_at or "") < ocr_since:
            done = False  # 打开文字识别之前索引的 PDF：扫描页还没识别
        # 大小与修改时间都没变时不必重新计算哈希（全量 2–4 GB 时能省下大量读盘）
        if done and record.size_bytes == st.st_size and record.mtime == st.st_mtime:
            run.files_skipped += 1
            return "unchanged", None

        content_hash = sha256_of(p)
        info = FileInfo(path=path, file_type=parser.file_type, content_hash=content_hash,
                        size_bytes=st.st_size, mtime=st.st_mtime)
        if done and record.content_hash == content_hash and not (use_ocr and ocr_since
                                                                and (record.indexed_at or "") < ocr_since):  # 只是 touch 过
            self.repo.touch_file(record.id, st.st_mtime)
            run.files_skipped += 1
            return "unchanged", None

        if record is None:
            moved = self.repo.find_missing_by_hash(content_hash)
            if moved:
                self.repo.move_file(moved.id, path, source_id, st.st_mtime)
                run.files_moved += 1
                return "moved", moved.path

        t_parse = time.perf_counter()
        try:
            options = {"ocr": use_ocr, "before_ocr": gate.wait if gate is not None else None} if use_ocr else {}
            blocks = parse_file(parser, p, **options)
        except EncryptedFileError as e:
            self.repo.mark_skipped(source_id, info, f"encrypted: {e}")
            run.files_skipped += 1
            return "skipped", str(e)
        except UnsupportedFileError as e:   # 旧版 .ppt 等：跳过并提示处理办法，不算失败
            self.repo.mark_skipped(source_id, info, f"unsupported: {e}")
            run.files_skipped += 1
            return "skipped", str(e)
        except ParseError as e:
            self.repo.mark_failed(source_id, info, str(e))
            run.files_failed += 1
            return "failed", str(e)

        chunks = self.chunker.chunk(blocks)
        run.parse_ms += int((time.perf_counter() - t_parse) * 1000)
        if not chunks:
            self.repo.mark_skipped(source_id, info, "no extractable text")
            run.files_skipped += 1
            return "skipped", ("文字识别也没有识别出文字" if use_ocr else
                               "没有文字层（可能是扫描件），在文件夹设置里打开“文字识别”后可以检索")

        # Embedding 缓存：相同文本（重复的页眉页脚、模板页、修改文件中没改的段落）只算一次向量
        hashes = [text_hash(c.text) for c in chunks]
        cached = self.repo.existing_hashes(hashes)
        todo: dict[str, str] = {}
        for c, h in zip(chunks, hashes):
            if h not in cached and h not in todo:
                todo[h] = c.text
        new_vecs: dict[str, list[float]] = {}
        t_embed = time.perf_counter()
        items = list(todo.items())
        batch = self.cfg.embedding.batch_size
        for i in range(0, len(items), batch):
            if gate is not None and i:
                t_wait = time.perf_counter()
                gate.wait()
                t_embed += time.perf_counter() - t_wait  # 暂停时间不计入向量耗时
            part = items[i:i + batch]
            for (h, _), vec in zip(part, self.gateway.embed([t for _, t in part])):
                new_vecs[h] = vec
        run.embed_ms += int((time.perf_counter() - t_embed) * 1000)
        vectors: list[list[float] | None] = []
        for h in hashes:
            vectors.append(new_vecs.pop(h, None))  # 同一文件内重复的文本只写入一次向量
        tokens = [to_fts_tokens(c.text, c.lang) for c in chunks]
        self.repo.replace_file_chunks(source_id, info, chunks, vectors, tokens)
        run.files_indexed += 1
        run.chunks_added += len(chunks)
        run.chunks_reused += len(chunks) - len(todo)
        return "indexed", None
