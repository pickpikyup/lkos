"""按文本块检测语言：汉字占比判断中文；其余用 lingua 在英、法之间区分。"""

from __future__ import annotations

import re
from functools import lru_cache

SUPPORTED = ("zh", "en", "fr")
_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_LATIN = re.compile(r"[A-Za-zÀ-ÿŒœ]")
_FR_HINT = re.compile(
    r"[àâçéèêëîïôûùüÿœ]|\b(le|la|les|des|du|une|est|sont|et|pour|dans|avec|qui|que|où|au|aux|nous|vous)\b",
    re.IGNORECASE,
)
_EN_HINT = re.compile(
    r"\b(the|is|are|and|for|with|what|which|how|of|to|in|on|does|do|policy|who|when|where)\b", re.IGNORECASE
)

ZH_RATIO = 0.3  # 汉字占全部字母类字符的比例达到该值即判为中文


@lru_cache(maxsize=1)
def _detector():
    from lingua import Language, LanguageDetectorBuilder

    return LanguageDetectorBuilder.from_languages(Language.ENGLISH, Language.FRENCH).build()


def detect_lang(text: str) -> str:
    """返回 zh | en | fr | other。"""
    han = len(_HAN.findall(text))
    latin = len(_LATIN.findall(text))
    if han + latin == 0:
        return "other"
    if han / (han + latin) >= ZH_RATIO:
        return "zh"
    if latin < 3:
        return "other"
    # 短文本（典型是用户的问题）先看功能词，lingua 对极短文本不稳定
    fr_hits, en_hits = len(_FR_HINT.findall(text)), len(_EN_HINT.findall(text))
    if latin < 40 and fr_hits != en_hits:
        return "fr" if fr_hits > en_hits else "en"
    from lingua import Language

    result = _detector().detect_language_of(text)
    if result == Language.FRENCH:
        return "fr"
    if result == Language.ENGLISH:
        return "en"
    return "other"
