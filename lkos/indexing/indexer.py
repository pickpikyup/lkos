"""索引流程编排（设计文档第 9 节）。

按文件逐个提交事务：未变化的跳过，改名/移动的只改路径，变化的重新解析并整体替换文本块；
只有完整扫描（未设 limit）时才清理磁盘上已删除的文件。中断后重跑即自然续传。
"""

from __future__ import annotations

import hashlib
import os
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from lkos.config import Config
from lkos.errors import EncryptedFileError, ParseError
from lkos.indexing.chunker import Chunker
from lkos.indexing.tokenize import to_fts_tokens
from lkos.parsers import parse_file, parser_for
from lkos.storage.repository import IndexRun, Repository
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
    failures: list[tuple[str, str]] = field(default_factory=list)
    cleanup_done: bool = True


def sha256_of(path: Path, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(bufsize):
            h.update(chunk)
    return h.hexdigest()


def norm_path(path: Path) -> str:
    """统一为 NFC：macOS 文件名可能是 NFD，避免同一中文文件名出现两条记录。"""
    return unicodedata.normalize("NFC", str(path))


class Indexer:
    def __init__(self, cfg: Config, repo: Repository, gateway, chunker: Chunker | None = None):
        self.cfg = cfg
        self.repo = repo
        self.gateway = gateway
        self.chunker = chunker or Chunker(cfg.indexing.chunk_max_chars, cfg.indexing.chunk_overlap_chars)

    # ---------- 遍历 ----------
    def walk(self, folder: Path) -> Iterator[Path]:
        max_bytes = self.cfg.indexing.max_file_size_mb * 1024 * 1024
        for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
            if self.cfg.indexing.skip_hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            dirnames.sort()
            for name in sorted(filenames):
                if self.cfg.indexing.skip_hidden and name.startswith("."):
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
                     on_progress: ProgressCallback | None = None) -> IndexReport:
        import time

        t0 = time.perf_counter()
        folder = Path(folder).expanduser().resolve()
        if not folder.is_dir():
            raise ParseError(f"文件夹不存在：{folder}")
        source_id = self.repo.upsert_source(norm_path(folder))
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
                action, detail = self._index_one(p, path, source_id, run)
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
        self.repo.finish_run(run)
        return IndexReport(
            run_id=run.id, files_seen=run.files_seen, files_indexed=run.files_indexed,
            files_skipped=run.files_skipped, files_moved=run.files_moved, files_failed=run.files_failed,
            files_removed=run.files_removed, chunks_added=run.chunks_added,
            seconds=round(time.perf_counter() - t0, 2), failures=failures, cleanup_done=cleanup,
        )

    def _index_one(self, p: Path, path: str, source_id: int, run: IndexRun) -> tuple[str, str | None]:
        parser = parser_for(p, self.cfg.indexing.extensions)
        st = p.stat()
        record = self.repo.get_file(path)
        done = record is not None and record.status in ("indexed", "skipped")
        # 大小与修改时间都没变时不必重新计算哈希（全量 2–4 GB 时能省下大量读盘）
        if done and record.size_bytes == st.st_size and record.mtime == st.st_mtime:
            run.files_skipped += 1
            return "unchanged", None

        content_hash = sha256_of(p)
        info = FileInfo(path=path, file_type=parser.file_type, content_hash=content_hash,
                        size_bytes=st.st_size, mtime=st.st_mtime)
        if done and record.content_hash == content_hash:  # 只是 touch 过，内容未变
            self.repo.touch_file(record.id, st.st_mtime)
            run.files_skipped += 1
            return "unchanged", None

        if record is None:
            moved = self.repo.find_missing_by_hash(content_hash)
            if moved:
                self.repo.move_file(moved.id, path, source_id, st.st_mtime)
                run.files_moved += 1
                return "moved", moved.path

        try:
            blocks = parse_file(parser, p)
        except EncryptedFileError as e:
            self.repo.mark_skipped(source_id, info, f"encrypted: {e}")
            run.files_skipped += 1
            return "skipped", str(e)
        except ParseError as e:
            self.repo.mark_failed(source_id, info, str(e))
            run.files_failed += 1
            return "failed", str(e)

        chunks = self.chunker.chunk(blocks)
        if not chunks:
            self.repo.mark_skipped(source_id, info, "no extractable text")
            run.files_skipped += 1
            return "skipped", "没有可抽取的文本（可能是扫描件，POC 不做 OCR）"

        batch = self.cfg.embedding.batch_size
        vectors: list[list[float]] = []
        for i in range(0, len(chunks), batch):
            vectors.extend(self.gateway.embed([c.text for c in chunks[i:i + batch]]))
        tokens = [to_fts_tokens(c.text, c.lang) for c in chunks]
        self.repo.replace_file_chunks(source_id, info, chunks, vectors, tokens)
        run.files_indexed += 1
        run.chunks_added += len(chunks)
        return "indexed", None
