"""检索与问答评测（POC 验收标准第 2 节；M1 增加块级命中与答案正确率，见 docs/m1-design.md 第 3 节）。

questions.yaml：
    - question: 项目二期的预算上限是多少？
      expected_files: [项目计划书.docx]
      expected_text: 预算上限为 120 万元      # 可选：含答案的原文（可写列表，任一命中即可）
      expected_page: 3                        # 可选：PDF 页码（可写列表）
      expected_heading: 二期计划              # 可选：DOCX / Markdown 标题
      expected_answer: [120 万, 一百二十万]    # 可选：回答中出现任一写法即判为答对

块级命中：送进模型的片段中，有来自期望文件、且包含 expected_text（没写时看页码，再看标题）的片段。
比较文本时统一全角半角、大小写，并去掉所有空白（PDF 抽取的文字常带换行）。
"""

from __future__ import annotations

import statistics
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from lkos.errors import ConfigError
from lkos.indexing.language import detect_lang


@dataclass
class Question:
    question: str
    expected_files: list[str]
    expected_text: list[str] = field(default_factory=list)
    expected_page: list[int] = field(default_factory=list)
    expected_heading: str | None = None
    expected_answer: list[str] = field(default_factory=list)

    @property
    def has_location(self) -> bool:
        return bool(self.expected_text or self.expected_page or self.expected_heading)


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
    chunk_hit: bool | None = None        # 含答案的段落是否送入模型（题目没写位置时为 None）
    answer_correct: bool | None = None   # 回答是否包含 expected_answer 之一
    not_found: bool | None = None        # 回答是否为“没有找到”
    context: list[str] = field(default_factory=list)  # 送入模型的片段位置
    lang: str | None = None              # 问题语言（按语言统计首 token）


@dataclass
class EvalReport:
    k: int
    results: list[QuestionResult] = field(default_factory=list)
    with_answer: bool = False
    mode: str | None = None
    model: str | None = None
    strategy: str | None = None

    def to_dict(self) -> dict:
        from dataclasses import asdict

        def r4(v):
            return None if v is None else round(v, 4)

        return {
            "k": self.k, "mode": self.mode, "model": self.model, "strategy": self.strategy,
            "with_answer": self.with_answer,
            "hit_rate": round(self.hit_rate, 4), "mrr": round(self.mrr, 4),
            "chunk_hit_rate": r4(self.chunk_hit_rate), "chunk_hit_total": len(self.with_location),
            "answer_accuracy": r4(self.answer_accuracy),
            "doc_ok_not_found": self.doc_ok_not_found, "unsupported": self.unsupported,
            "citation_rate": r4(self.citation_rate),
            "invalid_total": self.invalid_total, "ttft_median_ms": self.ttft_median_ms,
            "ttft_median_by_lang": self.ttft_median_by_lang,
            "results": [asdict(r) for r in self.results],
        }

    @property
    def with_location(self) -> list[QuestionResult]:
        return [r for r in self.results if r.chunk_hit is not None]

    @property
    def chunk_hit_rate(self) -> float | None:
        rs = self.with_location
        return sum(r.chunk_hit for r in rs) / len(rs) if rs else None

    @property
    def answer_accuracy(self) -> float | None:
        rs = [r for r in self.answered if r.answer_correct is not None]
        return sum(r.answer_correct for r in rs) / len(rs) if rs else None

    @property
    def doc_ok_not_found(self) -> int:
        """文件找对了，回答却是“没有找到”。"""
        return sum(1 for r in self.answered if r.hit and r.not_found)

    @property
    def unsupported(self) -> int:
        """回答不是“没有找到”，却没有任何有效引用。"""
        return sum(1 for r in self.answered if not r.not_found and r.valid_citations == 0)

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
    def ttft_median_by_lang(self) -> dict[str, float]:
        by: dict[str, list[int]] = {}
        for r in self.answered:
            if r.ttft_ms is not None and r.lang:
                by.setdefault(r.lang, []).append(r.ttft_ms)
        return {k: statistics.median(v) for k, v in sorted(by.items())}

    @property
    def ttft_median_ms(self) -> float | None:
        vals = [r.ttft_ms for r in self.answered if r.ttft_ms is not None]
        return statistics.median(vals) if vals else None


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _str_list(v, field_name: str, i: int) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (str, int, float)):
        return [str(v)]
    if isinstance(v, list) and all(isinstance(x, (str, int, float)) for x in v):
        return [str(x) for x in v]
    raise ConfigError(f"第 {i} 题的 {field_name} 必须是文字或文字列表")


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
        try:
            pages = [int(p) for p in _str_list(item.get("expected_page"), "expected_page", i)]
        except ValueError as e:
            raise ConfigError(f"第 {i} 题的 expected_page 必须是页码") from e
        heading = item.get("expected_heading")
        out.append(Question(
            str(item["question"]), _str_list(item["expected_files"], "expected_files", i),
            expected_text=_str_list(item.get("expected_text"), "expected_text", i),
            expected_page=pages,
            expected_heading=None if heading is None else str(heading),
            expected_answer=_str_list(item.get("expected_answer"), "expected_answer", i),
        ))
    return out


