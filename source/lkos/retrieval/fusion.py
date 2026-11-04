"""RRF（倒数排名融合）：score(d) = Σ 1 / (k + rank)，rank 从 1 开始。"""

from __future__ import annotations

from typing import Sequence


def rrf(rankings: Sequence[Sequence[int]], k: int = 60) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    first_seen: dict[int, int] = {}
    order = 0
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
            if doc_id not in first_seen:
                first_seen[doc_id] = order
                order += 1
    # 分数相同时，保持先出现者在前，结果稳定可复现
    return sorted(scores.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))
