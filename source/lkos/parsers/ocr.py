"""文字识别（OCR）：扫描版 PDF（页面上没有文字层）用 macOS 自带的 Vision 框架识别文字，完全离线。

只在 macOS 上可用，需要 pyobjc-framework-Vision 与 pyobjc-framework-Quartz（setup_mac.sh 会安装）；
其他系统或缺少依赖时 available() 返回 False，扫描版 PDF 照旧记为“没有文字层”。

识别引擎是一个函数：PNG 图片字节 → 识别出的文字。测试里可以换成假的引擎。
"""

from __future__ import annotations

import re
import sys
from functools import lru_cache
from typing import Callable

OcrEngine = Callable[[bytes], str]

LANGUAGES = ["zh-Hans", "en-US", "fr-FR"]   # 中文（简体）、英文、法文
DPI = 200                                   # 渲染分辨率：A4 约 1650×2340，识别准确率与速度的折中
MIN_PAGE_CHARS = 20                         # 文字层少于这么多字的页面才做识别（扫描页、只有页眉的扫描页）

_CJK = r"㐀-鿿豈-﫿"
_SENTENCE_END = "。！？；：!?;:…」』）)"


@lru_cache(maxsize=1)
def available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import Quartz  # noqa: F401
        import Vision  # noqa: F401
    except ImportError:
        return False
    return True


def join_lines(lines: list[str]) -> str:
    """把识别出的行拼成段落：中文行与行之间直接相连（扫描件里一句话常被折成几行），
    句末标点后、英文行之间保留换行或空格。"""
    out = ""
    for line in (x.strip() for x in lines):
        if not line:
            continue
        if not out:
            out = line
        elif re.search(f"[{_CJK}]$", out) and out[-1] not in _SENTENCE_END and re.match(f"[{_CJK}]", line):
            out += line
        else:
            out += "\n" + line
    return out


def _vision_recognize(png: bytes) -> str:
    import objc
    import Quartz
    import Vision
    from Foundation import NSData

    with objc.autorelease_pool():
        data = NSData.dataWithBytes_length_(png, len(png))
        source = Quartz.CGImageSourceCreateWithData(data, None)
        image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None) if source else None
        if image is None:
            return ""
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(True)
        try:
            request.setRecognitionLanguages_(LANGUAGES)
        except Exception:  # noqa: BLE001 - 老系统不支持某些语言时用默认
            pass
        ok, err = handler.performRequests_error_([request], None)
        if not ok:
            raise RuntimeError(f"文字识别失败：{err}")
        rows = []
        for obs in request.results() or []:
            cands = obs.topCandidates_(1)
            if not cands or not len(cands):
                continue
            box = obs.boundingBox()    # 归一化坐标，原点在左下
            rows.append((box.origin.y + box.size.height, box.origin.x, str(cands[0].string())))
        rows.sort(key=lambda r: (-round(r[0], 3), r[1]))   # 从上到下、从左到右
        return join_lines([r[2] for r in rows])


def engine() -> OcrEngine | None:
    """当前系统可用的识别引擎；不可用时返回 None。"""
    return _vision_recognize if available() else None