def norm_text(s: str) -> str:
    """比较用：全角转半角、小写、去掉所有空白。"""
    return "".join(unicodedata.normalize("NFKC", s).lower().split())


NOT_FOUND_MARKERS = ("没有找到", "未找到", "没有相关", "no relevant", "not found", "aucune information",
                     "aucun document")


def is_not_found(answer: str) -> bool:
    a = unicodedata.normalize("NFKC", answer).lower()
    return any(m in a for m in NOT_FOUND_MARKERS)


def chunk_matches(chunk, q: Question) -> bool:
    """片段是否是题目期望的“含答案段落”。"""
    if not any(matches(chunk.file_path, e) for e in q.expected_files):
        return False
    if q.expected_text:
        body = norm_text(chunk.text)
        return any(norm_text(t) in body for t in q.expected_text)
    if q.expected_page:
        loc = chunk.location
        if loc.page is None:
            return False
        end = loc.page_end if loc.page_end is not None else loc.page
        return any(loc.page <= p <= end for p in q.expected_page)
    if q.expected_heading:
        return bool(chunk.location.heading_path) and q.expected_heading in chunk.location.heading_path
    return False


def answer_correct(answer: str, expected: list[str]) -> bool:
    a = norm_text(answer)
    return any(norm_text(e) in a for e in expected)


def matches(retrieved_path: str, expected: str) -> bool:
    """expected 可以是文件名，也可以是相对路径的结尾部分。"""
    r, e = _nfc(retrieved_path), _nfc(expected).lstrip("/")
    return r == e or r.endswith("/" + e)


def evaluate(questions: list[Question], retriever, qa=None, k: int = 5, lang: str = "auto",
             mode: str | None = None, model: str | None = None, strategy: str | None = None) -> EvalReport:
    report = EvalReport(k=k, with_answer=qa is not None, mode=mode, model=model, strategy=strategy)
    extra = {} if strategy is None else {"strategy": strategy}
    for q in questions:
        if qa is not None:
            try:
                ans = qa.ask(q.question, k=k, lang=lang, mode=mode, model_override=model, **extra)
            except Exception as e:  # noqa: BLE001 - 单题失败不影响整体评测
                report.results.append(QuestionResult(q.question, q.expected_files, [], False, None, error=str(e)))
                continue
            hits = ans.context
        else:
            hits = retriever.search(q.question, k=k, lang=lang, mode=mode, **extra)
        files: list[str] = []
        for h in hits:
            if h.file_path not in files:
                files.append(h.file_path)
        rank = next((i for i, f in enumerate(files, start=1) if any(matches(f, e) for e in q.expected_files)), None)
        res = QuestionResult(q.question, q.expected_files, [Path(f).name for f in files], rank is not None, rank)
        res.context = [h.describe() for h in hits]
        res.lang = detect_lang(q.question)
        if q.has_location:
            res.chunk_hit = any(chunk_matches(h, q) for h in hits)
        if qa is not None:
            res.valid_citations = len(ans.citations)
            res.invalid_citations = len(ans.invalid_refs)
            res.ttft_ms = ans.ttft_ms
            res.answer = ans.text
            res.cited = [f"[{c.ref}] {c.chunk.describe()}" for c in ans.citations]
            res.not_found = bool(ans.no_context) or is_not_found(ans.text)
            if q.expected_answer:
                res.answer_correct = answer_correct(ans.text, q.expected_answer)
        report.results.append(res)
    return report
