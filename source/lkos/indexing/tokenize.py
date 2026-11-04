"""FTS5 写入与查询共用的切词。

中文先用 jieba 切词再交给 unicode61 分词器（trigram 要求查询词至少 3 个字符，两字词会漏检）。
写入用搜索引擎模式（同时产生长词与其中的短词），查询用精确模式。
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

_WORD = re.compile(r"\w+", re.UNICODE)
_HAN = re.compile(r"[㐀-䶿一-鿿]")

STOPWORDS = {
    # 中文
    "的", "了", "是", "在", "和", "与", "及", "或", "吗", "呢", "吧", "啊", "有", "也", "就", "都", "而",
    "这", "那", "什么", "多少", "怎么", "如何", "哪些", "哪个", "是否", "请问", "一下", "一个", "我", "你",
    # 英文
    "the", "a", "an", "is", "are", "was", "were", "of", "to", "in", "on", "for", "and", "or", "what", "which",
    "how", "does", "do", "did", "who", "when", "where", "why", "it", "this", "that", "be", "with", "by", "at",
    # 法文
    "le", "la", "les", "un", "une", "des", "du", "de", "et", "ou", "est", "sont", "que", "qui", "quoi",
    "quel", "quelle", "quels", "quelles", "où", "comment", "pour", "dans", "sur", "au", "aux", "en", "il", "elle",
}

_jieba_dir: Path | None = None


def set_cache_dir(path: Path) -> None:
    """jieba 默认把词典缓存写到系统临时目录；改到数据目录内。"""
    global _jieba_dir
    if _jieba.cache_info().currsize:  # 已初始化：词典已在内存中，不必重新加载
        return
    _jieba_dir = Path(path)
    _jieba_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def _jieba():
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        import jieba

    jieba.setLogLevel(logging.WARNING)
    tk = jieba.Tokenizer()
    if _jieba_dir is not None:
        tk.tmp_dir = str(_jieba_dir)
    tk.initialize()
    return tk


def to_fts_tokens(text: str, lang: str | None = None, for_query: bool = False) -> str:
    """返回空格分隔的词。中文（或含汉字的文本）走 jieba，其余原样交给 unicode61。"""
    if lang == "zh" or _HAN.search(text):
        tk = _jieba()
        words = tk.lcut(text) if for_query else tk.lcut_for_search(text)
        return " ".join(w for w in (w.strip() for w in words) if w)
    return text


def query_terms(text: str, lang: str | None = None) -> list[str]:
    tokens = to_fts_tokens(text, lang, for_query=True)
    seen: list[str] = []
    for w in _WORD.findall(tokens):
        lw = w.lower()
        if lw in STOPWORDS or lw in seen:
            continue
        seen.append(lw)
    return seen


def build_or_query(text: str, lang: str | None = None) -> str:
    """构造 FTS5 MATCH 表达式：每个词加双引号（避免语法字符），词之间用 OR。"""
    terms = query_terms(text, lang)
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
