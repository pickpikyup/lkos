"""检索与引用评测（POC 验收标准第 2 节）。

questions.yaml：
    - question: 项目二期的预算上限是多少？
      expected_files: [项目计划书.docx]
"""

from __future__ import annotations

import statistics
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from lkos.errors import ConfigError


@dataclass
class Question:
    question: str
    expected_files: list[str]


@dataclass
class QuestionResult:
    question: str
    expected_files: list[str]
    retrieved_files: list[str]
    hit: bool
    rank: int | None  # 第一个命中文件的名次（从 1 开始）
    valid_citations: int | None = None
    invalid_citations: int | None = None
    ttft_ms: int | None = None
    error: str | None = None
    answer: str | None = None
    cited: list[str] = field(default_factory=list)   # 引用的位置描述，便于人工核对


@dataclass
class EvalReport:
    k: int
    results: list[QuestionResult] = field(default_factory=list)
    with_answer: bool = False
    mode: str | None = None
    model: str | None = None

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return {
            "k": self.k, "mode": self.mode, "model": self.model, "with_answer": self.with_answer,
            "hit_rate": round(self.hit_rate, 4), "mrr": round(self.mrr, 4),
            "citation_rate": None if self.citation_rate is None else round(self.citation_rate, 4),
            "invalid_total": self.invalid_total, "ttft_median_ms": self.ttft_median_ms,
            "results": [asdict(r) for r in self.results],
        }

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def hit_rate(self) -> float:
        return sum(r.hit for r in self.results) / self.total if self.total else 0.0

    @property
    def mrr(self) -> float:
        return sum(1 / r.rank for r in self.results if r.rank) / self.total if self.total else 0.0

    @property
    def answered(self) -> list[QuestionResult]:
        return [r for r in self.results if r.valid_citations is not None]

    @property
    def citation_rate(self) -> float | None:
        a = self.answered
        return sum(r.valid_citations > 0 for r in a) / len(a) if a else None

    @property
    def invalid_total(self) -> int:
        return sum(r.invalid_citations or 0 for r in self.answered)

    @property
    def ttft_median_ms(self) -> float | None:
        vals = [r.ttft_ms for r in self.answered if r.ttft_ms is not None]
        return statistics.median(vals) if vals else None


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def load_questions(path: Path) -> list[Question]:
    try:
        data = yaml.safe_load(Path(path).read_text("utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"无法读取测试问题集 {path}：{e}") from e
    if not isinstance(data, list):
        raise ConfigError("测试问题集必须是列表")
    out = []
    for i, item in enumerate(data, start=1):
        if not isinstance(item, dict) or not item.get("question") or not item.get("expected_files"):
            raise ConfigError(f"第 {i} 题缺少 question 或 expected_files")
        files = item["expected_files"]
        if isinstance(files, str):
            files = [files]
        out.append(Question(str(item["question"]), [str(f) for f in files]))
    return out


def matches(retrieved_path: str, expected: str) -> bool:
    """expected 可以是文件名，也可以是相对路径的结尾部分。"""
    r, e = _nfc(retrieved_path), _nfc(expected).lstrip("/")
    return r == e or r.endswith("/" + e)


def evaluate(questions: list[Question], retriever, qa=None, k: int = 5, lang: str = "auto",
             mode: str | None = None, model: str | None = None) -> EvalReport:
    report = EvalReport(k=k, with_answer=qa is not None, mode=mode, model=model)
    for q in questions:
        if qa is not None:
            try:
                ans = qa.ask(q.question, k=k, lang=lang, mode=mode, model_override=model)
            except Exception as e:  # noqa: BLE001 - 单题失败不影响整体评测
                report.results.append(QuestionResult(q.question, q.expected_files, [], False, None, error=str(e)))
                continue
            hits = ans.context
        else:
            hits = retriever.search(q.question, k=k, lang=lang, mode=mode)
        files: list[str] = []
        for h in hits:
            if h.file_path not in files:
                files.append(h.file_path)
        rank = next((i for i, f in enumerate(files, start=1) if any(matches(f, e) for e in q.expected_files)), None)
        res = QuestionResult(q.question, q.expected_files, [Path(f).name for f in files], rank is not None, rank)
        if qa is not None:
            res.valid_citations = len(ans.citations)
            res.invalid_citations = len(ans.invalid_refs)
            res.ttft_ms = ans.ttft_ms
            res.answer = ans.text
            res.cited = [f"[{c.ref}] {c.chunk.describe()}" for c in ans.citations]
        report.results.append(res)
    return report
