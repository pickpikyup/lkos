"""模块之间共享的数据类（设计文档第 7 节）。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Location:
    page: int | None = None  # PDF 页码，从 1 开始
    heading_path: str | None = None  # 如 "第二章 > 2.1 预算"
    para_start: int | None = None  # DOCX 正文元素序号
    para_end: int | None = None
    line_start: int | None = None  # Markdown 行号，从 1 开始
    line_end: int | None = None
    page_end: int | None = None   # 相邻片段合并后跨页时的末页

    def describe(self, file_type: str) -> str:
        """按格式输出引用定位文字（需求文档第 7 节的粒度）。"""
        parts: list[str] = []
        if file_type == "pptx" and self.page is not None:
            parts.append(f"幻灯片 {self.page}" if self.page_end in (None, self.page)
                         else f"幻灯片 {self.page}–{self.page_end}")
        if file_type == "pdf" and self.page is not None:
            if self.page_end is not None and self.page_end != self.page:
                parts.append(f"第 {self.page}–{self.page_end} 页")
            else:
                parts.append(f"第 {self.page} 页")
        if self.heading_path:
            parts.append(self.heading_path)
        if file_type in ("docx", "doc") and self.para_start is not None:
            parts.append(_range("段落", self.para_start, self.para_end))
        if file_type == "md" and self.line_start is not None:
            parts.append(_range("行", self.line_start, self.line_end))
        return " · ".join(parts)


def _range(label: str, start: int, end: int | None) -> str:
    if end is None or end == start:
        return f"{label} {start}"
    return f"{label} {start}–{end}"


@dataclass
class Block:
    """解析器输出的最小单元：一个段落、一行表格或一段 Markdown。"""

    text: str
    location: Location


@dataclass
class Chunk:
    ordinal: int
    text: str
    lang: str  # zh | en | fr | other
    location: Location


@dataclass
class FileInfo:
    path: str
    file_type: str
    content_hash: str
    size_bytes: int
    mtime: float


@dataclass
class FileRecord:
    id: int
    source_id: int
    path: str
    file_type: str
    content_hash: str
    status: str
    error: str | None
    chunk_count: int
    size_bytes: int = 0
    mtime: float = 0.0
    indexed_at: str | None = None


@dataclass
class RetrievedChunk:
    chunk_id: int
    file_path: str
    file_type: str
    text: str
    lang: str
    location: Location
    score: float = 0.0
    file_id: int | None = None
    ordinal: int | None = None       # 文件内的块序号
    ordinal_end: int | None = None   # 相邻片段合并后的末块序号（未合并时等于 ordinal）

    @property
    def file_name(self) -> str:
        return self.file_path.rsplit("/", 1)[-1]

    def describe(self) -> str:
        loc = self.location.describe(self.file_type)
        return f"{self.file_name} · {loc}" if loc else self.file_name


@dataclass
class Citation:
    ref: int  # 提示中的片段编号 [n]
    chunk: RetrievedChunk


@dataclass
class Answer:
    text: str
    citations: list[Citation] = field(default_factory=list)
    invalid_refs: list[int] = field(default_factory=list)
    no_context: bool = False
    context: list[RetrievedChunk] = field(default_factory=list)
    ttft_ms: int | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class CallLog:
    task: str
    provider: str
    model: str
    latency_ms: int | None = None
    ttft_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    mem_available_mb: int | None = None
    swap_used_mb: int | None = None
    error: str | None = None


@dataclass
class ProgressEvent:
    path: str
    action: str  # indexed | skipped | moved | failed | unchanged
    files_seen: int
    detail: str | None = None
