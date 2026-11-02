"""引用解析与校验：只保留本次请求实际提供的片段编号，无效编号从正文中删除。"""

from __future__ import annotations

import re
from typing import Sequence

from lkos.types import Citation, RetrievedChunk

_MARKER = re.compile(r"\[(\s*\d+(?:\s*[,，、]\s*\d+)*\s*)\]")


def validate_citations(text: str, context: Sequence[RetrievedChunk]) -> tuple[str, list[Citation], list[int]]:
    n = len(context)
    valid: set[int] = set()
    invalid: list[int] = []

    def fix(m: re.Match) -> str:
        nums = [int(x) for x in re.findall(r"\d+", m.group(1))]
        keep = []
        for x in nums:
            if 1 <= x <= n:
                valid.add(x)
                if x not in keep:
                    keep.append(x)
            elif x not in invalid:
                invalid.append(x)
        return "[" + ", ".join(str(x) for x in keep) + "]" if keep else ""

    cleaned = _MARKER.sub(fix, text)
    cleaned = re.sub(r"[ \t]+([。．.,，;；!！?？])", r"\1", cleaned)  # 删除编号后残留的空格
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    citations = [Citation(ref=i, chunk=context[i - 1]) for i in sorted(valid)]
    return cleaned, citations, invalid
