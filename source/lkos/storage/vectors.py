"""向量压缩：为 100 GB 级语料设计的两级存储（设计变更，见 docs/design-changes-v0.2.md）。

- 二值向量（每维 1 bit，1024 维 = 128 字节）：放进 sqlite-vec 的 vec0 表做汉明距离粗筛，
  全表扫描的数据量只有 float32 的 1/32；
- int8 向量（每维 1 字节，1024 维 = 1 KB）：只对粗筛出的候选做余弦精排。

int8 采用“每个向量单独缩放”（最大绝对值映射到 127）。余弦距离与缩放无关，
所以每个向量各自缩放不影响结果，精度远高于固定区间 [-1, 1] 的缩放。
"""

from __future__ import annotations

import hashlib
import struct
import unicodedata
from typing import Sequence


def text_hash(text: str) -> str:
    """文本块内容哈希，用于 Embedding 缓存：相同文本只计算一次向量。"""
    norm = unicodedata.normalize("NFC", " ".join(text.split()))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def to_int8(vec: Sequence[float]) -> bytes:
    peak = max((abs(x) for x in vec), default=0.0)
    if peak == 0:
        return bytes(len(vec))
    scale = 127.0 / peak
    return struct.pack(f"{len(vec)}b", *(max(-127, min(127, round(x * scale))) for x in vec))


def from_int8(blob: bytes) -> list[int]:
    return list(struct.unpack(f"{len(blob)}b", blob))
